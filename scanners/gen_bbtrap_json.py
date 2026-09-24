#!/usr/bin/env python3
"""Generate data/bbtrap.json — BB Trap v2 signals over NSE 200 from NSE CM bhavcopies.

Same rulebook as the browser engine (direct-scan.js runBBTrap) and the vault
scanner scan_bb_trap_v2.py:
  primary candle entirely outside BB(20, 2σ)  (low > upper for shorts,
  high < lower for longs; BB computed on closes INCLUDING the primary bar)
  alert candle rejection wick >= 50% of its range
  alert volume >= 1.5 x primary volume; 10-day avg volume >= 100,000
  entry = latest close (>= 50)
  short: SL = entry + 0.30 x primary range, target = entry - 0.80 x range
  long:  SL = entry - 0.50 x range, target = entry + 1.00 x range
  RSI(14, simple MA) flagged; short score adds RSI>70 bonus

Works from a directory of cm_*.zip UDiFF bhavcopies. Deterministic — runs
identically in CI and locally.
"""
import argparse
import csv
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
BB_PERIOD, BB_STD = 20, 2.0
MIN_WICK, MIN_VOL_MULT, MIN_AVG_VOL, PRICE_MIN = 0.50, 1.5, 100000, 50
RSI_PERIOD, RSI_THRESHOLD = 14, 70
LOOKBACK_ZIPS = 130


def load_bars(bhav_dir: Path, universe: set):
    zips = sorted(bhav_dir.glob("cm_*.zip"))[-LOOKBACK_ZIPS:]
    bars = {s: [] for s in universe}
    for zp in zips:
        try:
            with zipfile.ZipFile(zp) as zf:
                name = [n for n in zf.namelist() if n.lower().endswith(".csv")][0]
                with zf.open(name) as raw:
                    for r in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig")):
                        if r.get("SctySrs") != "EQ" or r.get("TckrSymb") not in bars:
                            continue
                        try:
                            bars[r["TckrSymb"]].append((
                                r["TradDt"],
                                float(r["OpnPric"]), float(r["HghPric"]),
                                float(r["LwPric"]), float(r["ClsPric"]),
                                int(float(r["TtlTradgVol"] or 0)),
                            ))
                        except (ValueError, KeyError):
                            continue
        except (zipfile.BadZipFile, KeyError):
            continue
    for s in bars:
        bars[s].sort(key=lambda b: b[0])
    return bars, (zips[-1].stem.replace("cm_", "") if zips else "")


def bb(closes):
    if len(closes) < BB_PERIOD:
        return None
    w = closes[-BB_PERIOD:]
    m = sum(w) / len(w)
    sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5
    return m + BB_STD * sd, m - BB_STD * sd


def rsi14(closes):
    if len(closes) < RSI_PERIOD + 1:
        return None
    gains = losses = 0.0
    for i in range(len(closes) - RSI_PERIOD, len(closes)):
        ch = closes[i] - closes[i - 1]
        if ch > 0: gains += ch
        else: losses -= ch
    ag, al = gains / RSI_PERIOD, losses / RSI_PERIOD
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def wick(o, h, l, c, upper):
    rng = h - l
    return 0.0 if rng <= 0 else ((h - max(o, c)) / rng if upper else (min(o, c) - l) / rng)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bhav-dir", required=True)
    ap.add_argument("--universe", default="data/nse200_symbols.csv")
    ap.add_argument("--out", default="data/bbtrap.json")
    a = ap.parse_args()

    uni = []
    with open(a.universe) as f:
        for line in f:
            s = line.strip().split(",")[0]
            if s and s.lower() != "symbol":
                uni.append(s)
    bars, latest_zip = load_bars(Path(a.bhav_dir), set(uni))

    # Sweep the last 5 primary/alert pairs per symbol (most recent first).
    # A single-pair scan blinked and missed — signals cluster on volatile
    # days and the tab stayed empty for days after one quiet session.
    PAIRS = 5
    shorts, longs, used = [], [], 0
    latest_date = ""
    for sym in uni:
        b = bars.get(sym) or []
        if len(b) < BB_PERIOD + 2:
            continue
        latest_date = max(latest_date, b[-1][0])
        if b[-1][4] < PRICE_MIN:
            continue
        used += 1
        taken_short = taken_long = False
        for j in range(len(b) - 2, max(len(b) - 2 - PAIRS, BB_PERIOD), -1):
            if taken_short and taken_long:
                break
            p, al = b[j - 1], b[j]
            up, lo = bb([x[4] for x in b[:j - 1]]) or (None, None)
            if up is None:
                break
            vol_mult = (al[5] / p[5]) if p[5] > 0 else 999.0
            avg10 = sum(x[5] for x in b[j - 12:j - 2]) / 10
            if avg10 < MIN_AVG_VOL or vol_mult < MIN_VOL_MULT:
                continue
            rng = p[2] - p[3]
            if rng <= 0:
                continue
            r = rsi14([x[4] for x in b[:j + 1]])
            rsi_pass = r is not None and r > RSI_THRESHOLD
            entry = al[4]
            if entry < PRICE_MIN:
                continue

            def _wick(upper):
                return wick(al[1], al[2], al[3], al[4], upper)

            def _row(side, sl, tgt, w, rsi_flag):
                risk, reward = abs(sl - entry), abs(tgt - entry)
                return {
                    "kind": "bb", "type": side, "symbol": sym,
                    "entry_price": round(entry, 2), "sl_price": round(sl, 2),
                    "target_price": round(tgt, 2),
                    "rr": round(reward / risk, 1) if risk > 0 else 0,
                    "wick_pct": round(w * 100),
                    "vol_multiple": round(vol_mult, 1),
                    "rsi": round(r) if r is not None else None,
                    "rsi_pass": rsi_flag,
                    "primary_range": round(rng, 2),
                    "primary_date": p[0], "alert_date": al[0],
                    "score": round((reward / risk if risk > 0 else 0) * 10
                                   + vol_mult * 2 + w * 5
                                   + (10 if rsi_flag else 0), 1),
                }

            # primary CLOSE beyond the band (browser engine's screen-1). The
            # original full-outside test (low above the upper band on a window
            # that includes the primary close) never fired once in 64 scan
            # versions — close-beyond-band + rejection wick + volume is the
            # workable trap definition.
            if not taken_short and p[4] > up and _wick(True) >= MIN_WICK:
                shorts.append(_row("SHORT", entry + rng * 0.30,
                                   entry - rng * 0.80, _wick(True), rsi_pass))
                taken_short = True
            if not taken_long and p[4] < lo and _wick(False) >= MIN_WICK:
                longs.append(_row("LONG", entry - rng * 0.50,
                                  entry + rng * 1.00, _wick(False), False))
                taken_long = True

    shorts.sort(key=lambda x: -x["score"])
    longs.sort(key=lambda x: -x["score"])
    out = {
        "fetched_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "latest_date": latest_date,
        "universe_count": used,
        "shorts_count": len(shorts),
        "longs_count": len(longs),
        "shorts": shorts,
        "longs": longs,
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print(f"BB Trap v2 scan ({latest_date}): {len(shorts)} shorts, {len(longs)} longs "
          f"over {used} symbols -> {a.out}")


if __name__ == "__main__":
    main()
