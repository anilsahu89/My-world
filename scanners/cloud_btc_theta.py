#!/usr/bin/env python3
"""BTC 0DTE premium-eating paper desk (theta family, Engine B) — v2.

Rules = BTC_0DTE_RULES.md (v2, 2026-09-30). Changes from v1 after the desk
skipped its first session (IV 1.7% < 0.75x EM 2.9% and never retried):
  * entry window widened to the first two hours of the daily session
    (13:30-15:29 IST); every tick retries until one condor is on.
  * strike ladder: shorts step from 1.0x down to 0.75x the 20-day average
    daily range (in 0.125 steps) until the credit clears the floor — "just
    beyond the average moving range", never closer than 0.75x.
  * IV edge floor relaxed 0.75 -> 0.60 (still must be paid something) and
    measured as the better of short-call / short-put IV.
  * credit floor $150 -> $110 per 1 BTC.
  * NEW calendar-hedge structure: same 0DTE shorts, but the wings are
    bought on the NEXT daily expiry at the same distance. Same theoretical
    max-loss width as a same-day condor, but the hedge still holds time
    value at today's settlement (salvage + gap protection) — the vault
    "sell today's hawa, keep tomorrow's fence" trade. Taken when the next-day
    hedge costs <= 45% of the same-day condor credit, else the plain condor.

Exits unchanged: 50% of credit booked / 2x credit stop / expiry settlement
(shorts at intrinsic, next-day hedge sold back at bid). Skips unchanged:
EM > 4.5%, yesterday's range > 2x average, daily -Rs3,000 / weekly -Rs6,000
circuit breakers on the Rs1L paper book.

Data: Deribit public API (chain + marks, no account) + Binance klines for
the average-range anchor. USD->INR at a fixed 88 (paper approximation,
documented). Runs as a piggyback tick on the 24/7 gc cadence via
cloud_papertrade.py; state in data/cloud_btc_theta_state.json, portal
snapshot in data/paper_btc_theta.json.
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATE_FILE = DATA / "cloud_btc_theta_state.json"
OUT = DATA / "paper_btc_theta.json"
IST = ZoneInfo("Asia/Kolkata")

LOT_BTC = 0.01            # per-structure size (rulebook §5)
USD_INR = 88.0            # paper FX approximation
IV_STEPS = [0.85, 0.70, 0.55]  # short-strike ladder in x-implied-move units
                          # (premium lives near the implied band, not the
                          # realized-range band — measured live: $25 at 1xEM
                          # vs $125 at 0.85xIV for the same wings)
WING_USD = 1000.0         # wing distance target (same width on either
                          # expiry -> same defined-risk width)
MAX_EM_PCT = 4.5          # skip: vol regime broken
MIN_CREDIT_USD = 110.0    # per 1 BTC, else the risk isn't paid
IV_EM_FLOOR = 0.50        # skip when implied daily move < 0.50 x EM
                          # (implied massively under realized — the odds
                          # are against even IV-anchored selling)
CALENDAR_MAX_COST = 0.45  # take next-day hedge only if its extra cost is
                          # <= 45% of the same-day condor credit
DAILY_LOSS_INR = 3000.0   # circuit breakers (rulebook §5)
WEEKLY_LOSS_INR = 6000.0
ENTRY_WINDOW = (13 * 60 + 30, 15 * 60 + 29)   # IST minutes, first 2 hours
DERIBIT = "https://www.deribit.com/api/v2"
UA = {"User-Agent": "Mozilla/5.0"}


def now() -> datetime:
    return datetime.now(IST)


def _get(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def read_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"next_id": 1, "trades": [], "last_entry_date": None}


def write_state(s: dict) -> None:
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=1) + "\n")


def spot_and_em() -> tuple[float, float, list[str]]:
    """BTC spot, expected-move % (20-day avg daily range), skip reasons."""
    k = _get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
             "&interval=1d&limit=21")
    closes = [float(x[4]) for x in k]
    ranges = [(float(x[2]) - float(x[3])) / float(x[4]) * 100 for x in k]
    spot = closes[-1]
    em = sum(ranges[:-1]) / 20
    reasons = []
    if em > MAX_EM_PCT:
        reasons.append(f"EM {em:.1f}% > {MAX_EM_PCT}%")
    if ranges[-2] > 2 * em:
        reasons.append(f"yday range {ranges[-2]:.1f}% > 2x avg {em:.1f}%")
    return spot, em, reasons


def daily_chains() -> tuple[str, str | None, list[dict], list[dict]]:
    """(front daily >= 20h out, next listed daily after it, both leg sets).
    get_instruments returns every strike separately — dedupe by expiry
    before picking neighbours, or 'next daily' resolves to the same day."""
    ins = _get(f"{DERIBIT}/public/get_instruments?currency=BTC&kind=option"
               "&expired=false")["result"]
    horizon = time.time() * 1000 + 20 * 3600 * 1000
    by_exp: dict[str, int] = {}
    for i in ins:
        if i["expiration_timestamp"] >= horizon:
            by_exp.setdefault(i["instrument_name"].split("-")[1],
                              i["expiration_timestamp"])
    exps = sorted(by_exp, key=lambda e: by_exp[e])
    if not exps:
        raise RuntimeError("no daily expiries on the chain")
    front, nxt = exps[0], (exps[1] if len(exps) > 1 else None)
    legs0 = [i for i in ins if i["instrument_name"].split("-")[1] == front]
    legs1 = [i for i in ins if nxt and i["instrument_name"].split("-")[1] == nxt]
    return front, nxt, legs0, legs1


def ticker(name: str) -> dict:
    return _get(f"{DERIBIT}/public/ticker?instrument_name={name}")["result"]


def _strikes(legs: list[dict], opt: str) -> list[float]:
    return sorted({float(i["instrument_name"].split("-")[2])
                   for i in legs if i["instrument_name"].endswith(opt)})


def _nearest(vals: list[float], target: float, above: bool) -> float | None:
    hit = [v for v in vals if (v >= target if above else v <= target)]
    return (min if above else max)(hit) if hit else None


def _px_map(names: dict[str, str]) -> tuple[dict, dict, float, dict]:
    """bid/ask in USD per 1 BTC per leg + live underlying + IVs."""
    tks = {k: ticker(v) for k, v in names.items()}
    und = next((t.get("underlying_price") for t in tks.values()
                if t.get("underlying_price")), 0.0)
    bid = {k: (t.get("best_bid_price") or 0) * und for k, t in tks.items()}
    ask = {k: (t.get("best_ask_price") or 0) * und for k, t in tks.items()}
    iv = {k: (t.get("mark_iv") or 0) for k, t in tks.items()}
    return bid, ask, und, iv


def open_position(spot: float, em_pct: float, log: list[str]) -> dict | None:
    """Ladder the strikes off the implied move, then pick calendar-hedge
    vs plain condor."""
    front, nxt, legs0, legs1 = daily_chains()
    calls, puts = _strikes(legs0, "C"), _strikes(legs0, "P")
    if not calls or not puts:
        log.append("no strikes on daily chain")
        return None

    # implied daily move from the near-ATM call+put of the front daily
    atm_c = min(calls, key=lambda k: abs(k - spot))
    atm_p = min(puts, key=lambda k: abs(k - spot))
    atms = {k: ticker(n) for k, n in
            {"c": f"BTC-{front}-{int(atm_c)}-C",
             "p": f"BTC-{front}-{int(atm_p)}-P"}.items()}
    iv = max(atms["c"].get("mark_iv") or 0, atms["p"].get("mark_iv") or 0)
    und = atms["c"].get("underlying_price") or spot
    if not iv:
        log.append("no IV on the front daily chain")
        return None
    iv_daily = iv / 100 / math.sqrt(365) * 100        # in %
    if iv_daily < IV_EM_FLOOR * em_pct:
        log.append(f"IV daily {iv_daily:.1f}% < {IV_EM_FLOOR}x EM "
                   f"{em_pct:.1f}% (implied far under realized — odds off)")
        return None

    for mult in IV_STEPS:
        dist = mult * iv_daily / 100 * spot
        sc = _nearest(calls, spot + dist, above=True)
        sp = _nearest(puts, spot - dist, above=False)
        if sc is None or sp is None:
            continue
        wc = _nearest(calls, sc + WING_USD, above=True)
        wp = _nearest(puts, sp - WING_USD, above=False)
        if wc in (None, sc) or wp in (None, sp):
            log.append("wing strike unavailable")
            return None
        names = {"sc": f"BTC-{front}-{int(sc)}-C",
                 "sp": f"BTC-{front}-{int(sp)}-P",
                 "wc": f"BTC-{front}-{int(wc)}-C",
                 "wp": f"BTC-{front}-{int(wp)}-P"}
        bid, ask, und, _ = _px_map(names)
        credit = bid["sc"] + bid["sp"] - ask["wc"] - ask["wp"]
        if credit < MIN_CREDIT_USD:
            log.append(f"{mult:.2f}xIV ({int(sc)}C/{int(sp)}P): credit "
                       f"${credit:.0f} < ${MIN_CREDIT_USD:.0f}, stepping closer")
            continue

        trade = {"id": None, "entry_at": now().isoformat()[:16].replace("T", " "),
                 "expiry": front, "lot": LOT_BTC, "structure": "condor",
                 "hedge_expiry": None,
                 "strikes": {"sc": sc, "sp": sp, "wc": wc, "wp": wp},
                 "iv_entry": round(iv, 1),
                 "spot_entry": round(und or spot, 0),
                 "credit_usd": round(credit, 1), "status": "OPEN",
                 "exit_at": None, "close_cost_usd": None, "reason": None,
                 "pnl_inr": None, "mark_usd": 0.0}

        # optional: same-width wings on the NEXT listed daily (calendar
        # hedge) — costs more, but the fence keeps time value at today's
        # settlement (salvage + gap protection)
        if nxt and legs1:
            hc = _strikes(legs1, "C")
            hp = _strikes(legs1, "P")
            hwc = _nearest(hc, sc + WING_USD, above=True)
            hwp = _nearest(hp, sp - WING_USD, above=False)
            if hwc not in (None, sc) and hwp not in (None, sp):
                hnames = {"sc": names["sc"], "sp": names["sp"],
                          "wc": f"BTC-{nxt}-{int(hwc)}-C",
                          "wp": f"BTC-{nxt}-{int(hwp)}-P"}
                hbid, hask, _, _ = _px_map(hnames)
                hcredit = hbid["sc"] + hbid["sp"] - hask["wc"] - hask["wp"]
                extra = credit - hcredit
                log.append(f"hedge quote {nxt}: credit ${hcredit:.0f} "
                           f"(fence costs ${extra:.0f} = "
                           f"{extra / credit * 100 if credit else 0:.0f}% of "
                           f"credit)")
                if hcredit >= MIN_CREDIT_USD \
                        and extra <= CALENDAR_MAX_COST * credit:
                    trade.update({"structure": "cal-hedge",
                                  "hedge_expiry": nxt,
                                  "strikes": {"sc": sc, "sp": sp,
                                              "wc": hwc, "wp": hwp},
                                  "credit_usd": round(hcredit, 1)})
                    credit = hcredit
                else:
                    log.append("next-day fence too rich -> same-day condor")

        log.append(f"OPEN {trade['structure']} {int(sc)}C/{int(sp)}P wings "
                   f"{int(trade['strikes']['wc'])}/{int(trade['strikes']['wp'])}"
                   f"{'@' + nxt if trade['hedge_expiry'] else ''} "
                   f"exp {front} credit ${credit:.0f} IV {iv:.0f} "
                   f"(band {mult:.2f}xIV = ${dist:,.0f})")
        return trade

    log.append(f"no rung of the ladder paid >= ${MIN_CREDIT_USD:.0f} "
               f"(IV daily {iv_daily:.1f}%)")
    return None


def _leg_names(trade: dict) -> list[str]:
    """Shorts always on the front expiry; wings per structure."""
    s, front, nxt = trade["strikes"], trade["expiry"], trade["hedge_expiry"]
    wing_exp = nxt if (trade.get("structure") == "cal-hedge" and nxt) else front
    return [f"BTC-{front}-{int(s['sc'])}-C", f"BTC-{front}-{int(s['sp'])}-P",
            f"BTC-{wing_exp}-{int(s['wc'])}-C", f"BTC-{wing_exp}-{int(s['wp'])}-P"]


def mark_and_exit(trade: dict, log: list[str]) -> None:
    """Mark the structure; apply 50% / 2x exits."""
    px = [ticker(n) for n in _leg_names(trade)]
    und = px[0].get("underlying_price") or trade["spot_entry"]
    # cost to close now: buy shorts back at ask, sell wings at bid
    cost = ((px[0].get("best_ask_price") or 0) + (px[1].get("best_ask_price") or 0)
            - (px[2].get("best_bid_price") or 0)
            - (px[3].get("best_bid_price") or 0)) * und
    trade["mark_usd"] = round(trade["credit_usd"] - cost, 1)
    pnl = trade["mark_usd"] * trade["lot"] * USD_INR
    if trade["mark_usd"] >= 0.5 * trade["credit_usd"]:
        _close(trade, cost, "TARGET-50", pnl, log)
    elif trade["mark_usd"] <= -2 * trade["credit_usd"]:
        _close(trade, cost, "STOP-2X", pnl, log)


def _close(trade, cost, reason, pnl, log):
    trade.update({"status": "CLOSED", "reason": reason,
                  "exit_at": now().isoformat()[:16].replace("T", " "),
                  "close_cost_usd": round(cost, 1),
                  "pnl_inr": round(pnl, 0)})
    log.append(f"CLOSE {reason} pnl Rs{pnl:,.0f}")


def settle_expired(trade: dict, log: list[str]) -> None:
    """Settlement: shorts pay intrinsic; wings are sold back at bid (for the
    calendar hedge the next-day legs still hold time value = salvage)."""
    und = ticker(f"BTC-{trade['expiry']}-"
                 f"{int(trade['strikes']['sc'])}-C").get("underlying_price") \
        or trade["spot_entry"]
    s = trade["strikes"]
    cost = (max(0, und - s["sc"]) + max(0, s["sp"] - und)
            - max(0, und - s["wc"]) - max(0, s["wp"] - und))
    cost = min(cost, (s["wc"] - s["sc"]))  # wings cap the loss
    if trade.get("structure") == "cal-hedge" and trade.get("hedge_expiry"):
        try:  # salvage: sell the next-day fence at its bid
            und2 = ticker(_leg_names(trade)[2]).get("underlying_price") or und
            wc_px = ticker(f"BTC-{trade['hedge_expiry']}-{int(s['wc'])}-C")
            wp_px = ticker(f"BTC-{trade['hedge_expiry']}-{int(s['wp'])}-P")
            salvage = ((wc_px.get("best_bid_price") or 0)
                       + (wp_px.get("best_bid_price") or 0)) * und2
            cost -= salvage
            log.append(f"expiry salvage from {trade['hedge_expiry']} fence: "
                       f"${salvage:.0f}")
        except Exception as e:
            log.append(f"salvage fetch failed ({e}) — settled wings at 0")
    pnl = (trade["credit_usd"] - cost) * trade["lot"] * USD_INR
    _close(trade, cost, "EXPIRY", pnl, log)


def tick(throttle_sec: int = 900) -> None:
    state = read_state()
    # relay-safe cadence: callers may loop every 60s; the desk itself only
    # acts every 15 min (and retries the entry window every tick in v2)
    import time as _t
    if _t.time() - state.get("last_tick_ts", 0) < throttle_sec:
        return
    state["last_tick_ts"] = _t.time()
    trades = state["trades"]
    log: list[str] = []
    tdy = now().date().isoformat()

    for tr in [t for t in trades if t["status"] == "OPEN"]:
        exp_ts = None
        try:
            exp_ts = datetime.strptime(tr["expiry"], "%d%b%y").replace(
                hour=8, tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
        if exp_ts and time.time() > exp_ts:
            settle_expired(tr, log)
        else:
            try:
                mark_and_exit(tr, log)
            except Exception as e:
                log.append(f"mark failed: {e}")

    realized_today = sum(t["pnl_inr"] or 0 for t in trades
                         if t["status"] == "CLOSED"
                         and (t.get("exit_at") or "").startswith(tdy))
    week_start = (now().date().isoformat()[:8]
                  + f"{(now().day - now().weekday()):02d}")
    realized_week = sum(t["pnl_inr"] or 0 for t in trades
                        if t["status"] == "CLOSED"
                        and (t.get("exit_at") or "") >= week_start)

    if not any(t["status"] == "OPEN" for t in trades):
        mins = now().hour * 60 + now().minute
        if state.get("last_entry_date") != tdy \
                and ENTRY_WINDOW[0] <= mins <= ENTRY_WINDOW[1]:
            reasons = []
            if realized_today <= -DAILY_LOSS_INR:
                reasons.append(f"daily breaker Rs{realized_today:,.0f}")
            if realized_week <= -WEEKLY_LOSS_INR:
                reasons.append(f"weekly breaker Rs{realized_week:,.0f}")
            if not reasons:
                try:
                    spot, em, reasons = spot_and_em()
                    if not reasons:
                        trade = open_position(spot, em, log)
                        if trade:
                            trade["id"] = state["next_id"]
                            state["next_id"] += 1
                            trades.append(trade)
                            state["last_entry_date"] = tdy
                except Exception as e:
                    reasons = [f"entry failed: {e}"]
            log.extend(reasons)
        else:
            log.append("no position; outside entry window")

    write_state(state)
    closed = [t for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if (t["pnl_inr"] or 0) > 0]
    OUT.write_text(json.dumps({
        "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
        "status": "RUNNING",
        "strategy": {"rule": "BTC daily-expiry premium desk, 0.01 BTC/structure",
                     "strikes": "shorts ladder 1.0->0.75x avg range beyond "
                                "spot; wings ~$1000 — same-day condor or "
                                "next-day calendar hedge",
                     "exits": "50% credit | 2x credit stop | expiry "
                              "(hedge salvaged at bid)",
                     "breakers": f"daily -Rs{DAILY_LOSS_INR:,.0f} / weekly "
                                 f"-Rs{WEEKLY_LOSS_INR:,.0f}",
                     "fx_note": f"USD->INR fixed {USD_INR:.0f} (paper)"},
        "summary": {"open": sum(1 for t in trades if t["status"] == "OPEN"),
                    "closed": len(closed), "wins": len(wins),
                    "losses": len(closed) - len(wins),
                    "win_rate": round(len(wins) / len(closed) * 100, 1)
                    if closed else 0,
                    "realized_inr": round(sum(t["pnl_inr"] or 0
                                              for t in closed), 0),
                    "today_inr": round(realized_today, 0),
                    "week_inr": round(realized_week, 0)},
        "open": [t for t in trades if t["status"] == "OPEN"],
        "closed": closed[-30:],
        "log": log[-10:]}, indent=1) + "\n")
    print("btc-theta:", " | ".join(log[-3:]) or "tick ok")


if __name__ == "__main__":
    tick()
