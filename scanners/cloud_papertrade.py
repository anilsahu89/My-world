#!/usr/bin/env python3
"""Stateless GitHub Actions paper trader for NSE O=L/O=H and Gold/BTC.

The committed JSON state is the ledger. Each Actions run reads it, updates
positions once, and writes the dashboard snapshots consumed by paper.html.
No credentials or machine-local paths are required.
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
NSE_MAX_PER_SETUP = 5

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


def migrate_nse_history(state: dict) -> None:
    """Keep prior closed dashboard history on the first Actions run only."""
    if state["seeded"]:
        return
    legacy = read_json(NSE_SNAPSHOT, {})
    for trade in legacy.get("closed", []):
        copied = dict(trade)
        copied["id"] = state["next_id"]
        state["next_id"] += 1
        copied["status"] = "CLOSED"
        state["trades"].append(copied)
    state["seeded"] = True


def migrate_gc_history(state: dict) -> None:
    """Keep the prior Gold/BTC closed-book history on first cloud execution."""
    if state["seeded"]:
        return
    legacy = read_json(GC_SNAPSHOT, {})
    for trade in legacy.get("closed", []):
        copied = dict(trade)
        copied["id"] = state["next_id"]
        state["next_id"] += 1
        copied["status"] = "CLOSED"
        state["trades"].append(copied)
    state["seeded"] = True


def flatten(frame):
    if frame is None or frame.empty:
        return frame
    if getattr(frame.columns, "nlevels", 1) > 1:
        frame.columns = frame.columns.get_level_values(0)
    return frame.dropna(how="all")


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


def update_nse(state: dict) -> dict:
    migrate_nse_history(state)
    moment, today = now(), now().date().isoformat()
    quotes = nse_quotes()
    stamp = moment.strftime("%H:%M:%S")
    open_trades = [t for t in state["trades"] if t.get("status") == "OPEN" and t.get("date") == today]
    for trade in open_trades:
        quote = quotes.get(trade["symbol"])
        if not quote:
            continue
        sl_hit = quote["low"] <= trade["sl_price"] if trade["side"] == "BUY" else quote["high"] >= trade["sl_price"]
        if sl_hit:
            close(trade, trade["sl_price"], "SL", stamp)
        elif moment.time() >= time(15, 30):
            close(trade, quote["ltp"], "EOD", stamp)
    entry_window = time(9, 30) <= moment.time() <= time(10, 15)
    if entry_window:
        seen = {(t["symbol"], t["setup"]) for t in state["trades"] if t.get("date") == today}
        active = {(t["symbol"], t["setup"]) for t in state["trades"] if t.get("status") == "OPEN" and t.get("date") == today}
        for setup, side in (("ol", "BUY"), ("oh", "SELL")):
            eligible = []
            for symbol, quote in quotes.items():
                edge = abs(quote["open"] - quote["low"]) if setup == "ol" else abs(quote["high"] - quote["open"])
                if (symbol, setup) in seen or (symbol, setup) in active or quote["open"] < NSE_MIN_PRICE:
                    continue
                if edge <= min(NSE_TOL, quote["open"] * 0.0005) and quote.get("vol_ratio", 0) >= NSE_MIN_VOL:
                    eligible.append((quote.get("vol_ratio", 0), symbol, quote))
            already = sum(1 for t in state["trades"] if t.get("date") == today and t.get("setup") == setup)
            for _, symbol, quote in sorted(eligible, reverse=True)[:max(0, NSE_MAX_PER_SETUP - already)]:
                qty = int(NSE_CAPITAL / quote["ltp"])
                if qty < 1:
                    continue
                state["trades"].append({"id": state["next_id"], "date": today, "setup": setup,
                    "symbol": symbol, "side": side, "qty": qty, "entry_time": stamp,
                    "entry_price": round(quote["ltp"], 2), "sl_price": round(quote["low"] if side == "BUY" else quote["high"], 2),
                    "exit_time": None, "exit_price": None, "reason": None, "pnl": None, "status": "OPEN", "feed": "yahoo"})
                state["next_id"] += 1
    return nse_snapshot(state, quotes, today)


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
    closed = [{k: v for k, v in t.items() if k != "status"} for t in trades if t.get("status") == "CLOSED"]
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
            "feed_note": "Cloud runner: Yahoo data may be delayed; entries use a 10:15 IST cutoff.",
            "status": "DONE" if now().time() >= time(15, 30) else "RUNNING", "today": today,
            "strategy": {"rule": "fresh O=L -> BUY / fresh O=H -> SELL, ₹10,000/stock", "sl": "O=L low / O=H high", "square_off": "15:30", "entry_cutoff": "10:15"},
            "summary": {"open_count": len(opens), "closed_count": len(closed), "trades_today": today_row["trades"] if today_row else 0,
                        "today_pnl": today_row["pnl"] if today_row else 0, "realized_total": realized, "unrealized": unrealized,
                        "wins": wins, "losses": len(closed) - wins, "win_rate": round(wins / len(closed) * 100, 1) if closed else 0, "days": len(daily)},
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
    migrate_gc_history(state)
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
                "sl_price": round(quote["low"] if side == "BUY" else quote["high"], 2), "exit_time": None, "exit_price": None,
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
    parser.add_argument("desk", choices=("nse", "gc", "all"))
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


if __name__ == "__main__":
    main()
