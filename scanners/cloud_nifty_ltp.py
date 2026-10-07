#!/usr/bin/env python3
"""NIFTY Long-Term Premium desk (paper) — NK sir's rules, video SMky9fADQZw.

Owner-approved 2026-10-03. The idea from the video (t≈723-1250s): far-expiry
(~quarterly-to-yearly) NIFTY options periodically trade at a DISCOUNT to the
value implied by current near-month volatility ("option worth 2500 available
at 1500"). The desk buys that discounted far option and harvests premium
against it with nearer-expiry shorts — buyer at a discount, seller at price,
both edges in one position:

  CALL side (ratio — the video's all-weather structure, profits whether the
  market falls or drifts; only a fast rally beyond the shorts hurts):
     BUY 3 lots of the discounted far CALL
     SELL 6 lots (1:2) of the nearest-weekly CALL just above spot
  PUT side (spread):
     BUY 3 lots of the discounted far PUT
     SELL 3 lots of the nearest-weekly PUT just below spot
  Which side trades = wherever the discount is (video: both a put spread and
  a call ratio were taken the SAME day in a falling market). The 20-week SMA
  is context on the board, not a side gate.

Trade management (video rules, EOD cadence — bhavcopy marks, honest
limitation: his intraday fills are approximated by EOD closes):
  * target +70 NIFTY points (per 225 qty) -> exit          ("70 se 150")
  * time-stop: 4 trading days -> exit                      ("do chaar din")
  * momentum: close beyond short strike +/-150 -> exit     ("fast momentum = cost exit")
  * shorts that expire while the trade is open are rolled to the next
    weekly at the current rule strike                      ("niche ka sell karte rahenge")
  * max 10 new trades per calendar month (owner raised from 4 on 6 Oct —
    trade-frequency-first; was "mahine me chaar baar")
  * max 1 open structure
Sizing: 3 lots (225 qty) per Rs 5L model book — the video's capital story.

Discount detector:
  * spot via put-call parity of the nearest weekly ATM strike (self-contained)
  * near IV = mean BS-implied vol of nearest-expiry strikes within +/-1% spot
  * far legs (DTE >= 45): BS fair at near IV; discount = 1 - close/fair
  * board >= 12%, trade signal >= 20% (his example was ~40%), premium >= Rs 100,
    moneyness calls 0.90-1.15S / puts 0.85-1.10S, volume >= 5

State:  data/cloud_nifty_ltp_state.json   (done_date, trades, month caps)
Output: data/paper_nifty_ltp.json         (board + book, portal-shaped)
Runs:   piggybacked on gc ticks, weekdays, first tick >= 18:45 IST (bhavcopy).
"""
from __future__ import annotations

import csv
import io
import math
import urllib.request
import zipfile
import json
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
RAW_DIR = BASE / "data" / "raw"
STATE_FILE = BASE.parent / "data" / "cloud_nifty_ltp_state.json"
SNAPSHOT_FILE = BASE.parent / "data" / "paper_nifty_ltp.json"

FAR_MIN_DTE = 45          # "long term": beyond current + next month
BOARD_DISCOUNT = 0.04     # show on the board
TRADE_DISCOUNT = 0.05     # owner directive 6 Oct: frequency first (was 20% —
                          # a week of scans showed best 7.4-15.4%, zero trades);
                          # raise the gate back once the book has mileage
MIN_PREMIUM = 100.0       # Rs — points worth harvesting
MIN_VOL = 5               # contracts traded — price must be real
TARGET_PTS = 70.0         # per 225 qty
TIME_STOP_DAYS = 4
MOMENTUM_PTS = 0.0        # close through the short strike = exit at cost
                          # (14-month replay: +150 buffer let the 1:2 ratio
                          # lose up to -873 pts — NK's spec is a COST exit)
MOMENTUM_BUFFER = 25.0    # small buffer past the strike to avoid same-day noise
HARD_STOP_PTS = 70.0      # risk = target ("risk defined before entry");
                          # replay: one-day gaps blow through any EOD exit
SHORT_MULT = 1            # 1:1 spread both sides. The video's call side was a
                          # 1:2 ratio, but that needs the intraday adjustments
                          # this EOD paper desk can't do — one gap = -873 pts.
                          # 1:1 keeps every structure defined-risk.
