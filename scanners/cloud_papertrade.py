#!/usr/bin/env python3
"""Stateless GitHub Actions paper trader for NSE O=L/O=H + F3 and Gold/BTC.

The committed JSON state is the ledger. Each Actions run reads it, updates
positions once, and writes the dashboard snapshots consumed by paper.html.
No credentials, Mac, or manual steps required — this is the primary engine;
the Mac install is a retired fallback.

NSE rules = the tuned local engine (parity since 2026-09-20):
  entry cutoff 09:45 · square-off 15:00 · max 5/setup by est. volume
  O=L needs est. volume >= 3.0x avg20 (PF 8.1 backtest) · O=H >= 1.5x —
  cap RESTORED 2026-10-01: the cap-free live run 28-30 Sep confirmed the
  volume-gated picks are fewer but better (owner decision: minimise trade
  count / cut losses). Unlike the 09-20 version, a throttled 30d history
  now flags vol_known=False and the blocked picks are counted in the
  snapshot (vol_blocked) instead of silently zeroing vol_ratio.
  SL = 0.5% beyond day low/high · NIFTY-direction gate on both legs
  2026-09-28 opening-candle experiment (monitored for a week):
    entries from 09:20 (first 5-min candle complete) · primary stop is a
    5-min candle CLOSE beyond the opening low/high captured at entry
    (CANDLE-CLOSE); the buffered tick SL stays as backstop
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

import telegram_notify

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
NSE_MIN_VOL = 1.5              # O=H short entry cap (and alerts-page split)
NSE_MIN_VOL_OL = 3.0           # O=L long entry cap (PF 8.1 backtest)
NSE_MAX_PER_SETUP = 5
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


FEED_NAME = "yahoo"            # set per run: "angel" when SmartAPI answered


def nse_symbols() -> list[str]:
    rows = (DATA / "nse200_symbols.csv").read_text().splitlines()
    return [row.strip().split(",")[0] for row in rows[1:] if row.strip()]


def nse_quotes() -> dict[str, dict]:
    """Live day-OHLC quotes for the universe. Angel SmartAPI when its
    credentials are present (real-time, TOTP auto-login), Yahoo's delayed
    feed as automatic fallback — the desk never skips a poll over auth."""
    global FEED_NAME
    try:
        import angel_feed
        if angel_feed.available():
            q = angel_feed.quotes(nse_symbols())
            if len(q) >= 100:            # healthy universe coverage
                FEED_NAME = "angel"
                return q
    except Exception as e:
        print(f"angel feed unavailable, falling back to yahoo: {e}")
    return _yahoo_quotes()


def _yahoo_quotes() -> dict[str, dict]:
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
        quote = quotes[symbol]
        quote["vol_known"] = False
        try:
            frame = flatten(volume_bars[f"{symbol}.NS"].copy())
            history = frame["Volume"].iloc[:-1].tail(20)
            average = float(history.mean()) if len(history) else 0.0
            # <5 history bars = throttled/partial download, not a real
            # avg20 — mark unknown so the entry cap blocks it VISIBLY
            # (vol_blocked) instead of silently treating it as low volume
            quote["vol_known"] = bool(len(history) >= 5 and average > 0)
            quote["vol_ratio"] = (quote["volume"] / session_fraction / average
                                  if quote["vol_known"] else 0.0)
        except (KeyError, TypeError, ValueError):
            quote["vol_ratio"] = 0.0
    return quotes


def close(trade: dict, price: float, reason: str, stamp: str) -> None:
    sign = 1 if trade["side"] == "BUY" else -1
    trade.update({"status": "CLOSED", "exit_price": round(price, 2),
                  "exit_time": stamp, "reason": reason,
                  "pnl": round(sign * (price - trade["entry_price"]) * trade["qty"], 2)})
    desk = "Gold/BTC" if trade.get("session") else "O=L/O=H"
    emoji = "✅" if trade["pnl"] > 0 else "🛑"
    try:
        telegram_notify.notify(
            f"{emoji} CLOSE · {desk} ({reason})\n"
            f"{trade['side']} {trade['symbol']} · qty {trade['qty']}\n"
            f"entry {'$' if trade.get('session') else '₹'}"
            f"{trade['entry_price']:,.2f} → exit "
            f"{'$' if trade.get('session') else '₹'}{price:,.2f}\n"
            f"P&L {'$' if trade.get('session') else '₹'}{trade['pnl']:,.2f}")
    except Exception:
        pass


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


def candle_close_exits(state: dict, moment: datetime) -> None:
    """2026-09-28 opening-candle stop: an O=L long exits when a COMPLETED
    5-min candle CLOSES below the opening low captured at entry; an O=H
    short when one closes above the opening high. The tick-level SL in
    manage_nse stays as the disaster backstop. Candle data via Yahoo
    (delayed a few minutes); exits are stamped at the candle's close."""
    import pandas as pd
    today = moment.date().isoformat()
    live = [t for t in state["trades"]
            if t.get("status") == "OPEN" and t.get("date") == today
            and t.get("setup") in ("ol", "oh") and t.get("candle_stop")]
    if not live:
        return
    try:
        bars = yf.download([f"{t['symbol']}.NS" for t in live], period="1d",
                           interval="5m", group_by="ticker", progress=False,
                           threads=True, auto_adjust=False, prepost=False)
    except Exception as e:
        print(f"candle fetch failed: {e}")
        return
    for trade in live:
        if trade["status"] != "OPEN":
            continue
        try:
            frame = flatten(bars[f"{trade['symbol']}.NS"].copy())
            if frame is None or frame.empty:
                continue
            cut = datetime.strptime(trade["entry_time"], "%H:%M:%S").time()
            for ts, row in frame.iterrows():
                end = (ts + pd.Timedelta(minutes=5)).time()
                if end > moment.time() or end <= cut:   # complete + post-entry
                    continue
                c = float(row["Close"])
                if not math.isfinite(c):
                    continue
                if (trade["side"] == "BUY" and c < trade["candle_stop"]) or \
                        (trade["side"] == "SELL" and c > trade["candle_stop"]):
                    close(trade, c, "CANDLE-CLOSE", end.strftime("%H:%M:%S"))
                    break
        except (KeyError, TypeError, ValueError):
            continue


