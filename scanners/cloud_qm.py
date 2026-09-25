#!/usr/bin/env python3
"""QM — Quantity Model desk (4th paper-trading setup).

NK's "Trading as a Business" system (video uPrYdYzIMh4, ingested
2026-09-25 → concepts/trading-as-business.html) implemented as a paper book:

  Capital model   : ₹10,00,000 paper capital · ₹30,000 (3%) per entry lot
  Entry signal    : his rule "use your own system" — we use the HM entry
                    (WMA21(RSI9) < RSI9 buy state, V-shape, RSI9 > 55,
                    close > rising SMA20, green candle) with Nifty itself in
                    HM buy state (the variant that backtested PF 1.35 on
                    NIFTY-100). Fills at next open, like the swing desk.
  Max 2 new entries/day (hard rule from the class)
  Exits           : +12% -> book HALF, trail the rest at cost (exit at entry)
                    RSI9 >= 86 -> exit everything at close (his universal
                    booking level)
                    NEVER book losses: no stop-loss. -50% from entry ->
                    average ONE more ₹30k lot at close ("mutual funds average")
  Circuit breaker : if losing positions' invested capital > 50% of the book
                    (₹5L) -> no new entries; review (his "stop and review")
  Costs           : 0.30% round trip on realized legs.
Runs daily in the evening relay job on completed daily bars (NIFTY-500).
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
STATE_FILE = DATA / "cloud_qm_state.json"
OUT = DATA / "paper_qm.json"
IST = ZoneInfo("Asia/Kolkata")

CAPITAL = 1_000_000.0
LOT = 30_000.0            # 3% position sizing
MAX_NEW_PER_DAY = 2
MAX_OPEN = 16             # hard sanity cap (~48% of capital at 1 lot each)
BOOK_AT = 0.12            # +12% -> book half
RSI_EXIT = 86
AVG_AT = 0.50             # -50% -> average once
COST = 0.003
MIN_TURNOVER = 5e7        # ₹5cr avg daily turnover (quality/liquidity proxy)


def now() -> datetime:
    return datetime.now(IST)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "next_id": 1, "trades": [], "done_date": ""}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")


def rsi9(c: pd.Series) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).rolling(9).mean()
    dn = (-d.clip(upper=0)).rolling(9).mean()
    out = 100 - 100 / (1 + up / dn.replace(0, pd.NA))
    return out.fillna(100.0)


def wma(s: pd.Series, n: int) -> pd.Series:
    w = pd.Series(range(1, n + 1))
    return s.rolling(n).apply(lambda x: float((x * w[::-1]).sum() / w.sum()),
                              raw=False)


def _daily(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, period="1y", interval="1d",
                         progress=False, threads=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna(how="all")
    except Exception:
        return None


def _hm_buy(df: pd.DataFrame) -> bool:
    """HM buy state + structure on the LAST completed bar."""
    if len(df) < 60:
        return False
    c = df["Close"]
    r = rsi9(c)
    red = wma(r, 21)
    if any(v is None or pd.isna(v) for v in
           (r.iloc[-1], red.iloc[-1], r.iloc[-2], red.iloc[-2])):
        return False
    r0, red0 = float(r.iloc[-1]), float(red.iloc[-1])
    if not (red0 < r0 and r0 > 55):                    # HM buy state + filter
        return False
    if not (float(r.iloc[-2]) <= float(r.iloc[-6:-1].min())
            and r0 > float(r.iloc[-2])):               # V-shape bottoming
        return False
    s20 = c.rolling(20).mean()
    if not (float(c.iloc[-1]) > float(s20.iloc[-1]) > float(s20.iloc[-2])):
        return False                                   # structure
    if not float(df["Close"].iloc[-1]) > float(df["Open"].iloc[-1]):
        return False                                   # green candle
    vol_avg = df["Volume"].rolling(20).mean()
    if float(df["Close"].iloc[-1]) * float(vol_avg.iloc[-1]) < MIN_TURNOVER:
        return False
    return True


def run_day(dry: bool = False) -> dict:
    state = read_state()
    today = now().date().isoformat()
    if today > "2027-12-31" or now().weekday() >= 5:
        return export(state)
    if state.get("done_date") == today and not dry:
        return export(state)
    trades = state["trades"]

    # ---- 1) fill yesterday's pending at today's open ----
    filled = 0
    for t in [x for x in trades if x["status"] == "PENDING"]:
        df = _daily(f"{t['symbol']}.NS")
        if df is None or str(df.index[-1].date()) != today:
            continue
        o = float(df["Open"].iloc[-1])
        qty = round(LOT / o, 0)
        if qty < 1:
            t["status"] = "SKIP"
            continue
        t.update(status="OPEN", entry_date=today, qty=qty, entry=o,
                 invested=round(qty * o, 2), booked=0.0, avg_count=0,
                 trail=False, sessions=0, exit=None, reason=None, pnl=None,
                 mark=o)
        filled += 1

    # ---- 2) manage opens on today's completed bar ----
    nifty = _daily("^NSEI")
    nifty_hm = _hm_buy(nifty) if nifty is not None else False
    blocked = 0.0
    for t in [x for x in trades if x["status"] == "OPEN"]:
        df = _daily(f"{t['symbol']}.NS")
        if df is None:
            continue
        c = float(df["Close"].iloc[-1])
        t["mark"] = c
        t["sessions"] = (t.get("sessions") or 0) + 1
        r = rsi9(df["Close"])
        r0 = float(r.iloc[-1]) if not pd.isna(r.iloc[-1]) else 0.0
        entry = t["entry"]
        # RSI 86 -> exit everything at close
        if r0 >= RSI_EXIT:
            _close(t, c, "RSI86")
            continue
        # +12% -> book half, arm the cost-trail
        if not t.get("trail") and c >= entry * (1 + BOOK_AT):
            half = t["qty"] / 2
            t["booked"] = round(t["booked"] + half * (c - entry)
                                - half * entry * COST, 2)
            t["qty"] = t["qty"] - half
            t["trail"] = True
            t["note"] = f"half booked @ {c:.1f}"
        # trail armed and round-tripped to cost -> out
        elif t.get("trail") and c <= entry:
            _close(t, c, "TRAIL-COST")
            continue
        # never book losses: -50% -> average one more lot
        elif c <= entry * AVG_AT and t.get("avg_count", 0) < 1:
            qty2 = round(LOT / c, 0)
            t["entry"] = round((t["entry"] * t["qty"] + c * qty2)
                               / (t["qty"] + qty2), 2)
            t["qty"] += qty2
            t["invested"] = round(t["invested"] + qty2 * c, 2)
            t["avg_count"] = 1
            t["note"] = f"averaged @ {c:.1f}"
        if t["status"] == "OPEN" and c < entry:
            blocked += t["invested"]

    # ---- 3) new signals (max 2, circuit breaker) ----
    open_n = sum(1 for t in trades if t["status"] == "OPEN")
    cb = blocked > CAPITAL * 0.5
    if not dry:
        state["circuit_breaker"] = bool(cb)
    entered = 0
    if nifty_hm and not cb and open_n + filled < MAX_OPEN:
        csv = (DATA / "nifty500_symbols.csv").read_text().splitlines()[1:]
        syms = [ln.split(",")[2].strip() for ln in csv
                if len(ln.split(",")) >= 4 and ln.split(",")[3].strip() == "EQ"]
        held = {t["symbol"] for t in trades
                if t["status"] in ("OPEN", "PENDING")}
        cands = []
        for k, s in enumerate(syms):
            if s in held:
                continue
            df = _daily(f"{s}.NS")
            if df is not None and _hm_buy(df):
                cands.append(s)
            if (k + 1) % 50 == 0:
                time.sleep(0.8)
        for s in cands:
            if entered >= MAX_NEW_PER_DAY or open_n + filled + entered >= MAX_OPEN:
                break
            trades.append({"id": state["next_id"], "signal_date": today,
                           "symbol": s, "setup": "QM", "side": "BUY",
                           "status": "PENDING", "entry_date": None,
                           "qty": None, "entry": None, "invested": None,
                           "booked": 0.0, "avg_count": 0, "trail": False,
                           "sessions": 0, "exit": None, "reason": None,
                           "pnl": None, "mark": None})
            state["next_id"] += 1
            entered += 1

    if not dry:
        state["done_date"] = today
        write_state(state)
    return export(state)


def _close(t: dict, px: float, reason: str) -> None:
    pnl = t.get("booked", 0.0) + t["qty"] * (px - t["entry"]) \
        - t["qty"] * t["entry"] * COST
    t.update(status="CLOSED", exit=round(px, 2), reason=reason,
             pnl=round(pnl, 2), exit_date=now().date().isoformat())


def export(state: dict) -> dict:
    trades = state["trades"]
    fields = ("id", "signal_date", "symbol", "setup", "side", "status",
              "entry_date", "qty", "entry", "invested", "booked",
              "avg_count", "trail", "sessions", "mark", "exit", "reason",
              "pnl", "note")

    def row(t): return {k: t.get(k) for k in fields}

    opens = [row(t) for t in trades if t["status"] == "OPEN"]
    pend = [row(t) for t in trades if t["status"] == "PENDING"]
    closed = [row(t) for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    realized = round(sum((t.get("pnl") or 0) for t in closed), 2)
    unreal = round(sum((t["mark"] - t["entry"]) * t["qty"]
                       for t in opens if t.get("mark")), 2)
    payload = {
        "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
        "rule": "QM (Quantity Model, NK's 'Trading as a Business'): HM-buy "
                "signal on NIFTY-500 · ₹30k (3% of ₹10L) per lot · max 2 new/"
                "day · +12% book half & trail at cost · RSI9≥86 exit all · "
                "never book losses, average once at −50% · 50%-blocked "
                "circuit breaker",
        "note": "Daily-bar desk, scans after 17:30 IST. Position sizing is "
                "the edge — fills at next open.",
        "summary": {
            "open": len(opens), "pending": len(pend), "closed": len(closed),
            "wins": len(wins), "losses": len(closed) - len(wins),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "realized": realized, "unrealized": unreal,
            "invested": round(sum(t.get("invested") or 0 for t in opens), 0),
            "circuit_breaker": bool(state.get("circuit_breaker")),
        },
        "open": opens, "pending": pend, "closed": closed,
    }
    if not dry_global[0]:
        OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    return payload


dry_global = [False]


if __name__ == "__main__":
    import sys
    dry_global[0] = "--dry" in sys.argv
    print(json.dumps(run_day(dry=dry_global[0])["summary"]))
