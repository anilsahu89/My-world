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
SHADOW_FILE = ROOT / "data" / "future_arb_shadow.json"
SHADOW_MAX_OPEN = 3


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


# ---------------------------------------------------- bhav self-fetch -------

FO_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
CM_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36"


def _ensure_bhav(today: date, lookback: int = 7) -> int:
    """Download missing CM+FO bhavcopy zips for the last `lookback` weekdays
    into data/raw. The desk reads LOCAL zips and they were only ever produced
    by the retired Mac launchd job — on GitHub Actions the desk starved
    (stuck on its first day's bhav). Runs before every scan."""
    import time as _time
    import urllib.request
    from datetime import timedelta

    raw = BASE / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    got = 0
    for i in range(lookback):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ymd = d.strftime("%Y%m%d")
        for kind, url in (("cm", CM_URL), ("fo", FO_URL)):
            path = raw / f"{kind}_{ymd}.zip"
            if path.exists() and path.stat().st_size > 20_000:
                continue
            try:
                req = urllib.request.Request(
                    url.format(yyyymmdd=ymd),
                    headers={"User-Agent": UA, "Accept": "application/zip",
                             "Referer": "https://www.nseindia.com/"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read()
                if body.startswith(b"PK"):
                    path.write_bytes(body)
                    got += 1
                    _time.sleep(0.6)
            except Exception:
                continue    # 404 = not published yet; other failures retry next run
    if got:
        print(f"bhav self-fetch: {got} new zip(s)", flush=True)
    return got


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
    """NSE-200 stocks in the raw pattern with per-rule verdicts
    (verdicts computed against the LIVE preset, not a hardcoded one)."""
    import config as config_mod
    rl = config_mod.load().rules
    n200 = {s.strip() for s in NSE200.read_text().splitlines()
            if s.strip() and not s.startswith("symbol")}
    spots, futs = _load_day(day)
    if not spots:
        return []
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
        # display prefilter (7 Oct): rows with no realistic path to ever
        # passing cluttered the board — near-zero discounts and dead-far-leg
        # names. Trading gates below are untouched.
        if basis / s < 0.003 or (nxt["vol"] < 100 and basis < 10):
            continue
        ok = (basis >= rl.min_basis
              and basis / s * 100 >= rl.min_basis_pct
              and spread <= basis * rl.max_spread_to_basis
              and spread * cur["lot"] >= rl.min_spread_value
              and cur["vol"] >= rl.min_current_volume
              and nxt["vol"] >= rl.min_next_volume
              and rl.min_days_to_expiry <= dte <= rl.max_days_to_expiry)
        fails = []
        if basis < rl.min_basis:
            fails.append(f"basis<{rl.min_basis:g} ({basis:.1f})")
        if basis / s * 100 < rl.min_basis_pct:
            fails.append(f"basis<{rl.min_basis_pct:g}%")
        if spread > rl.max_spread_to_basis * basis:
            fails.append(f"spread>{rl.max_spread_to_basis:g}x-basis")
        if nxt["vol"] < rl.min_next_volume:
            fails.append(f"nextvol<{rl.min_next_volume:g} ({nxt['vol']})")
        if not (rl.min_days_to_expiry <= dte <= rl.max_days_to_expiry):
            fails.append(f"dte {dte}")
        out.append(dict(
            symbol=sym, spot=s, current=c1, next=c2,
            basis=round(basis, 2), spread=round(spread, 2),
            basis_pct=round(basis / s * 100, 2),
            spread_value=round(spread * cur["lot"]), lot=cur["lot"],
            vol_curr=cur["vol"], vol_next=nxt["vol"], dte=dte,
            preset=rl.name, preset_pass=ok,
            verdict=(f"{rl.name} pass" if ok else
                     "rejected: " + "; ".join(fails[:3])),
        ))
    out.sort(key=lambda x: (not x["preset_pass"], -x["basis_pct"]))
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


# ------------------------------------------------------- R2 shadow book ----
# Exploratory paper book: same exit rules as the real desk (convergence or
# expiry; pnl = (curr_exit - entry_curr)*lot + (entry_next - nxt_exit)*lot)
# but LOOSER R2 entry gates, tracked separately so a few weeks of live stats
# can justify or kill a frequency-first rule change. Never touches the real
# ledger or the Rs 1L pilot equity.

def _shadow_load() -> dict:
    book = _read(SHADOW_FILE, None)
    if not isinstance(book, dict) or "open" not in book:
        return {"version": 1, "open": [], "closed": []}
    book.setdefault("open", [])
    book.setdefault("closed", [])
    # dates round-trip as ISO strings through JSON
    from datetime import date as _date
    for pos in book["open"]:
        pos["curr_expiry"] = (_date.fromisoformat(pos["curr_expiry"])
                              if isinstance(pos["curr_expiry"], str)
                              else pos["curr_expiry"])
        pos["next_expiry"] = (_date.fromisoformat(pos["next_expiry"])
                              if isinstance(pos["next_expiry"], str)
                              else pos["next_expiry"])
    return book


def _shadow_save(book: dict) -> None:
    _write(SHADOW_FILE, book)


def shadow_book(bhav_day: date, enter_new: bool, real_open_symbols: set) -> dict:
    import presets as presets_mod

    rl2 = presets_mod.R2
    book = _shadow_load()
    loaded = _load_day(bhav_day)
    spots, futs = (loaded if loaded else ({}, {}))

    # 1) manage exits on the latest available bhav
    still_open = []
    for pos in book["open"]:
        sym = pos["symbol"]
        spot = spots.get(sym)
        quotes = {q["exp"]: q for q in futs.get(sym, [])}
        curr_q = quotes.get(pos["curr_expiry"])
        nxt_q = quotes.get(pos["next_expiry"])
        if spot is None or curr_q is None or nxt_q is None:
            still_open.append(pos)      # no data today — stays open
            continue
        reason = None
        if curr_q["close"] >= spot:
            reason = "convergence"
        elif bhav_day >= pos["curr_expiry"]:
            reason = "expiry"
        if reason:
            pnl = ((curr_q["close"] - pos["entry_curr"]) * pos["lot"]
                   + (pos["entry_next"] - nxt_q["close"]) * pos["lot"])
            book["closed"].append({
                "symbol": sym, "entry_date": pos["entry_date"],
                "exit_date": bhav_day.isoformat(), "reason": reason,
                "pnl": round(pnl, 2),
                "basis_at_entry": pos["basis"], "spread_at_entry": pos["spread"],
            })
            continue
        # refresh mark-to-market fields
        pos["last_spot"] = spot
        pos["last_curr"] = curr_q["close"]
        pos["last_next"] = nxt_q["close"]
        pos["last_spread"] = round(curr_q["close"] - nxt_q["close"], 2)
        pos["last_date"] = bhav_day.isoformat()
        still_open.append(pos)
    book["open"] = still_open

    # 2) fresh R2 entries (only on a same-day bhav; one entry per symbol/day)
    entered = []
    if enter_new and spots and len(book["open"]) < SHADOW_MAX_OPEN:
        import scanner as arb_scanner
        skip = ({p["symbol"] for p in book["open"]}
                | set(real_open_symbols)
                | {c["symbol"] for c in book["closed"]
                   if c["exit_date"] >= bhav_day.isoformat()})
        try:
            cands = arb_scanner.scan_day(
                bhav_day, rl2, BASE / "data" / "raw", exclude=skip)
        except Exception:
            cands = []
        for c in cands:
            if len(book["open"]) >= SHADOW_MAX_OPEN:
                break
            lot = 1
            for q in futs.get(c.symbol, []):
                if q["exp"] == c.curr_expiry and q.get("lot"):
                    lot = int(q["lot"])
                    break
            book["open"].append({
                "symbol": c.symbol, "entry_date": bhav_day.isoformat(),
                "spot": c.spot, "curr_expiry": c.curr_expiry,
                "next_expiry": c.next_expiry,
                "entry_curr": c.curr_future, "entry_next": c.next_future,
                "basis": round(c.basis, 2), "spread": round(c.spread, 2),
                "lot": lot, "preset": rl2.name,
            })
            entered.append(c.symbol)

    wins = [c for c in book["closed"] if (c.get("pnl") or 0) > 0]
    _shadow_save(book)
    return {
        "rule": (f"R2 shadow (exploratory): basis>={rl2.min_basis:g} & >="
                 f"{rl2.min_basis_pct:g}% of spot, spread<={rl2.max_spread_to_basis:g}x basis, "
                 f"next-vol>={rl2.min_next_volume:g}, {rl2.min_days_to_expiry}-"
                 f"{rl2.max_days_to_expiry}d to expiry \u00b7 same exits as the real book "
                 f"\u00b7 1 lot \u00b7 max {SHADOW_MAX_OPEN} open \u00b7 tracked for rule "
                 f"learning only — not in the pilot equity"),
        "entered_today": entered,
        "open": [{"symbol": p["symbol"], "entry_date": p["entry_date"],
                  "basis": p["basis"], "spread": p["spread"],
                  "last_spread": p.get("last_spread"), "dte_exp": str(p["curr_expiry"])}
                 for p in book["open"]],
        "closed": book["closed"][-30:],
        "summary": {"open": len(book["open"]), "closed": len(book["closed"]),
                    "wins": len(wins), "losses": len(book["closed"]) - len(wins),
                    "win_rate": round(100 * len(wins) / len(book["closed"]), 1) if book["closed"] else 0,
                    "pnl": round(sum(c.get("pnl") or 0 for c in book["closed"]), 2)},
    }


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
    try:
        _ensure_bhav(today)
    except Exception as e:
        print(f"bhav self-fetch failed: {e}", flush=True)
    bhav_day = _latest_bhav_day(today)
    if bhav_day is None:
        return {"skipped": "no bhavcopy"}
    state = _read(STATE_FILE, {"done_date": ""})

    import config as config_mod
    import engine as arb_engine
    cfg = config_mod.load()
    rl = cfg.rules

    # pre-run book for the Telegram diff (what opened / closed today)
    prev_open_syms = {r["symbol"] for r in _book()[0]}
    prev_closed_keys = {(r["symbol"], r.get("exit_date"))
                        for r in _book()[1]}

    # engine runs only for the real session day (entries/exits/mtm);
    # a stale bhav just re-publishes the snapshot
    result = {"entered": [], "kill_switch_tripped": False, "open_mtm": 0.0}
    if state.get("done_date") != bhav_day.isoformat() and bhav_day == today:
        try:
            result = arb_engine.run(today, cfg, broker_mode="paper")
        except Exception as e:
            result["error"] = f"engine failed: {type(e).__name__}: {str(e)[:120]}"
            print(f"arb engine failed: {e}", flush=True)

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
                     "spot, or expiry. "
                     + f"Live preset {cfg.preset}: basis>={rl.min_basis:g} & >={rl.min_basis_pct:g}% "
                       f"of spot, spread<={rl.max_spread_to_basis:g}x basis, "
                       f"next-vol>={rl.min_next_volume:g}, entries {rl.min_days_to_expiry}-"
                       f"{rl.max_days_to_expiry} days before expiry. "
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

    # R2 shadow book (rule-learning, never in the pilot equity)
    try:
        snap["shadow"] = shadow_book(
            bhav_day, enter_new=(bhav_day == today),
            real_open_symbols={r["symbol"] for r in opens})
    except Exception as e:
        snap["shadow"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    if result.get("error"):
        snap["status"] = "DEGRADED"
        snap["blocked"] = [result["error"]]
    if (bhav_day - _now_ist().date()).days >= 3:
        snap["status"] = "DEGRADED"
        snap.setdefault("blocked", []).append(
            f"stale bhavcopy ({bhav_day}) — downloads failing on runner")
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
