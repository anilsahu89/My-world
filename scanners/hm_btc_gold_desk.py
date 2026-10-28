#!/usr/bin/env python3
"""
HM Multi-Asset Desk — Bitcoin & Gold (paper).
All owner-defined HM setups, both directions, on 1-hour / daily / weekly:

  GAP-ANGLE-BUY          red below black+green, gap + rising angle, RSI>50  (full lot)
  GAP-ANGLE-BUY-SUB50    same structure, RSI<=50                            (half lot)
  GAP-ANGLE-SELL         mirror: red above black+green, gap + falling angle, RSI<50 (full)
  GAP-ANGLE-SELL-OVER50  mirror risk variant, RSI>=50                       (half lot)
  NEW-LOW-TRICK-SHORT    black crossed above red, dwell<=5 bars, closed back below
                         -> short, expects prior-low break. SL = failed attempt extreme.

Exits: BUY -> RSI<50 TREND-END / RSI>=86 RSI86-BOOK.
       SELL -> RSI>50 TREND-END / RSI<=14 RSI14-BOOK (mirror of 86).
       TRICK shorts also exit if RSI closes back above red (INVALIDATED).
       Green/RSI touch after separation = WARNING only.
Ledger columns include SETUP (explicit setup name) and SIDE.
"""
import argparse
import json
import warnings
from datetime import datetime, date, timezone
from pathlib import Path

warnings.filterwarnings("ignore")
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
import os
ROOT = HERE.parent
PORTAL = Path(os.environ.get("HM_PORTAL_DIR", str(ROOT)))  # my-world repo root
LEDGER = HERE / "papertrades" / "hm_btc_gold_trades.csv"
JSON_OUT = PORTAL / "data" / "scanners" / "hm-btc-gold-latest.json"

ASSETS = {"BTC-USD": "BTC", "GC=F": "GOLD"}
LOT = 25000.0
BOOK = 200000.0
MAX_OPEN = 5
MAX_NEW_PER_ASSET_FRAME_DAY = 2
COST = 0.003
GAP_MIN = 3.0
ANGLE_MIN = 1.0
TOUCH_EPS = 1.5
TRICK_DWELL_MAX = 5
TRICK_LOW_LOOKBACK = 20

COLS = ["entry_date", "asset", "frame", "setup", "side", "entry_price", "lots", "capital",
        "gap_rsi", "angle_rsi", "rsi_entry", "red_entry", "green_entry", "stop_ref",
        "status", "exit_date", "exit_reason", "exit_price", "pnl", "pnl_pct", "notes"]


def rsi(close: pd.Series, n: int = 9) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    ru = up.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = ru / rd.replace(0, pd.NA)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def wma(s: pd.Series, n: int = 21) -> pd.Series:
    w = pd.Series(range(1, n + 1), dtype=float)
    w /= w.sum()
    return s.rolling(n).apply(lambda x: float((x * w.values).sum()), raw=False)


def drop_inprogress(s: pd.Series, frame: str, bar_minutes: int) -> pd.Series:
    now = pd.Timestamp.now(tz=timezone.utc)
    if not len(s):
        return s
    last = s.index[-1]
    if last.tzinfo is None:
        last = last.tz_localize("UTC")
    if frame in ("1h",) and now < last + pd.Timedelta(minutes=bar_minutes):
        return s.iloc[:-1]
    if frame == "daily" and last.date() == now.date():
        return s.iloc[:-1]
    return s