FAR_MONEYNESS_MAX = 1.05  # far strike within 5% of spot (replay: every
                          # disaster had the far leg 10-13% OTM — a delta-poor
                          # long leg cannot hedge the shorts through a gap)
MAX_TRADES_MONTH = 10
QTY = 225                 # 3 lots x 75
R_FREE = 0.065
IST_WEEKEND = (5, 6)


# ------------------------------------------------------------------ data ----

def _now_ist() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.now()


def read_zip_csv(path: Path):
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return
        with zf.open(names[0]) as raw:
            yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def load_bhav(day) -> dict:
    """NIFTY IDO rows for a date -> {(expiry, strike, opt): {close, vol}}.
    Downloads the FO bhavcopy when missing (same source as the theta desk).
    A saved response must start with the PK zip magic — NSE sometimes returns
    200 with an HTML error page, which would crash read_zip_csv later."""
    ymd = day.strftime("%Y%m%d")
    path = RAW_DIR / f"fo_{ymd}.zip"
    if not path.exists():
        url = ("https://nsearchives.nseindia.com/content/fo/"
               f"BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.nseindia.com/"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read()
            if not body.startswith(b"PK"):
                print(f"bhavcopy {ymd}: not a zip (NSE error page)",
                      flush=True)
                return {}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        except Exception as e:
            print(f"bhavcopy {ymd} unavailable: {e}", flush=True)
            return {}
    opts = {}
    for row in read_zip_csv(path):
        if row.get("TckrSymb") != "NIFTY" or row.get("FinInstrmTp") != "IDO":
            continue
        try:
            close = float(row.get("ClsPric") or 0)
            vol = int(float(row.get("TtlTradgVol") or 0))
            strike = float(row.get("StrkPric") or 0)
        except ValueError:
            continue
        if close <= 0 or strike <= 0:
            continue
        key = (row.get("XpryDt", "")[:10], strike, row.get("OptnTp", ""))
        opts[key] = {"close": close, "vol": vol}
    return opts


def read_json(p: Path, default):
    try:
        import json
        return json.loads(p.read_text())
    except Exception:
        return default


def write_json(p: Path, payload) -> None:
    import json
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=1) + "\n")


# ------------------------------------------------------- option math (BS) ----

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, years, vol, r=R_FREE, kind="CE"):
    if years <= 0 or vol <= 0:
        return max(0.0, (spot - strike) if kind == "CE" else (strike - spot))
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (r + vol * vol / 2) * years) / sq
    d2 = d1 - sq
    if kind == "CE":
        return spot * _ncdf(d1) - strike * math.exp(-r * years) * _ncdf(d2)
    return strike * math.exp(-r * years) * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_implied_vol(price, spot, strike, years, kind, r=R_FREE):
    lo, hi = 0.01, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p = bs_price(spot, strike, years, mid, r, kind)
        if p > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ------------------------------------------------------------- analytics ----

def _dte(expiry: str, day) -> int:
    return (datetime.strptime(expiry, "%Y-%m-%d").date() - day).days


def parity_spot(opts: dict, day):
    """Spot from put-call parity at the nearest expiry's busiest strike."""
    expiries = sorted({k[0] for k in opts if _dte(k[0], day) >= 1})
    if not expiries:
        return None
    near = expiries[0]
    strikes = {k[1] for k in opts if k[0] == near}
    if not strikes:
        return None
    # busiest strike = max combined volume
    best, best_v = None, -1
    for s in strikes:
        v = sum(opts.get((near, s, t), {}).get("vol", 0) for t in ("CE", "PE"))
        if v > best_v:
            best, best_v = s, v
    c = opts.get((near, best, "CE"), {}).get("close")
    p = opts.get((near, best, "PE"), {}).get("close")
    if not c or not p:
        return None
    return best + c - p            # parity: S = K + C - P (r*T tiny at weekly)


