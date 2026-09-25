#!/usr/bin/env python3
"""QM — Quantity Model desk (4th paper-trading setup).

NK's "Trading as a Business" system (video uPrYdYzIMh4 →
concepts/trading-as-business.html) — the money management is the strategy;
signals come from "your own system". Three signal families feed the desk:

  QM-HM   Hilega-Milega buy (RSI9>55, red WMA21(RSI9) < RSI9, V-shape,
          close > rising SMA20, green candle) — NIFTY itself must be in
          HM buy state (the alignment that backtested PF 1.35)
  QM-52W  fresh 52-week closing high with volume >= 1.5x avg20
  QM-BBd/w/m  BB Blast: BB-width in the bottom 20% of the trailing 100
          bars + close > SMA50 (rising) + green candle + vol >= 1.5x prev
          day (prev day falling) + close > 10-bar max close — computed on
          DAILY, WEEKLY (complete weeks) and MONTHLY (complete months)
          bars. 52W + BB families need NIFTY > SMA20 (regime gate).
          Weekly/monthly rank over available history (5y download).

Money management (the class, verbatim):
  ₹10L paper capital · ₹30k (3%) lots · max 2 NEW entries/day
  +12% -> book half, trail rest at cost · RSI9>=86 -> exit all at close
  never book losses: -50% -> average ONE more lot
  circuit breaker: >50% of capital stuck in losers -> no new entries
Also writes data/qm_picks.json for the Alerts page 🧮 QM tab (all today's
candidates, not just the ones taken).
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
PICKS = DATA / "qm_picks.json"
IST = ZoneInfo("Asia/Kolkata")

CAPITAL = 1_000_000.0
LOT = 30_000.0
MAX_NEW_PER_DAY = 2
MAX_OPEN = 16
BOOK_AT = 0.12
RSI_EXIT = 86
AVG_AT = 0.50
COST = 0.003
MIN_TURNOVER = 5e7
FAMILY_PRIO = {"QM-HM": 0, "QM-BBw": 1, "QM-BBm": 2, "QM-BBd": 3, "QM-52W": 4}


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


def fetch_5y(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, period="5y", interval="1d",
                         progress=False, threads=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna(how="all")
    except Exception:
        return None


def _turnover_ok(df: pd.DataFrame) -> bool:
    vol_avg = df["Volume"].rolling(20).mean().iloc[-1]
    return float(df["Close"].iloc[-1]) * float(vol_avg) >= MIN_TURNOVER


def sig_hm(df: pd.DataFrame) -> dict | None:
    """HM buy on the last daily bar (needs NIFTY in HM state — checked by
    the caller for this family)."""
    if len(df) < 60:
        return None
    c = df["Close"]
    r = rsi9(c)
    red = wma(r, 21)
    if any(pd.isna(v) for v in (r.iloc[-1], red.iloc[-1])):
        return None
    r0, red0 = float(r.iloc[-1]), float(red.iloc[-1])
    if not (red0 < r0 and r0 > 55):
        return None
    if not (float(r.iloc[-2]) <= float(r.iloc[-6:-1].min())
            and r0 > float(r.iloc[-2])):
        return None
    s20 = c.rolling(20).mean()
    if not (float(c.iloc[-1]) > float(s20.iloc[-1]) > float(s20.iloc[-2])):
        return None
    if not float(df["Close"].iloc[-1]) > float(df["Open"].iloc[-1]):
        return None
    if not _turnover_ok(df):
        return None
    return {"setup": "QM-HM", "rsi": round(r0, 1),
            "note": f"RSI9 {r0:.0f} V-turn above rising SMA20"}


def sig_52w(df: pd.DataFrame) -> dict | None:
    c = df["Close"]
    if len(df) < 260:
        return None
    prior_max = float(c.iloc[-253:-1].max())
    close = float(c.iloc[-1])
    if not (close > prior_max and float(df["Open"].iloc[-1]) < close):
        return None
    vr = float(df["Volume"].iloc[-1]) / max(float(df["Volume"].rolling(20)
                                                 .mean().iloc[-1]), 1)
    if vr < 1.5 or not _turnover_ok(df):
        return None
    return {"setup": "QM-52W", "rsi": round(float(rsi9(c).iloc[-1]), 1),
            "note": f"new 52w closing high {close:.1f}, vol {vr:.1f}x"}


def _bb_blast(bars: pd.DataFrame, rank_window: int, label: str) -> dict | None:
    """Squeeze blast on resampled bars: BB width in the bottom 20% of the
    trailing window + trend + trigger candle + volume expansion."""
    if len(bars) < min(rank_window, 40) + 5:
        return None
    c, v = bars["Close"], bars["Volume"]
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    width = (4 * sd) / mid
    # the blast bar's own band is already expanded — rank the PRIOR bar's
    # width (the squeeze state) against the window before it
    if len(bars) < min(rank_window, 40) + 6:
        return None
    prior = width.iloc[-2]
    win = width.iloc[-rank_window - 2:-2] if len(bars) > rank_window + 2 \
        else width.iloc[:-2]
    if pd.isna(prior) or len(win) < 20:
        return None
    rank = float((win < prior).sum()) / len(win)
    if rank > 0.20:
        return None
    s50 = c.rolling(50).mean()
    if any(pd.isna(x) for x in (mid.iloc[-1], s50.iloc[-1], s50.iloc[-51]
                                if len(bars) > 51 else s50.iloc[0])):
        return None
    if not (float(c.iloc[-1]) > float(s50.iloc[-1])
            and float(s50.iloc[-1]) > float(s50.iloc[-2])):
        return None
    if not (float(c.iloc[-1]) > float(bars["Open"].iloc[-1])
            and float(c.iloc[-1]) > float(c.iloc[-11:-1].max())):
        return None
    v1, v2, v3 = float(v.iloc[-1]), float(v.iloc[-2]), float(v.iloc[-3])
    if not (v1 >= 1.5 * v2 and v2 < v3):
        return None
    return {"setup": f"QM-BB{label}", "rsi": round(float(rsi9(c).iloc[-1]), 1),
            "note": f"BB squeeze rank {rank*100:.0f}% blast on {label} bars"}


def resample(df: pd.DataFrame, rule: str, complete_only: bool) -> pd.DataFrame:
    out = df.resample(rule).agg({"Open": "first", "High": "max",
                                 "Low": "min", "Close": "last",
                                 "Volume": "sum"}).dropna()
    if complete_only and len(out) and len(df):
        # drop the forming period: weekly bar incomplete unless Friday,
        # monthly bar incomplete unless the month has rolled over
        if rule == "W-FRI" and df.index[-1].weekday() < 4:
            out = out.iloc[:-1]
        elif rule == "ME":
            last_daily = df.index[-1]
            if last_daily.month == out.index[-1].month:
                out = out.iloc[:-1]
    return out


def scan_signals(df: pd.DataFrame, nifty_ok_hm: bool,
                 nifty_ok_sma: bool) -> list[dict]:
    sigs = []
    if nifty_ok_hm:
        s = sig_hm(df)
        if s:
            sigs.append(s)
    if nifty_ok_sma:
        s = sig_52w(df)
        if s:
            sigs.append(s)
        for rule, label, complete in (("W-FRI", "w", True), ("ME", "m", True),
                                      (None, "d", False)):
            bars = df if rule is None else resample(df, rule, complete)
            s = _bb_blast(bars, 100, label)
            if s:
                sigs.append(s)
    for s in sigs:
        s["close"] = round(float(df["Close"].iloc[-1]), 2)
        s["date"] = str(df.index[-1].date())
    return sigs


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
        df = fetch_5y(f"{t['symbol']}.NS")
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
    blocked = 0.0
    for t in [x for x in trades if x["status"] == "OPEN"]:
        df = fetch_5y(f"{t['symbol']}.NS")
        if df is None:
            continue
        c = float(df["Close"].iloc[-1])
        t["mark"] = c
        t["sessions"] = (t.get("sessions") or 0) + 1
        r0 = float(rsi9(df["Close"]).iloc[-1])
        entry = t["entry"]
        if r0 >= RSI_EXIT:
            _close(t, c, "RSI86")
            continue
        if not t.get("trail") and c >= entry * (1 + BOOK_AT):
            half = t["qty"] / 2
            t["booked"] = round(t["booked"] + half * (c - entry)
                                - half * entry * COST, 2)
            t["qty"] = t["qty"] - half
            t["trail"] = True
            t["note"] = f"half booked @ {c:.1f}"
        elif t.get("trail") and c <= entry:
            _close(t, c, "TRAIL-COST")
            continue
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

    # ---- 3) scan NIFTY-500 for signals ----
    nifty = fetch_5y("^NSEI")
    nifty_hm = sig_hm(nifty) is not None if nifty is not None else False
    nifty_sma = False
    if nifty is not None:
        c = nifty["Close"]
        s20 = c.rolling(20).mean()
        nifty_sma = float(c.iloc[-1]) > float(s20.iloc[-1])

    open_n = sum(1 for t in trades if t["status"] == "OPEN")
    cb = blocked > CAPITAL * 0.5
    if not dry:
        state["circuit_breaker"] = bool(cb)
    candidates: list[dict] = []
    csv = (DATA / "nifty500_symbols.csv").read_text().splitlines()[1:]
    syms = [ln.split(",")[2].strip() for ln in csv
            if len(ln.split(",")) >= 4 and ln.split(",")[3].strip() == "EQ"]
    held = {t["symbol"] for t in trades if t["status"] in ("OPEN", "PENDING")}
    for k, s in enumerate(syms):
        if s not in held:
            df = fetch_5y(f"{s}.NS")
            if df is not None:
                candidates.extend(dict(sym=s, **sig)
                                  for sig in scan_signals(df, nifty_hm, nifty_sma))
        if (k + 1) % 50 == 0:
            time.sleep(0.8)
    candidates.sort(key=lambda c: (FAMILY_PRIO.get(c["setup"], 9), -c["close"]))
    # write the Alerts-tab pick list (every candidate found today)
    if not dry:
        PICKS.write_text(json.dumps({
            "date": today,
            "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
            "nifty_gate": {"hm_buy_state": nifty_hm, "above_sma20": nifty_sma},
            "rule": "QM entry families: HM-buy (RSI9>55 V-turn, NIFTY in HM "
                    "buy state) · 52-week closing high (vol ≥1.5x) · BB Blast "
                    "squeeze on daily/weekly/monthly (NIFTY > SMA20) · "
                    "NIFTY-500 · ₹5cr turnover · entries capped 2/day by the "
                    "desk",
            "signals": candidates}, indent=1, ensure_ascii=False) + "\n")

    entered = 0
    if not cb and open_n + filled < MAX_OPEN:
        for cand in candidates:
            if entered >= MAX_NEW_PER_DAY or open_n + filled + entered >= MAX_OPEN:
                break
            trades.append({"id": state["next_id"], "signal_date": today,
                           "symbol": cand["sym"], "setup": cand["setup"],
                           "side": "BUY", "status": "PENDING",
                           "entry_date": None, "qty": None, "entry": None,
                           "invested": None, "booked": 0.0, "avg_count": 0,
                           "trail": False, "sessions": 0, "exit": None,
                           "reason": None, "pnl": None, "mark": None,
                           "note": cand.get("note")})
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
        "rule": "QM (Quantity Model, NK's 'Trading as a Business'): signals "
                "from HM-buy / 52W-high / BB-Blast(d,w,m) on NIFTY-500 · "
                "₹30k (3% of ₹10L) lots · max 2 new/day · +12% book half & "
                "trail at cost · RSI9≥86 exit all · never book losses, "
                "average once at −50% · 50%-blocked circuit breaker",
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
