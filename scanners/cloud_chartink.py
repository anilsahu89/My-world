#!/usr/bin/env python3
"""ChartLink desk — live paper trading of the top-5 Chartink setups.

Source: 3-5y backtest of the MAHI dashboard scans (chartink_rules/REPORT.md,
124,578 trades, Nov 2026). Rules ported 1:1 from the backtest engine
(ZCodeProject/chartink_rules/backtest_scans.py) so live behaviour matches
the tested edge.

  Rank  Setup        Label                  20d win%  avg/trade
  1     BB-STACK     BB Stack (M+W+D)         59.6%   +3.6%
  2     ABV-MBB      Above Monthly BB         57.9%   +3.0%
  3     SWING-SH     Swing Shashi             57.8%   +3.1%
  4     GAP-UP       Gap-up > Prev-High       57.6%   +3.0%
  5     HM-POS       HM Positional            56.5%   +2.6%

Signal on day D completed bar -> entry D+1 open, 10,000 INR/trade.
Exits (backtest "TS" business exit): +4% target / -2% stop (stop first,
conservative), else time exit at the 20th session close.
Universe: NSE-500 (EQ series) via yfinance daily bars.
"""
from __future__ import annotations

import json
import time
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATE_FILE = DATA / "cloud_chartlink_state.json"
OUT = DATA / "chartink_picks.json"
IST = ZoneInfo("Asia/Kolkata")

CFG = {"end_date": "2027-03-31", "capital_inr": 10_000.0,
       "max_open": 10, "max_new_per_day": 5, "tgt_pct": 0.04,
       "sl_pct": 0.02, "max_sessions": 20, "fetch_period": "5y"}

# priority = backtest rank (lower scans first when slots are scarce)
SETUPS = {
    "BB-STACK": {"prio": 0, "label": "BB Stack (M+W+D)",
                 "rule": "Monthly+Weekly+Daily close all above their upper "
                         "Bollinger band(20,2) and above the prior bar, "
                         "close <= open*1.16, prior turnover >= 40L"},
    "ABV-MBB": {"prio": 1, "label": "Above Monthly BB",
                "rule": "Daily close >= monthly upper BB(20,2) while the "
                        "previous 2 monthly closes were below their BBs"},
    "SWING-SH": {"prio": 2, "label": "Swing Shashi",
                 "rule": "Weekly WMA(RSI9,21) < weekly EMA(RSI9,3) and < RSI9, "
                         "close >= SMA20 for 6 days, 1-month-ago close above "
                         "1-month-ago upper BB, RSI14 >= 60"},
    "GAP-UP": {"prio": 3, "label": "Gap-up > Prev-High",
               "rule": "Open >= previous day high, RSI14 >= RSI50, volume >= "
                       "10L, day change > 3%"},
    "HM-POS": {"prio": 4, "label": "HM Positional",
               "rule": "Monthly+Weekly+Daily WMA(RSI9,21) < RSI9, all three "
                       "RSI9 > 50 and rising vs prior bar"},
}


def now() -> datetime:
    return datetime.now(IST)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "next_id": 1, "trades": [], "done_date": ""}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- indicators
