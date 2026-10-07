#!/usr/bin/env python3
"""Stock Premium Condor — 24-month backtest.

Owner brief (7 Oct 2026): sell MONTHLY options on ONE stock with a hedge,
strikes anchored to the stock's own trailing average monthly move, keep
eating premium; hedge keeps us alive on fast moves either way. Vault rules
apply (strategies/stock-premium-mill.md): F&O-only, liquid, always hedged,
50% profit exit, SL at the strike->hedge midpoint, enter the day after the
monthly expiry.

Mechanics per monthly cycle (per stock):
  entry day    = first trading day after the stock's monthly expiry
                 (NSE stock monthlies expire the last Tuesday)
  avg move     = mean |monthly % close| over the trailing 24 months
  short put    = nearest strike <= spot * (1 - 1.25 * avg_move)
  short call   = nearest strike >= spot * (1 + 1.25 * avg_move)
  long put     = nearest strike <= spot * (1 - 2.25 * avg_move)
  long call    = nearest strike >= spot * (1 + 2.25 * avg_move)
  premiums     = REAL closes from that day's FO bhavcopy
  exits        = 50% of net credit (BS-marked daily) | spot through the
                 short->hedge midpoint | expiry intrinsic
Settlement spot = parity spot on entry; daily closes from Yahoo.
"""
from __future__ import annotations

import io
import csv
import json
import math
import zipfile
import urllib.request
from datetime import date, datetime, timedelta

import yfinance as yf
import pandas as pd
import sys

STOCKS = ["ZYDUSLIFE", "ICICIBANK", "CIPLA"]
K_SHORT = float(sys.argv[1]) if len(sys.argv) > 1 else 1.25   # short strikes (x avg move)
K_HEDGE = float(sys.argv[2]) if len(sys.argv) > 2 else 2.25   # hedges (x avg move)
SL_POLICY = sys.argv[3] if len(sys.argv) > 3 else "mid"       # mid | hedge | none
MIN_DTE, MAX_DTE = 15, 60
MIN_SHORT_PREMIUM = 2.0
TARGET_PCT = 0.50       # exit at 50% of net credit
R_FREE = 0.065
FO_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 Chrome/125 Safari/537.36",
      "Accept": "application/zip", "Referer": "https://www.nseindia.com/"}
CACHE = "/tmp/condor-bhav"


# ------------------------------------------------------------------ greeks --
def _norm_pdf(x): return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def bs_price(spot, strike, t_years, iv, call: bool, r=R_FREE):
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, spot - strike) if call else max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    nd = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    px = spot * nd(d1) - strike * math.exp(-r * t_years) * nd(d2) if call else \
        strike * math.exp(-r * t_years) * nd(-d2) - spot * nd(-d1)
    return max(px, 0.0)


def implied_iv(price, spot, strike, t_years, call: bool):
    lo, hi = 0.01, 5.0
    if price <= 0 or t_years <= 0:
        return None
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


# ------------------------------------------------------------------- data ---
def last_tuesday(year: int, month: int) -> date:
    d = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    while nxt.weekday() != 1:            # Tuesday
        nxt -= timedelta(days=1)
    return d if False else nxt


def fetch_fo(ymd_day: date) -> dict | None:
    """FO bhav rows for one day -> {symbol: {'sto': rows, 'stf': rows}} (cached)."""
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
    """Nearest available strike at-or-beyond target (safer side)."""
    if below:
        cands = [s for s in strikes if s <= target]
        return max(cands) if cands else None
    cands = [s for s in strikes if s >= target]
    return min(cands) if cands else None