def _vol_block(state: dict, today: str, setup: str, symbol: str, reason: str) -> None:
    """Record an O=L/O=H pick rejected by the volume cap (daily, deduped).
    Surfaced in the snapshot as vol_blocked so a zero-trade day is
    explainable at a glance instead of silently empty."""
    vb = state.get("vol_blocked") or {}
    if vb.get("date") != today:
        vb = {"date": today,
              "ol": {"low_vol": [], "no_vol_data": []},
              "oh": {"low_vol": [], "no_vol_data": []}}
        state["vol_blocked"] = vb
    bucket = vb.setdefault(setup, {"low_vol": [], "no_vol_data": []})
    if symbol not in bucket.setdefault(reason, []):
        bucket[reason].append(symbol)


def enter_nse(state: dict, quotes: dict, moment: datetime) -> None:
    today, stamp = moment.date().isoformat(), moment.strftime("%H:%M:%S")
    # 2026-09-28 opening-candle rules: entries start once the FIRST 5-min
    # candle of the day has completed (09:20 IST) — a fresh day O=L/O=H at
    # that point is an opening-candle setup, not an intraday drift
    if not (time(9, 20) <= moment.time() <= NSE_ENTRY_CUTOFF):
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
        # volume cap (restored 2026-10-01): O=L 3.0x avg20, O=H 1.5x —
        # fewer, higher-quality entries; blocked picks are counted
        min_vol = NSE_MIN_VOL_OL if setup == "ol" else NSE_MIN_VOL
        eligible = []
        for symbol, quote in quotes.items():
            edge = abs(quote["open"] - quote["low"]) if setup == "ol" else abs(quote["high"] - quote["open"])
            if (symbol, setup) in seen or quote["open"] < NSE_MIN_PRICE:
                continue
            if edge > min(NSE_TOL, quote["open"] * 0.0005):
                continue
            if not quote.get("vol_known"):
                _vol_block(state, today, setup, symbol, "no_vol_data")
                continue
            if quote.get("vol_ratio", 0) < min_vol:
                _vol_block(state, today, setup, symbol, "low_vol")
                continue
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
                "candle_stop": round(quote["low"] if side == "BUY" else quote["high"], 2),
                "sl_price": round((quote["low"] if side == "BUY" else quote["high"])
                                  * (0.995 if side == "BUY" else 1.005), 2),
                "exit_time": None, "exit_price": None, "reason": None, "pnl": None,
                "status": "OPEN", "feed": FEED_NAME})
            state["next_id"] += 1
            try:
                telegram_notify.notify(
                    f"⚡ OPEN · O=L/O=H ({setup.upper()})\n"
                    f"{side} {symbol} · qty {qty}\n"
                    f"entry ₹{quote['ltp']:,.2f} · SL ₹"
                    f"{round((quote['low'] if side == 'BUY' else quote['high']) * (0.995 if side == 'BUY' else 1.005), 2):,.2f}")
            except Exception:
                pass


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
            "status": "OPEN", "feed": f"{FEED_NAME}-f3", "drive_pct": round(drive, 2)})
        state["next_id"] += 1
        try:
            telegram_notify.notify(
                f"⚡ OPEN · First-3 momentum\n"
                f"BUY {symbol} · qty {qty}\n"
                f"entry ₹{entry:,.2f} · SL ₹{round(sl * 0.995, 2):,.2f} · "
                f"drive {drive:.1f}%")
        except Exception:
            pass