def near_iv(opts: dict, day, spot):
    """Mean implied vol of nearest-expiry strikes within +/-1% of spot."""
    expiries = sorted({k[0] for k in opts if _dte(k[0], day) >= 1})
    if not expiries or not spot:
        return None
    near = expiries[0]
    years = max(_dte(near, day), 1) / 365.0
    ivs = []
    for (e, k, t), row in opts.items():
        if e != near or not (0.99 * spot <= k <= 1.01 * spot):
            continue
        if row["close"] < 5 or row["vol"] < 50:
            continue
        intrinsic = max(0.0, (spot - k) if t == "CE" else (k - spot))
        if row["close"] <= intrinsic + 0.5:
            continue
        try:
            ivs.append(bs_implied_vol(row["close"], spot, k, years, t))
        except Exception:
            continue
    if not ivs:
        return None
    iv = sum(ivs) / len(ivs)
    return iv if 0.05 < iv < 1.5 else None


def weekly_20sma():
    """NIFTY 20-week SMA from yfinance dailies (the engine's usual feed)."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance as yf
        import pandas as pd
        df = yf.download("^NSEI", period="400d", interval="1d",
                         progress=False, threads=False, auto_adjust=False)
        if isinstance(df.columns, __import__("pandas").MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df is None or len(df) < 120:
            return None, None
        wk = df["Close"].resample("W-FRI").last().dropna()
        if len(wk) < 21:
            return None, None
        last = float(df["Close"].iloc[-1])
        return last, float(wk.rolling(20).mean().iloc[-1])
    except Exception as e:
        print(f"weekly sma unavailable: {e}", flush=True)
        return None, None


def discount_board(opts: dict, day, spot, iv):
    """Far-expiry legs trading below their near-IV fair value."""
    board = []
    if not spot or not iv:
        return board
    for (exp, k, t), row in opts.items():
        dte = _dte(exp, day)
        if dte < FAR_MIN_DTE or row["vol"] < MIN_VOL:
            continue
        if t == "CE" and not (0.97 * spot <= k <= FAR_MONEYNESS_MAX * spot):
            continue          # OTM-ish only: ITM "discounts" are rate/arb noise,
        if t == "PE" and not ((2 - FAR_MONEYNESS_MAX) * spot <= k <= 1.03 * spot):
            continue          # and the far leg must be delta-rich (<=5% OTM)
        if row["close"] < MIN_PREMIUM:
            continue
        fair = bs_price(spot, k, dte / 365.0, iv, R_FREE, t)
        if fair <= row["close"]:
            continue                      # no discount
        disc = 1 - row["close"] / fair
        if disc < BOARD_DISCOUNT:
            continue
        try:
            own_iv = bs_implied_vol(row["close"], spot, k, dte / 365.0, t)
        except Exception:
            own_iv = None
        board.append(dict(expiry=exp, strike=k, opt=t, dte=dte,
                          price=round(row["close"], 2), vol=row["vol"],
                          fair=round(fair, 1), discount=round(disc, 3),
                          edge_pts=round(fair - row["close"], 1),
                          own_iv=round(own_iv, 3) if own_iv else None))
    board.sort(key=lambda b: -b["discount"])
    return board[:10]


# ------------------------------------------------------------- desk core ----

def _near_leg(opts, day, spot, side):
    """Nearest-weekly short strike per the video rules: call just above spot,
    put just below. Returns (expiry, strike, price) or None."""
    expiries = sorted({k[0] for k in opts if 1 <= _dte(k[0], day) <= 8})
    for exp in expiries:
        t = "CE" if side == "call" else "PE"
        strikes = sorted({k[1] for k in opts
                          if k[0] == exp and k[2] == t
                          and opts[k]["vol"] >= 50})
        if not strikes:
            continue
        if side == "call":
            k = min((s for s in strikes if s >= spot * 1.01), default=None)
        else:
            k = max((s for s in strikes if s <= spot * 0.99), default=None)
        if k is None:
            continue
        row = opts.get((exp, k, t))
        if row and row["close"] >= 1.0:
            return exp, k, row["close"]
    return None


def _month_count(trades, month):
    return sum(1 for t in trades if str(t.get("entry_date", ""))[:7] == month)


def open_trade(trades):
    for t in trades:
        if t.get("status") == "OPEN":
            return t
    return None


def _trade_points(tr) -> float:
    """Realized + unrealized P&L expressed in NIFTY points per 225 qty."""
    pnl = 0.0
    far = tr["far"]
    ref = far["exit"] if far.get("exit") is not None else far.get("mark", far["entry"])
    pnl += (ref - far["entry"]) * far["qty"]
    for s in tr["shorts"]:
        ref = s["exit"] if s.get("exit") is not None else s.get("mark", s["entry"])
        pnl += (s["entry"] - ref) * s["qty"]
    return pnl / QTY


def _mark(tr, opts, day):
    """Refresh mark prices on all open legs from today's bhavcopy."""
    far = tr["far"]
    key = (far["expiry"], far["strike"], "CE" if tr["side"] == "call" else "PE")
    if key in opts:
        far["mark"] = opts[key]["close"]
    for s in tr["shorts"]:
        if s.get("exit") is None:
            k2 = (s["expiry"], s["strike"], "CE" if tr["side"] == "call" else "PE")
            if k2 in opts:
                s["mark"] = opts[k2]["close"]


