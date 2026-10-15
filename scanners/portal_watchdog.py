#!/usr/bin/env python3
"""Portal watchdog — check every paper-trade tab and apply safe auto-fixes.

Runs on a schedule (GitHub Actions) or locally:

  python3 scanners/portal_watchdog.py              # check + fix
  python3 scanners/portal_watchdog.py --check-only # report only
  python3 scanners/portal_watchdog.py --fix=nse  # force one desk

Writes data/portal_health.json for the portal / race board.
Never invents trades or changes strategy rules — only restarts feeds,
forces desk ticks, and re-lights a dead relay.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
IST = ZoneInfo("Asia/Kolkata")
HEALTH_FILE = DATA / "portal_health.json"

TABS = [
    {
        "id": "nse_ol_oh_f3",
        "label": "Intraday O=L/O=H + F3",
        "path": DATA / "paper_ol.json",
        "max_age_min": 45,
        "live_window": (time(9, 15), time(15, 45)),
        "weekdays_only": True,
        "fix": "nse",
    },
    {
        "id": "gc",
        "label": "Gold / BTC (GC desk)",
        "path": DATA / "paper_gc.json",
        "max_age_min": 35,
        "live_window": None,
        "weekdays_only": False,
        "fix": "gc",
    },
    {
        "id": "btc_theta",
        "label": "BTC Theta",
        "path": DATA / "paper_btc_theta.json",
        "max_age_min": 35,
        "live_window": None,
        "weekdays_only": False,
        "fix": "btc_theta",
    },
    {
        "id": "qm",
        "label": "QM Swing",
        "path": DATA / "paper_qm.json",
        "max_age_min": 90,
        "live_window": (time(16, 0), time(22, 0)),
        "weekdays_only": True,
        "fix": "qm",
    },
    {
        "id": "swing",
        "label": "Candle Swing",
        "path": DATA / "swing_picks.json",
        "max_age_min": 90,
        "live_window": (time(17, 30), time(22, 0)),
        "weekdays_only": True,
        "fix": "swing",
    },
    {
        "id": "ltp",
        "label": "NIFTY Long-Term Premium",
        "path": DATA / "paper_nifty_ltp.json",
        "max_age_min": 90,
        "live_window": (time(17, 30), time(22, 0)),
        "weekdays_only": True,
        "fix": "ltp",
    },
    {
        "id": "arb",
        "label": "Future Arbitrage",
        "path": DATA / "paper_future_arb.json",
        "max_age_min": 90,
        "live_window": (time(17, 30), time(22, 0)),
        "weekdays_only": True,
        "fix": "arb",
    },
]

RELAY_STATUS = DATA / "relay_status.json"
HEARTBEAT_STALE_S = 25 * 60


def now() -> datetime:
    return datetime.now(IST)


def parse_updated_at(raw) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in (
        "%d %b %Y %H:%M:%S IST",
        "%d %b %Y %H:%M IST",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s.replace("Z", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.astimezone(IST)
        except ValueError:
            continue
    try:
        return datetime.fromtimestamp(float(s), tz=IST)
    except (TypeError, ValueError):
        return None


def in_window(moment: datetime, window, weekdays_only: bool) -> bool:
    if weekdays_only and moment.weekday() >= 5:
        return False
    if window is None:
        return True
    start, end = window
    t = moment.time()
    return start <= t <= end


def load_json(path: Path):
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception as e:
        print(f"watchdog: read {path.name} failed: {e}", flush=True)
        return None


def check_tab(tab: dict, moment: datetime) -> dict:
    path: Path = tab["path"]
    data = load_json(path)
    result = {
        "id": tab["id"],
        "label": tab["label"],
        "path": str(path.relative_to(ROOT)),
        "status": "ok",
        "age_min": None,
        "updated_at": None,
        "detail": "",
        "needs_fix": False,
        "fix": tab.get("fix"),
    }
    if data is None:
        result["status"] = "missing"
        result["detail"] = "snapshot file missing or unreadable"
        result["needs_fix"] = in_window(moment, tab["live_window"], tab["weekdays_only"])
        return result
    if not isinstance(data, dict):
        result["status"] = "bad"
        result["detail"] = "snapshot is not a JSON object"
        result["needs_fix"] = True
        return result

    updated_raw = data.get("updated_at") or data.get("asof") or data.get("scanned_at")
    updated = parse_updated_at(updated_raw)
    if updated is None and data.get("date"):
        updated = parse_updated_at(str(data.get("date")))
    result["updated_at"] = updated_raw

    live = in_window(moment, tab["live_window"], tab["weekdays_only"])
    if updated is None:
        result["status"] = "unknown"
        result["detail"] = "no parseable updated_at"
        result["needs_fix"] = live
        return result

    age_min = (moment - updated).total_seconds() / 60.0
    result["age_min"] = round(age_min, 1)

    if live and age_min > tab["max_age_min"]:
        result["status"] = "stale"
        result["detail"] = f"stale {age_min:.0f}m (limit {tab['max_age_min']}m in live window)"
        result["needs_fix"] = True
        return result

    errs = data.get("engine_errors") or []
    if errs and live:
        result["status"] = "warn"
        result["detail"] = f"engine_errors: {errs[-1]}"
        if age_min > tab["max_age_min"] * 0.7:
            result["needs_fix"] = True
        return result

    open_count = data.get("summary", {}).get("open_count")
    if open_count is None and isinstance(data.get("open"), list):
        open_count = len(data["open"])
    if open_count is not None:
        result["open_count"] = open_count

    result["detail"] = f"ok age={age_min:.0f}m"
    return result


def check_relay(moment: datetime) -> dict:
    out = {
        "id": "relay",
        "label": "Market poller / relay",
        "status": "ok",
        "detail": "",
        "needs_fix": False,
        "fix": "relay",
    }
    candidates = []
    st = load_json(RELAY_STATUS)
    if isinstance(st, dict) and st.get("updated_at"):
        u = parse_updated_at(st["updated_at"])
        if u:
            candidates.append(("relay_status", (moment - u).total_seconds()))
    for path, name in (
        (DATA / "paper_gc.json", "paper_gc"),
        (DATA / "paper_btc_theta.json", "paper_btc"),
        (DATA / "paper_ol.json", "paper_ol"),
    ):
        data = load_json(path)
        if isinstance(data, dict):
            u = parse_updated_at(data.get("updated_at"))
            if u:
                candidates.append((name, (moment - u).total_seconds()))
                continue
        if path.exists():
            candidates.append((name + "_mtime", moment.timestamp() - path.stat().st_mtime))

    if candidates:
        name, age = min(candidates, key=lambda x: x[1])
        out["age_sec"] = round(age, 1)
        out["source"] = name
        limit = HEARTBEAT_STALE_S
        if moment.weekday() < 5 and time(9, 15) <= moment.time() <= time(15, 45):
            limit = 45 * 60
        if age > limit:
            out["status"] = "stale"
            out["detail"] = f"board age {age/60:.0f}m via {name} (limit {limit/60:.0f}m)"
            out["needs_fix"] = True
        else:
            out["detail"] = f"board ok ({age/60:.1f}m via {name})"
        return out

    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import market_poller as mp
        age = mp.board_heartbeat_age()
        out["age_sec"] = round(age, 1)
        if age > HEARTBEAT_STALE_S:
            out["status"] = "stale"
            out["detail"] = f"heartbeat age {age/60:.0f}m"
            out["needs_fix"] = True
        else:
            out["detail"] = f"heartbeat ok ({age/60:.1f}m)"
    except Exception as e:
        out["status"] = "unknown"
        out["detail"] = f"no relay signal: {e}"
        out["needs_fix"] = True
    return out


def check_feeds() -> dict:
    result = {
        "id": "feeds",
        "label": "Live feeds (Angel / Yahoo)",
        "status": "ok",
        "angel": "unknown",
        "yahoo": "unknown",
        "detail": "",
        "needs_fix": False,
        "fix": "feeds",
    }
    angel_ok = False
    yahoo_ok = False
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import angel_feed
        if not angel_feed._creds():
            result["angel"] = "no_creds"
        else:
            q = angel_feed.quotes(["SBIN", "TCS", "RELIANCE", "INFY", "HDFCBANK"])
            n = len(q or {})
            if n >= 3:
                result["angel"] = f"ok ({n} quotes)"
                angel_ok = True
            else:
                result["angel"] = f"weak ({n} quotes)"
    except Exception as e:
        result["angel"] = f"fail: {str(e)[:80]}"
    try:
        import yfinance as yf
        hist = yf.Ticker("SBIN.NS").history(period="5d")
        if hist is not None and len(hist) >= 1:
            result["yahoo"] = "ok"
            yahoo_ok = True
        else:
            result["yahoo"] = "empty"
    except Exception as e:
        result["yahoo"] = f"fail: {str(e)[:80]}"

    if angel_ok:
        result["status"] = "ok"
        result["detail"] = "Angel healthy"
    elif yahoo_ok:
        result["status"] = "warn"
        result["detail"] = "Angel down — Yahoo fallback available"
    else:
        result["status"] = "bad"
        result["detail"] = "Angel and Yahoo both failing"
        result["needs_fix"] = True
    return result


def fix_relay() -> str:
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import cloud_papertrade as cp
        cp.maybe_start_poller()
        return "maybe_start_poller() called"
    except Exception as e:
        try:
            import market_poller as mp
            m = now()
            if m.weekday() < 5 and time(9, 15) <= m.time() < time(15, 45):
                mp.dispatch_poller("day")
                return "dispatch_poller(day)"
            mp.dispatch_poller("gcwatch")
            return "dispatch_poller(gcwatch)"
        except Exception as e2:
            return f"relay fix failed: {e} / {e2}"


def fix_nse() -> str:
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import cloud_papertrade as cp
        state = cp.read_json(cp.NSE_STATE, cp.empty_state())
        snapshot = cp.update_nse(state)
        cp.write_json(cp.NSE_STATE, state)
        cp.write_json(cp.NSE_SNAPSHOT, snapshot)
        return f"nse tick ok open={snapshot.get('summary', {}).get('open_count')}"
    except Exception as e:
        return f"nse fix failed: {e}\n{traceback.format_exc()[-400:]}"


def fix_gc() -> str:
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import cloud_papertrade as cp
        state = cp.read_json(cp.GC_STATE, cp.empty_state())
        snapshot = cp.update_gc(state)
        cp.write_json(cp.GC_STATE, state)
        cp.write_json(cp.GC_SNAPSHOT, snapshot)
        return "gc tick ok"
    except Exception as e:
        return f"gc fix failed: {e}"


def fix_btc_theta() -> str:
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import cloud_btc_theta as btc
        btc.tick(throttle_sec=0)
        return "btc_theta tick ok"
    except Exception as e:
        return f"btc_theta fix failed: {e}"


def fix_qm() -> str:
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import cloud_qm
        cloud_qm.run_day()
        return "qm run_day ok"
    except Exception as e:
        return f"qm fix failed: {e}"


def fix_swing() -> str:
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import cloud_swing
        cloud_swing.run_day()
        return "swing run_day ok"
    except Exception as e:
        return f"swing fix failed: {e}"


def fix_ltp() -> str:
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import cloud_nifty_ltp as ltp
        ltp.tick()
        return "ltp tick ok"
    except Exception as e:
        return f"ltp fix failed: {e}"


def fix_arb() -> str:
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import cloud_future_arb as arb
        arb.tick()
        return "arb tick ok"
    except Exception as e:
        return f"arb fix failed: {e}"


FIXERS = {
    "relay": fix_relay,
    "nse": fix_nse,
    "gc": fix_gc,
    "btc_theta": fix_btc_theta,
    "qm": fix_qm,
    "swing": fix_swing,
    "ltp": fix_ltp,
    "arb": fix_arb,
}


def apply_fix(name: str) -> str:
    fn = FIXERS.get(name)
    if not fn:
        return f"no fixer for {name}"
    print(f"watchdog: applying fix → {name}", flush=True)
    msg = fn()
    print(f"watchdog: {name}: {msg}", flush=True)
    return msg


def commit_if_possible() -> str:
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import cloud_papertrade as cp
        cp.commit_state()
        return "commit_state() done"
    except Exception as e:
        return f"commit skipped: {e}"


def admin_notify(text: str) -> None:
    if os.environ.get("WATCHDOG_TELEGRAM", "").strip() not in ("1", "true", "yes"):
        return
    try:
        sys.path.insert(0, str(ROOT / "scanners"))
        import telegram_notify
        telegram_notify.notify(text, force=True)
    except Exception as e:
        print(f"watchdog: admin notify failed: {e}", flush=True)


def run(check_only: bool = False, force: str | None = None) -> dict:
    moment = now()
    checks: list[dict] = []
    fixes: list[dict] = []

    if force:
        msg = apply_fix(force)
        fixes.append({"id": force, "result": msg})
        commit_if_possible()
        report = {
            "updated_at": moment.strftime("%d %b %Y %H:%M:%S IST"),
            "mode": "force",
            "force": force,
            "fixes": fixes,
        }
        HEALTH_FILE.write_text(json.dumps(report, indent=1) + "\n")
        return report

    checks.append(check_relay(moment))
    checks.append(check_feeds())
    for tab in TABS:
        checks.append(check_tab(tab, moment))

    broken = [c for c in checks if c.get("needs_fix")]
    if not check_only:
        seen = set()
        for c in broken:
            fid = c.get("fix")
            if not fid or fid in seen or fid == "feeds":
                continue
            seen.add(fid)
            msg = apply_fix(fid)
            fixes.append({"id": fid, "result": msg, "reason": c.get("detail")})
        if fixes:
            commit_if_possible()

    worst = "ok"
    for c in checks:
        st = c.get("status")
        if st in ("bad", "missing") and worst != "bad":
            worst = "bad"
        elif st == "stale" and worst == "ok":
            worst = "stale"
        elif st == "warn" and worst == "ok":
            worst = "warn"

    report = {
        "updated_at": moment.strftime("%d %b %Y %H:%M:%S IST"),
        "overall": worst,
        "checks": checks,
        "fixes": fixes,
        "check_only": check_only,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps(report, indent=1) + "\n")

    print(f"\n=== portal health @ {report['updated_at']}  overall={worst} ===")
    for c in checks:
        age = c.get("age_min")
        age_s = f" age={age}m" if age is not None else ""
        print(f"  [{c['status']:7}] {c['label']}: {c.get('detail', '')}{age_s}")
    if fixes:
        print("fixes:")
        for f in fixes:
            print(f"  → {f['id']}: {str(f['result'])[:120]}")
    else:
        print("fixes: (none)")

    if worst in ("bad", "stale") and not check_only:
        lines = [f"⚠️ Portal watchdog · overall={worst}"]
        for c in checks:
            if c.get("status") in ("bad", "stale", "missing"):
                lines.append(f"· {c['label']}: {c.get('detail')}")
        for f in fixes:
            lines.append(f"fix {f['id']}: {str(f['result'])[:80]}")
        admin_notify("\n".join(lines))

    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Portal tab health check + auto-fix")
    ap.add_argument("--check-only", action="store_true", help="Do not apply fixes")
    ap.add_argument("--fix", choices=list(FIXERS.keys()), help="Force one fix and exit")
    args = ap.parse_args()
    report = run(check_only=args.check_only, force=args.fix)
    overall = report.get("overall", "ok")
    if overall == "bad":
        return 2
    if overall == "stale":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
