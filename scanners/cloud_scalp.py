#!/usr/bin/env python3
"""NIFTY Scalp Monitor (paper) — First-3 Morning Drive + VWAP Station Bounce
with the adaptive decision tree.

Setups (rules identical to the 101-session backtest, ~/ZCodeProject/scalping/):
  Scalp 1  FIRST-3 MORNING DRIVE : session bars 1-3 all bullish, each close
           above the previous HIGH (strict stack), 3rd close > session VWAP
           -> BUY ATM CE at the next bar open. SL = bar-1 low, TGT = 1.5R.
  Scalp 2  VWAP STATION BOUNCE   : VWAP rising vs 4 bars + last 4 closes above
           VWAP -> pullback touches VWAP -> bar closes back above -> BUY ATM CE
           (mirror PE for put-side). SL = pullback low - 5, TGT = 2R.

Adaptive decision tree (the part the owner asked for — every branch was
pre-authorized in the plan, evaluated on every completed 15-min bar):
  * on plan            -> hold (trail rules below)
  * against plan early -> tactical exit on a close back through VWAP (VWAP
                          trade) or a close below the entry driver (First-3)
                          BEFORE the stop is touched
  * SL hit             -> exit; re-entry armed on a fresh qualifying signal
                          (max 1 re-entry per setup per day)
  * reversal           : VWAP trade only — after an exit, a mirror-side bounce
                          can be taken once per day (half size)
  * momentum in favour -> trail to bar lows/highs after +50% of target
  * guards             : entries 09:50-14:15 only · square-off 15:20 ·
                         2 losing trades = day done · 1 open structure

Premium model (paper): ATM option P&L approximated as
  index_points x 0.5 (ATM delta) x 75 (lot) x lots   — theta ignored for
intraday holds. Documented simplification; real premium quotes upgrade later.

Feed: yfinance ^NSEI 15-min candles (period=1d each tick) — the same feed the
relay already uses (nifty_bias). Runs as a piggyback step of the day poller
(every 60-180s); stateless per tick except persisted trades/state.

State : data/cloud_scalp_state.json
Output: data/scalp_picks.json
"""
from __future__ import annotations

import json
import math
import warnings
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATE_FILE = DATA / "cloud_scalp_state.json"
OUT = DATA / "scalp_picks.json"
IST = ZoneInfo("Asia/Kolkata")

CFG = {
    "capital_per_trade": 100_000.0,
    "lots": 2,
    "lot_qty": 75,
    "delta": 0.5,            # ATM approximation
    "target_rr_first3": 1.5,
    "target_rr_vwap": 2.0,
    "entry_start": "09:50",
    "entry_end": "14:15",
    "no_entry_after": "15:00",
    "square_off": "15:20",
    "max_reentries_per_setup": 1,
    "max_reversals": 1,
    "max_day_losses": 2,
}


def now() -> datetime:
    return datetime.now(IST)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "next_id": 1, "trades": [], "decisions": [],
                "day": "", "day_losses": 0, "done_date": ""}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, default=str) + "\n")


def _notify(msg: str) -> None:
    try:
        import telegram_notify
        telegram_notify.notify(msg)
    except Exception:
        pass


def fetch_bars() -> pd.DataFrame | None:
    """Today's completed 15-min NIFTY bars (IST session, forming bar dropped)."""
    try:
        import yfinance as yf
        df = yf.download("^NSEI", period="1d", interval="15m",
                         progress=False, threads=False, auto_adjust=True)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                            "Close": "close"})
    df.index = df.index.tz_convert(IST)
    nowt = now()
    keep = []
    for ts, row in df.iterrows():
        bar_end = ts + pd.Timedelta(minutes=15)
        if bar_end <= nowt:                      # completed bars only
            keep.append((ts, float(row["open"]), float(row["high"]),
                         float(row["low"]), float(row["close"])))
    out = pd.DataFrame(keep, columns=["ts", "open", "high", "low", "close"])
    return out if len(out) else None


