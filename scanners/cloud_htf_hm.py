#!/usr/bin/env python3
"""HM Positional desk — NK sir's Hilega-Milega state machine on HIGHER time
frames (weekly trading tier + monthly investing watchlist).

Sources (vault): NK ChartIQ PDF (HM = RSI9 OB50/OS50 + 21WMA-on-RSI red
volume line + 3WMA-on-RSI green price line), NK Jul-2026 podcast (RSI 86
universal booking; monthly = strongest), Upsurge bottoms video 30 Sep 2026
(bottom→confirm→ride sequence, "distance between the lines" momentum phase).

Backtested 30 Sep 2026 on the 5y NIFTY-500 lake (hm_htf_backtest.py):
  weekly momentum-phase entry, calm exits -> 168 capped trades,
  PF 2.19, avg +4.78%/trade, WR 36%, ~7-week holds.
  (naive first-50-cross entry REFUTED: PF ~1.0; red-trail exit REFUTED:
   cuts winners at ~3w. Monthly tier PF 4.22/43t — watchlist only for now.)

Rules:
  Entry (weekly): HM momentum phase newly true on a COMPLETED week —
      RSI9(w) > 50  AND  red21(RSI) < RSI9  AND  green3(RSI) > red
      (three lines separated, aligned upward). Enter at the day's close.
  Exits (weekly close): RSI9 < 50 (TREND-END) | RSI9 >= 86 (RSI86-BOOK).
  Book: Rs 2,00,000 paper capital, Rs 25k lots, max 5 open,
      max 2 new per week, no averaging, 0.3% round-trip costs.
  Monthly watchlist: RSI9(m) > 50 with red inside — informational
      (NK: "monthly = investing, years horizon").

Run: python3 cloud_htf_hm.py [--output data/scanners/htf-hm-latest.json]
Called once per weekday after 15:35 IST by the day relay (post-close; a
490 x 5y yf scan must not block market-hours ticks).
"""
from __future__ import annotations  # 3.9-safe type hints (local Mac python)

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

BASE = Path(__file__).parent
ROOT = BASE.parent
DATA = ROOT / "data"
TRADES_FILE = BASE / "papertrades" / "htf_hm_trades.csv"
OUT_DEFAULT = DATA / "scanners" / "htf-hm-latest.json"

CAPITAL, LOT = 100_000.0, 20_000.0   # October mandate: Rs1L per desk (1 Oct 2026)
MAX_OPEN, MAX_NEW_PER_WEEK = 5, 2
COST = 0.003
RSI_BOOK = 86
MIN_TURNOVER = 5e7
IST = timezone(timedelta(hours=5, minutes=30))


def now() -> datetime:
    return datetime.now(IST)


def rsi9(c: pd.Series) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 9, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 9, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: float((x * w).sum() / w.sum()), raw=True)


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


def to_tf(df: pd.DataFrame, rule: str, today=None) -> pd.DataFrame:
    """Resample daily -> weekly/monthly, keeping only COMPLETED bars
    (a W-FRI bar is complete once its Friday label day has arrived; a
    month bar once its month-end label day has arrived)."""
    today = today or now().date()
    g = df.resample(rule)
    out = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(),
                        "Low": g["Low"].min(), "Close": g["Close"].last(),
                        "Volume": g["Volume"].sum()}).dropna()
    return out[[d.date() <= today for d in out.index]]


def hm_bars(df: pd.DataFrame, rule: str):
    t = to_tf(df, rule)
    if len(t) < 35:
        return None
    r = rsi9(t["Close"])
    t = t.assign(rsi=r, red=wma(r, 21), green=wma(r, 3)).dropna(
        subset=["rsi", "red", "green"])
    return t if len(t) else None


def momentum_new(w: pd.DataFrame) -> bool:
    """HM momentum phase newly true on the last completed weekly bar."""
    r, red, g = w["rsi"], w["red"], w["green"]
    if len(w) < 3:
        return False
    ok = (r > 50) & (red < r) & (g > red)
    ok1 = (r.shift(1) > 50) & (red.shift(1) < r.shift(1)) & (g.shift(1) > red.shift(1))
    return bool(ok.iloc[-1]) and not bool(ok1.fillna(False).iloc[-1])