def export_alerts_json(quotes: dict, today: str) -> None:
    """Refresh data/alerts.json for the portal Alerts page (O=L / O=H tabs).
    Same shape the scheduled scanners wrote, fed by the live quotes — the
    page prefers this over its CORS-proxy browser scan and only shows stale
    data when this file is old (its cron-fed writers barely survive GitHub's
    schedule throttling; called from every NSE poll instead)."""
    import yfinance as _yf
    sess = max(0.20, min(1.0, (now().hour * 60 + now().minute - 555) / 375))

    def build(setup: str) -> list[dict]:
        rows = []
        for symbol, q in quotes.items():
            o, h, low, ltp, vol = (q["open"], q["high"], q["low"],
                                   q["ltp"], q["volume"])
            if o < NSE_MIN_PRICE or vol <= 0:
                continue
            edge = abs(o - low) if setup == "ol" else abs(h - o)
            if edge > min(NSE_TOL, o * 0.0005):
                continue
            avg20 = 0.0
            try:
                vb = flatten(_yf.download(f"{symbol}.NS", period="30d",
                                          interval="1d", progress=False,
                                          threads=False, auto_adjust=False))
                if vb is not None and not vb.empty:
                    avg20 = float(vb["Volume"].iloc[:-1].tail(20).mean())
            except Exception:
                pass
            vol_ratio = vol / avg20 if avg20 else 0.0
            est_full = vol_ratio / sess
            qty = int(NSE_CAPITAL / ltp) if ltp else 0
            sign = 1 if setup == "ol" else -1
            rows.append({
                "symbol": symbol,
                "yf_open": o, "yf_high": h, "yf_low": low, "yf_close": ltp,
                "yf_volume": vol, "avg_vol_20d": int(avg20),
                "ol_diff": round(edge, 2), "vol_ratio": round(vol_ratio, 2),
                "est_full_vol_ratio": round(est_full, 2),
                "shares": qty, "invested": int(qty * ltp),
                "sl_price": round(low * 0.995 if setup == "ol" else h * 1.005, 2),
                "pnl": round(sign * (ltp - o) * qty, 2),
                "pnl_pct": round(sign * (ltp - o) / o * 100, 2) if o else 0.0,
                "gap_pct": 0.0,
                "day_high_pct": round((h - o) / o * 100, 2) if setup == "ol"
                                else round((o - low) / o * 100, 2) if o else 0.0,
                "price": ltp, "date": f"{today} 00:00:00", "in_nse200": True,
                "_est": est_full,
            })
        rows.sort(key=lambda r: r["_est"], reverse=True)
        for r in rows:
            r.pop("_est", None)
        return rows

    def split(rows: list[dict]):
        return ([r for r in rows if r["est_full_vol_ratio"] >= NSE_MIN_VOL],
                [r for r in rows if r["est_full_vol_ratio"] < NSE_MIN_VOL])

    ol_rows, oh_rows = build("ol"), build("oh")
    ol_with, ol_without = split(ol_rows)
    oh_with, oh_without = split(oh_rows)
    payload = {
        "fetched_at": now().isoformat(),
        "chartink_raw_count": len(ol_rows),
        "with_volume": ol_with, "without_volume": ol_without,
        "oh": {"raw_count": len(oh_rows),
               "with_volume": oh_with, "without_volume": oh_without},
    }
    write_json(DATA / "alerts.json", payload)
    try:
        export_live_stocks(quotes, today)
    except Exception as e:
        print(f"live_stocks export failed: {e}")


