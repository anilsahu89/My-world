#!/usr/bin/env python3
"""
BB Trap Positional v2 — Daily Scanner
Scans all NSE EQ stocks for the optimized BB Trap v2 setup.

SHORT Setup (primary edge, PF 2.10):
  1. Primary candle (yesterday): entire candle above upper BB (low > upper_bb)
  2. Alert candle (today): upper wick >= 50% of range (rejection)
  3. Volume: today's vol >= 1.5x yesterday's vol
  4. RSI > 70 (recommended)

LONG Setup (marginal, PF 1.06):
  1. Primary candle: entire candle below lower BB (high < lower_bb)
  2. Alert candle: lower wick >= 50% of range
  3. Volume: today's vol >= 1.5x yesterday's vol

Usage:
  python scan_bb_trap_v2.py                  # scan latest available day
  python scan_bb_trap_v2.py --date 2026-07-10
  python scan_bb_trap_v2.py --shorts-only    # only short setups (recommended)
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

RAW_DIR = Path(__file__).parent / "data" / "raw"
FO_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
CM_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36"

BB_PERIOD = 20
BB_STD = 2.0
MIN_PRICE = 50.0
MIN_AVG_VOL = 100000
MIN_VOL_MULT = 1.5
MIN_WICK_PCT = 0.50
RSI_THRESHOLD = 70  # for shorts
RSI_PERIOD = 14


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def parse_date(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def download(url, path, retries=3):
    if path.exists() and path.stat().st_size > 0:
        return True
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/zip,text/csv,*/*",
        "Referer": "https://www.nseindia.com/",
    })
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                content = response.read()
            if not content.startswith(b"PK"):
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 404, 503}:
                return False
        except Exception:
            if attempt == retries:
                return False
    return False


def read_zip_csv(path):
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return
        with zf.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            yield from csv.DictReader(text)


def load_day_ohlcv(day):
    ymd = day.strftime("%Y%m%d")
    cm_path = RAW_DIR / f"cm_{ymd}.zip"
    if not download(CM_URL.format(yyyymmdd=ymd), cm_path):
        return None
    data = {}
    for row in read_zip_csv(cm_path):
        if row.get("FinInstrmTp") != "STK" or row.get("SctySrs") != "EQ":
            continue
        sym = row.get("TckrSymb", "")
        if not sym:
            continue
        o = to_float(row.get("OpnPric", ""))
        h = to_float(row.get("HghPric", ""))
        l = to_float(row.get("LwPric", ""))
        c = to_float(row.get("ClsPric", ""))
        v = to_int(row.get("TtlTradgVol", ""))
        if any(math.isnan(x) for x in (o, h, l, c)) or c <= 0:
            continue
        data[sym] = (o, h, l, c, v)
    return data


def find_latest_date(start_from=None):
    d = start_from or date.today()
    for _ in range(10):
        if d.weekday() >= 5:
            d -= timedelta(days=1)
            continue
        ymd = d.strftime("%Y%m%d")
        cm_path = RAW_DIR / f"cm_{ymd}.zip"
        if cm_path.exists() and cm_path.stat().st_size > 0:
            return d
        if download(CM_URL.format(yyyymmdd=ymd), cm_path):
            return d
        cm_path.unlink(missing_ok=True)
        d -= timedelta(days=1)
    return None


def load_history(symbol_history, num_days=25, end_date=None):
    """Load last N days of OHLCV for all symbols from cached bhavcopy files."""
    end = end_date or date.today()
    dates = []
    d = end
    while len(dates) < num_days:
        if d.weekday() < 5:
            ymd = d.strftime("%Y%m%d")
            cm_path = RAW_DIR / f"cm_{ymd}.zip"
            if cm_path.exists() and cm_path.stat().st_size > 0:
                dates.append(d)
        d -= timedelta(days=1)
        if d < date(2024, 1, 1):
            break
    dates.sort()

    for day in dates:
        ohlcv = load_day_ohlcv(day)
        if ohlcv is None:
            continue
        for sym, (o, h, l, c, v) in ohlcv.items():
            if sym not in symbol_history:
                symbol_history[sym] = {"dates": [], "opens": [], "highs": [], "lows": [], "closes": [], "volumes": []}
            sh = symbol_history[sym]
            sh["dates"].append(day)
            sh["opens"].append(o)
            sh["highs"].append(h)
            sh["lows"].append(l)
            sh["closes"].append(c)
            sh["volumes"].append(v)

    return dates[-1] if dates else None


def compute_bb(closes):
    if len(closes) < BB_PERIOD:
        return None
    w = closes[-BB_PERIOD:]
    sma = sum(w) / BB_PERIOD
    var = sum((x - sma) ** 2 for x in w) / BB_PERIOD
    std = math.sqrt(var)
    return sma, sma + BB_STD * std, sma - BB_STD * std


def compute_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        ch = closes[i] - closes[i - 1]
        gains.append(ch if ch > 0 else 0)
        losses.append(-ch if ch < 0 else 0)
    ag, al = sum(gains) / period, sum(losses) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def upper_wick_pct(o, h, l, c):
    rng = h - l
    return (h - max(o, c)) / rng if rng > 0 else 0


def lower_wick_pct(o, h, l, c):
    rng = h - l
    return (min(o, c) - l) / rng if rng > 0 else 0


def scan_shorts(symbol_history, scan_date):
    """Scan for SHORT BB Trap v2 signals."""
    signals = []
    for sym, hd in symbol_history.items():
        if len(hd["closes"]) < BB_PERIOD + 2:
            continue
        if hd["closes"][-1] < MIN_PRICE:
            continue
        vols = hd["volumes"]
        if len(vols) >= 10 and sum(vols[-10:]) / 10 < MIN_AVG_VOL:
            continue

        # BB computed from closes up to t-2 (primary candle day)
        closes_t2 = hd["closes"][:-2]
        if len(closes_t2) < BB_PERIOD:
            continue
        bb = compute_bb(closes_t2)
        if not bb:
            continue
        sma, upper, lower = bb

        # Primary candle = t-2 (second to last in our history)
        # Alert candle = t-1 (last candle)
        idx_p = -3
        idx_a = -2

        o_p, h_p, l_p, c_p = hd["opens"][idx_p], hd["highs"][idx_p], hd["lows"][idx_p], hd["closes"][idx_p]
        o_a, h_a, l_a, c_a = hd["opens"][idx_a], hd["highs"][idx_a], hd["lows"][idx_a], hd["closes"][idx_a]
        vol_p, vol_a = vols[idx_p], vols[idx_a]

        # Rule 1: Primary candle entirely above upper BB
        if not (l_p > upper):
            continue

        # Rule 2: Alert candle upper wick >= 50%
        uw = upper_wick_pct(o_a, h_a, l_a, c_a)
        if uw < MIN_WICK_PCT:
            continue

        # Rule 3: Volume >= 1.5x
        vol_mult = vol_a / vol_p if vol_p > 0 else 999
        if vol_mult < MIN_VOL_MULT:
            continue

        # Rule 4: RSI
        rsi = compute_rsi(hd["closes"])
        rsi_pass = rsi is not None and rsi > RSI_THRESHOLD

        primary_range = h_p - l_p
        entry_price = hd["closes"][-1]  # today's close
        sl_price = entry_price + primary_range * 0.30
        target_price = entry_price - primary_range * 0.80
        risk = sl_price - entry_price
        reward = entry_price - target_price
        rr = reward / risk if risk > 0 else 0

        signals.append({
            "symbol": sym,
            "type": "SHORT",
            "entry_price": entry_price,
            "sl_price": sl_price,
            "target_price": target_price,
            "rr": rr,
            "primary_date": hd["dates"][idx_p].isoformat(),
            "alert_date": hd["dates"][idx_a].isoformat(),
            "primary_high": h_p, "primary_low": l_p,
            "alert_close": c_a,
            "upper_wick_pct": uw,
            "vol_multiple": vol_mult,
            "rsi": rsi,
            "rsi_pass": rsi_pass,
            "bb_width_pct": (upper - lower) / sma * 100 if sma else 0,
            "primary_range": primary_range,
            "score": (rr * 10) + (vol_mult * 2) + (uw * 5) + (10 if rsi_pass else 0),
        })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def scan_longs(symbol_history, scan_date):
    """Scan for LONG BB Trap v2 signals."""
    signals = []
    for sym, hd in symbol_history.items():
        if len(hd["closes"]) < BB_PERIOD + 2:
            continue
        if hd["closes"][-1] < MIN_PRICE:
            continue
        vols = hd["volumes"]
        if len(vols) >= 10 and sum(vols[-10:]) / 10 < MIN_AVG_VOL:
            continue

        closes_t2 = hd["closes"][:-2]
        if len(closes_t2) < BB_PERIOD:
            continue
        bb = compute_bb(closes_t2)
        if not bb:
            continue
        sma, upper, lower = bb

        idx_p = -3
        idx_a = -2

        h_p = hd["highs"][idx_p]
        l_p = hd["lows"][idx_p]
        o_a, h_a, l_a, c_a = hd["opens"][idx_a], hd["highs"][idx_a], hd["lows"][idx_a], hd["closes"][idx_a]
        vol_p, vol_a = vols[idx_p], vols[idx_a]

        # Rule 1: Primary entirely below lower BB
        if not (h_p < lower):
            continue

        # Rule 2: Lower wick >= 50%
        lw = lower_wick_pct(o_a, h_a, l_a, c_a)
        if lw < MIN_WICK_PCT:
            continue

        # Rule 3: Volume >= 1.5x (mandatory for longs)
        vol_mult = vol_a / vol_p if vol_p > 0 else 999
        if vol_mult < MIN_VOL_MULT:
            continue

        rsi = compute_rsi(hd["closes"])
        primary_range = h_p - l_p
        entry_price = hd["closes"][-1]
        sl_price = entry_price - primary_range * 0.50
        target_price = entry_price + primary_range * 1.00
        risk = entry_price - sl_price
        reward = target_price - entry_price
        rr = reward / risk if risk > 0 else 0

        signals.append({
            "symbol": sym,
            "type": "LONG",
            "entry_price": entry_price,
            "sl_price": sl_price,
            "target_price": target_price,
            "rr": rr,
            "primary_date": hd["dates"][idx_p].isoformat(),
            "alert_date": hd["dates"][idx_a].isoformat(),
            "primary_high": h_p, "primary_low": l_p,
            "alert_close": c_a,
            "lower_wick_pct": lw,
            "vol_multiple": vol_mult,
            "rsi": rsi,
            "bb_width_pct": (upper - lower) / sma * 100 if sma else 0,
            "primary_range": primary_range,
            "score": (rr * 10) + (vol_mult * 2) + (lw * 5),
        })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def main():
    parser = argparse.ArgumentParser(description="BB Trap Positional v2 Daily Scanner")
    parser.add_argument("--date", help="Specific date YYYY-MM-DD (default: latest)")
    parser.add_argument("--shorts-only", action="store_true", help="Only scan short setups (recommended)")
    parser.add_argument("--longs-only", action="store_true", help="Only scan long setups")
    parser.add_argument("--top", type=int, default=10, help="Show top N candidates per direction")
    args = parser.parse_args()

    if args.date:
        scan_date = parse_date(args.date)
    else:
        scan_date = find_latest_date()
        if scan_date is None:
            print("Could not find any available NSE data")
            return

    print(f"BB Trap Positional v2 Scanner")
    print(f"Scan date: {scan_date} (latest available)")
    print(f"Rules: Primary outside BB → Alert rejection wick ≥ {MIN_WICK_PCT*100:.0f}% → Vol ≥ {MIN_VOL_MULT}x")
    print(f"Filters: Price ≥ ₹{MIN_PRICE}, Avg vol ≥ {MIN_AVG_VOL:,}, RSI > {RSI_THRESHOLD} (shorts)")
    print()

    # Load history
    print(f"Loading 25-day OHLCV history (up to {scan_date})...")
    symbol_history = {}
    latest = load_history(symbol_history, num_days=25, end_date=scan_date)
    print(f"  Loaded history for {len(symbol_history)} symbols, latest date: {latest}")
    print()

    # Scan
    short_signals = []
    long_signals = []

    if not args.longs_only:
        print("Scanning SHORTS...")
        short_signals = scan_shorts(symbol_history, scan_date)
        print(f"  Found {len(short_signals)} short signals")

    if not args.shorts_only:
        print("Scanning LONGS...")
        long_signals = scan_longs(symbol_history, scan_date)
        print(f"  Found {len(long_signals)} long signals")

    print()
    print("=" * 110)

    # Print results
    for label, signals, rsi_col in [
        ("SHORT SETUPS (PF 2.10 with filters)", short_signals, True),
        ("LONG SETUPS (PF 1.06 — marginal)", long_signals, False),
    ]:
        if not signals:
            continue
        print(f"\n  {label}")
        print(f"  {'#':<3} {'Symbol':<14} {'Entry':>8} {'SL':>8} {'Target':>8} {'R:R':>5} {'Wick%':>6} {'Vol x':>6} {'RSI':>6} {'P.Range':>8} {'Score':>6}")
        print(f"  {'-'*105}")
        for i, s in enumerate(signals[:args.top], 1):
            rsi_str = f"{s['rsi']:.0f}" if s.get("rsi") else "N/A"
            rsi_mark = " ✓" if rsi_col and s.get("rsi_pass") else ""
            wick_key = "upper_wick_pct" if "upper_wick_pct" in s else "lower_wick_pct"
            print(f"  {i:<3} {s['symbol']:<14} {s['entry_price']:>8.1f} {s['sl_price']:>8.1f} {s['target_price']:>8.1f} {s['rr']:>4.1f}x {s[wick_key]*100:>5.0f}% {s['vol_multiple']:>5.1f}x {rsi_str:>5}{rsi_mark} {s['primary_range']:>8.1f} {s['score']:>6.1f}")

        if signals:
            print()
            print(f"  Trade plan for top candidate:")
            top = signals[0]
            print(f"    {top['symbol']}: {'SHORT' if top['type']=='SHORT' else 'LONG'} at ₹{top['entry_price']:.2f}")
            print(f"    SL: ₹{top['sl_price']:.2f} | Target: ₹{top['target_price']:.2f} | R:R = {top['rr']:.1f}x")
            print(f"    Primary range: ₹{top['primary_range']:.2f} | Vol multiple: {top['vol_multiple']:.1f}x")
            if rsi_col:
                print(f"    RSI: {top.get('rsi', 'N/A')} {'✓ PASS' if top.get('rsi_pass') else '✗ FAIL (tradeable but lower confidence)'}")

    if not short_signals and not long_signals:
        print("\n  No BB Trap v2 signals found today. This is normal — signals are infrequent (~4/month shorts).")

    print()
    print("=" * 110)
    print("  DISCLAIMER: Backtested PF 2.10 (shorts, in-sample). Expect regression live.")
    print("  Always verify on chart before entering. Not investment advice.")
    print("=" * 110)


if __name__ == "__main__":
    main()
