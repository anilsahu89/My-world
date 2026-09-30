#!/usr/bin/env python3
"""BTC weekly theta desk (theta family, Engine B) — v3 "bulletproof shape".

v3 (2026-09-30, after the v2 6-month backtest lost and the user reset the
design): stop selling daily condors. Eat premium the vault way —
  * hawa/chips : SELL the front-week OTM option (pure hawa) and HOLD the
    same-strike NEXT-week option (chips+hawa) as the fence.
  * AVWAP      : anchors at the major 30-day high and major 30-day low;
    anchored VWAPs from those bars form the band. Trade only the FAR side
    (the strike with more room to spot).
  * hedging    : same-strike calendar => MAX LOSS = the debit paid at
    entry (+fees), by construction, even on a 10% day: the short settles
    intrinsic while the long keeps a full week of time value. No stop-loss
    needed, no mid-trade adjustments (vault doctrine) — the risk is
    pre-paid, not managed.
  * eat small  : one structure per week (Monday 13:30-15:29 IST entry
    window), 0.01 BTC. Skip the week unless the fence is cheap:
    debit <= $1,000/1 BTC and front-week hawa >= 45% of the debit
    (the only pocket the 26-week backtest paid in).

Legacy: v2/v1 condor trades already open are still marked and settled by
the old paths below.

Data: Deribit public API (weeklies chain + marks) + Binance klines for the
anchors. USD->INR fixed 88 (paper). Piggybacks the 24/7 gc cadence via
cloud_papertrade.py; state data/cloud_btc_theta_state.json, portal snapshot
data/paper_btc_theta.json.
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

LOT_BTC = 0.01
USD_INR = 88.0
IV_MARGIN = 0.50          # never strike inside 0.5x the weekly implied move
DEBIT_CAP = 1000.0        # $/1 BTC — the week's pre-paid worst case
HAWA_RATIO = 0.45         # front-week premium must be >= 45% of the debit
MAX_EM_PCT = 4.5          # legacy regime gate still applies on entry day
DAILY_LOSS_INR = 3000.0   # circuit breakers (rulebook §5)
WEEKLY_LOSS_INR = 6000.0
ENTRY_WINDOW = (13 * 60 + 30, 15 * 60 + 29)   # IST minutes, Mondays
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
    return {"next_id": 1, "trades": [], "last_entry_date": None,
            "last_entry_week": None}


def write_state(s: dict) -> None:
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=1) + "\n")


def em_and_skips() -> tuple[float | None, list[str]]:
    """20-day avg daily range gate (regime check only in v3)."""
    k = _get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
             "&interval=1d&limit=21")
    ranges = [(float(x[2]) - float(x[3])) / float(x[4]) * 100 for x in k]
    em = sum(ranges[:-1]) / 20
    reasons = []
    if em > MAX_EM_PCT:
        reasons.append(f"EM {em:.1f}% > {MAX_EM_PCT}%")
    if ranges[-2] > 2 * em:
        reasons.append(f"yday range {ranges[-2]:.1f}% > 2x avg {em:.1f}%")
    return em, reasons


def avwap_band() -> tuple[float, float, float] | None:
    """spot + anchored VWAPs from the major 30-day high and low."""
    end = int(time.time() * 1000)
    d = _get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
             f"&interval=1d&endTime={end}&limit=31")
    dailies = [{"ts": c[0], "h": float(c[2]), "l": float(c[3])} for c in d]
    hi = max(dailies[:-1], key=lambda x: x["h"])
    lo = min(dailies[:-1], key=lambda x: x["l"])
    start = min(hi["ts"], lo["ts"]) - 3600_000
    k = _get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
             f"&interval=1h&startTime={start}&limit=1000")
    spot = float(k[-1][4])

    def vwap(anchor_ts):
        pv = v = 0.0
        for c in k:
            if c[0] >= anchor_ts:
                tp = (float(c[2]) + float(c[3]) + float(c[4])) / 3
                pv += tp * float(c[5])
                v += float(c[5])
        return pv / v if v else None

    av_hi, av_lo = vwap(hi["ts"]), vwap(lo["ts"])
    if not av_hi or not av_lo:
        return None
    return spot, av_hi, av_lo


def weekly_expiries() -> tuple[str, str, list[dict], list[dict]]:
    """front weekly (Friday >= 3 days out) + the Friday after, with legs."""
    ins = _get(f"{DERIBIT}/public/get_instruments?currency=BTC&kind=option"
               "&expired=false")["result"]
    by_exp: dict[str, int] = {}
    for i in ins:
        e = i["instrument_name"].split("-")[1]
        dt = datetime.strptime(e, "%d%b%y")
        by_exp[e] = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    horizon = time.time() * 1000 + 3 * 86400 * 1000
    fridays = sorted((e for e, ts in by_exp.items()
                      if ts >= horizon
                      and datetime.strptime(e, "%d%b%y").weekday() == 4),
                     key=lambda e: by_exp[e])
    if len(fridays) < 2:
        raise RuntimeError("need two weekly expiries on the chain")
    front, nxt = fridays[0], fridays[1]
    legs_f = [i for i in ins if i["instrument_name"].split("-")[1] == front]
    legs_n = [i for i in ins if i["instrument_name"].split("-")[1] == nxt]
    return front, nxt, legs_f, legs_n


def ticker(name: str) -> dict:
    return _get(f"{DERIBIT}/public/ticker?instrument_name={name}")["result"]


def _strikes(legs: list[dict], opt: str) -> list[float]:
    return sorted({float(i["instrument_name"].split("-")[2])
                   for i in legs if i["instrument_name"].endswith(opt)})


def grid(x: float, up: bool) -> float:
    return (math.ceil if up else math.floor)(x / 500.0) * 500.0


def open_weekly_calendar(log: list[str]) -> dict | None:
    """One-side same-strike weekly calendar, AVWAP-anchored, gated."""
    band = avwap_band()
    if not band:
        log.append("no AVWAP band (data gap)")
        return None
    spot, av_hi, av_lo = band
    log.append(f"band: anchor-VWAPs {av_lo:,.0f} / {av_hi:,.0f}, spot "
               f"{spot:,.0f}")

    front, nxt, legs_f, legs_n = weekly_expiries()
    atm_c = min(_strikes(legs_f, "C"), key=lambda k: abs(k - spot))
    atm = ticker(f"BTC-{front}-{int(atm_c)}-C")
    und = atm.get("underlying_price") or spot
    iv = atm.get("mark_iv") or 0
    if not iv:
        log.append("no IV on the front weekly")
        return None
    ivw = iv / 100 / math.sqrt(52)
    kc_t = max(av_hi, spot * (1 + IV_MARGIN * ivw))
    kp_t = min(av_lo, spot * (1 - IV_MARGIN * ivw))
    side = "call" if abs(kc_t - spot) >= abs(kp_t - spot) else "put"
    suf = "C" if side == "call" else "P"
    target = kc_t if side == "call" else kp_t

    # pick the listed strike nearest the target (on the far side of it)
    def listed(legs, outward_up):
        ks = _strikes(legs, suf)
        hit = [x for x in ks if (x >= target if outward_up else x <= target)]
        return (min if outward_up else max)(hit) if hit else None

    k = listed(legs_f, side == "call")
    kn = listed(legs_n, side == "call")
    if k is None or kn is None:
        log.append(f"no listed {side} strike near ${target:,.0f} on both "
                   "weeklies")
        return None
    k = (max if side == "call" else min)(k, kn)   # strike valid on BOTH weeks
    log.append(f"far side: {side} @ {k:,.0f} (call band {kc_t:,.0f} / "
               f"put band {kp_t:,.0f})")

    sh_px = ticker(f"BTC-{front}-{int(k)}-{suf}")
    lo_px = ticker(f"BTC-{nxt}-{int(k)}-{suf}")
    hawa = (sh_px.get("best_bid_price") or 0) * und      # we sell, take bid
    long_ask = (lo_px.get("best_ask_price") or 0) * und  # we buy, pay ask
    debit = long_ask - hawa
    if debit > DEBIT_CAP or hawa < HAWA_RATIO * debit:
        log.append(f"SKIP: fence debit ${debit:.0f} vs hawa ${hawa:.0f} "
                   f"(need debit <= ${DEBIT_CAP:.0f} and hawa >= "
                   f"{HAWA_RATIO:.0%} of debit)")
        return None

    fees = (min(0.0003 * und, 0.125 * max(hawa, 1e-9))
            + min(0.0003 * und, 0.125 * max(long_ask, 1e-9)))
    log.append(f"OPEN weekly-cal {side} {int(k)}{suf}: sell {front} hawa "
               f"${hawa:.0f}, buy {nxt} fence ${long_ask:.0f}, debit "
               f"${debit:.0f} (max loss this week, pre-paid)")
    return {"id": None, "entry_at": now().isoformat()[:16].replace("T", " "),
            "expiry": front, "hedge_expiry": nxt, "lot": LOT_BTC,
            "structure": "weekly-cal", "side": side,
            "strikes": {"k": k}, "iv_entry": round(iv, 1),
            "spot_entry": round(und, 0), "debit_usd": round(debit, 1),
            "hawa_usd": round(hawa, 1), "fees_usd": round(fees, 1),
            "status": "OPEN", "exit_at": None, "close_cost_usd": None,
            "reason": None, "pnl_inr": None, "mark_usd": -debit,
            "spot_now": round(und, 0)}


def _leg_names(trade: dict) -> list[str]:
    if trade.get("structure") == "weekly-cal":
        k = int(trade["strikes"]["k"])
        suf = "C" if trade["side"] == "call" else "P"
        return [f"BTC-{trade['expiry']}-{k}-{suf}",
                f"BTC-{trade['hedge_expiry']}-{k}-{suf}"]
    s, front = trade["strikes"], trade["expiry"]
    nxt = trade.get("hedge_expiry")
    wing_exp = nxt if (trade.get("structure") == "cal-hedge" and nxt) else front
    return [f"BTC-{front}-{int(s['sc'])}-C", f"BTC-{front}-{int(s['sp'])}-P",
            f"BTC-{wing_exp}-{int(s['wc'])}-C",
            f"BTC-{wing_exp}-{int(s['wp'])}-P"]


def mark_trade(trade: dict, log: list[str]) -> None:
    """Mark only. v3 has no stops — the risk was pre-paid at entry."""
    px = [ticker(n) for n in _leg_names(trade)]
    und = px[0].get("underlying_price") or trade["spot_entry"]
    trade["spot_now"] = round(und, 0)
    if trade.get("structure") == "weekly-cal":
        bid_sh = (px[0].get("best_bid_price") or 0) * und
        ask_sh = (px[0].get("best_ask_price") or 0) * und
        bid_lo = (px[1].get("best_bid_price") or 0) * und
        # mark: unwind now = sell the fence at bid, buy hawa back at ask
        trade["mark_usd"] = round(bid_lo - ask_sh - trade["debit_usd"], 1)
        return
    # legacy condor marks + 50%/2x exits
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
    und = ticker(_leg_names(trade)[0]).get("underlying_price") \
        or trade["spot_entry"]
    trade["spot_now"] = round(und, 0)
    if trade.get("structure") == "weekly-cal":
        k, suf = trade["strikes"]["k"], "C" if trade["side"] == "call" else "P"
        intrinsic = max(0, und - k) if suf == "C" else max(0, k - und)
        fence_px = ticker(f"BTC-{trade['hedge_expiry']}-{int(k)}-{suf}")
        salvage = (fence_px.get("best_bid_price") or 0) * und
        fees = trade.get("fees_usd", 0) \
            + min(0.0003 * und, 0.125 * max(salvage, 1e-9)) \
            + (0.00015 * und if intrinsic > 0 else 0)
        pnl = (-trade["debit_usd"] - intrinsic + salvage - fees) \
            * trade["lot"] * USD_INR
        log.append(f"settle {trade['expiry']}: hawa intrinsic ${intrinsic:.0f}"
                   f", fence salvaged ${salvage:.0f}")
        _close(trade, intrinsic, "EXPIRY", pnl, log)
        return
    s = trade["strikes"]
    cost = (max(0, und - s["sc"]) + max(0, s["sp"] - und)
            - max(0, und - s["wc"]) - max(0, s["wp"] - und))
    cost = min(cost, (s["wc"] - s["sc"]))
    if trade.get("structure") == "cal-hedge" and trade.get("hedge_expiry"):
        try:
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
                mark_trade(tr, log)
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

    mins = now().hour * 60 + now().minute
    this_week = now().date().isocalendar()[:2]
    week_key = f"{this_week[0]}-W{this_week[1]:02d}"
    if not any(t["status"] == "OPEN" for t in trades) \
            and state.get("last_entry_week") != week_key \
            and now().weekday() == 0 \
            and ENTRY_WINDOW[0] <= mins <= ENTRY_WINDOW[1]:
        reasons = []
        if realized_today <= -DAILY_LOSS_INR:
            reasons.append(f"daily breaker Rs{realized_today:,.0f}")
        if realized_week <= -WEEKLY_LOSS_INR:
            reasons.append(f"weekly breaker Rs{realized_week:,.0f}")
        if not reasons:
            try:
                _, em_reasons = em_and_skips()
                if em_reasons:
                    log.extend(em_reasons)
                else:
                    trade = open_weekly_calendar(log)
                    if trade:
                        trade["id"] = state["next_id"]
                        state["next_id"] += 1
                        trades.append(trade)
                        state["last_entry_week"] = week_key
            except Exception as e:
                log.append(f"entry failed: {e}")
        else:
            log.extend(reasons)
    elif not any(t["status"] == "OPEN" for t in trades):
        log.append("no position; weekly entry window is Monday 13:30-15:29 IST")

    write_state(state)
    closed = [t for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if (t["pnl_inr"] or 0) > 0]
    OUT.write_text(json.dumps({
        "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
        "status": "RUNNING",
        "strategy": {"rule": "BTC weekly theta, 0.01 BTC — v3 bulletproof shape",
                     "strikes": "sell front-week hawa outside the 30-day "
                                "AVWAP band (major high/low anchors), hold "
                                "the same-strike next-week option as the "
                                "fence (chips+hawa)",
                     "risk": "max loss = debit paid at entry (pre-paid, "
                             "no stops) — gated to <= $1,000/1 BTC with "
                             "hawa >= 45% of debit, else skip the week",
                     "exits": "front-week settles at expiry; fence sold "
                              "back at bid (salvage). No adjustments.",
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