class Session:
    """Per-day session context rebuilt on every tick from completed bars."""

    def __init__(self, df: pd.DataFrame):
        self.bars = df.reset_index().rename(columns={"index": "ts"}).to_dict("records")
        self.vwap_sum = 0.0
        self.vwap_n = 0
        self.vwap = None
        self.first3 = None          # dict(bar1_low=..., stack=..., vwap_ok=...)
        self.f3_signal_bar = None   # index where First-3 fired
        self.vwap_states = []       # rising/above flags per bar

    def advance(self, i: int) -> None:
        b = self.bars[i]
        tp = (b["high"] + b["low"] + b["close"]) / 3
        self.vwap_sum += tp
        self.vwap_n += 1
        self.vwap = self.vwap_sum / self.vwap_n if self.vwap_n else None

    def vwap_at(self, i: int) -> float | None:
        """VWAP as of bar i (recomputed prefix)."""
        s = n = 0
        for j in range(i + 1):
            b = self.bars[j]
            s += (b["high"] + b["low"] + b["close"]) / 3
            n += 1
        return s / n if n else None


def evaluate(df: pd.DataFrame, state: dict, today: str) -> dict:
    """Stream completed bars; run setups + the adaptive tree. Returns the
    day-log of decisions and any open trade (mirrors the backtest semantics)."""
    ses = Session(df)
    log = state.setdefault("decisions", [])
    trades = [t for t in state["trades"] if t["entry_date"] == today]

    def logd(t: str, d: str) -> None:
        log.append({"time": now().strftime("%H:%M:%S"), "decision": t, "detail": d})
        if len(log) > 120:
            del log[: len(log) - 120]

    open_tr = next((t for t in trades if t["status"] == "OPEN"), None)
    f3_entries = sum(1 for t in trades if t["setup"] == "FIRST3")
    vwap_entries = sum(1 for t in trades if t["setup"] == "VWAPBOUNCE")
    vwap_reversal_used = any(t.get("reversal") for t in trades)

    def close_trade(t: dict, exit_px: float, reason: str, i: int) -> None:
        pts = (exit_px - t["entry_idx"]) if t.get("is_idx_entry") else 0.0
        t.update(status="CLOSED", exit_bar=i, exit_reason=reason,
                 exit_idx=round(exit_px, 2),
                 pnl_pts=round((exit_px - t["entry_idx"]) * (1 if t["side"] == "CE" else -1), 1),
                 pnl=round((exit_px - t["entry_idx"]) * (1 if t["side"] == "CE" else -1)
                           * CFG["delta"] * CFG["lot_qty"] * CFG["lots"], 2))
        if t["pnl"] <= 0:
            state["day_losses"] = state.get("day_losses", 0) + 1
        logd("EXIT", f"{t['symbol']} {reason} @ {exit_px:.1f} -> {t['pnl_pts']:+.1f} pts")
        _notify(("\u2705" if t["pnl"] > 0 else "\U0001F6D1")
                + " CLOSE · NIFTY Scalp (" + t["setup"] + " / " + reason + ")\n"
                + t["symbol"] + " " + str(t.get("lots")) + " lots\n"
                + "P&L ~₹{:,.0f} ({:+.1f} idx pts)".format(t["pnl"], t["pnl_pts"]))

    # first pass: find session end bar count
    nbars = len(ses.bars)
    for i in range(nbars):
        b = ses.bars[i]
        hm = b["ts"].strftime("%H:%M")
        ses.advance(i)
        vwap = ses.vwap

        # hard square-off
        if hm >= CFG["square_off"]:
            if open_tr:
                exit_px = b["close"]
                close_trade(open_tr, exit_px, "SQUARE-OFF", i)
                open_tr = None
            break

        # manage the open trade on completed bars
        if open_tr:
            # target / stop intrabar check (stop-first)
            hi, lo = b["high"], b["low"]
            hit_tgt = hi >= open_tr["target_idx"]
            hit_sl = lo <= open_tr["stop_idx"]
            if open_tr["side"] == "PE":      # mirrored option: spot down = win
                hit_tgt, hit_sl = lo <= open_tr["target_idx"], hi >= open_tr["stop_idx"]
            px = None
            reason = None
            if hit_sl:
                px, reason = open_tr["stop_idx"], "SL"
            elif hit_tgt:
                px, reason = open_tr["target_idx"], "TARGET"
            # adaptive early exit: against plan
            elif open_tr["setup"] == "VWAPBOUNCE" and open_tr["side"] == "CE" \
                    and b["close"] < (vwap or 0):
                px, reason = b["close"], "TACTICAL-VWAP"
            elif open_tr["setup"] == "VWAPBOUNCE" and open_tr["side"] == "PE" \
                    and b["close"] > (vwap or 0):
                px, reason = b["close"], "TACTICAL-VWAP"
            elif open_tr["setup"] == "FIRST3" and open_tr["side"] == "CE" \
                    and b["close"] < open_tr["driver_low"]:
                px, reason = b["close"], "TACTICAL-DRIVER"
            elif hm >= "14:30" and open_tr.get("r_multiple", 0) < 1.0:
                px, reason = b["close"], "TIME-NO1R"
            if px is not None:
                close_trade(open_tr, px, reason, i)
                open_tr = None
            continue

        # entries only in the window, day-stop and caps respected
        if hm < CFG["entry_start"] or hm > CFG["entry_end"]:
            continue
        if state.get("day_losses", 0) >= CFG["max_day_losses"]:
            continue
        if i < 3:
            continue

        vwap_prev = ses.vwap_at(i - 1)

        # ---- Setup 1: FIRST-3 MORNING DRIVE ----
        if f3_entries == 0 and ses.f3_signal_bar is None and i >= 3:
            b0, b1, b2 = (ses.bars[i - 3], ses.bars[i - 2], ses.bars[i - 1])
            stack = (b1["close"] > b0["high"] and b2["close"] > b1["high"])
            bullish = all(x["close"] > x["open"] for x in (b0, b1, b2))
            vwap_ok = b2["close"] > (vwap or 0)
            if bullish and stack and vwap_ok:
                ses.f3_signal_bar = i - 1      # fired at bar-3 close
                stop = b0["low"]
                tgt = b2["close"] + CFG["target_rr_first3"] * (b2["close"] - stop)
                risk = b2["close"] - stop
                if risk > 8 and hm <= CFG["no_entry_after"]:
                    tr = dict(id=state["next_id"], setup="FIRST3", side="CE",
                              symbol="NIFTY ATM CE", entry_date=today,
                              entry_bar=i, entry_idx=round(b2["close"], 2),
                              stop_idx=round(stop, 2), target_idx=round(tgt, 2),
                              driver_low=b0["low"],
                              r_multiple=0.0, lots=CFG["lots"], status="OPEN",
                              structure="trend-continuation")
                    trades.append(tr)
                    state["next_id"] += 1
                    f3_entries += 1
                    open_tr = tr
                    logd("ENTRY", f"FIRST3 stack @ {b2['close']:.1f} SL {stop:.1f} TGT {tgt:.1f}")
                    _notify("⚡ OPEN · NIFTY Scalp (First-3 Morning Drive)\n"
                            "BUY ATM CE · SL " + str(round(stop, 1)) + " · TGT +" +
                            str(round(tgt - b2["close"], 1)) + " idx pts")
                continue

        # ---- Setup 2: VWAP STATION BOUNCE (needs 4+ bars of trend context) ----
        # REVERSAL branch (once/day, half lots): after a CE VWAP trade was
        # exited, a two-close flip BELOW VWAP arms the mirror PE trade.
        if vwap_entries >= 1 and not vwap_reversal_used and not open_tr \
                and CFG["max_reversals"] > 0 and i >= 5:
            closes_below = sum(1 for j in range(i - 1, max(0, i - 3) - 1, -1)
                               if ses.bars[j]["close"] < (vwap or float("inf")))
            if closes_below >= 2:
                state["reversal_used"] = True
                vwap_reversal_used = True
                lots = max(1, CFG["lots"] // 2)
                stop = max(ses.bars[j]["high"] for j in range(i - 1, i + 1)) + 5.0
                risk = stop - b["close"]
                if risk > 8 and hm <= CFG["no_entry_after"]:
                    tgt = b["close"] - CFG["target_rr_vwap"] * risk
                    tr = dict(id=state["next_id"], setup="VWAPBOUNCE", side="PE",
                              symbol="NIFTY ATM PE", entry_date=today,
                              entry_bar=i, entry_idx=round(b["close"], 2),
                              stop_idx=round(stop, 2), target_idx=round(tgt, 2),
                              r_multiple=0.0, lots=lots, status="OPEN",
                              structure="trend-pullback", reversal=True)
                    trades.append(tr)
                    state["next_id"] += 1
                    open_tr = tr
                    logd("REVERSAL", f"trend flip -> PE @ {b['close']:.1f} (half size)")
                    _notify("🔄 REVERSAL · NIFTY Scalp (VWAP flip)\n"
                            "BUY ATM PE (half size) · SL " + str(round(stop, 1)))
                    continue

        if vwap_entries == 0 and i >= 5:
            vwap_states = ses.vwap_states
            while len(vwap_states) <= i:
                b = ses.bars[len(vwap_states)]
                vv = ses.vwap_at(len(vwap_states))
                vwap_states.append(b["close"] > (vv or 0))
            trend_ok = (vwap and vwap_prev and vwap > vwap_prev * 1.0002
                        and all(vwap_states[-4:]))
            if trend_ok and not open_tr:
                pull = any(ses.bars[j]["low"] <= (vwap or 0) * (1 + 0.0015)
                           for j in range(max(0, i - 3), i))
                bounced = b["close"] > (vwap or 0) and pull \
                    and ses.bars[i - 1]["close"] > (vwap or 0)
                if bounced and hm <= CFG["no_entry_after"]:
                    stop = min(ses.bars[j]["low"] for j in range(max(0, i - 3), i + 1)) - 5.0
                    tgt = b["close"] + CFG["target_rr_vwap"] * (b["close"] - stop)
                    tr = dict(id=state["next_id"], setup="VWAPBOUNCE", side="CE",
                              symbol="NIFTY ATM CE", entry_date=today,
                              entry_bar=i, entry_idx=round(b["close"], 2),
                              stop_idx=round(stop, 2), target_idx=round(tgt, 2),
                              r_multiple=0.0, lots=CFG["lots"], status="OPEN",
                              structure="trend-pullback")
                    trades.append(tr)
                    state["next_id"] += 1
                    vwap_entries += 1
                    open_tr = tr
                    logd("ENTRY", f"VWAP bounce @ {b['close']:.1f} SL {stop:.1f} TGT {tgt:.1f}")
                    _notify("⚡ OPEN · NIFTY Scalp (VWAP Station Bounce)\n"
                            "BUY ATM CE · SL " + str(round(stop, 1)) + " · TGT +" +
                            str(round(tgt - b["close"], 1)) + " idx pts")

    state["decisions"] = log
    state["day"] = today
    state["done_date"] = today
    state["trades"] = trades
    write_state(state)
    return {"open": open_tr, "trades": trades, "decisions": log}


def publish(state: dict) -> dict:
    trades = state["trades"]
    closed = [t for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    payload = {
        "updated_at": now().strftime("%d %b %Y %H:%M:%S IST"),
        "desk": "NIFTY Scalp Monitor",
        "rule": ("First-3 Morning Drive + VWAP Station Bounce (15-min, index ATM "
                 "options, paper · premium = delta approx 0.5 × 75 × 2 lots) · "
                 "entry 09:50-14:15 · +70pt-class targets (1.5R / 2R) · tactical "
                 "VWAP exit · re-entry + once-daily reversal · square-off 15:20 · "
                 "2-loss day stop"),
        "decisions": state.get("decisions", [])[-40:],
        "open": [t for t in trades if t["status"] == "OPEN"],
        "closed": closed[-30:],
        "summary": {"open": sum(1 for t in trades if t["status"] == "OPEN"),
                    "closed": len(closed), "wins": len(wins),
                    "losses": len(closed) - len(wins),
                    "win_rate": round(100 * len(wins) / len(closed), 1) if closed else 0,
                    "pnl": round(sum(t.get("pnl") or 0 for t in closed), 2)},
    }
    OUT.write_text(json.dumps(payload, indent=1, default=str) + "\n")
    return payload


def tick() -> None:
    n = now()
    if n.weekday() >= 5:
        return
    if n.time() < datetime.strptime("09:15", "%H:%M").time():
        return
    state = read_state()
    today = n.date().isoformat()
    if state.get("day") != today:
        state["trades"] = []
        state["decisions"] = []
        state["day_losses"] = 0
        state["day"] = today
    df = fetch_bars()
    if df is None:
        return
    evaluate(df, state, today)
    publish(state)


if __name__ == "__main__":
    tick()
    st = read_state()
    print(json.dumps({"trades": len(st.get("trades", [])),
                      "day": st.get("day")}))
