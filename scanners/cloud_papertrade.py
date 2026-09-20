#!/usr/bin/env python3
"""Stateless GitHub Actions paper trader for NSE O=L/O=H + F3 and Gold/BTC.

The committed JSON state is the ledger. Each Actions run reads it, updates
positions once, and writes the dashboard snapshots consumed by paper.html.
No credentials, Mac, or manual steps required — this is the primary engine;
the Mac install is a retired fallback.

NSE rules = the tuned local engine (parity since 2026-09-20):
  entry cutoff 09:45 · square-off 15:00 · max 3/setup by est. volume
  O=L volume >= 3.0x avg20 (PF 8.1 backtest) · O=H >= 1.5x
  SL = 0.5% beyond day low/high · NIFTY-direction gate on both legs
  long time-stop 10:45 (scratch O=L longs not back above entry)
  F3 first-3-candle scan 12:20-13:30, NIFTY-gated, top 5 by drive
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
IST = ZoneInfo("Asia/Kolkata")
NSE_STATE = DATA / "cloud_paper_ol_state.json"
GC_STATE = DATA / "cloud_paper_gc_state.json"
NSE_SNAPSHOT = DATA / "paper_ol.json"
GC_SNAPSHOT = DATA / "paper_gc.json"

NSE_CAPITAL = 10_000.0
NSE_TOL = 0.10
NSE_MIN_PRICE = 50.0
NSE_MIN_VOL = 1.5
NSE_MIN_VOL_OL = 3.0          # O=L longs: higher-conviction gate (PF 8.1)
NSE_MAX_PER_SETUP = 3
NSE_ENTRY_CUTOFF = time(9, 45)
NSE_TIME_STOP = time(10, 45)  # scratch ol longs not in profit by now
NSE_SQUARE_OFF = time(15, 0)
F3_WINDOW = (time(12, 20), time(13, 30))
F3_MAX_TRADES = 5

GC_MARKETS = {"GOLD": "GC=F", "BTC": "BTC-USD"}
GC_CAPITAL = 60.0
GC_TOL = 0.001


def now() -> datetime:
    return datetime.now(IST)


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n")


def empty_state() -> dict:
    return {"version": 1, "next_id": 1, "trades": [], "seeded": False}


def flatten(frame):
    if frame is None or frame.empty:
        return frame
    if getattr(frame.columns, "nlevels", 1) > 1:
        frame.columns = frame.columns.get_level_values(0)
    return frame.dropna(how="all")


def nifty_bias() -> int:
    """+1 above day open, -1 below, 0 unknown. Longs need +1, shorts -1."""
    try:
        frame = flatten(yf.download("^NSEI", period="1d", interval="15m",
                                    progress=False, threads=False,
                                    auto_adjust=False))
        if frame is None or frame.empty:
            return 0
        return 1 if float(frame["Close"].iloc[-1]) > float(frame["Open"].iloc[0]) else -1
    except Exception:
        return 0


def nse_symbols() -> list[str]:
    rows = (DATA / "nse200_symbols.csv").read_text().splitlines()
    return [row.strip().split(",")[0] for row in rows[1:] if row.strip()]


def nse_quotes() -> dict[str, dict]:
    symbols = nse_symbols()
    tickers = [f"{symbol}.NS" for symbol in symbols]
    bars = yf.download(tickers, period="2d", interval="1d", group_by="ticker",
                       progress=False, threads=True, auto_adjust=False)
    quotes: dict[str, dict] = {}
    for symbol, ticker in zip(symbols, tickers):
        try:
            frame = flatten(bars[ticker].copy())
            if frame is None or len(frame) < 2:
                continue
            latest, previous = frame.iloc[-1], frame.iloc[-2]
            o, h, low, ltp = (float(latest[k]) for k in ("Open", "High", "Low", "Close"))
            if not all(math.isfinite(v) and v > 0 for v in (o, h, low, ltp)):
                continue
            quotes[symbol] = {"open": o, "high": h, "low": low, "ltp": ltp,
                              "volume": float(latest.get("Volume", 0)),
                              "prev_close": float(previous["Close"])}
        except (KeyError, TypeError, ValueError):
            continue
    candidates = [s for s, q in quotes.items()
                  if abs(q["open"] - q["low"]) <= min(NSE_TOL, q["open"] * 0.0005)
                  or abs(q["high"] - q["open"]) <= min(NSE_TOL, q["open"] * 0.0005)]
    if not candidates:
        return quotes
    volume_bars = yf.download([f"{s}.NS" for s in candidates], period="30d", interval="1d",
                              group_by="ticker", progress=False, threads=True, auto_adjust=False)
    session_fraction = max(0.20, min(1.0, (now().hour * 60 + now().minute - 555) / 375))
    for symbol in candidates:
        try:
            frame = flatten(volume_bars[f"{symbol}.NS"].copy())
            history = frame["Volume"].iloc[:-1].tail(20)
            average = float(history.mean()) if len(history) else 0.0
            quotes[symbol]["vol_ratio"] = (quotes[symbol]["volume"] / session_fraction / average
                                            if average else 0.0)
        except (KeyError, TypeError, ValueError):
            quotes[symbol]["vol_ratio"] = 0.0
    return quotes


def close(trade: dict, price: float, reason: str, stamp: str) -> None:
    sign = 1 if trade["side"] == "BUY" else -1
    trade.update({"status": "CLOSED", "exit_price": round(price, 2),
                  "exit_time": stamp, "reason": reason,
                  "pnl": round(sign * (price - trade["entry_price"]) * trade["qty"], 2)})


def manage_nse(state: dict, quotes: dict, moment: datetime) -> None:
    today, stamp = moment.date().isoformat(), moment.strftime("%H:%M:%S")
    for trade in [t for t in state["trades"]
                  if t.get("status") == "OPEN" and t.get("date") == today]:
        quote = quotes.get(trade["symbol"])
        if not quote:
            continue
        # strict-breach: SL sits 0.5% beyond the day extreme at entry, so the
        # cumulative-extreme check only fires on a genuinely NEW extreme
        sl_hit = quote["low"] <= trade["sl_price"] if trade["side"] == "BUY" \
            else quote["high"] >= trade["sl_price"]
        if sl_hit:
            close(trade, trade["sl_price"], "SL", stamp)
        elif moment.time() >= NSE_SQUARE_OFF:
            close(trade, quote["ltp"], "EOD", stamp)
        elif (moment.time() >= NSE_TIME_STOP and trade["setup"] == "ol"
              and trade["side"] == "BUY" and quote["ltp"] <= trade["entry_price"]):
            close(trade, quote["ltp"], "TSTOP", stamp)


def enter_nse(state: dict, quotes: dict, moment: datetime) -> None:
    today, stamp = moment.date().isoformat(), moment.strftime("%H:%M:%S")
    if not (time(9, 30) <= moment.time() <= NSE_ENTRY_CUTOFF):
        return
    if moment.weekday() >= 5:
        return
    bias = nifty_bias()
    seen = {(t["symbol"], t["setup"]) for t in state["trades"] if t.get("date") == today}
    for setup, side in (("ol", "BUY"), ("oh", "SELL")):
        # NIFTY gate: longs only into a rising tape, shorts only into a
        # falling one; unknown bias blocks longs, not shorts
        if setup == "ol" and bias != 1:
            continue
        if setup == "oh" and bias == 1:
            continue
        min_vol = NSE_MIN_VOL_OL if setup == "ol" else NSE_MIN_VOL
        eligible = []
        for symbol, quote in quotes.items():
            edge = abs(quote["open"] - quote["low"]) if setup == "ol" else abs(quote["high"] - quote["open"])
            if (symbol, setup) in seen or quote["open"] < NSE_MIN_PRICE:
                continue
            if edge <= min(NSE_TOL, quote["open"] * 0.0005) and quote.get("vol_ratio", 0) >= min_vol:
                eligible.append((quote.get("vol_ratio", 0), symbol, quote))
        already = sum(1 for t in state["trades"] if t.get("date") == today and t.get("setup") == setup)
        for _, symbol, quote in sorted(eligible, reverse=True)[:max(0, NSE_MAX_PER_SETUP - already)]:
            # don't chase >3% beyond the open
            ext = quote["ltp"] / quote["open"]
            if (setup == "ol" and ext > 1.03) or (setup == "oh" and ext < 0.97):
                continue
            qty = int(NSE_CAPITAL / quote["ltp"])
            if qty < 1:
                continue
            state["trades"].append({"id": state["next_id"], "date": today, "setup": setup,
                "symbol": symbol, "side": side, "qty": qty, "entry_time": stamp,
                "entry_price": round(quote["ltp"], 2),
                "sl_price": round((quote["low"] if side == "BUY" else quote["high"])
                                  * (0.995 if side == "BUY" else 1.005), 2),
                "exit_time": None, "exit_price": None, "reason": None, "pnl": None,
                "status": "OPEN", "feed": "yahoo"})
            state["next_id"] += 1


def f3_scan(state: dict, quotes: dict, moment: datetime) -> None:
    """First-3-candle momentum longs (Rita's rule) once a day in the window."""
    today = moment.date().isoformat()
    if state.get("f3_done_date") == today or moment.weekday() >= 5:
        return
    if not (F3_WINDOW[0] <= moment.time() <= F3_WINDOW[1]):
        return
    state["f3_done_date"] = today
    if nifty_bias() != 1:
        return
    symbols = nse_symbols()
    bars = yf.download([f"{s}.NS" for s in symbols], period="5d", interval="1h",
                       group_by="ticker", progress=False, threads=True, auto_adjust=False)
    picks = []
    for symbol in symbols:
        try:
            frame = flatten(bars[f"{symbol}.NS"].copy())
            if frame is None or len(frame) < 4:
                continue
            days = frame.index.tz_convert(IST).strftime("%Y-%m-%d")
            idx = [i for i, d in enumerate(days) if d == today]
            if len(idx) < 3 or (idx[0] > 0 and days[idx[0] - 1] == today):
                continue
            i = idx[0]
            h, l, c = frame["High"].values, frame["Low"].values, frame["Close"].values
            if c[i + 1] > h[i] and c[i + 2] > h[i + 1]:
                entry, sl = float(c[i + 2]), float(l[i])
                if entry > 0 and sl * 0.995 < entry:
                    picks.append(((entry - float(c[i])) / float(c[i]) * 100,
                                  symbol, entry, sl))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    picks.sort(reverse=True)
    stamp = moment.strftime("%H:%M:%S")
    already = {t["symbol"] for t in state["trades"]
               if t.get("date") == today and t.get("setup") == "f3"}
    for drive, symbol, _, sl in [p for p in picks if p[1] not in already][:F3_MAX_TRADES]:
        quote = quotes.get(symbol)
        if not quote:
            continue
        entry = quote["ltp"]            # fill where the market is NOW
        qty = int(NSE_CAPITAL / entry)
        if qty < 1:
            continue
        state["trades"].append({"id": state["next_id"], "date": today, "setup": "f3",
            "symbol": symbol, "side": "BUY", "qty": qty, "entry_time": stamp,
            "entry_price": round(entry, 2), "sl_price": round(sl * 0.995, 2),
            "exit_time": None, "exit_price": None, "reason": None, "pnl": None,
            "status": "OPEN", "feed": "f3scan-cloud", "drive_pct": round(drive, 2)})
        state["next_id"] += 1


def update_nse(state: dict) -> dict:
    moment = now()
    quotes = nse_quotes()
    manage_nse(state, quotes, moment)
    enter_nse(state, quotes, moment)
    f3_scan(state, quotes, moment)
    return nse_snapshot(state, quotes, moment.date().isoformat())


def nse_snapshot(state: dict, quotes: dict, today: str) -> dict:
    trades = state["trades"]
    opens = []
    for trade in trades:
        if trade.get("status") != "OPEN" or trade.get("date") != today:
            continue
        quote, sign = quotes.get(trade["symbol"]), 1 if trade["side"] == "BUY" else -1
        ltp = quote["ltp"] if quote else None
        copy = {k: v for k, v in trade.items() if k != "status"}
        copy["ltp"] = round(ltp, 2) if ltp else None
        copy["pnl"] = round(sign * (ltp - trade["entry_price"]) * trade["qty"], 2) if ltp else None
        copy["pnl_pct"] = round(sign * (ltp - trade["entry_price"]) / trade["entry_price"] * 100, 2) if ltp else None
        opens.append(copy)
    closed = [{k: v for k, v in t.items() if k not in ("status", "drive_pct")} for t in trades if t.get("status") == "CLOSED"]
    closed.sort(key=lambda t: (t.get("date", ""), t.get("entry_time", "")), reverse=True)
    daily, cumulative = [], 0.0
    for day in sorted({t.get("date") for t in trades if t.get("date")}):
        rows = [t for t in trades if t.get("date") == day]
        exits = [t for t in rows if t.get("status") == "CLOSED"]
        pnl = round(sum(t.get("pnl") or 0 for t in exits), 2)
        cumulative += pnl
        daily.append({"date": day, "trades": len(rows), "ol": sum(t.get("setup") == "ol" for t in rows),
                      "oh": sum(t.get("setup") == "oh" for t in rows), "wins": sum((t.get("pnl") or 0) > 0 for t in exits),
                      "losses": sum((t.get("pnl") or 0) <= 0 for t in exits), "pnl": pnl, "cum": round(cumulative, 2)})
    wins = sum((t.get("pnl") or 0) > 0 for t in closed)
    realized = round(sum(t.get("pnl") or 0 for t in closed), 2)
    unrealized = round(sum(t.get("pnl") or 0 for t in opens), 2)
    today_row = next((row for row in daily if row["date"] == today), None)
    return {"updated_at": now().strftime("%d %b %Y %H:%M:%S IST"), "feed": "yahoo",
            "feed_note": "Cloud engine (GitHub Actions + Yahoo, no Mac needed)",
            "status": "DONE" if now().time() >= time(15, 30) else "RUNNING", "today": today,
            "strategy": {"rule": "fresh O=L -> BUY / fresh O=H -> SELL, ₹10,000/stock",
                         "sl": "O=L long: 0.5% below day low | O=H short: 0.5% above day high",
                         "square_off": "15:00", "entry_cutoff": "09:45",
                         "time_stop_long": "10:45", "nifty_gate": True,
                         "min_vol_mult": 1.5, "min_vol_mult_by_setup": {"ol": 3.0}},
            "summary": {"open_count": len(opens), "closed_count": len(closed), "trades_today": today_row["trades"] if today_row else 0,
                        "today_pnl": today_row["pnl"] if today_row else 0, "realized_total": realized, "unrealized": unrealized,
                        "wins": wins, "losses": len(closed) - wins, "win_rate": round(wins / len(closed) * 100, 1) if closed else 0,
                        "days": len(daily)},
            "open": opens, "closed": closed, "daily": daily}


def market_quote(symbol: str) -> dict | None:
    frame = flatten(yf.download(GC_MARKETS[symbol], period="2d", interval="1m", progress=False, auto_adjust=False))
    if frame is None or frame.empty:
        return None
    latest = frame.iloc[-1]
    day = frame[frame.index.date == frame.index[-1].date()]
    if day.empty:
        return None
    return {"session": str(day.index[-1].date()), "open": float(day.iloc[0]["Open"]), "high": float(day["High"].max()),
            "low": float(day["Low"].min()), "ltp": float(latest["Close"])}


def update_gc(state: dict) -> dict:
    moment, stamp = now(), now().strftime("%H:%M:%S")
    quotes = {symbol: quote for symbol in GC_MARKETS if (quote := market_quote(symbol))}
    for trade in state["trades"]:
        quote = quotes.get(trade["symbol"])
        if trade.get("status") != "OPEN" or not quote:
            continue
        hit = quote["low"] <= trade["sl_price"] if trade["side"] == "BUY" else quote["high"] >= trade["sl_price"]
        if hit:
            close(trade, trade["sl_price"], "SL", stamp)
        elif moment.time() >= time(22, 30):
            close(trade, quote["ltp"], "EOD", stamp)
    if time(5, 30) <= moment.time() <= time(7, 30):
        seen = {(t["session"], t["symbol"], t["setup"]) for t in state["trades"]}
        for symbol, quote in quotes.items():
            ol = abs(quote["open"] - quote["low"]) / quote["open"] <= GC_TOL
            oh = abs(quote["high"] - quote["open"]) / quote["open"] <= GC_TOL
            if ol == oh:
                continue
            setup, side = ("ol", "BUY") if ol else ("oh", "SELL")
            if (quote["session"], symbol, setup) in seen:
                continue
            state["trades"].append({"id": state["next_id"], "session": quote["session"], "setup": setup, "symbol": symbol,
                "side": side, "qty": round(GC_CAPITAL / quote["ltp"], 4), "entry_time": stamp, "entry_price": round(quote["ltp"], 2),
                # 0.5% buffer beyond the session extreme — the raw session
                # low/high stop went 0W/8L on sub-0.5% noise
                "sl_price": round(quote["low"] * 0.995 if side == "BUY" else quote["high"] * 1.005, 2), "exit_time": None, "exit_price": None,
                "reason": None, "pnl": None, "status": "OPEN"})
            state["next_id"] += 1
    return gc_snapshot(state, quotes)


def gc_snapshot(state: dict, quotes: dict) -> dict:
    opens, closed = [], []
    for trade in state["trades"]:
        copy = {k: v for k, v in trade.items() if k != "status"}
        quote = quotes.get(trade["symbol"])
        if trade.get("status") == "OPEN":
            ltp = quote["ltp"] if quote else None
            sign = 1 if trade["side"] == "BUY" else -1
            copy["ltp"] = round(ltp, 2) if ltp else None
            copy["pnl"] = round(sign * (ltp - trade["entry_price"]) * trade["qty"], 2) if ltp else None
            opens.append(copy)
        elif trade.get("status") == "CLOSED":
            closed.append(copy)
    closed.sort(key=lambda t: (t.get("session", ""), t.get("entry_time", "")), reverse=True)
    wins = sum((t.get("pnl") or 0) > 0 for t in closed)
    return {"updated_at": now().strftime("%d %b %Y %H:%M:%S IST"), "status": "DONE" if now().time() >= time(22, 30) else "RUNNING",
            "summary": {"open_count": len(opens), "closed_count": len(closed), "realized_total": round(sum(t.get("pnl") or 0 for t in closed), 2),
                        "unrealized": round(sum(t.get("pnl") or 0 for t in opens), 2), "wins": wins, "losses": len(closed) - wins,
                        "win_rate": round(wins / len(closed) * 100, 1) if closed else 0}, "open": opens, "closed": closed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("desk", choices=("nse", "gc", "swing", "all"))
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    if args.desk in ("nse", "all"):
        state = read_json(NSE_STATE, empty_state())
        snapshot = update_nse(state)
        write_json(NSE_STATE, state)
        write_json(NSE_SNAPSHOT, snapshot)
    if args.desk in ("gc", "all"):
        state = read_json(GC_STATE, empty_state())
        snapshot = update_gc(state)
        write_json(GC_STATE, state)
        write_json(GC_SNAPSHOT, snapshot)
    if args.desk in ("swing", "all"):
        import cloud_swing
        cloud_swing.run_day()
    if args.desk == "gc":
        # Scheduled gc ticks run every 15 min around the clock — piggyback
        # the other desks on them so the system needs no extra workflow
        # schedules (the PAT can't edit workflow files; this keeps the whole
        # migration inside plain code):
        #   * NSE update during the 09:15-15:45 IST window (entries, SL /
        #     time-stop / square-off management on market days)
        #   * swing scan once per weekday evening (first tick >= 17:35 IST)
        moment = now()
        if moment.weekday() < 5 and time(9, 15) <= moment.time() <= time(15, 45):
            state = read_json(NSE_STATE, empty_state())
            snapshot = update_nse(state)
            write_json(NSE_STATE, state)
            write_json(NSE_SNAPSHOT, snapshot)
        if moment.weekday() < 5 and time(17, 35) <= moment.time() < time(22, 0):
            import cloud_swing
            if read_json(cloud_swing.STATE_FILE, {}).get("done_date") \
                    != moment.date().isoformat():
                cloud_swing.run_day()


if __name__ == "__main__":
    main()
