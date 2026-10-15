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
CFG = {"end_date": "2026-12-31", "capital_inr": 10_000.0,
       "max_open": 5, "rr": 2.0, "max_sessions": 15, "min_spread": 3.0,
       "max_new_per_day": 3}
ENABLED_SETUPS = {"HM-BUY", "HM-SELL"}  # CS-MARU paused (0/6 live)


def now() -> datetime:
    return datetime.now(IST)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "next_id": 1, "trades": [], "done_date": ""}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")



def rsi9(c):
    import numpy as np
    d = c.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ma_up = up.ewm(alpha=1/9, min_periods=9, adjust=False).mean()
    ma_dn = dn.ewm(alpha=1/9, min_periods=9, adjust=False).mean()
    rs = ma_up / ma_dn.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def wma(s, n):
    import numpy as np
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: (x * w).sum() / w.sum(), raw=True)


def weekly_hm(df):
    if df is None or len(df) < 80:
        return None
    w = df.resample("W-FRI").agg(
        {"Open": "first", "High": "max", "Low": "min",
         "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    if len(w) < 30:
        return None
    r = rsi9(w["Close"])
    w = w.assign(rsi=r, red=wma(r, 21), green=wma(r, 3)).dropna(
        subset=["rsi", "red", "green"])
    return w if len(w) >= 5 else None


def detect(df):
    """Weekly HM newly-true signals. Returns list of (setup, side, sl_px)."""
    w = weekly_hm(df)
    if w is None or len(w) < 4:
        return []
    last, prev = w.iloc[-1], w.iloc[-2]
    r, red, g = float(last["rsi"]), float(last["red"]), float(last["green"])
    r1, red1, g1 = float(prev["rsi"]), float(prev["red"]), float(prev["green"])
    spread = max(r, red, g) - min(r, red, g)
    if spread < CFG.get("min_spread", 3.0):
        return []
    lo3 = float(w["Low"].iloc[-3:].min())
    hi3 = float(w["High"].iloc[-3:].max())
    buy_now = r > 50 and red < r and g > red
    buy_prev = r1 > 50 and red1 < r1 and g1 > red1
    sell_now = r < 50 and red > r and g < red
    sell_prev = r1 < 50 and red1 > r1 and g1 < red1
    sigs = []
    if buy_now and not buy_prev:
        sigs.append(("HM-BUY", "BUY", lo3 * 0.995))
    if sell_now and not sell_prev:
        sigs.append(("HM-SELL", "SELL", hi3 * 1.005))
    return sigs




def fetch_daily(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, period="2y", interval="1d",
                         progress=False, threads=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.columns.duplicated().any():     # yf sometimes returns dup cols
            df = df.loc[:, ~df.columns.duplicated()]
        return df
    except Exception:
        return None


def _ticker_for(t: dict) -> str:
    return MARKETS[t["market"]] if t["market"] in MARKETS else f"{t['symbol']}.NS"


def run_day() -> dict:
    state = read_state()
    today = now().date().isoformat()
    # no-op days must not rewrite swing_picks.json — every write is a commit,
    # and the piggyback ticks would bump the timestamp 20x a day
    if today > CFG["end_date"] or now().weekday() >= 5 \
            or state.get("done_date") == today:
        try:
            return json.loads(OUT.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return export(state)
    trades = state["trades"]
    try:
        import close_requests
        _swing_refs = set(close_requests.consume("swing"))
    except Exception:
        _swing_refs = set()

    # 1) manage open positions with today's bar (SL first = conservative)
    for t in [x for x in trades if x["status"] == "OPEN"]:
        df = fetch_daily(_ticker_for(t))
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-1]
        px_hi, px_lo, px_cl = float(last["High"]), float(last["Low"]), float(last["Close"])
        t["sessions"] = (t.get("sessions") or 0) + 1
        exit_px = reason = None
        if str(t.get("id")) in _swing_refs:
            exit_px, reason = px_cl, "MANUAL"
        elif t["side"] == "BUY":
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
            exit_px, reason = px_cl, "TIME15"
        if exit_px is not None:
            sign = 1 if t["side"] == "BUY" else -1
            t.update(status="CLOSED", exit=round(exit_px, 2), reason=reason,
                     pnl=round(sign * (exit_px - t["entry"]) * t["qty"], 2))
            try:
                import telegram_notify
                emoji = "\u2705" if t["pnl"] > 0 else "\U0001F6D1"
                cur = "$" if t.get("market") != "NSE" else "\u20b9"
                telegram_notify.notify(
                    emoji + " CLOSE \u00b7 HM Swing (" + reason + ")\n"
                    + t["symbol"] + " " + t["side"] + " qty " + str(t["qty"])
                    + "\nP&L " + cur + "{:,.0f}".format(t["pnl"]))
            except Exception:
                pass

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
        try:
            import telegram_notify
            cur = "$" if t.get("market") != "NSE" else "\u20b9"
            telegram_notify.notify(
                "\U0001F486 OPEN \u00b7 HM Swing\n"
                + t["symbol"] + " " + t["side"] + " qty " + str(qty)
                + "\nentry " + cur + "{:,.2f}".format(entry))
        except Exception:
            pass

    # 3) scan today's completed bars for NEW signals (fill tomorrow at open)
    open_n = sum(1 for t in trades if t["status"] == "OPEN")
    existing = {(t["symbol"], t["setup"], t["signal_date"]) for t in trades}
    prio = {"HM-BUY": 0, "HM-SELL": 1}
    cands = []

    def consider(market: str, symbol: str, df: pd.DataFrame) -> None:
        d = df.iloc[-1].name.strftime("%Y-%m-%d")
        for setup, side, sl in detect(df):
            if setup not in ENABLED_SETUPS:
                continue
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
    for r in opens:                      # live MTM on ledger rows (same fix
        if r.get("mark"):                # as QM: opens showed pnl "-")
            r["pnl"] = round((r.get("pnl") or 0) + (r["mark"] - r["entry"])
                             * r["qty"], 2)
    pend = [row(t) for t in trades if t["status"] == "PENDING"]
    closed = [row(t) for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    payload = {
        "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
        "rule": "HM Swing (weekly): BUY RSI9>50 red<RSI green>red · SELL RSI9<50 red>RSI green<red · next open · 3w extreme SL · 2R · 15 sessions · max 5"
                "PF 1.39 · ShootingStar+VOL SELL PF 1.15 · Engulf-at-low BUY "
                "PF 1.13 | entry next open, SL=pattern extreme, target 2R, "
                "10-session stop | max 5 concurrent",
        "note": "Candlestick CS-MARU/PINS/ENGF retired 15 Oct 2026. Same HM lines as HM Positional."
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
