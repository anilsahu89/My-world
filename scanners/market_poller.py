#!/usr/bin/env python3
"""Market-hours poller — one long Actions job covering the whole session,
replacing dependence on GitHub's throttled sub-hourly cron.

  day mode     (09:15-15:05 IST weekdays): NSE entries (60s polls in the
               09:30-09:45 window, 3-min after) + SL/time-stop/square-off
               management + Gold/BTC updates; commits every change.
               At session end dispatches the evening poller.
  evening mode (dispatched ~15:05, sleeps to 17:30): swing scan once,
               Gold/BTC ticks until 21:05, commits.

Guards: weekend skip; duplicate day-pollers are naturally rare (starters
check the board heartbeat — a commit to paper_ol.json in the last 5 min
means a poller is alive) and worst case they double-commit harmlessly.
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
DAY_END = dtime(15, 5)          # square-off 15:00 + margin; stays under the 6h job cap
EVE_START = dtime(17, 30)
EVE_END = dtime(21, 5)


def _git(*args, check=False):
    return subprocess.run(["git", "-C", str(cp.ROOT), *args],
                          capture_output=True, text=True, check=check)


def commit_and_push() -> None:
    cp.commit_state()
    if _git("diff", "HEAD~1", "--name-only").returncode == 0:
        pass
    r = _git("push", "origin", "main")
    if r.returncode != 0:                      # remote moved (a cron run slipped in)
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


def board_heartbeat_alive(max_age_s: int = 300) -> bool:
    """A commit to the board in the last few minutes = a poller is running."""
    r = _api("GET", f"/repos/{REPO}/commits?path=data/paper_ol.json&per_page=1")
    try:
        from datetime import datetime, timezone
        when = datetime.fromisoformat(
            r[0]["commit"]["committer"]["date"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - when).total_seconds() < max_age_s
    except Exception:
        return False


def update_desks(gc_too: bool = True) -> None:
    state = cp.read_json(cp.NSE_STATE, cp.empty_state())
    snap = cp.update_nse(state)
    cp.write_json(cp.NSE_STATE, state)
    cp.write_json(cp.NSE_SNAPSHOT, snap)
    if gc_too:
        gs = cp.read_json(cp.GC_STATE, cp.empty_state())
        gsnap = cp.update_gc(gs)
        cp.write_json(cp.GC_STATE, gs)
        cp.write_json(cp.GC_SNAPSHOT, gsnap)


def run_day() -> None:
    print("day poller: starting", flush=True)
    while True:
        now = cp.now()
        if now.weekday() >= 5:
            print("weekend — day poller exiting", flush=True)
            return
        if now.time() >= DAY_END:
            break
        try:
            update_desks()
            commit_and_push()
        except Exception as e:
            print(f"poll iteration failed: {e}", flush=True)
        dense = dtime(9, 14) <= now.time() <= dtime(9, 50)
        time.sleep(60 if dense else 180)
    # one final square-off pass then hand over to the evening poller
    try:
        update_desks()
        commit_and_push()
    except Exception as e:
        print(f"final pass failed: {e}", flush=True)
    print("day poller: session done, dispatching evening poller", flush=True)
    dispatch_poller("evening")


def run_evening() -> None:
    print("evening poller: waiting for 17:30 IST", flush=True)
    while cp.now().time() < EVE_START:
        if cp.now().weekday() >= 5:
            print("weekend — evening poller exiting", flush=True)
            return
        time.sleep(120)
    if cp.now().weekday() >= 5:
        return
    try:
        import cloud_swing
        cloud_swing.run_day()          # once — its own done-date guard applies
        commit_and_push()
    except Exception as e:
        print(f"swing run failed: {e}", flush=True)
    while cp.now().time() < EVE_END:
        try:
            gs = cp.read_json(cp.GC_STATE, cp.empty_state())
            gsnap = cp.update_gc(gs)
            cp.write_json(cp.GC_STATE, gs)
            cp.write_json(cp.GC_SNAPSHOT, gsnap)
            commit_and_push()
        except Exception as e:
            print(f"evening gc tick failed: {e}", flush=True)
        time.sleep(300)
    print("evening poller: done", flush=True)


def main(mode: str = "day") -> None:
    (run_day if mode == "day" else run_evening)()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "day")
