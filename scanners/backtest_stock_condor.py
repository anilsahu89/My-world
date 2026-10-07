#!/usr/bin/env python3
"""Stock Premium Spread — monthly single-sided hedged credit spread backtest.

Owner brief v2 (7 Oct 2026): drop the both-sides condor. Sell ONE side only —
the side the 20-SMA trend favors (vault rule: above 20 SMA -> Bull Put,
below -> Bear Call) — hedged for large moves, strikes anchored to the
stock's trailing average monthly move. Defined risk: max loss per cycle is
the spread width minus credit. Frequency-first: every month, every stock.

Usage:
  python3 backtest_stock_condor.py K_SHORT K_HEDGE SL_POLICY SIDE STOCKS
    K_SHORT   short strike distance in avg-monthly-moves (e.g. 1.0)
    K_HEDGE   hedge distance   in avg-monthly-moves (e.g. 2.0)
    SL_POLICY mid | short | hedge | none
    SIDE      single | both
    STOCKS    comma list (default all five tested)

Cycle: entry = first trading day after the stock's monthly expiry (last
Tuesday); premiums = REAL closes from that day's FO bhavcopy; exits =
50% of net credit (BS-marked daily on the trend-scaled Yahoo path),
SL per policy, else expiry intrinsic. Corporate actions (bonus/split)
handled by rescaling the path to the entry-day parity spot.
"""
from __future__ import annotations

import io
import csv
import json
import math
import zipfile
import urllib.request
from datetime import date, timedelta

import yfinance as yf
import pandas as pd
import sys

K_SHORT = float(sys.argv[1]) if len(sys.argv) > 1 else 1.25
K_HEDGE = float(sys.argv[2]) if len(sys.argv) > 2 else 2.25
SL_POLICY = sys.argv[3] if len(sys.argv) > 3 else "mid"     # mid|short|hedge|none
SIDE = sys.argv[4] if len(sys.argv) > 4 else "single"       # single|both
STOCKS = (sys.argv[5].split(",") if len(sys.argv) > 5
          else ["ZYDUSLIFE", "ICICIBANK", "CIPLA", "HDFCBANK", "RELIANCE"])

MIN_DTE, MAX_DTE = 15, 60
MIN_SHORT_PREMIUM = 2.0
TARGET_PCT = 0.50
R_FREE = 0.065
FO_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 Chrome/125 Safari/537.36",
      "Accept": "application/zip", "Referer": "https://www.nseindia.com/"}
CACHE = "/tmp/condor-bhav"


def _norm_pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def bs_price(spot, strike, t_years, iv, call, r=R_FREE):
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, spot - strike) if call else max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    nd = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    px = spot * nd(d1) - strike * math.exp(-r * t_years) * nd(d2) if call else \
        strike * math.exp(-r * t_years) * nd(-d2) - spot * nd(-d1)
    return max(px, 0.0)


def implied_iv(price, spot, strike, t_years, call):
    if price <= 0 or t_years <= 0:
        return None
    lo, hi = 0.01, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p = bs_price(spot, strike, t_years, mid, call)
        if abs(p - price) < 1e-4:
            return mid
        if p < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def last_tuesday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    while nxt.weekday() != 1:
        nxt -= timedelta(days=1)
    return nxt