def _close_trade(state, tr, reason, day_iso, spot):
    far = tr["far"]
    mark = far.get("mark", far["entry"])
    far["exit"] = mark
    far["exit_date"] = day_iso
    pnl = (mark - far["entry"]) * far["qty"]
    for s in tr["shorts"]:
        if s.get("exit") is None:
            s["exit"] = s.get("mark", s["entry"])
            s["exit_date"] = day_iso
        pnl += (s["entry"] - s["exit"]) * s["qty"]
    tr.update(status="CLOSED", exit_date=day_iso, exit_reason=reason,
              exit_spot=spot, pnl=round(pnl, 2),
              points=round(pnl / QTY, 1))
    _notify(state, tr, reason)


def _notify(state, tr, event):
    try:
        import telegram_notify
        emoji = {"OPEN": "⚡", "TARGET": "✅", "TIME": "⏱", "MOMENTUM": "🛑"}.get(event, "•")
        side = "CALL RATIO" if tr["side"] == "call" else "PUT SPREAD"
        if event == "OPEN":
            far, s0 = tr["far"], tr["shorts"][0]
            telegram_notify.notify(
                f"{emoji} OPEN · NIFTY Long-Term Premium ({side})\n"
                f"BUY {far['qty']} {far['expiry']} {far['strike']}{'CE' if tr['side']=='call' else 'PE'} @ ₹{far['entry']:.1f}"
                f" (discount {tr['discount_at_entry']*100:.0f}%)\n"
                f"SELL {s0['qty']}×{len(tr['shorts'])} {s0['expiry']} {s0['strike']} @ ₹{s0['entry']:.1f}\n"
                f"target +{TARGET_PTS:.0f} pts · time-stop {TIME_STOP_DAYS}d · trade #{tr['id']}")
        else:
            telegram_notify.notify(
                f"{emoji} CLOSE · NIFTY LTP {side} ({event})\n"
                f"P&L ₹{tr['pnl']:+,.0f} ({tr['points']:+.0f} pts) · trade #{tr['id']}")
    except Exception:
        pass


def publish_blocked(now, state, reason: str) -> None:
    """Keep the portal JSON alive even when no bhavcopy loads — stale-silent
    days read as a dead desk (Oct 5-7: cloud JSON frozen for 2 days)."""
    p = SNAPSHOT_FILE
    try:
        payload = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        payload = {}
    payload["updated_at"] = now.strftime("%d %b %Y %H:%M IST")
    payload["bhav_date"] = state.get("done_date") or ""
    payload["desk"] = "NIFTY Long-Term Premium"
    payload["status"] = "DEGRADED"
    payload["blocked"] = [reason]
    try:
        write_json(p, payload)
    except Exception as e:
        print(f"publish_blocked failed: {e}", flush=True)