def _company_names() -> dict[str, str]:
    """symbol -> company name from the NIFTY-500 CSV in the repo."""
    names: dict[str, str] = {}
    csv = DATA / "nifty500_symbols.csv"
    if not csv.exists():
        return names
    for line in csv.read_text().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 4 and parts[3].strip() == "EQ":
            names[parts[2].strip()] = parts[0].strip()
    return names


def export_live_stocks(quotes: dict, today: str) -> None:
    """Live Stocks page snapshot (data/live_stocks.json) — was fed by the
    throttled refresh-stocks cron on Yahoo; now written from the live Angel
    quotes on every NSE poll."""
    import time as _time
    names = _company_names()
    stocks = []
    for symbol, q in sorted(quotes.items()):
        prev = q.get("prev_close") or q["open"]
        tol = min(NSE_TOL, q["open"] * 0.0005)
        stocks.append({
            "symbol": symbol, "name": names.get(symbol, symbol),
            "ltp": round(q["ltp"], 2), "prev_close": round(prev, 2),
            "chg": round(q["ltp"] - prev, 2),
            "pct": round((q["ltp"] - prev) / prev * 100, 2) if prev else 0.0,
            "open": round(q["open"], 2), "high": round(q["high"], 2),
            "low": round(q["low"], 2), "volume": int(q["volume"]),
            "ol": abs(q["open"] - q["low"]) <= tol,
            "market_time": int(_time.time()),
        })
    write_json(DATA / "live_stocks.json", {
        "fetched_at": now().isoformat(), "source": "angel-smartapi",
        "universe": "NSE200", "count": len(stocks), "stocks": stocks})


def export_f3_picks(state: dict, today: str) -> None:
    """3-Candle alerts tab (data/f3_picks.json) — the Mac scan that wrote it
    is retired; mirror its shape from today's cloud F3 trades so the tab
    stays current (empty list when the scan found nothing)."""
    rows = [t for t in state["trades"]
            if t.get("date") == today and t.get("setup") == "f3"]
    write_json(DATA / "f3_picks.json", {
        "date": today, "scanned_at": now().strftime("%H:%M:%S"),
        "rule": "day's first 3 hourly candles same-side, each closing above "
                "the previous HIGH -> BUY | SL = candle-1 low - 0.5% | "
                "square-off 15:00 | NIFTY-gated | cutoff 13:30",
        "max_trades": F3_MAX_TRADES,
        "signals": [{"symbol": t["symbol"], "drive_pct": t.get("drive_pct"),
                     "qty": t["qty"], "entry": t["entry_price"],
                     "sl": t["sl_price"], "status": t["status"],
                     "exit": t["exit_price"], "reason": t["reason"],
                     "pnl": t["pnl"]} for t in rows],
    })