def rsi_wilder(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = -d.clip(upper=0.0)
    ru = up.ewm(alpha=1.0 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = ru / rd.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def wma(s: pd.Series, n: int) -> pd.Series:
    v = s.values.astype(float)
    out = np.full(len(v), np.nan)
    if len(v) >= n:
        norm = n * (n + 1) / 2.0
        k = np.arange(n, 0, -1, dtype=float) / norm
        out[n - 1:] = np.convolve(v, k, mode="valid")
    return pd.Series(out, index=s.index)


def bband_up(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    return close.rolling(n).mean() + k * close.rolling(n).std(ddof=0)


def _completed_rows(w: pd.DataFrame, last_daily) -> pd.DataFrame:
    """Drop the in-progress resample row (label beyond the last daily bar) so
    weekly/monthly conditions read the last COMPLETED candle — the exact
    semantics the 5y backtest validated (no intramonth repaint)."""
    if len(w) and w.index[-1] > last_daily:
        return w.iloc[:-1]
    return w


def weekly_frame(df: pd.DataFrame) -> pd.DataFrame:
    w = df.resample("W-FRI").agg({"Open": "first", "High": "max",
                                  "Low": "min", "Close": "last",
                                  "Volume": "sum"}).dropna(subset=["Close"])
    return _completed_rows(w, df.index[-1])


def monthly_frame(df: pd.DataFrame) -> pd.DataFrame:
    m = df.resample("ME").agg({"Open": "first", "High": "max",
                               "Low": "min", "Close": "last",
                               "Volume": "sum"}).dropna(subset=["Close"])
    return _completed_rows(m, df.index[-1])


def detect(df: pd.DataFrame) -> list[str]:
    """Which of the 5 setups are newly true on the LAST completed daily bar.

    Weekly/monthly values mirror the backtest exactly: resample includes the
    in-progress week/month as its final row (that is how a live Chartink
    monthly candle reads intraday too), shift(1) is the prior completed bar.
    """
    out: list[str] = []
    c = df["Close"]
    if len(df) < 260:
        return out
    last, prev = df.iloc[-1], df.iloc[-2]

    rsi14 = rsi_wilder(c, 14)
    rsi50 = rsi_wilder(c, 50)
    rsi9 = rsi_wilder(c, 9)
    sma20 = c.rolling(20).mean()
    bbu_d = bband_up(c)

    # GAP-UP — daily-only setup (no weekly/monthly history needed)
    if (last["Open"] >= prev["High"] and rsi14.iloc[-1] >= rsi50.iloc[-1]
            and last["Volume"] >= 1_000_000
            and c.iloc[-1] / c.iloc[-2] - 1 > 0.03):
        out.append("GAP-UP")

    w = weekly_frame(df)
    m = monthly_frame(df)
    if len(w) < 21 or len(m) < 21:      # WMA(RSI,21) needs 21 completed rows
        return out
    wr = rsi_wilder(w["Close"], 9)
    mr = rsi_wilder(m["Close"], 9)

    # ABV-MBB — close above current monthly UBB, prior 2 monthly closes below
    mbb = bband_up(m["Close"])
    if (c.iloc[-1] >= mbb.iloc[-1]
            and m["Close"].iloc[-2] < mbb.iloc[-2]
            and m["Close"].iloc[-3] < mbb.iloc[-3]):
        out.append("ABV-MBB")

    # BB-STACK — M+W+D all above their UBBs and prior bars, sane candle, turnover
    wbb = bband_up(w["Close"])
    if (m["Close"].iloc[-1] > mbb.iloc[-1] and m["Close"].iloc[-1] > m["Close"].iloc[-2]
            and w["Close"].iloc[-1] > wbb.iloc[-1] and w["Close"].iloc[-1] > w["Close"].iloc[-2]
            and c.iloc[-1] > bbu_d.iloc[-1] and c.iloc[-1] > c.iloc[-2]
            and c.iloc[-1] <= last["Open"] * 1.16
            and prev["Close"] * prev["Volume"] >= 4_000_000):
        out.append("BB-STACK")

    # SWING-SH — weekly RSI structure + 6-day SMA20 ride + monthly breakout + RSI14
    wr_wma = wma(wr, 21)
    wr_ema = wr.ewm(span=3, adjust=False).mean()
    if (wr_wma.iloc[-1] < wr_ema.iloc[-1] and wr_wma.iloc[-1] < wr.iloc[-1]
            and all(c.iloc[-i] >= sma20.iloc[-i] for i in range(1, 7))
            and m["Close"].iloc[-2] >= mbb.iloc[-2]
            and rsi14.iloc[-1] >= 60):
        out.append("SWING-SH")

    # HM-POS — three-timeframe RSI9 squeeze with all above 50 and rising
    r9_wma = wma(rsi9, 21)
    if (wma(mr, 21).iloc[-1] < mr.iloc[-1]
            and wr_wma.iloc[-1] < wr.iloc[-1]
            and r9_wma.iloc[-1] < rsi9.iloc[-1]
            and mr.iloc[-1] > mr.iloc[-2]
            and wr.iloc[-1] > wr.iloc[-2]
            and rsi9.iloc[-1] > rsi9.iloc[-2]
            and rsi9.iloc[-1] > 50 and wr.iloc[-1] > 50 and mr.iloc[-1] > 50):
        out.append("HM-POS")

    return out


# ---------------------------------------------------------------- data / loop
def fetch_daily(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, period=CFG["fetch_period"], interval="1d",
                         progress=False, threads=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated()]
        return df
    except Exception:
        return None


def nifty500() -> list[str]:
    syms = []
    csv = DATA / "nifty500_symbols.csv"
    for line in csv.read_text().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 4 and parts[3].strip() == "EQ":
            syms.append(parts[2].strip())
    return syms


def _notify(msg: str) -> None:
    try:
        import telegram_notify
        telegram_notify.notify(msg)
    except Exception:
        pass


def run_day(dry: bool = False) -> dict:
    state = read_state()
    today = now().date().isoformat()
    if not dry and (today > CFG["end_date"] or now().weekday() >= 5
                    or state.get("done_date") == today):
        try:
            return json.loads(OUT.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return export(state)
    trades = state["trades"]

    try:
        import close_requests
        manual_refs = set(close_requests.consume("chartlink"))
    except Exception:
        manual_refs = set()

    # 1) manage open positions on the latest completed bar (stop first)
    for t in [x for x in trades if x["status"] == "OPEN"]:
        df = fetch_daily(f"{t['symbol']}.NS")
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-1]
        px_hi, px_lo, px_cl = float(last["High"]), float(last["Low"]), float(last["Close"])
        t["sessions"] = (t.get("sessions") or 0) + 1
        exit_px = reason = None
        if str(t.get("id")) in manual_refs:
            exit_px, reason = px_cl, "MANUAL"
        elif px_lo <= t["sl"]:
            exit_px, reason = t["sl"], "SL-2%"
        elif px_hi >= t["tgt"]:
            exit_px, reason = t["tgt"], "TGT+4%"
        if exit_px is None and t["sessions"] >= CFG["max_sessions"]:
            exit_px, reason = px_cl, "TIME20"
        if exit_px is not None:
            t.update(status="CLOSED", exit=round(exit_px, 2), reason=reason,
                     exit_date=df.index[-1].strftime("%Y-%m-%d"),
                     pnl=round((exit_px - t["entry"]) * t["qty"], 2))
            _notify(("\u2705" if t["pnl"] > 0 else "\U0001F6D1")
                    + " CLOSE \u00b7 ChartLink (" + SETUPS[t["setup"]]["label"] + " / " + reason + ")\n"
                    + t["symbol"] + " BUY qty " + str(t["qty"])
                    + "\nP&L \u20b9{:,.0f}".format(t["pnl"]))

    # 2) fill yesterday's signals at TODAY's open
    for t in [x for x in trades if x["status"] == "PENDING"]:
        df = fetch_daily(f"{t['symbol']}.NS")
        if df is None or df.index[-1].strftime("%Y-%m-%d") != today:
            continue
        entry = float(df.iloc[-1]["Open"])
        qty = int(CFG["capital_inr"] / entry)
        if qty < 1:
            t["status"] = "SKIP"
            continue
        t.update(status="OPEN", entry_date=today, qty=qty, entry=round(entry, 2),
                 tgt=round(entry * (1 + CFG["tgt_pct"]), 2),
                 sl=round(entry * (1 - CFG["sl_pct"]), 2), sessions=0)
        _notify("\U0001F4C8 OPEN \u00b7 ChartLink (" + SETUPS[t["setup"]]["label"] + ")\n"
                + t["symbol"] + " BUY qty " + str(qty)
                + "\nentry \u20b9{:,.2f} \u00b7 tgt \u20b9{:,.2f} \u00b7 sl \u20b9{:,.2f}"
                .format(entry, t["tgt"], t["sl"]))

    # 3) scan the day's completed bars for NEW signals (fill tomorrow)
    open_n = sum(1 for t in trades if t["status"] == "OPEN")
    existing = {(t["symbol"], t["setup"], t["signal_date"]) for t in trades}
    cands = []
    for k, s in enumerate(nifty500()):
        try:
            df = fetch_daily(f"{s}.NS")
            if df is not None:
                d = df.index[-1].strftime("%Y-%m-%d")
                for setup in detect(df):
                    if (s, setup, d) not in existing:
                        cands.append((SETUPS[setup]["prio"], s, setup, d))
        except Exception:
            pass
        if (k + 1) % 50 == 0:
            time.sleep(0.8)
    if dry:
        return {"dry_signals": cands}

    cands.sort(key=lambda x: x[0])
    queued = 0
    per_day = 0
    for _, sym, setup, d in cands:
        if open_n + queued >= CFG["max_open"] or per_day >= CFG["max_new_per_day"]:
            break
        trades.append({"id": state["next_id"], "signal_date": d, "symbol": sym,
                       "setup": setup, "setup_name": SETUPS[setup]["label"],
                       "side": "BUY", "status": "PENDING", "entry_date": None,
                       "qty": None, "entry": None,
                       "tgt": None, "sl": None, "sessions": 0,
                       "exit": None, "exit_date": None, "reason": None,
                       "pnl": None})
        state["next_id"] += 1
        queued += 1
        per_day += 1

    state["done_date"] = today
    write_state(state)
    return export(state)


def export(state: dict) -> dict:
    trades = state["trades"]
    fields = ("id", "signal_date", "symbol", "setup", "setup_name", "side",
              "status", "entry_date", "qty", "entry", "tgt", "sl",
              "sessions", "exit", "exit_date", "reason", "pnl")

    def row(t: dict) -> dict:
        return {k: t.get(k) for k in fields}

    opens = [row(t) for t in trades if t["status"] == "OPEN"]
    pend = [row(t) for t in trades if t["status"] == "PENDING"]
    closed = [row(t) for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if (t["pnl"] or 0) > 0]

    by_setup = []
    for code, meta in SETUPS.items():
        ct = [t for t in closed if t["setup"] == code]
        w = [t for t in ct if (t["pnl"] or 0) > 0]
        by_setup.append({
            "setup": code, "label": meta["label"], "rule": meta["rule"],
            "open": sum(1 for t in opens + pend if t["setup"] == code),
            "closed": len(ct),
            "win_rate": round(100 * len(w) / len(ct), 1) if ct else None,
            "pnl": round(sum(t["pnl"] or 0 for t in ct), 2),
        })

    payload = {
        "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
        "rule": "Top-5 Chartink setups by 20-day win rate (5y backtest, "
                "124,578 trades) \u00b7 entry next open \u00b7 +4% target / "
                "-2% stop / 20-session time exit \u00b7 \u20b910,000/trade "
                "\u00b7 max 10 open, 5 new/day \u00b7 EOD scan 17:50 IST",
        "note": "Signals scanned directly from completed NSE-500 daily bars "
                "(same conditions as the Chartink MAHI dashboard scans; "
                "setup column shows which rule fired). First scan 5 Nov 2026.",
        "end_date": CFG["end_date"],
        "summary": {"open": len(opens), "pending": len(pend),
                    "closed": len(closed), "wins": len(wins),
                    "losses": len(closed) - len(wins),
                    "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
                    "pnl_inr": round(sum(t["pnl"] or 0 for t in closed), 2)},
        "by_setup": by_setup, "open": opens, "pending": pend, "closed": closed,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    return payload


if __name__ == "__main__":
    import sys
    dry = "--dry" in sys.argv
    out = run_day(dry=dry)
    if dry:
        print(json.dumps(out.get("dry_signals", [])[:20], indent=1))
    else:
        print(json.dumps(out["summary"]))