def run_day() -> dict:
    """One EOD update: mark/exit opens, then scan for a new entry."""
    now = _now_ist()
    state = read_json(STATE_FILE, {"done_date": "", "trades": [], "next_id": 1})
    trades = state["trades"]

    day = now.date()
    if now.weekday() in IST_WEEKEND:
        return {"skipped": "weekend"}
    # the bhavcopy being processed: today if published, else walk back over
    # recent days (runners could not fetch some days — never go silent, the
    # 7-day walk mirrors the future-arb desk's proven self-fetch pattern)
    opts, bhav_date, day = None, None, None
    probe = now.date()
    for _ in range(7):
        if probe.weekday() not in IST_WEEKEND:
            cand = load_bhav(probe)
            if cand:
                opts = cand
                bhav_date = probe.isoformat()
                day = probe
                break
            if state.get("done_date") == probe.isoformat():
                break          # nothing newer than what we already processed
        probe -= timedelta(days=1)
    if not opts:
        publish_blocked(now, state,
                        "no bhavcopy available (downloads failing)")
        return {"skipped": "no bhavcopy"}
    if state.get("done_date") == bhav_date:
        return {"skipped": "already done"}

    spot = parity_spot(opts, day)
    iv = near_iv(opts, day, spot)
    wk_spot, wk20 = weekly_20sma()
    bias = 0
    if wk_spot and wk20:
        bias = 1 if wk_spot > wk20 else -1
    board = discount_board(opts, day, spot, iv)

    # ---- manage the open structure first ----
    tr = open_trade(trades)
    if tr:
        tr["days_seen"] = tr.get("days_seen", 0) + 1
        _mark(tr, opts, day)
        pts = _trade_points(tr)
        # shorts that expired today: realize at today's mark
        for s in tr["shorts"]:
            if s.get("exit") is None and s["expiry"] <= bhav_date:
                s["exit"] = s.get("mark", s["entry"])
                s["exit_date"] = bhav_date
        live_short = tr["shorts"][-1]
        if tr["days_seen"] >= TIME_STOP_DAYS:
            _close_trade(state, tr, "TIME", bhav_date, spot)
        elif pts >= TARGET_PTS:
            _close_trade(state, tr, "TARGET", bhav_date, spot)
        elif pts <= -HARD_STOP_PTS:
            _close_trade(state, tr, "STOP", bhav_date, spot)
        elif (tr["side"] == "call" and spot
                and spot >= live_short["strike"] + MOMENTUM_BUFFER):
            _close_trade(state, tr, "MOMENTUM", bhav_date, spot)
        elif (tr["side"] == "put" and spot
                and spot <= live_short["strike"] - MOMENTUM_BUFFER):
            _close_trade(state, tr, "MOMENTUM", bhav_date, spot)
        else:
            # roll expired shorts so the position keeps harvesting
            rolled = any(s.get("exit_date") == bhav_date for s in tr["shorts"])
            if rolled and spot:
                leg = _near_leg(opts, day, spot, tr["side"])
                if leg:
                    exp, k, px = leg
                    qty = QTY * SHORT_MULT
                    tr["shorts"].append(dict(
                        expiry=exp, strike=k, qty=qty, entry=px,
                        mark=px, exit=None, exit_date=None, rolled=True))

    # ---- new entry? (max 1 open, max 4/month; side follows the DISCOUNT —
    # video: both a put spread AND a call ratio were taken the same day in a
    # falling market; the 1:2 call ratio is built to profit either way) ----
    blocked = []
    if open_trade(trades) is None:
        month = bhav_date[:7]
        if _month_count(trades, month) >= MAX_TRADES_MONTH:
            blocked.append(f"month cap ({MAX_TRADES_MONTH}/month)")
        elif not spot or not iv:
            blocked.append("spot/IV unavailable")
        else:
            cands = [b for b in board if b["discount"] >= TRADE_DISCOUNT]
            if not cands:
                # report the TRUE best discount on the chain, even below the
                # board-display gate, so "why no trade" is answerable at a glance
                global BOARD_DISCOUNT
                saved_gate = BOARD_DISCOUNT
                try:
                    BOARD_DISCOUNT = -9
                    all_rows = discount_board(opts, day, spot, iv)
                finally:
                    BOARD_DISCOUNT = saved_gate
                best_pct = max((b["discount"] for b in all_rows), default=0) * 100
                blocked.append(f"no discount >= {TRADE_DISCOUNT:.0%} "
                               f"(best {best_pct:.0f}% of {len(all_rows)} scanned)")
            else:
                best = cands[0]
                want = "call" if best["opt"] == "CE" else "put"
                leg = _near_leg(opts, day, spot, want)
                if not leg:
                    blocked.append("no liquid near-weekly short strike")
                else:
                    exp, k, px = leg
                    q_short = QTY * SHORT_MULT
                    tr = dict(
                        id=state.get("next_id", 1), side=want,
                        entry_date=bhav_date, days_seen=1, status="OPEN",
                        spot_entry=spot, iv_near_entry=round(iv, 3),
                        discount_at_entry=best["discount"],
                        far=dict(expiry=best["expiry"], strike=best["strike"],
                                 qty=QTY, entry=best["price"], mark=best["price"],
                                 exit=None, exit_date=None),
                        shorts=[dict(expiry=exp, strike=k, qty=q_short,
                                     entry=px, mark=px, exit=None, exit_date=None)],
                        pnl=None, points=None, exit_reason=None)
                    trades.append(tr)
                    state["next_id"] = tr["id"] + 1
                    _notify(state, tr, "OPEN")

    state["done_date"] = bhav_date
    write_json(STATE_FILE, state)
    snap = snapshot(state, board, spot, iv, wk_spot, wk20, bias, blocked, bhav_date)
    write_json(SNAPSHOT_FILE, snap)
    return snap


