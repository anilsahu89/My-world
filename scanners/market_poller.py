#!/usr/bin/env python3
"""24/7 relay — the cloud paper-trade system's continuous runner.

GitHub's schedule queue drops most sub-hourly crons (measured ~5-8%
delivery; on 23 Sep 2026 every morning cron was dropped and the session
was missed). The relay removes that dependency: one long-running job per
phase, each dispatching the next via the API (dispatches are reliable;
schedules are not).

  day      09:14-15:05 IST weekdays: NSE entries (60s polls 09:14-09:50,
           3-min after), SL/time-stop/square-off, gc each pass.
           -> dispatches evening
  evening  dispatched ~15:05, sleeps to 17:25: swing scan once (weekday),
           gc ticks to 21:02 (its 6h cap). -> dispatches gcwatch
  gcwatch  generic 5h45 gap-filler: gc ticks (15-min in Gold's 05:30-07:30
           entry window, 20-min otherwise). On any tick, if it's a weekday
           from 09:10 and no day-poller heartbeat -> dispatch day and exit.
           At 5h45 -> dispatch the next gcwatch. This carries the chain
           through nights and weekends, and auto-starts Monday's session.

Self-healing on top (cloud_papertrade.maybe_start_poller): any cron run
that DOES survive re-lights a dead relay (no board commit in ~25 min).

Job lifetimes stay under GitHub's 6h/job cap: day 5h51, evening 5h57,
gcwatch 5h45.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cloud_papertrade as cp  # noqa: E402

REPO = "anilsahu89/My-world"
WF = "cloud-papertrade.yml"
DAY_START = dtime(9, 10)
DAY_END = dtime(15, 5)
EVE_WORK = dtime(17, 25)
EVE_END = dtime(21, 2)
WATCH_SPAN = 5 * 3600 + 45 * 60          # 5h45 per gcwatch job
HEARTBEAT_STALE_S = 25 * 60


def _git(*args):
    return subprocess.run(["git", "-C", str(cp.ROOT), *args],
                          capture_output=True, text=True)


def commit_and_push() -> None:
    cp.commit_state()
    if _git("push", "origin", "main").returncode != 0:
        _git("pull", "--rebase", "origin", "main")
        _git("push", "origin", "main")


def _api(method: str, path: str, body: dict | None = None):
    import urllib.request
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not tok:
        return None
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def dispatch_poller(mode: str) -> None:
    _api("POST", f"/repos/{REPO}/actions/workflows/{WF}/dispatches",
         {"ref": "main", "inputs": {"desk": "poller", "mode": mode}})


def board_heartbeat_age() -> float:
    """Seconds since either board file last changed on origin."""
    best = None
    for path in ("data/paper_ol.json", "data/paper_gc.json"):
        r = _api("GET", f"/repos/{REPO}/commits?path={path}&per_page=1")
        try:
            from datetime import datetime, timezone
            when = datetime.fromisoformat(
                r[0]["commit"]["committer"]["date"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - when).total_seconds()
            best = age if best is None else min(best, age)
        except Exception:
            pass
    return best if best is not None else 1e9


def gc_tick() -> None:
    gs = cp.read_json(cp.GC_STATE, cp.empty_state())
    snap = cp.update_gc(gs)
    cp.write_json(cp.GC_STATE, gs)
    cp.write_json(cp.GC_SNAPSHOT, snap)


def nse_tick(gc_too: bool = True) -> None:
    state = cp.read_json(cp.NSE_STATE, cp.empty_state())
    snap = cp.update_nse(state)
    cp.write_json(cp.NSE_STATE, state)
    cp.write_json(cp.NSE_SNAPSHOT, snap)
    if gc_too:
        gc_tick()


# ------------------------------------------------------------------ modes --
def another_poller_running() -> bool:
    """True if a different poller run is actually in progress. Prefers the
    Actions API (GITHUB_RUN_ID separates self from others); falls back to
    the commit heartbeat for local runs. A commit-age-only guard once
    mistook a just-cancelled poller's final commit for a live one and left
    the session unattended (23 Sep)."""
    rid = os.environ.get("GITHUB_RUN_ID")
    if rid:
        r = _api("GET", f"/repos/{REPO}/actions/workflows/{WF}/runs"
                        "?event=workflow_dispatch&status=in_progress&per_page=10")
        try:
            return any(x["id"] != int(rid) for x in r.get("workflow_runs", []))
        except Exception:
            pass
    return board_heartbeat_age() < 150


def run_day() -> None:
    print("relay: day poller starting", flush=True)
    if another_poller_running():
        print("relay: another poller is alive — exiting", flush=True)
        return
    while True:
        now = cp.now()
        if now.weekday() >= 5 or now.time() >= DAY_END:
            break
        try:
            nse_tick()
            commit_and_push()
        except Exception as e:
            print(f"day tick failed: {e}", flush=True)
        dense = dtime(9, 14) <= now.time() <= dtime(9, 50)
        time.sleep(60 if dense else 180)
    try:                                   # final square-off pass
        nse_tick()
        commit_and_push()
    except Exception as e:
        print(f"final pass failed: {e}", flush=True)
    print("relay: session done -> evening", flush=True)
    dispatch_poller("evening")


def run_evening() -> None:
    print("relay: evening job (waits for 17:25 IST)", flush=True)
    while cp.now().time() < EVE_WORK:
        time.sleep(120)
    if cp.now().weekday() < 5:
        try:
            import cloud_swing
            cloud_swing.run_day()
            commit_and_push()
        except Exception as e:
            print(f"swing run failed: {e}", flush=True)
        # QM (Quantity Model) desk — 4th setup, daily bars, same evening slot
        try:
            import cloud_qm
            cloud_qm.run_day()
            commit_and_push()
        except Exception as e:
            print(f"qm run failed: {e}", flush=True)
        # BB Trap scan (EOD bhavcopies) — ported from the throttled cron
        try:
            import subprocess
            r = subprocess.run([sys.executable,
                                str(cp.ROOT / "scanners" / "fetch_bhavcopies.py")],
                               cwd=str(cp.ROOT), capture_output=True, text=True,
                               timeout=25 * 60)
            print(f"bhavcopy fetch: {r.stdout.strip()}", flush=True)
            if r.returncode == 0:
                subprocess.run([sys.executable,
                                str(cp.ROOT / "scanners" / "gen_bbtrap_json.py"),
                                "--bhav-dir", "bhav",
                                "--universe", "data/nse200_symbols.csv",
                                "--out", "data/bbtrap.json"],
                               cwd=str(cp.ROOT), check=True, timeout=600,
                               capture_output=True, text=True)
                commit_and_push()
                print("bbtrap scan done", flush=True)
        except Exception as e:
            print(f"bbtrap scan failed: {e}", flush=True)
    while cp.now().time() < EVE_END:
        try:
            gc_tick()
            commit_and_push()
        except Exception as e:
            print(f"evening gc tick failed: {e}", flush=True)
        time.sleep(300)
    print("relay: evening done -> gcwatch", flush=True)
    dispatch_poller("gcwatch")


def run_gcwatch() -> None:
    print("relay: gcwatch gap-filler starting", flush=True)
    t0 = time.time()
    while time.time() - t0 < WATCH_SPAN:
        now = cp.now()
        # market window on a weekday: hand over to the day poller
        if now.weekday() < 5 and now.time() >= DAY_START \
                and now.time() < DAY_END and board_heartbeat_age() > 150:
            print("relay: gcwatch -> day poller", flush=True)
            dispatch_poller("day")
            return
        try:
            gc_tick()
            commit_and_push()
        except Exception as e:
            print(f"gcwatch tick failed: {e}", flush=True)
        gold_window = dtime(5, 25) <= now.time() <= dtime(7, 35)
        time.sleep(900 if gold_window else 1200)
    print("relay: gcwatch span done -> next gcwatch", flush=True)
    dispatch_poller("gcwatch")


def main(mode: str = "day") -> None:
    if mode == "day":
        run_day()
    elif mode == "evening":
        run_evening()
    elif mode == "gcwatch":
        run_gcwatch()
    else:
        raise SystemExit(f"unknown poller mode: {mode}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "day")