def fetch_fo(ymd_day: date) -> dict | None:
    import os
    os.makedirs(CACHE, exist_ok=True)
    p = f"{CACHE}/fo_{ymd_day:%Y%m%d}.zip"
    if not os.path.exists(p):
        req = urllib.request.Request(FO_URL.format(ymd=ymd_day.strftime("%Y%m%d")),
                                     headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read()
            if not body.startswith(b"PK"):
                return None
            open(p, "wb").write(body)
        except Exception:
            return None
    try:
        z = zipfile.ZipFile(p)
        csv_name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        rows = list(csv.DictReader(io.TextIOWrapper(z.open(csv_name),
                                                    encoding="utf-8-sig")))
    except Exception:
        return None
    out: dict[str, dict] = {}
    for r in rows:
        sym = r.get("TckrSymb")
        if not sym:
            continue
        slot = out.setdefault(sym, {"sto": [], "stf": []})
        if r.get("FinInstrmTp") == "STO":
            slot["sto"].append(r)
        elif r.get("FinInstrmTp") == "STF":
            slot["stf"].append(r)
    return out


def pick(strikes: list[float], target: float, below: bool) -> float | None:
    if below:
        cands = [s for s in strikes if s <= target]
        return max(cands) if cands else None
    cands = [s for s in strikes if s >= target]
    return min(cands) if cands else None


def main() -> None:
    today = date(2026, 10, 7)
    closes = {}
    print(f"downloading daily closes for {STOCKS} …", flush=True)
    px = yf.download([s + ".NS" for s in STOCKS],
                     start=(today - timedelta(days=800)).isoformat(),
                     interval="1d", group_by="ticker", progress=False,
                     threads=True, auto_adjust=False)
    for s in STOCKS:
        closes[s] = px[s + ".NS"].dropna(subset=["Close"])["Close"]

    months = []
    anchor = today - timedelta(days=800)
    y, m = anchor.year, anchor.month
    while date(y, m, 1) < date(today.year, today.month, 1):
        months.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1

    results = {s: [] for s in STOCKS}
    log = []

    for (y, m) in months:
        exp_day = last_tuesday(y, m)
        cal = closes[STOCKS[0]]
        after = cal.index[cal.index.date > exp_day]
        if after.empty:
            continue
        entry_day = after[0].date()
        if entry_day >= today:
            continue
        nm_y, nm_m = (y + m // 12, m % 12 + 1)
        settle_day = last_tuesday(nm_y, nm_m)

        bhav = fetch_fo(entry_day)
        if not bhav:
            continue

        for s in STOCKS:
            book = bhav.get(s)
            if not book or not book["sto"]:
                continue
            series = closes[s][closes[s].index.date <= entry_day]
            if len(series) < 240:
                continue
            mclose = series.resample("ME").last().dropna()
            if len(mclose) < 13:
                continue
            avg_move = float(mclose.pct_change().dropna().abs().tail(24).mean())
            if not avg_move:
                continue
            sto = book["sto"]
            exps = sorted({r["XpryDt"][:10] for r in sto
                           if MIN_DTE <= (date.fromisoformat(r["XpryDt"][:10])
                                          - entry_day).days <= MAX_DTE})
            if not exps:
                continue
            expiry = exps[0]
            dte = (date.fromisoformat(expiry) - entry_day).days
            px_map = {(float(r["StrkPric"]), r["OptnTp"]): float(r["ClsPric"] or 0)
                      for r in sto if r["XpryDt"][:10] == expiry}
            strikes_all = sorted({k[0] for k in px_map})
            atm = min(strikes_all, key=lambda k: abs(k - series.iloc[-1]))
            ce, pe = px_map.get((atm, "CE")), px_map.get((atm, "PE"))
            if not ce or not pe:
                continue
            spot = atm + ce - pe                    # parity spot, bhav scale
            px_last = float(series.iloc[-1])
            scale = spot / px_last if px_last > 0 and \
                abs(spot - px_last) / px_last > 0.20 else 1.0

            sma20 = float(series.tail(20).mean())
            side = "put" if spot > sma20 else "call"
            if side == "put":
                s1 = pick(strikes_all, spot * (1 - K_SHORT * avg_move), True)
                h1 = pick(strikes_all, spot * (1 - K_HEDGE * avg_move), True)
                call1 = False
                breach = (lambda c: c <= (s1 + h1) / 2 if SL_POLICY == "mid"
                          else c <= s1 if SL_POLICY == "short"
                          else c <= h1 if SL_POLICY == "hedge" else False)
            else:
                s1 = pick(strikes_all, spot * (1 + K_SHORT * avg_move), False)
                h1 = pick(strikes_all, spot * (1 + K_HEDGE * avg_move), False)
                call1 = True
                breach = (lambda c: c >= (s1 + h1) / 2 if SL_POLICY == "mid"
                          else c >= s1 if SL_POLICY == "short"
                          else c >= h1 if SL_POLICY == "hedge" else False)
            if not (s1 and h1) or s1 == h1:
                continue
            prem_s = px_map.get((s1, "CE" if call1 else "PE"), 0.0)
            prem_h = px_map.get((h1, "CE" if call1 else "PE"), 0.0)
            if SIDE == "both":
                # condor mode (kept for comparison)
                if side == "put":
                    s2t = spot * (1 + K_SHORT * avg_move); h2t = spot * (1 + K_HEDGE * avg_move)
                else:
                    s2t = spot * (1 - K_SHORT * avg_move); h2t = spot * (1 - K_HEDGE * avg_move)
                s2 = pick(strikes_all, s2t, s2t < spot)
                h2 = pick(strikes_all, h2t, h2t < spot)
                if not (s2 and h2) or s2 == h2:
                    continue
                o2 = "CE" if side == "put" else "PE"
                prem = {**(px_map.get((s2, o2), 0.0),)} if False else None
                prem_s2 = px_map.get((s2, o2), 0.0)
                prem_h2 = px_map.get((h2, o2), 0.0)
                prem_s += prem_s2
                prem_h += prem_h2
                legs = [(s1, call1, True), (h1, call1, False),
                        (s2, not call1, True), (h2, not call1, False)]
            else:
                legs = [(s1, call1, True), (h1, call1, False)]
            credit = prem_s - prem_h
            if prem_s < MIN_SHORT_PREMIUM or credit <= 0:
                continue

            t_years = dte / 365.0
            ivs = {}
            for k, is_call, _is_short in legs:
                key = "CE" if is_call else "PE"
                iv = implied_iv(px_map.get((k, key), 0.0), spot, k, t_years, is_call)
                ivs[(k, is_call)] = iv or 0.55

            def pos_value(c, rem):
                v = 0.0
                for k, is_call, is_short in legs:
                    t = bs_price(c, k, rem, ivs[(k, is_call)], is_call)
                    v += t if is_short else -t
                return v

            path = closes[s][(closes[s].index.date > entry_day)
                             & (closes[s].index.date <= settle_day)]
            if scale != 1.0:
                path = path * scale
            pnl = None; exit_reason = "expiry"
            for ts, close in path.items():
                d = ts.date()
                rem = max(0.0, (settle_day - d).days / 365.0)
                if rem <= 0:
                    break
                value = pos_value(close, rem)
                if value <= credit * TARGET_PCT:
                    pnl = credit - value; exit_reason = "target-50%"
                    break
                if breach(close):
                    pnl = credit - value; exit_reason = f"SL-{SL_POLICY}"
                    break
            if pnl is None:
                c = float(path.iloc[-1]) if len(path) else spot
                intr = 0.0
                for k, is_call, is_short in legs:
                    intr += max(0.0, (c - k) if is_call else (k - c)) \
                        * (1 if is_short else -1)
                pnl = credit - intr
            results[s].append({"entry": entry_day.isoformat(),
                               "expiry": settle_day.isoformat(),
                               "side": side, "credit": round(credit, 2),
                               "pnl": round(pnl, 2), "reason": exit_reason,
                               "short": s1, "hedge": h1, "spot0": round(spot, 1),
                               "avg_move": round(avg_move * 100, 2), "dte": dte})
            log.append(f"  {s:11s} {entry_day} {side:5s} cr {credit:6.2f} "
                       f"→ pnl {pnl:+7.2f} ({exit_reason}) s{s1:.0f}/h{h1:.0f}")

    print("\n".join(log[-15:]))
    print("\n" + "=" * 78)
    grand = {"n": 0, "w": 0, "pnl": 0.0, "gw": 0.0, "gl": 0.0}
    for s in STOCKS:
        rs = results[s]
        if not rs:
            print(f"{s}: no trades"); continue
        pnl = [r["pnl"] for r in rs]
        wins = [p for p in pnl if p > 0]
        gl = -sum(p for p in pnl if p <= 0)
        gw = sum(wins)
        reasons = {}
        for r in rs: reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        sides = {}
        for r in rs: sides[r["side"]] = sides.get(r["side"], 0) + 1
        print(f"\n{s} — {len(rs)} cycles ({rs[0]['entry']} → {rs[-1]['entry']})")
        print(f"  win rate   : {len(wins)}/{len(rs)} = {len(wins)/len(rs)*100:.0f}%")
        print(f"  total P&L  : {sum(pnl):+,.1f} pts | avg {sum(pnl)/len(rs):+,.2f}/cycle")
        print(f"  avg credit : {sum(r['credit'] for r in rs)/len(rs):.2f} pts")
        print(f"  best/worst : {max(pnl):+,.1f} / {min(pnl):+,.1f} pts")
        print(f"  profit fac : {gw/gl:.2f}" if gl else "  profit fac : inf")
        print(f"  exits      : {reasons} | sides: {sides}")
        grand["n"] += len(rs); grand["w"] += len(wins)
        grand["pnl"] += sum(pnl); grand["gw"] += gw; grand["gl"] += gl
    print("\n" + "=" * 78)
    pf = grand["gw"] / max(1e-9, grand["gl"])
    print(f"COMBINED ({SIDE}, K {K_SHORT}/{K_HEDGE}, SL {SL_POLICY}): "
          f"{grand['n']} cycles | win {grand['w']}/{grand['n']} = "
          f"{grand['w']/max(1,grand['n'])*100:.0f}% | total {grand['pnl']:+,.1f} pts "
          f"| PF {pf:.2f}")
    json.dump(results, open("/tmp/condor-backtest.json", "w"), indent=1)


if __name__ == "__main__":
    main()