def evaluate(close: pd.Series, frame: str):
    r, red, green = rsi(close), None, None
    red, green = wma(r, 21), wma(r, 3)
    if len(r) < 25 or pd.isna(red.iloc[-1]):
        return None
    r_t, red_t, g_t = float(r.iloc[-1]), float(red.iloc[-1]), float(green.iloc[-1])
    r_p, red_p, g_p = float(r.iloc[-2]), float(red.iloc[-2]), float(green.iloc[-2])
    r_ang = float(r.iloc[-1] - r.iloc[-4]) / 3.0 if len(r) >= 4 else 0.0
    gap_up = r_t - red_t
    gap_dn = red_t - r_t

    setups = []
    sep_up = red_t < r_t and g_t > red_t and gap_up >= GAP_MIN and r_ang >= ANGLE_MIN and g_t > g_p
    sep_dn = red_t > r_t and g_t < red_t and gap_dn >= GAP_MIN and r_ang <= -ANGLE_MIN and g_t < g_p
    fresh_up = not (red_p < r_p and float(green.iloc[-2]) > red_p)
    fresh_dn = not (red_p > r_p and float(green.iloc[-2]) < red_p)
    if sep_up and r_t > 50:
        setups.append("GAP-ANGLE-BUY")
    elif sep_up:
        setups.append("GAP-ANGLE-BUY-SUB50")
    if sep_dn and r_t < 50:
        setups.append("GAP-ANGLE-SELL")
    elif sep_dn:
        setups.append("GAP-ANGLE-SELL-OVER50")

    # New Low Trick: RSI crossed above red, dwelt <= N bars, closed back below on this bar
    trick = False
    if r_p > red_p and r_t < red_t:
        i, above = len(r) - 2, True
        dwell = 0
        while i >= 0 and r.iloc[i] > red.iloc[i]:
            dwell += 1
            i -= 1
        if 1 <= dwell <= TRICK_DWELL_MAX and i >= TRICK_LOW_LOOKBACK:
            trick = True

    warn_touch_up = (r_t - g_t) <= TOUCH_EPS and (r_p - g_p) > 3.0
    warn_touch_dn = (g_t - r_t) <= TOUCH_EPS and (g_p - r_p) > 3.0
    return {
        "frame": frame, "date": str(r.index[-1])[:16].replace("T", " "),
        "rsi": round(r_t, 1), "red": round(red_t, 1), "green": round(g_t, 1),
        "gap": round(gap_up if gap_up >= gap_dn else -gap_dn, 1), "angle": round(r_ang, 2),
        "setups": setups, "trick": trick,
        "fresh_up": fresh_up, "fresh_dn": fresh_dn,
        "warn": warn_touch_up or warn_touch_dn,
        "rsi_lt50": r_t < 50, "rsi_gt50": r_t > 50, "rsi_ge86": r_t >= 86, "rsi_le14": r_t <= 14,
        "rsi_above_red": r_t > red_t, "price": round(float(close.iloc[-1]), 2),
    }


def load_ledger():
    if LEDGER.exists():
        return pd.read_csv(LEDGER)
    return pd.DataFrame(columns=COLS)


