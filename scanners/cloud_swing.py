#!/usr/bin/env python3
"""Candlestick Swing desk — cloud port of papertrade_ol/swing/engine_swing.py
(identical signal/exit semantics; JSON state in the repo instead of SQLite,
no Mac, no publish subprocess — the workflow commits the outputs).

  CS-MARU  BUY   big green marubozu (body>=85% range, range>=1.5*ATR14)
                 within 1% of the 20-day low        (PF 1.39, WR 50.2%)
  CS-PINS  SELL  shooting star after 3-day rise + volume>=1.5x avg20 (PF 1.15)
  CS-ENGF  BUY   bullish engulfing within 1% of 20-day low           (PF 1.13)

Signal on day D close -> entry at D+1 open, SL = pattern extreme, target 2R,
time stop 10 sessions, SL assumed first if both hit (conservative).
Markets: NSE-500 (₹10,000/trade) + GOLD + BTC ($60 notional, untested).
"""
from __future__ import annotations

import json
import time
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATE_FILE = DATA / "cloud_swing_state.json"
OUT = DATA / "swing_picks.json"
IST = ZoneInfo("Asia/Kolkata")

MARKETS = {"GOLD": "GC=F", "BTC": "BTC-USD"}
CFG = {"end_date": "2026-12-31", "capital_inr": 10_000.0, "capital_usd": 60.0,
       "max_open": 5, "rr": 2.0, "max_sessions": 10}


def now() -> datetime:
    return datetime.now(IST)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "next_id": 1, "trades": [], "done_date": ""}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")


def detect(df: pd.DataFrame) -> list[tuple[str, str, float]]:
    """(setup, side, sl_px) signals on the LAST completed bar."""
    o = df["Open"].values
    h, l, c, v = (df[k].values for k in ("High", "Low", "Close", "Volume"))
    n = len(df)
    if n < 30:
        return []
    i = n - 1
    body = abs(float(c[i]) - float(o[i]))
    rng = max(h[i] - l[i], 1e-9)
    up_wick = h[i] - max(o[i], c[i])
    lo20 = min(l[i - 19:i + 1])
    atr = (sum(float(h[j] - l[j]) for j in range(i - 14, i)) / 14) or 1e-9
    volavg = (sum(float(v[j]) for j in range(i - 20, i)) / 20) or 1e-9
    rose3 = sum(c[i - k] > c[i - k - 1] for k in range(3)) >= 2
    fell3 = sum(c[i - k] < c[i - k - 1] for k in range(3)) >= 2
    at_low = (l[i] - lo20) / lo20 < 0.01
    sigs = []
    green = c[i] > o[i]
    red = c[i] < o[i]
    if green and body >= 0.85 * rng and rng >= 1.5 * atr and at_low:
        sigs.append(("CS-MARU", "BUY", float(l[i])))
    if red and up_wick >= 2 * body and (h[i] - max(o[i], c[i])) / rng >= 0.5 \
            and rose3 and v[i] >= 1.5 * volavg:
        sigs.append(("CS-PINS", "SELL", float(h[i])))
    if green and i >= 1 and c[i - 1] < o[i - 1] and c[i] > o[i - 1] \
            and o[i] < c[i - 1] and fell3 and at_low:
        sigs.append(("CS-ENGF", "BUY", float(l[i])))
    return sigs