# ------------------------------------------------------------------- main ---
def main() -> None:
    today = date(2026, 10, 7)
    start = today - timedelta(days=800)
    print(f"downloading 26mo daily closes for {STOCKS} …", flush=True)
    px = yf.download([s + ".NS" for s in STOCKS], start=start.isoformat(),
                     interval="1d", group_by="ticker", progress=False,
                     threads=True, auto_adjust=False)
    closes = {}
    for s in STOCKS:
        df = px[s + ".NS"].dropna(subset=["Close"])
        closes[s] = df["Close"]

    # monthly cycles: entry = first trading day after each last-Tuesday
    months = []
    y, m = (today - timedelta(days=800)).year, (today - timedelta(days=800)).month
    while date(y, m, 1) < date(today.year, today.month, 1):
        months.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1

    results = {s: [] for s in STOCKS}
    cycle_log = []

    for (y, m) in months:
        exp_day = last_tuesday(y, m)
        # entry = next trading day after expiry (from the stock's own calendar)
        cal = closes[STOCKS[0]]
        after = cal.index[cal.index.date > exp_day]
        if after.empty:
            continue
        entry_day = after[0].date()
        if entry_day >= today:
            continue
        # the monthly expiry this cycle settles on = last Tuesday of NEXT month
        nm_y, nm_m = (y + m // 12, m % 12 + 1)
        settle_day = last_tuesday(nm_y, nm_m)

        bhav = fetch_fo(entry_day)
        if not bhav:
            print(f"  {entry_day}: bhav unavailable — cycle skipped", flush=True)
            continue

        for s in STOCKS:
            book = bhav.get(s)
            if not book or not book["sto"]:
                continue
            # trailing avg monthly move as of entry (24m, no lookahead)
            series = closes[s][closes[s].index.date <= entry_day]
            mclose = series.resample("ME").last().dropna()
            if len(mclose) < 13:
                continue
            avg_move = float((mclose.pct_change().dropna().abs().tail(24).mean())) \
                if len(mclose) >= 13 else None
            if not avg_move:
                continue
            spot = None
            try:                       # parity spot from ATM CE/PE of near expiry
                sto = book["sto"]
                exps = sorted({r["XpryDt"][:10] for r in sto
                               if MIN_DTE <= (date.fromisoformat(r["XpryDt"][:10])
                                              - entry_day).days <= MAX_DTE})
                if not exps:
                    continue
                expiry = exps[0]
                dte = (date.fromisoformat(expiry) - entry_day).days
                strikes_all = sorted({float(r["StrkPric"]) for r in sto
                                      if r["XpryDt"][:10] == expiry})
                atm = min(strikes_all, key=lambda k: abs(k - series.iloc[-1]))
                px_map = {(float(r["StrkPric"]), r["OptnTp"]): float(r["ClsPric"] or 0)
                          for r in sto if r["XpryDt"][:10] == expiry}
                ce, pe = px_map.get((atm, "CE")), px_map.get((atm, "PE"))
                if ce and pe:
                    spot = atm + ce - pe
            except Exception:
                continue
            if not spot or spot <= 0:
                continue

            step = avg_move
            sp_t = spot * (1 - K_SHORT * step)
            sc_t = spot * (1 + K_SHORT * step)
            lp_t = spot * (1 - K_HEDGE * step)
            lc_t = spot * (1 + K_HEDGE * step)
            ladder = sorted({float(r["StrkPric"]) for r in book["sto"]
                             if r["XpryDt"][:10] == expiry})
            sp = pick(ladder, sp_t, below=True)
            sc = pick(ladder, sc_t, below=False)
            lp = pick(ladder, lp_t, below=True)
            lc = pick(ladder, lc_t, below=False)
            if not (sp and sc and lp and lc) or not (lp < sp < spot < sc < lc):
                continue
            px_map = {(float(r["StrkPric"]), r["OptnTp"]): float(r["ClsPric"] or 0)
                      for r in book["sto"] if r["XpryDt"][:10] == expiry}
            prem = {k: px_map.get((k, o), 0.0)
                    for k, o in ((sp, "PE"), (sc, "CE"), (lp, "PE"), (lc, "CE"))}
            if any(v < MIN_SHORT_PREMIUM for k, v in prem.items() if k in (sp, sc)):
                continue
            credit = prem[sp] + prem[sc] - prem[lp] - prem[lc]
            if credit <= 0:
                continue

            t_years = dte / 365.0
            ivs = {}
            for k, o in ((sp, "PE"), (sc, "CE"), (lp, "PE"), (lc, "CE")):
                iv = implied_iv(prem[k], spot, k, t_years, o == "CE")
                ivs[(k, o)] = iv or 0.55
            put_mid, call_mid = (sp + lp) / 2, (sc + lc) / 2

            # daily simulation entry -> expiry
            path = closes[s][(closes[s].index.date > entry_day)
                             & (closes[s].index.date <= settle_day)]
            pnl = None; exit_reason = "expiry"; exit_day = settle_day
            remaining = t_years
            days_total = max(1, len(path))
            for i, (ts, close) in enumerate(path.items()):
                d = ts.date()
                rem = max(0.0, (settle_day - d).days / 365.0)
                if rem <= 0:
                    break
                value = (bs_price(close, sp, rem, ivs[(sp, "PE")], False)
                         + bs_price(close, sc, rem, ivs[(sc, "CE")], True)
                         + bs_price(close, lp, rem, ivs[(lp, "PE")], False)
                         + bs_price(close, lc, rem, ivs[(lc, "CE")], True))
                if value <= credit * TARGET_PCT:
                    pnl = credit - value; exit_reason = "target-50%"; exit_day = d
                    break
                breached = (close <= put_mid or close >= call_mid if SL_POLICY == "mid"
                            else close <= lp or close >= lc if SL_POLICY == "hedge"
                            else close <= sp or close >= sc if SL_POLICY == "short"
                            else False)
                if breached:
                    pnl = credit - value; exit_reason = f"SL-{SL_POLICY}"; exit_day = d
                    break
            if pnl is None:
                c = float(path.iloc[-1]) if len(path) else spot
                intrinsic = max(0.0, sp - c) + max(0.0, c - sc)
                pnl = credit - intrinsic; exit_reason = "expiry"
            results[s].append({"entry": entry_day.isoformat(), "expiry": settle_day.isoformat(),
                               "credit": round(credit, 2), "pnl": round(pnl, 2),
                               "reason": exit_reason, "spot0": round(spot, 1),
                               "sp": sp, "sc": sc, "lp": lp, "lc": lc,
                               "avg_move": round(avg_move * 100, 2), "dte": dte})
            cycle_log.append(f"  {s:11s} {entry_day} cr {credit:6.2f} → pnl {pnl:+7.2f} "
                             f"({exit_reason}) sp{sp:.0f}/sc{sc:.0f}")

    print("\n".join(cycle_log[-24:]))
    print("\n" + "=" * 78)
    grand = {"trades": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0}
    for s in STOCKS:
        rs = results[s]
        if not rs:
            print(f"{s}: no trades"); continue
        pnl = [r["pnl"] for r in rs]
        wins = [p for p in pnl if p > 0]
        losses = [p for p in pnl if p <= 0]
        gw, gl = sum(wins), -sum(losses)
        reasons = {}
        for r in rs: reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        print(f"\n{s} — {len(rs)} monthly cycles ({rs[0]['entry']} → {rs[-1]['entry']})")
        print(f"  win rate   : {len(wins)}/{len(rs)} = {len(wins)/len(rs)*100:.0f}%")
        print(f"  total P&L  : {sum(pnl):+,.1f} pts  | avg {sum(pnl)/len(rs):+,.2f}/cycle")
        print(f"  avg credit : {sum(r['credit'] for r in rs)/len(rs):.2f} pts/month")
        print(f"  best/worst : {max(pnl):+,.1f} / {min(pnl):+,.1f} pts")
        print(f"  profit fac : {(gw/gl):.2f}" if gl else "  profit fac : inf (no losses)")
        print(f"  exits      : {reasons}")
        grand["trades"] += len(rs); grand["wins"] += len(wins)
        grand["pnl"] += sum(pnl); grand["gross_win"] += gw; grand["gross_loss"] += gl
    print("\n" + "=" * 78)
    print(f"COMBINED: {grand['trades']} cycles | win rate "
          f"{grand['wins']}/{grand['trades']} = {grand['wins']/max(1,grand['trades'])*100:.0f}% | "
          f"total {grand['pnl']:+,.1f} pts | PF "
          f"{grand['gross_win']/max(1e-9, grand['gross_loss']):.2f}")
    json.dump(results, open("/tmp/condor-backtest.json", "w"), indent=1)
    print("saved /tmp/condor-backtest.json")


if __name__ == "__main__":
    main()
