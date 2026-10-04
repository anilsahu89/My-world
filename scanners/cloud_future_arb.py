#!/usr/bin/env python3
"""Future Arbitrage desk (paper) — the vault's Arbitage 2.x rules, daily.

Owner: "run from Monday — if any stock falls under the rules, enter and track
on the Paper page." Wraps engine.run() (exit pass -> kill switch -> entry
pass -> MTM) with the config defaults (preset v2.1, Rs 1L pilot, 1 lot,
max 3 concurrent, Rs 15k/day kill-switch), then publishes a portal snapshot:

  data/paper_future_arb.json
    - situation list: every NSE-200 stock in the raw double-discount shape
      (spot > current > next) with the exact filter verdicts — the "har gap
      arbitrage nahi hai" view
    - open spreads (from the papertrades ledgers, with MTM)
    - closed book (realized)
    - summary cards

Runs on the first gc tick >= 18:45 IST (bhavcopy landed), done_date-guarded.
Entries persist in scanners/papertrades/future_arbitaage_open_<day>.csv —
exits are written back into the same row by engine.run on a later day.
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, date
from pathlib import Path

BASE = Path(__file__).parent
ROOT = BASE.parent
STATE_FILE = ROOT / "data" / "cloud_future_arb_state.json"
SNAPSHOT_FILE = ROOT / "data" / "paper_future_arb.json"
NSE200 = BASE / "data" / "nse200_symbols.csv"


def _now_ist() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.now()


def _read(p: Path, default):
    try:
        import json
        return json.loads(p.read_text())
    except Exception:
        return default


def _write(p: Path, payload) -> None:
    import json
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=1, default=str) + "\n")


# ------------------------------------------------------- situation list ----

def _load_day(day: date):
    """spot + futures quotes from the day's CM/FO bhavcopy (None if absent)."""
    ymd = day.strftime("%Y%m%d")
    cm, fo = BASE / "data" / "raw" / f"cm_{ymd}.zip", BASE / "data" / "raw" / f"fo_{ymd}.zip"
    if not (cm.exists() and fo.exists()):
        return None, None

    def rows(kind):
        with zipfile.ZipFile(kind) as zf:
            n = [x for x in zf.namelist() if x.lower().endswith(".csv")][0]
            with zf.open(n) as raw:
                yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))

    spots = {}
    for r in rows(cm):
        if r.get("FinInstrmTp") == "STK" and r.get("SctySrs") == "EQ":
            try:
                spots[r["TckrSymb"]] = float(r["ClsPric"])
            except (TypeError, ValueError):
                pass
    futs = {}
    for r in rows(fo):
        if r.get("FinInstrmTp") != "STF":
            continue
        try:
            e = datetime.strptime(r["XpryDt"][:10], "%Y-%m-%d").date()
            futs.setdefault(r["TckrSymb"], []).append(dict(
                exp=e, close=float(r["ClsPric"]),
                lot=int(float(r.get("NewBrdLotQty") or r.get("LotSz") or 0)),
                vol=int(float(r.get("TtlTradgVol") or 0))))
        except (TypeError, ValueError):
            continue
    return spots, futs