def fetch_daily(ticker: str) -> pd.DataFrame | None:
    df = yf.download(ticker, period="1y", interval="1d",
                     progress=False, threads=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _ticker_for(t: dict) -> str:
    return MARKETS[t["market"]] if t["market"] in MARKETS else f"{t['symbol']}.NS"


def run_day() -> dict:
    state = read_state()
    today = now().date().isoformat()
    if today > CFG["end_date"]:
        return export(state)
    if now().weekday() >= 5 or state.get("done_date") == today:
        return export(state)
    trades = state["trades"]

    # 1) manage open positions with today's bar (SL first = conservative)
    for t in [x for x in trades if x["status"] == "OPEN"]:
        df = fetch_daily(_ticker_for(t))
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-1]
        px_hi, px_lo, px_cl = float(last["High"]), float(last["Low"]), float(last["Close"])
        t["sessions"] = (t.get("sessions") or 0) + 1
        exit_px = reason = None
        if t["side"] == "BUY":
            if px_lo <= t["sl"]:
                exit_px, reason = t["sl"], "SL"
            elif px_hi >= t["tgt"]:
                exit_px, reason = t["tgt"], "TGT2R"
        else:
            if px_hi >= t["sl"]:
                exit_px, reason = t["sl"], "SL"
            elif px_lo <= t["tgt"]:
                exit_px, reason = t["tgt"], "TGT2R"
        if exit_px is None and t["sessions"] >= CFG["max_sessions"]:
            exit_px, reason = px_cl, "TIME10"
        if exit_px is not None:
            sign = 1 if t["side"] == "BUY" else -1
            t.update(status="CLOSED", exit=round(exit_px, 2), reason=reason,
                     pnl=round(sign * (exit_px - t["entry"]) * t["qty"], 2))

    # 2) fill yesterday's signals at TODAY's open
    for t in [x for x in trades if x["status"] == "PENDING"]:
        df = fetch_daily(_ticker_for(t))
        if df is None or df.iloc[-1].name.strftime("%Y-%m-%d") != today:
            continue
        entry = float(df.iloc[-1]["Open"])
        risk = abs(entry - t["sl"])
        if risk <= 0 or risk / entry > 0.15:
            t["status"] = "SKIP"
            continue
        tgt = entry + (1 if t["side"] == "BUY" else -1) * CFG["rr"] * risk
        cap = CFG["capital_usd"] if t["market"] in MARKETS else CFG["capital_inr"]
        qty = round(cap / entry, 4) if t["market"] in MARKETS else int(cap / entry)
        if qty < (0.0001 if t["market"] in MARKETS else 1):
            t["status"] = "SKIP"
            continue
        t.update(status="OPEN", entry_date=today, qty=qty,
                 entry=round(entry, 2), tgt=round(tgt, 2), sessions=0)

    # 3) scan today's completed bars for NEW signals (fill tomorrow at open)
    open_n = sum(1 for t in trades if t["status"] == "OPEN")
    existing = {(t["symbol"], t["setup"], t["signal_date"]) for t in trades}
    prio = {"CS-MARU": 0, "CS-PINS": 1, "CS-ENGF": 2}
    cands = []

    def consider(market: str, symbol: str, df: pd.DataFrame) -> None:
        d = df.iloc[-1].name.strftime("%Y-%m-%d")
        for setup, side, sl in detect(df):
            if (symbol, setup, d) in existing:
                continue
            cands.append((prio[setup], market, symbol, setup, side, sl, d))

    for mkt, tick in MARKETS.items():
        try:
            df = fetch_daily(tick)
            if df is not None:
                consider(mkt, mkt, df)
        except Exception:
            pass

    syms = []
    csv = DATA / "nifty500_symbols.csv"
    for line in csv.read_text().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 4 and parts[3].strip() == "EQ":
            syms.append(parts[2].strip())
    for k, s in enumerate(syms):
        try:
            df = fetch_daily(f"{s}.NS")
            if df is not None:
                consider("NSE", s, df)
        except Exception:
            pass
        if (k + 1) % 50 == 0:
            time.sleep(0.8)

    cands.sort(key=lambda x: x[0])
    queued = 0
    for _, mkt, sym, setup, side, sl, d in cands:
        if open_n + queued >= CFG["max_open"]:
            break
        trades.append({"id": state["next_id"], "signal_date": d, "market": mkt,
                       "symbol": sym, "setup": setup, "side": side,
                       "status": "PENDING", "entry_date": None, "qty": None,
                       "entry": None, "sl": round(sl, 2), "tgt": None,
                       "sessions": 0, "exit": None, "reason": None, "pnl": None})
        state["next_id"] += 1
        queued += 1

    state["done_date"] = today
    write_state(state)
    return export(state)


def export(state: dict) -> dict:
    trades = state["trades"]
    fields = ("id", "signal_date", "market", "symbol", "setup", "side",
              "status", "entry_date", "qty", "entry", "sl", "tgt",
              "sessions", "exit", "reason", "pnl")

    def row(t: dict) -> dict:
        return {k: t.get(k) for k in fields}

    opens = [row(t) for t in trades if t["status"] == "OPEN"]
    pend = [row(t) for t in trades if t["status"] == "PENDING"]
    closed = [row(t) for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    payload = {
        "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
        "rule": "top-3 candlestick setups (backtested 5y): MARU-at-low BUY "
                "PF 1.39 · ShootingStar+VOL SELL PF 1.15 · Engulf-at-low BUY "
                "PF 1.13 | entry next open, SL=pattern extreme, target 2R, "
                "10-session stop | max 5 concurrent",
        "note": "NSE backtested; GOLD/BTC patterns untested (trial). "
                "Runs in the cloud (GitHub Actions) — no Mac required.",
        "end_date": CFG["end_date"],
        "summary": {"open": len(opens), "pending": len(pend),
                    "closed": len(closed), "wins": len(wins),
                    "losses": len(closed) - len(wins),
                    "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
                    "pnl_inr": round(sum(t["pnl"] or 0 for t in closed
                                         if t["market"] == "NSE"), 2),
                    "pnl_usd": round(sum(t["pnl"] or 0 for t in closed
                                         if t["market"] != "NSE"), 2)},
        "open": opens, "pending": pend, "closed": closed,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    return payload


if __name__ == "__main__":
    print(json.dumps(run_day()["summary"]))
