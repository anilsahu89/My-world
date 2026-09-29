#!/usr/bin/env python3
"""BTC 0DTE premium-eating paper desk (theta family, Engine B).

Rules = BTC_0DTE_RULES.md (2026-09-29):
  iron condor on the Deribit daily expiry (settles 08:00 UTC), shorts at
  1x the 20-day average daily range beyond spot, wings ~$1,000 further;
  0.01 BTC per condor; enter only in the 13:30-13:59 IST window; exits =
  50% of credit / 2x credit stop / expiry settlement; skip if the range
  estimate is broken (EM > 4.5%), yesterday's range > 2x average, implied
  vol too cheap vs the expected move, or a circuit breaker is tripped
  (daily -Rs3,000 / weekly -Rs6,000 on the Rs1L paper book).

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

LOT_BTC = 0.01            # per-condor size (rulebook §5)
USD_INR = 88.0            # paper FX approximation
RANGE_MULT = 1.0          # shorts at 1x expected move beyond spot
WING_USD = 1000.0         # wing distance target
MAX_EM_PCT = 4.5          # skip: vol regime broken
MIN_CREDIT_USD = 150.0    # per 1 BTC, else the risk isn't paid
IV_EM_FLOOR = 0.75        # skip when implied daily move < 0.75 x EM
DAILY_LOSS_INR = 3000.0   # circuit breakers (rulebook §5)
WEEKLY_LOSS_INR = 6000.0
ENTRY_WINDOW = (13 * 60 + 30, 13 * 60 + 59)   # IST minutes
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


def daily_chain() -> tuple[str, list[dict]]:
    """The active daily expiry (next 08:00 UTC >= 20h out) + its instruments."""
    ins = _get(f"{DERIBIT}/public/get_instruments?currency=BTC&kind=option"
               "&expired=false")["result"]
    horizon = time.time() * 1000 + 20 * 3600 * 1000
    daily = min((i for i in ins if i["expiration_timestamp"] >= horizon),
                key=lambda i: i["expiration_timestamp"])
    exp = daily["instrument_name"].split("-")[1]
    legs = [i for i in ins if i["instrument_name"].split("-")[1] == exp]
    return exp, legs


def ticker(name: str) -> dict:
    return _get(f"{DERIBIT}/public/ticker?instrument_name={name}")["result"]


def _strikes(legs: list[dict], opt: str) -> list[float]:
    return sorted({float(i["instrument_name"].split("-")[2])
                   for i in legs if i["instrument_name"].endswith(opt)})


def _nearest(vals: list[float], target: float, above: bool) -> float:
    if above:
        return min(v for v in vals if v >= target)
    return max(v for v in vals if v <= target)


def open_position(spot: float, em_pct: float, log: list[str]) -> bool:
    """Try to open the daily condor. Returns True if opened."""
    exp, legs = daily_chain()
    calls, puts = _strikes(legs, "C"), _strikes(legs, "P")
    if not calls or not puts:
        log.append("no strikes on daily chain")
        return False
    em = em_pct / 100
    sc = _nearest(calls, spot * (1 + RANGE_MULT * em), above=True)
    sp = _nearest(puts, spot * (1 - RANGE_MULT * em), above=False)
    wc = _nearest(calls, sc + WING_USD, above=True)
    wp = _nearest(puts, sp - WING_USD, above=False)
    if wc == sc or wp == sp:
        log.append("wing strike unavailable")
        return False
    names = {"sc": f"BTC-{exp}-{int(sc)}-C", "sp": f"BTC-{exp}-{int(sp)}-P",
             "wc": f"BTC-{exp}-{int(wc)}-C", "wp": f"BTC-{exp}-{int(wp)}-P"}
    px = {k: ticker(v) for k, v in names.items()}
    und = px["sc"].get("underlying_price") or spot
    # conservative fills: sell shorts at bid, buy wings at ask (in USD)
    bid = {k: (v.get("best_bid_price") or 0) * und for k, v in px.items()}
    ask = {k: (v.get("best_ask_price") or 0) * und for k, v in px.items()}
    credit = bid["sc"] + bid["sp"] - ask["wc"] - ask["wp"]
    iv = px["sc"].get("mark_iv") or 0
    iv_daily = (iv / 100) / math.sqrt(365) * 100
    if iv_daily < IV_EM_FLOOR * em_pct:
        log.append(f"IV daily {iv_daily:.1f}% < {IV_EM_FLOOR}x EM {em_pct:.1f}% "
                   "(premium underpriced)")
        return False
    if credit < MIN_CREDIT_USD:
        log.append(f"credit ${credit:.0f} < ${MIN_CREDIT_USD:.0f}")
        return False
    trade = {"id": None, "entry_at": now().isoformat()[:16].replace("T", " "),
             "expiry": exp, "lot": LOT_BTC,
             "strikes": {"sc": sc, "sp": sp, "wc": wc, "wp": wp},
             "iv_entry": round(iv, 1), "spot_entry": round(und, 0),
             "credit_usd": round(credit, 1), "status": "OPEN",
             "exit_at": None, "close_cost_usd": None, "reason": None,
             "pnl_inr": None, "mark_usd": 0.0}
    log.append(f"OPEN condor {int(sc)}C/{int(sp)}P wings {int(wc)}/{int(wp)} "
               f"exp {exp} credit ${credit:.0f} IV {iv:.0f}")
    return trade


def mark_and_exit(trade: dict, log: list[str]) -> None:
    """Mark the open condor; apply 50% / 2x / expiry exits."""
    names = [f"BTC-{trade['expiry']}-{int(v)}-{s}"
             for v, s in ((trade["strikes"]["sc"], "C"),
                          (trade["strikes"]["sp"], "P"),
                          (trade["strikes"]["wc"], "C"),
                          (trade["strikes"]["wp"], "P"))]
    px = [ticker(n) for n in names]
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
    """Approximate settlement: intrinsic vs the current underlying at the
    first tick after expiry (paper approximation, documented)."""
    und = ticker(f"BTC-{trade['expiry']}-"
                 f"{int(trade['strikes']['sc'])}-C").get("underlying_price") \
        or trade["spot_entry"]
    s = trade["strikes"]
    cost = (max(0, und - s["sc"]) + max(0, s["sp"] - und)
            - max(0, und - s["wc"]) - max(0, s["wp"] - und))
    cost = min(cost, (s["wc"] - s["sc"]))  # wings cap the loss
    pnl = (trade["credit_usd"] - cost) * trade["lot"] * USD_INR
    _close(trade, cost, "EXPIRY", pnl, log)


def tick() -> None:
    state = read_state()
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
        "strategy": {"rule": "BTC daily-expiry iron condor, 0.01 BTC/condor",
                     "strikes": "shorts at 1x 20-day avg range beyond spot, wings ~$1000",
                     "exits": "50% credit | 2x credit stop | expiry",
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