def update_nse(state: dict) -> dict:
    moment = now()
    quotes = nse_quotes()
    manage_nse(state, quotes, moment)
    candle_close_exits(state, moment)
    enter_nse(state, quotes, moment)
    f3_scan(state, quotes, moment)
    try:
        export_alerts_json(quotes, moment.date().isoformat())
    except Exception as e:
        print(f"alerts export failed: {e}")
    if state.get("f3_done_date") == moment.date().isoformat():
        try:
            export_f3_picks(state, moment.date().isoformat())
        except Exception as e:
            print(f"f3_picks export failed: {e}")
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
    vb = state.get("vol_blocked") or {}
    vol_blocked = ({s: {r: len(vb.get(s, {}).get(r, [])) for r in ("low_vol", "no_vol_data")}
                    for s in ("ol", "oh")}
                   if vb.get("date") == today else {})
    return {"updated_at": now().strftime("%d %b %Y %H:%M:%S IST"), "feed": FEED_NAME,
            "feed_note": ("Angel SmartAPI live feed (cloud, no Mac)"
                          if FEED_NAME == "angel" else
                          "Cloud engine (GitHub Actions + Yahoo, no Mac needed)"),
            "status": "DONE" if now().time() >= time(15, 30) else "RUNNING", "today": today,
            "strategy": {"rule": "first 5-min candle O=L -> BUY / O=H -> SELL (entries from 09:20, opening candle complete), ₹10,000/stock",
                         "sl": "primary: 5-min candle CLOSE below opening low (long) / above opening high (short) | backstop: 0.5% beyond day low/high",
                         "square_off": "15:00", "entry_cutoff": "09:45",
                         "time_stop_long": "10:45", "nifty_gate": True,
                         "min_vol_mult": 1.5, "min_vol_mult_by_setup": {"ol": 3.0},
                         "vol_note": "volume cap restored 2026-10-01 — cap-free live run 28-30 Sep confirmed fewer, better trades"},
            "vol_blocked": vol_blocked,
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


def export_gc_scan(state: dict, quotes: dict) -> None:
    """Gold/BTC Alerts tabs (data/gc_scan.json) — the Mac goldcrypto engine
    that wrote it retired 2026-09-20; mirror its shape from the cloud gc
    desk's live quotes and open positions."""
    open_by_sym = {t["symbol"]: t for t in state["trades"]
                   if t.get("status") == "OPEN"}
    moment = now()
    markets = []
    for symbol, q in quotes.items():
        tol = q["open"] * GC_TOL
        pos = open_by_sym.get(symbol)
        markets.append({
            "symbol": symbol, "yahoo": GC_MARKETS[symbol],
            "session": q["session"], "open": q["open"], "high": q["high"],
            "low": q["low"], "ltp": q["ltp"],
            "chg_pct": round((q["ltp"] - q["open"]) / q["open"] * 100, 2),
            "minutes_since_open": None,
            "entry_window_open": time(5, 30) <= moment.time() <= time(7, 30),
            "ol": abs(q["open"] - q["low"]) <= tol,
            "oh": abs(q["high"] - q["open"]) <= tol,
            "position": ({"side": pos["side"], "setup": pos["setup"],
                          "qty": pos["qty"], "entry_price": pos["entry_price"],
                          "entry_time": pos["entry_time"],
                          "sl_price": pos["sl_price"],
                          "pnl": pos.get("pnl")} if pos else None),
            "square_off": "22:30", "capital_usd": GC_CAPITAL,
        })
    write_json(DATA / "gc_scan.json", {
        "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
        "engine": "cloud gc desk (GitHub Actions relay — no Mac needed)",
        "markets": markets})


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
            try:
                gc_sl = round(quote["low"] * 0.995 if side == "BUY"
                              else quote["high"] * 1.005, 2)
                telegram_notify.notify(
                    f"🥇 OPEN · Gold/BTC ({setup.upper()})\n"
                    f"{side} {symbol} · qty {round(GC_CAPITAL / quote['ltp'], 4)}\n"
                    f"entry ${quote['ltp']:,.2f} · SL ${gc_sl:,.2f}")
            except Exception:
                pass
    try:
        export_gc_scan(state, quotes)
    except Exception as e:
        print(f"gc_scan export failed: {e}")
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


def commit_state() -> None:
    """Stage + commit exactly this engine's files. The workflow's own commit
    step has a fixed file list that lags behind new desks — files it doesn't
    list would stay unstaged and abort its `git pull --rebase` (run 118,
    20 Sep). Self-committing keeps every run's tree clean regardless."""
    import subprocess
    files = ["data/cloud_paper_ol_state.json", "data/cloud_paper_gc_state.json",
             "data/cloud_swing_state.json", "data/cloud_qm_state.json",
             "data/paper_ol.json", "data/paper_gc.json",
             "data/swing_picks.json", "data/paper_qm.json", "data/qm_picks.json",
             "data/cloud_btc_theta_state.json", "data/paper_btc_theta.json",
             "data/alerts.json", "data/live_stocks.json",
             "data/gc_scan.json", "data/f3_picks.json",
             "data/bbtrap.json",
             "data/scanners/nifty-daily-theta-latest.json",
             "scanners/papertrades/daily_theta_trades.csv",
             "data/scanners/nifty-premium-mill-latest.json",
             "scanners/papertrades/nifty_premium_mill_trades.csv",
             "scanners/papertrades/nifty_premium_mill_log.csv",
             "data/scanners/htf-hm-latest.json",
             "scanners/papertrades/htf_hm_trades.csv"]
    for f in files:
        subprocess.run(["git", "add", "--", f], cwd=ROOT,
                       check=False, capture_output=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT,
                      capture_output=True).returncode != 0:
        stamp = now().strftime("%Y-%m-%dT%H:%MZ")
        subprocess.run(
            ["git", "-c", "user.name=papertrade-bot",
             "-c", "user.email=papertrade-bot@users.noreply.github.com",
             "commit", "-q", "-m", f"chore: paper trade state {stamp}"],
            cwd=ROOT, check=False, capture_output=True)


def maybe_start_poller() -> None:
    """Self-heal: any scheduled run that survives GitHub's cron throttling
    re-lights a dead relay. The relay commits continuously while alive, so a
    stale board heartbeat (>25 min) means it died — relight with whichever
    phase fits now (day during the session, gcwatch otherwise)."""
    try:
        import market_poller as mp
    except ImportError:
        return
    try:
        if mp.board_heartbeat_age() < mp.HEARTBEAT_STALE_S:
            return                    # relay is alive and committing
        m = now()
        if m.weekday() < 5 and mp.DAY_START <= m.time() < mp.DAY_END:
            mp.dispatch_poller("day")
        else:
            mp.dispatch_poller("gcwatch")
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("desk", choices=("nse", "gc", "swing", "poller", "all"))
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    if args.desk == "poller":
        import os
        import market_poller as mp
        mp.main(os.environ.get("POLLER_MODE") or "day")
        return
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
        # BTC 0DTE theta desk (rulebook BTC_0DTE_RULES.md v2): 24/7 market,
        # piggybacks on every gc tick — marks/exits continuously, opens the
        # daily structure inside its 13:30-15:29 IST entry window
        try:
            import cloud_btc_theta
            cloud_btc_theta.tick()
        except Exception as e:
            print(f"btc theta tick failed: {e}", flush=True)
    commit_state()
    maybe_start_poller()


if __name__ == "__main__":
    main()