def situation_list(day: date):
    """NSE-200 stocks in the raw pattern with per-rule verdicts."""
    from backtest_future_arbitaage import StrategyRules
    n200 = {s.strip() for s in NSE200.read_text().splitlines()
            if s.strip() and not s.startswith("symbol")}
    spots, futs = _load_day(day)
    if not spots:
        return []
    r20 = StrategyRules()          # v2.0 baseline
    r21_pass = ("basis>=20 & >=1.5%", "spread<=0.5x basis", "next-vol>=500",
                "7-21 dte")
    out = []
    for sym in sorted(set(futs) & (n200 & set(spots))):
        fs = sorted(futs[sym], key=lambda f: f["exp"])
        cur = next((f for f in fs if f["exp"] >= day), None)
        nxt = next((f for f in fs if f["exp"] > cur["exp"]), None) if cur else None
        if not (cur and nxt):
            continue
        s, c1, c2 = spots[sym], cur["close"], nxt["close"]
        if not (c1 < s and c2 < c1):          # core shape only
            continue
        basis, spread = s - c1, c1 - c2
        dte = (cur["exp"] - day).days
        v20_ok = (basis >= r20.min_basis and spread <= basis * r20.max_spread_to_basis
                  and spread * cur["lot"] >= r20.min_spread_value
                  and cur["vol"] >= r20.min_current_volume
                  and nxt["vol"] >= r20.min_next_volume)
        v21_ok = v20_ok and basis >= 20.0 and basis / s >= 0.015 \
            and spread <= 0.5 * basis and nxt["vol"] >= 500 and 7 <= dte <= 21
        fails = []
        if basis < 20: fails.append(f"basis<20 ({basis:.1f})")
        if basis / s < 0.015: fails.append("basis<1.5%")
        if spread > 0.5 * basis: fails.append("spread>half-basis")
        if nxt["vol"] < 500: fails.append(f"nextvol<500 ({nxt['vol']})")
        if not (7 <= dte <= 21): fails.append(f"dte {dte}")
        out.append(dict(
            symbol=sym, spot=s, current=c1, next=c2,
            basis=round(basis, 2), spread=round(spread, 2),
            basis_pct=round(basis / s * 100, 2),
            spread_value=round(spread * cur["lot"]), lot=cur["lot"],
            vol_curr=cur["vol"], vol_next=nxt["vol"], dte=dte,
            v20_pass=v20_ok, v21_pass=v21_ok,
            verdict=("v2.1 pass" if v21_ok else
                     ("v2.0 pass" if v20_ok else "rejected: " + "; ".join(fails[:3]))),
        ))
    out.sort(key=lambda x: (not x["v21_pass"], not x["v20_pass"], -x["basis_pct"]))
    return out


# ------------------------------------------------------------ book read ----

def _ledger_rows():
    rows = []
    for p in sorted((BASE / "papertrades").glob("future_arbitaage_open_*.csv")):
        try:
            with p.open() as fp:
                rows.extend(csv.DictReader(fp))
        except OSError:
            continue
    return rows


def _book():
    rows = _ledger_rows()
    opens = [r for r in rows if r.get("status") == "OPEN"]
    closed = [r for r in rows if r.get("status") == "CLOSED"]
    realized = sum(float(r.get("paper_pnl") or 0) for r in closed)
    return opens, closed, realized


# ------------------------------------------------------------- desk run ----

def _latest_bhav_day(today: date, lookback: int = 7):
    """Most recent calendar day whose CM+FO bhavcopy is available locally."""
    for i in range(lookback):
        d = today - __import__("datetime").timedelta(days=i)
        ymd = d.strftime("%Y%m%d")
        cm, fo = BASE / "data" / "raw" / f"cm_{ymd}.zip", BASE / "data" / "raw" / f"fo_{ymd}.zip"
        # >20KB: NSE 404 pages are a few hundred bytes, real bhavcopies ~200KB+
        if cm.exists() and fo.exists() and cm.stat().st_size > 20_000 \
                and fo.stat().st_size > 20_000:
            return d
    return None