def side_of(setup: str) -> str:
    return "LONG" if setup.startswith("GAP-ANGLE-BUY") else "SHORT"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true",
                    help="enter the best valid setup per asset+frame even if not fresh (first-fill; marked SEED)")
    args = ap.parse_args()

    led = load_ledger()
    rows, warns = [], []
    closed_keys = set()
    today = str(date.today())

    for ysym, name in ASSETS.items():
        try:
            df = yf.Ticker(ysym).history(period="60d", interval="1h")
            d1 = yf.Ticker(ysym).history(period="2y", interval="1d")
        except Exception:
            continue
        frames = []
        if df is not None and len(df) > 60:
            c = df["Close"]
            c = c[~c.index.duplicated()]
            frames.append((drop_inprogress(c, "1h", 60), "1h"))
        if d1 is not None and len(d1) > 120:
            c = d1["Close"]
            c = c[~c.index.duplicated()]
            cd = drop_inprogress(c, "daily", 1440)
            frames.append((cd, "daily"))
            cw = cd.resample("W-FRI").last().dropna()
            if len(cw) and cw.index[-1] > cd.index[-1]:
                cw = cw.iloc[:-1]
            frames.append((cw, "weekly"))
        # GOLD: weekly frame only (Oct evidence: all 1h/daily gold trades stopped)
        if name == "GOLD":
            frames = [fr for fr in frames if fr[1] == "weekly"]

        for close, frame in frames:
            ev = evaluate(close, frame)
            if ev is None:
                continue
            ev["asset"] = name

            # exits on open trades for this asset+frame
            m = (led["status"] == "OPEN") & (led["asset"] == name) & (led["frame"] == frame)
            closed_this_run = set()
            for idx in led.index[m]:
                setup = led.at[idx, "setup"]
                short = side_of(setup) == "SHORT"
                reason = None
                if short:
                    if setup == "NEW-LOW-TRICK-SHORT":
                        # trick shorts start near/above 50 — RSI>50 is NOT an exit for them
                        if ev["rsi_above_red"]:
                            reason = "INVALIDATED"
                        elif ev["rsi_le14"]:
                            reason = "RSI14-BOOK"
                    elif setup == "GAP-ANGLE-SELL-OVER50":
                        if ev["rsi_above_red"]:
                            reason = "TREND-END"
                        elif ev["rsi_le14"]:
                            reason = "RSI14-BOOK"
                    else:
                        if ev["rsi_gt50"]:
                            reason = "TREND-END"
                        elif ev["rsi_le14"]:
                            reason = "RSI14-BOOK"
                else:
                    if ev["rsi_lt50"]:
                        reason = "TREND-END"
                    elif ev["rsi_ge86"]:
                        reason = "RSI86-BOOK"
                if reason:
                    ep, xp = float(led.at[idx, "entry_price"]), ev["price"]
                    move = (xp - ep) if not short else (ep - xp)
                    qty = float(led.at[idx, "capital"]) / ep
                    pnl = move * qty - float(led.at[idx, "capital"]) * COST
                    led.at[idx, "status"] = "CLOSED"
                    led.at[idx, "exit_date"] = ev["date"]
                    led.at[idx, "exit_reason"] = reason
                    led.at[idx, "exit_price"] = xp
                    led.at[idx, "pnl"] = round(pnl, 2)
                    led.at[idx, "pnl_pct"] = round(pnl / float(led.at[idx, "capital"]) * 100, 2)
                    closed_keys.add((name, frame, setup))

            if ev["warn"]:
                warns.append(f"{name} ({frame}): fast line touched RSI after separation — exhaustion warning")

            ev["entry_candidates"] = []
            for s in ev["setups"]:
                fresh = ev["fresh_up"] if s.startswith("GAP-ANGLE-BUY") else ev["fresh_dn"]
                if fresh or (args.seed and s in ("GAP-ANGLE-BUY", "GAP-ANGLE-SELL")):
                    ev["entry_candidates"].append((s, 1.0 if ("BUY" in s and "SUB" not in s) or ("SELL" in s and "OVER" not in s) else 0.5, fresh))
            if ev["trick"]:
                ev["entry_candidates"].append(("NEW-LOW-TRICK-SHORT", 1.0, True))
            rows.append(ev)

    # entries, best-first
    closed_this_run_all = closed_keys
    cand = []
    for ev in rows:
        for setup, lotmult, fresh in ev["entry_candidates"]:
            cand.append((ev, setup, lotmult, fresh))
    order = {"GAP-ANGLE-BUY": 0, "GAP-ANGLE-SELL": 1, "GAP-ANGLE-BUY-SUB50": 2,
             "GAP-ANGLE-SELL-OVER50": 3, "NEW-LOW-TRICK-SHORT": 4}
    cand.sort(key=lambda t: (order.get(t[1], 9), -abs(t[0]["gap"]), -abs(t[0]["angle"])))
    for ev, setup, lotmult, fresh in cand:
        already = (led["status"] == "OPEN") & (led["asset"] == ev["asset"]) & (led["frame"] == ev["frame"]) & (led["setup"] == setup)
        if (ev["asset"], ev["frame"], setup) in closed_this_run_all:
            continue
        same_day = len(led[(led["asset"] == ev["asset"]) & (led["frame"] == ev["frame"]) & (led["entry_date"] == ev["date"])])
        if args.dry_run or already.any() or same_day >= MAX_NEW_PER_ASSET_FRAME_DAY or (led["status"] == "OPEN").sum() >= MAX_OPEN:
            continue
        capital = LOT * lotmult
        stop_ref = ev["price"]
        notes = "gap+angle" if fresh else "SEED: established state (owner instruction)"
        if setup == "NEW-LOW-TRICK-SHORT":
            notes = "short; SL = failed-attempt high; target = prior low break"
        led.loc[len(led)] = {
            "entry_date": ev["date"], "asset": ev["asset"], "frame": ev["frame"],
            "setup": setup, "side": side_of(setup), "entry_price": ev["price"],
            "lots": lotmult, "capital": capital, "gap_rsi": ev["gap"], "angle_rsi": ev["angle"],
            "rsi_entry": ev["rsi"], "red_entry": ev["red"], "green_entry": ev["green"],
            "stop_ref": stop_ref, "status": "OPEN", "exit_date": "", "exit_reason": "",
            "exit_price": "", "pnl": "", "pnl_pct": "", "notes": notes,
        }

    if not args.dry_run:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        led.to_csv(LEDGER, index=False)

    lines = [f"HM BTC/GOLD desk — {datetime.now():%a %d %b %Y %H:%M}", ""]
    for ev in rows:
        sigs = ev["entry_candidates"] or ev["setups"] or (["NEW-LOW-TRICK"] if ev["trick"] else [])
        tag = " + ".join(s for s, _, _f in ev["entry_candidates"]) if ev["entry_candidates"] else ("(state, not fresh)" if ev["setups"] else ("TRICK!" if ev["trick"] else ""))
        star = " ⚠" if ev["warn"] else ""
        lines.append(f"{ev['asset']:<5}{ev['frame']:<8}{ev['date']:<18}RSI {ev['rsi']:>5} red {ev['red']:>5} green {ev['green']:>5} gap {ev['gap']:>6} ang {ev['angle']:>6}  {tag}{star}")
    if warns:
        lines.append("")
        lines.extend("  " + w for w in warns)
    open_now = led[led["status"] == "OPEN"] if len(led) else led
    if len(open_now):
        lines.append("")
        lines.append(f"Open paper positions: {len(open_now)}")
        for _, t in open_now.iterrows():
            lines.append(f"  {t['entry_date']} {t['asset']:<5}{t['frame']:<8}{t['setup']:<24}{t['side']:<6}@ {t['entry_price']} ({t['capital']:,.0f})")
    report = "\n".join(lines)
    print(report)

    # open positions for the portal (side label segregates the setup kind)
    side_label = {"GAP-ANGLE-BUY": "BUY", "GAP-ANGLE-BUY-SUB50": "BUY · sub-50",
                  "GAP-ANGLE-SELL": "SELL", "GAP-ANGLE-SELL-OVER50": "SELL · over-50",
                  "NEW-LOW-TRICK-SHORT": "SHORT · new-low"}
    closed_rows = [{"asset": r["asset"], "frame": r["frame"], "setup": r["setup"],
                    "side": side_label.get(r["setup"], side_of(r["setup"])),
                    "entry_date": str(r["entry_date"]), "entry_price": r["entry_price"],
                    "exit_date": str(r.get("exit_date", "")), "exit_price": r.get("exit_price", ""),
                    "exit_reason": r.get("exit_reason", ""), "pnl_inr": float(r.get("pnl") or 0)}
                   for _, r in led.iterrows() if r["status"] == "CLOSED"]
    open_rows = [{"asset": r["asset"], "frame": r["frame"], "setup": r["setup"],
                  "side": side_label.get(r["setup"], side_of(r["setup"])),
                  "entry_date": r["entry_date"], "entry_price": r["entry_price"],
                  "stop_ref": r.get("stop_ref", ""), "lots": r["lots"],
                  "capital": r["capital"]} for _, r in led.iterrows()
                 if r["status"] == "OPEN"]

    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps({
        "strategy": "hm-btc-gold",
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
        "status": "success",
        "signals": sum(len(e["entry_candidates"]) for e in rows),
        "open": open_rows,
        "open_pnl_inr": round(sum(float(o.get("pnl_inr") or 0) for o in open_rows), 2),
        "closed": closed_rows,
        "book": {"open": len(open_rows),
                 "open_pnl_inr": round(sum(float(o.get("pnl_inr") or 0) for o in open_rows), 2),
                 "realized_pnl_inr": round(sum(float(c.get("pnl_inr") or 0) for c in closed_rows), 2),
                 "net_pnl_inr": round(sum(float(o.get("pnl_inr") or 0) for o in open_rows)
                                      + sum(float(c.get("pnl_inr") or 0) for c in closed_rows), 2),
                 "wins": sum(1 for c in closed_rows if float(c.get("pnl_inr") or 0) > 0),
                 "losses": sum(1 for c in closed_rows if float(c.get("pnl_inr") or 0) <= 0)},
        "open_pnl_inr": round(sum(float(o.get("pnl_inr") or 0) for o in open_rows), 2),
        "closed": closed_rows,
        "book": {"open": len(open_rows),
                 "open_pnl_inr": round(sum(float(o.get("pnl_inr") or 0) for o in open_rows), 2),
                 "realized_pnl_inr": round(sum(float(c.get("pnl_inr") or 0) for c in closed_rows), 2),
                 "net_pnl_inr": round(sum(float(o.get("pnl_inr") or 0) for o in open_rows)
                                      + sum(float(c.get("pnl_inr") or 0) for c in closed_rows), 2),
                 "wins": sum(1 for c in closed_rows if float(c.get("pnl_inr") or 0) > 0),
                 "losses": sum(1 for c in closed_rows if float(c.get("pnl_inr") or 0) <= 0)},
        "table": {"headers": ["Asset", "Frame", "Bar", "RSI", "Red", "Green", "Gap", "Angle", "Setups"],
                  "rows": [[e["asset"], e["frame"], e["date"], e["rsi"], e["red"], e["green"], e["gap"], e["angle"],
                            ", ".join(s for s, _, _f in e["entry_candidates"]) or (", ".join(e["setups"]) or ("NEW-LOW-TRICK" if e["trick"] else ""))] for e in rows]},
        "text_report": report,
    }, indent=2))
    print(f"\nLedger: {LEDGER} ({len(led)} rows) | Portal JSON: {JSON_OUT}")


if __name__ == "__main__":
    main()