def snapshot(state, board, spot, iv, wk_spot, wk20, bias, blocked, bhav_date):
    trades = state["trades"]
    closed = [t for t in trades if t["status"] == "CLOSED"]
    opens = [t for t in trades if t["status"] == "OPEN"]
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    realized = round(sum(t.get("pnl") or 0 for t in closed), 2)
    unreal = round(sum(_trade_points(t) * QTY for t in opens), 2)
    month = bhav_date[:7]
    return {
        "updated_at": _now_ist().strftime("%d %b %Y %H:%M IST"),
        "bhav_date": bhav_date,
        "desk": "NIFTY Long-Term Premium",
        "status": "RUNNING",
        "strategy": {
            "rule": ("NK sir (video SMky9fADQZw): BUY the discounted far-expiry NIFTY option "
                     "(fair value from near-month IV), SELL nearer-weekly premium against it — "
                     "buyer at discount + seller at price. Discounted CALL -> 1:2 ratio "
                     "(profits if market falls OR rises; only fast momentum hurts). "
                     "Discounted PUT -> spread. Shorts 1:1 (defined risk). "
                     "3 lots (225 qty) per Rs 5L model book."),
            "exits": (f"target +{TARGET_PTS:.0f} pts · time-stop {TIME_STOP_DAYS} trading days · "
                      f"momentum exit on close through the short strike +/-{MOMENTUM_BUFFER:.0f} pt buffer · "
                      "expired shorts rolled to the next weekly (keep selling)"),
            "caps": f"max {MAX_TRADES_MONTH} trades/month · 1 open structure · signal: discount >= {TRADE_DISCOUNT:.0%}",
            "cadence": "EOD bhavcopy marks (intraday fills approximated by closes)",
            "source": "NK Stock Talk session 2026-10-03; rules doc NK_NIFTY_PREMIUM_RULES.md",
        },
        "levels": {"spot": round(spot, 1) if spot else None,
                   "weekly_20sma": round(wk20, 1) if wk20 else None,
                   "near_iv": round(iv, 3) if iv else None,
                   "bias": {1: "above 20W-SMA (call side)", -1: "below (put side)",
                            0: "unknown"}[bias]},
        "board": board,
        "blocked": blocked,
        "open": [{"id": t["id"], "side": t["side"], "entry": t["entry_date"],
                  "days": t["days_seen"], "points": round(_trade_points(t), 1),
                  "far": t["far"], "shorts": t["shorts"]} for t in opens],
        "trades": [{"id": t["id"], "side": t["side"], "entry": t["entry_date"],
                    "exit": t.get("exit_date"), "reason": t.get("exit_reason"),
                    "points": t.get("points"), "pnl": t.get("pnl"),
                    "discount_at_entry": t.get("discount_at_entry")}
                   for t in trades[-30:]],
        "summary": {"trades": len(trades), "closed": len(closed),
                    "open": len(opens),
                    "this_month": _month_count(trades, month),
                    "wins": len(wins),
                    "win_rate": round(100 * len(wins) / len(closed), 1) if closed else 0,
                    "realized": realized, "unrealized": unreal},
    }


def tick() -> None:
    """Piggyback entry: gc ticks call this; work happens on the first
    weekday tick >= 18:45 IST (bhavcopy has landed by then)."""
    n = _now_ist()
    if n.weekday() in IST_WEEKEND:
        return
    if n.hour < 18 or (n.hour == 18 and n.minute < 45):
        return
    try:
        run_day()
    except Exception as e:
        print(f"nifty ltp tick failed: {e}", flush=True)


if __name__ == "__main__":
    import json
    print(json.dumps(run_day(), indent=1, default=str))