def hm_state(w: pd.DataFrame) -> str:
    r, red, g = (float(w[k].iloc[-1]) for k in ("rsi", "red", "green"))
    if r > 50 and red < r and g > red:
        return "BUY-MOMENTUM"
    if r > 50 and red < r:
        return "BUY"
    if r > 50:
        return "TOP-FORMING"
    if red > r:
        return "SELL"
    return "NO-TRADE"


def load_trades() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    with TRADES_FILE.open() as f:
        rows = list(csv.DictReader(f))
    for t in rows:
        for k in ("entry", "exit", "qty", "invested", "pnl", "rsi_entry"):
            t[k] = float(t.get(k) or 0)
        t["weeks_held"] = int(float(t.get("weeks_held") or 0))
    return rows


FIELDS = ["symbol", "signal_date", "entry_date", "entry", "qty", "invested",
          "rsi_entry", "status", "exit_date", "exit", "reason", "pnl",
          "weeks_held", "notes"]


def save_trades(trades: list[dict]) -> None:
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TRADES_FILE.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)


def run(output: Path) -> dict:
    today = now().date()
    csv_rows = (DATA / "nifty500_symbols.csv").read_text().splitlines()[1:]
    syms = [ln.split(",")[2].strip() for ln in csv_rows
            if len(ln.split(",")) >= 4 and ln.split(",")[3].strip() == "EQ"]
    trades = load_trades()
    open_syms = {t["symbol"] for t in trades if t["status"] == "OPEN"}

    weekly, monthly = {}, {}
    failed: list[str] = []

    def _scan(sym: str):
        try:
            df = fetch_5y(f"{sym}.NS")
            if df is None:
                return
            w, m = hm_bars(df, "W-FRI"), hm_bars(df, "M")
            if w is not None:
                weekly[sym] = w
            if m is not None:
                monthly[sym] = m
        except Exception:   # yf + threads is flaky locally — retried in-line
            failed.append(sym)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_scan, syms))
    for sym in failed:      # sequential retry: same call, no concurrency
        try:
            df = fetch_5y(f"{sym}.NS")
            if df is None:
                continue
            w, m = hm_bars(df, "W-FRI"), hm_bars(df, "M")
            if w is not None:
                weekly[sym] = w
            if m is not None:
                monthly[sym] = m
        except Exception as e:
            print(f"  skip {sym}: {type(e).__name__}: {str(e)[:80]}", flush=True)
    if failed:
        print(f"  threaded sweep: {len(failed)} retried sequentially", flush=True)

    px_today = {s: float(w["Close"].iloc[-1]) for s, w in weekly.items()
                if w.index[-1] >= pd.Timestamp(today) - timedelta(days=7)}

    # ---- exits first (weekly close basis; price at today's close) ----
    for t in trades:
        if t["status"] != "OPEN":
            continue
        w = weekly.get(t["symbol"])
        if w is None or len(w) < 2:
            continue
        r = float(w["rsi"].iloc[-1])
        if r < 50:
            t["reason"] = "TREND-END"
        elif r >= RSI_BOOK:
            t["reason"] = "RSI86-BOOK"
        else:
            continue
        px = px_today.get(t["symbol"]) or float(w["Close"].iloc[-1])
        t.update(status="CLOSED", exit_date=today.isoformat(), exit=round(px, 2),
                 pnl=round(t["qty"] * (px - t["entry"])
                           - t["qty"] * t["entry"] * COST, 2),
                 weeks_held=t["weeks_held"] + 1)
        print(f"  EXIT {t['symbol']} @{px:.1f} ({t['reason']}, "
              f"rsi {r:.0f}) pnl {t['pnl']:+,.0f}")

    # ---- entries: fresh weekly momentum signals ----
    signals = []
    for sym, w in weekly.items():
        vol20 = float(w["Volume"].rolling(20, min_periods=1).mean().iloc[-1])
        if float(w["Close"].iloc[-1]) * vol20 * 5 < MIN_TURNOVER:
            continue                      # ~weekly turnover proxy for 5 days
        if momentum_new(w):
            signals.append(dict(symbol=sym,
                                rsi=round(float(w["rsi"].iloc[-1]), 1),
                                red=round(float(w["red"].iloc[-1]), 1),
                                green=round(float(w["green"].iloc[-1]), 1),
                                state=hm_state(w),
                                signal_date=w.index[-1].date().isoformat(),
                                close=round(float(w["Close"].iloc[-1]), 2)))
    signals.sort(key=lambda s: -s["rsi"])

    open_now = sum(1 for t in trades if t["status"] == "OPEN")
    week_iso = today.strftime("%G-W%V")
    taken_this_week = sum(1 for t in trades
                          if _week_of(t.get("entry_date") or "") == week_iso)
    entered = 0
    for s in signals:
        if open_now + entered >= MAX_OPEN or entered + taken_this_week >= MAX_NEW_PER_WEEK:
            break
        if s["symbol"] in open_syms:
            continue
        if any(t["symbol"] == s["symbol"] and t["signal_date"] == s["signal_date"]
               for t in trades):
            continue                      # this signal already taken
        px = px_today.get(s["symbol"]) or s["close"]
        qty = int(LOT / px)
        if qty < 1:
            continue
        trades.append(dict(symbol=s["symbol"], signal_date=s["signal_date"],
                           entry_date=today.isoformat(), entry=round(px, 2),
                           qty=qty, invested=round(qty * px, 2),
                           rsi_entry=s["rsi"], status="OPEN", exit_date="",
                           exit=0.0, reason="", pnl=0.0, weeks_held=0,
                           notes=f"HM momentum {s['state']}"))
        open_syms.add(s["symbol"])
        entered += 1
        print(f"  ENTER {s['symbol']} @{px:.1f} (rsi {s['rsi']}, "
              f"red {s['red']}, green {s['green']})")

    save_trades(trades)

    # ---- monthly investing watchlist (informational) ----
    watch = []
    for sym, m in monthly.items():
        r, red, g = (float(m[k].iloc[-1]) for k in ("rsi", "red", "green"))
        prev_ok = False
        if len(m) > 1:
            r1, red1 = float(m["rsi"].iloc[-2]), float(m["red"].iloc[-2])
            prev_ok = r1 > 50 and red1 < r1
        if r > 50 and red < r:
            watch.append(dict(symbol=sym, rsi=round(r, 1), red=round(red, 1),
                              state="BUY-MOMENTUM" if g > red else "BUY",
                              fresh=not prev_ok))
    watch.sort(key=lambda s: (-s["fresh"], -s["rsi"]))

    closed = [t for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if t["pnl"] > 0]
    invested = sum(t["invested"] for t in trades if t["status"] == "OPEN")
    # live columns for the paper page: today's close per open position
    # (market-hours ticks in cloud_papertrade.refresh_hm_quotes keep them
    # moving intraday; display-only fields, never written to the CSV)
    for t in trades:
        if t["status"] != "OPEN":
            continue
        px = px_today.get(t["symbol"])
        if px and px > 0:
            t["ltp"] = round(px, 2)
            t["live_pnl"] = round(t["qty"] * (px - t["entry"]), 2)
            t["live_pct"] = round((px / t["entry"] - 1) * 100, 2)
    out = {
        "date": today.isoformat(),
        "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
        "rule": ("HM Positional (NK Hilega-Milega on weekly bars): enter when "
                 "RSI9>50 & red WMA21(RSI)<RSI9 & green WMA3(RSI)>red newly "
                 "true on a completed week; exit weekly RSI<50 (TREND-END) or "
                 ">=86 (RSI86-BOOK). 5y backtest PF 2.19, avg +4.8%/trade. "
                 "Book: Rs1L (Oct mandate), Rs20k lots, max 5 open, max 2 "
                 "new/week. Monthly tab = investing watchlist (RSI9>50, red "
                 "inside)."),
        "book": {"capital": CAPITAL, "open": len(open_syms), "invested": invested,
                 "realized": round(sum(t["pnl"] for t in closed), 2),
                 "wins": len(wins), "losses": len(closed) - len(wins),
                 "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0},
        "signals": signals[:25],
        "monthly_watchlist": watch[:40],
        "open": [t for t in trades if t["status"] == "OPEN"],
        "closed": closed[-60:],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return out


def _week_of(iso_date: str) -> str:
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d").date()
        return d.strftime("%G-W%V")
    except Exception:
        return ""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT_DEFAULT))
    a = ap.parse_args()
    res = run(Path(a.output))
    b = res["book"]
    print(f"HM-HTF: open {b['open']}/{MAX_OPEN} invested Rs{b['invested']:,.0f} | "
          f"realized Rs{b['realized']:+,.0f} ({b['wins']}W/{b['losses']}L) | "
          f"{len(res['signals'])} weekly signals, "
          f"{len(res['monthly_watchlist'])} monthly watch")