def run_day() -> dict:
    now = _now_ist()
    today = now.date()
    if now.weekday() >= 5:
        return {"skipped": "weekend"}
    bhav_day = _latest_bhav_day(today)
    if bhav_day is None:
        return {"skipped": "no bhavcopy"}
    state = _read(STATE_FILE, {"done_date": ""})

    import config as config_mod
    import engine as arb_engine
    cfg = config_mod.load()

    # pre-run book for the Telegram diff (what opened / closed today)
    prev_open_syms = {r["symbol"] for r in _book()[0]}
    prev_closed_keys = {(r["symbol"], r.get("exit_date"))
                        for r in _book()[1]}

    # engine runs only for the real session day (entries/exits/mtm);
    # a stale bhav just re-publishes the snapshot
    result = {"entered": [], "kill_switch_tripped": False, "open_mtm": 0.0}
    if state.get("done_date") != bhav_day.isoformat() and bhav_day == today:
        result = arb_engine.run(today, cfg, broker_mode="paper")

    opens, closed, realized = _book()
    mtm = float(result.get("open_mtm", 0.0) or 0.0)
    snap = {
        "updated_at": now.strftime("%d %b %Y %H:%M IST"),
        "desk": "Future Arbitrage",
        "status": "RUNNING",
        "preset": cfg.preset,
        "strategy": {
            "rule": ("Vault Arbitage 2.x (ARBITAGE_2_0_RULES.md): when spot > current-month "
                     "future > next-month future and the gaps pass the filters, BUY the "
                     "current-month future and SELL the next-month — collect the basis as it "
                     "converges. Direction-immune. Exit: current future closes back at/above "
                     "spot, or expiry. v2.1 gates: basis>=20 & >=1.5% of spot, spread<=half "
                     "the basis, next-vol>=500, entries 7-21 days before expiry. "
                     "Rs 1L pilot, 1 lot, max 3 concurrent, Rs 15k/day kill-switch."),
            "source": "video SMky9fADQZw: 'not every gap is arbitrage — track the pair's history'",
        },
        "situation": situation_list(bhav_day),
        "open": [{"symbol": r["symbol"], "entry_date": r["paper_entry_date"],
                  "current_expiry": r["current_expiry"], "next_expiry": r["next_expiry"],
                  "entry_spot": r["entry_spot"],
                  "entry_current": r["entry_current_future"],
                  "entry_next": r["entry_next_future"],
                  "basis": r["spot_minus_current"], "spread": r["current_minus_next"],
                  "lot": r["lot"], "spread_value": r["entry_spread_value"]}
                 for r in opens],
        "closed": [{"symbol": r["symbol"], "entry_date": r["paper_entry_date"],
                    "exit_date": r.get("exit_date"), "reason": r.get("exit_reason"),
                    "pnl": float(r.get("paper_pnl") or 0)}
                   for r in closed[-60:]],
        "summary": {"open": len(opens), "closed": len(closed),
                    "realized": round(realized, 2), "mtm": round(mtm, 2),
                    "equity": round(cfg.capital + realized + mtm, 2),
                    "entered_today": result.get("entered", []),
                    "kill_switch": result.get("kill_switch_tripped", False)},
    }
    snap["bhav_date"] = bhav_day.isoformat()

    # Telegram alerts for what this run changed (best-effort)
    try:
        opens_now, closed_now, _ = _book()
        import telegram_notify
        for o in snap["open"]:
            if o["symbol"] not in prev_open_syms:
                telegram_notify.notify(
                    f"⚡ OPEN · Future Arbitrage\n"
                    f"BUY {o['symbol']} {o['current_expiry']} @ ₹{float(o['entry_current']):.2f}\n"
                    f"SELL {o['next_expiry']} @ ₹{float(o['entry_next']):.2f}\n"
                    f"basis ₹{o['basis']} · spread ₹{o['spread']} · lot {o['lot']} "
                    f"(preset {cfg.preset})")
        for c in closed_now:
            key = (c["symbol"], c.get("exit_date"))
            if key not in prev_closed_keys:
                telegram_notify.notify(
                    f"✅ CLOSE · Future Arbitrage\n"
                    f"{c['symbol']} (entered {c['entry_date']})\n"
                    f"reason {(c.get('reason') or '').replace('SL_current_future_matched_or_crossed_spot', 'convergence (current future back at spot)')}\n"
                    f"P&L ₹{float(c.get('pnl') or 0):+,.0f}")
    except Exception as e:
        print(f"arb telegram notify failed: {e}", flush=True)

    _write(SNAPSHOT_FILE, snap)
    if bhav_day == today:
        state["done_date"] = bhav_day.isoformat()
        _write(STATE_FILE, state)
    return snap


def tick() -> None:
    n = _now_ist()
    if n.weekday() >= 5:
        return
    if n.hour < 18 or (n.hour == 18 and n.minute < 45):
        return
    try:
        run_day()
    except Exception as e:
        print(f"future arb tick failed: {e}", flush=True)


if __name__ == "__main__":
    import json
    print(json.dumps(run_day(), indent=1, default=str))
