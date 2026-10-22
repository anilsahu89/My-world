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


_BOT = ["-c", "user.name=papertrade-bot",
        "-c", "user.email=papertrade-bot@users.noreply.github.com"]
STATUS_FILE = cp.ROOT / "data" / "relay_status.json"
_RELAY = {"last_push": "none yet", "last_push_ok": None}


def write_status(mode: str, errors: list[str] | None = None) -> None:
    """data/relay_status.json — one glance at whether the relay is ticking,
    what failed, and whether its last push landed (15 Oct 2026: the day relay
    ran 5+ hours on 13 Oct without a single commit and nothing said why)."""
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps({
            "mode": mode,
            "last_tick": cp.now().strftime("%d %b %Y %H:%M:%S IST"),
            "errors": (errors or [])[-6:],
            "last_push_ok": _RELAY["last_push_ok"],
            "last_push": _RELAY["last_push"],
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        }, indent=1) + "\n")
    except Exception:
        pass


def commit_and_push() -> bool:
    """Commit this tick's output and get it onto origin/main WITHOUT losing it.

    15 Oct 2026 — root cause of the frozen NSE board: commit_state() stages a
    fixed file list, so any other generated file (flags, guard state, new
    desks' outputs) stayed dirty and made `git pull --rebase` refuse; several
    listed files (alerts.json, live_stocks.json, bbtrap.json) are ALSO written
    by other bots, so rebases conflicted — and the old fallback was
    `reset --hard origin/main`, which silently erased the tick's ledger.
    Now: sweep every generated path into the commit, retry
    fetch + rebase (-X theirs = our generated data wins), fall back to a
    merge (-X ours), and NEVER reset: an unpushed commit just retries on the
    next tick. Returns True when the push landed. Never raises."""
    try:
        cp.commit_state()
        for sweep_path in ("data", "scanners/papertrades", "scanners/data"):
            # one path at a time: git aborts the whole `add` if any single
            # pathspec matches nothing, which would silently stage nothing
            if (cp.ROOT / sweep_path).exists():
                _git("add", "-A", "--", sweep_path)
        if _git("diff", "--cached", "--quiet").returncode != 0:
            _git(*_BOT, "commit", "-q", "-m",
                 f"chore: paper trade state {cp.now().strftime('%Y-%m-%dT%H:%M')} (relay)")
        # a previous half-finished rebase/merge leaves git unusable (exit 128)
        _git("rebase", "--abort")
        _git("merge", "--abort")
        detail = ""
        for _ in range(4):
            r = _git("push", "origin", "main")
            if r.returncode == 0:
                _RELAY.update(last_push="ok", last_push_ok=True)
                return True
            detail = (r.stderr or r.stdout or "")[-180:].replace("\n", " ")
            _git("fetch", "-q", "origin", "main")
            rb = _git(*_BOT, "rebase", "-X", "theirs", "--autostash", "origin/main")
            if rb.returncode != 0:
                _git("rebase", "--abort")
                mg = _git(*_BOT, "merge", "-X", "ours", "--no-edit", "origin/main")
                if mg.returncode != 0:
                    _git("merge", "--abort")
                    detail += " | merge: " + (mg.stderr or mg.stdout)[-120:].replace("\n", " ")
        _RELAY.update(last_push=f"FAILED after 4 tries: {detail}", last_push_ok=False)
        print(f"relay: push failed (commit kept for next tick): {detail}", flush=True)
    except Exception as e:
        _RELAY.update(last_push=f"exception: {e}", last_push_ok=False)
        print(f"relay: commit_and_push error: {e}", flush=True)
    return False


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
    btc_tick()


def btc_tick() -> None:
    """BTC 0DTE theta desk — 24/7 market, self-throttled to 15 min inside
    cloud_btc_theta.tick(); carried by every gc/day tick so it never
    depends on the (throttled) scheduled crons."""
    try:
        import cloud_btc_theta
        cloud_btc_theta.tick()
    except Exception as e:
        print(f"btc theta tick failed: {e}", flush=True)


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


def theta_tick() -> None:
    """Nifty Daily Theta desk — hedged credit spread tracker (backtested
    92.9% WR / PF 1.77). Once per weekday morning via the day poller: the
    run-scanner cron is throttled to near-zero, and a daily run also
    manages Thu/Fri exits the Mon-Wed-only cron never covered."""
    moment = cp.now()
    if moment.weekday() >= 5 or moment.time() < dtime(9, 14):
        return
    flag = cp.ROOT / "data" / "scanners" / ".theta_last_run"
    today = moment.date().isoformat()
    try:
        if flag.exists() and flag.read_text().strip() == today:
            return
    except OSError:
        pass
    out = cp.ROOT / "data" / "scanners" / "nifty-daily-theta-latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [sys.executable, str(cp.ROOT / "scanners" / "run_nifty_daily_theta.py"),
         "--output", str(out)],
        cwd=str(cp.ROOT), capture_output=True, text=True, timeout=300)
    print(f"theta scan: rc={r.returncode} "
          f"{(r.stdout or '')[-200:]}", flush=True)
    # retry discipline (13 Oct): a DATA failure must not lock the day —
    # only a healthy scan (real data, rules decision made) sets the flag
    try:
        import desk_retry
        if r.returncode == 0 and out.exists() \
                and desk_retry.healthy("theta", desk_retry.last_output("theta")):
            flag.write_text(today)
    except OSError:
        pass


def mill_tick() -> None:
    """Nifty Premium Mill desk — modernized hedged credit-spread tracker
    (1-year real-premium backtest: PF 1.58, 91% WR on this calibration).
    Same cadence as the theta desk: once per weekday morning via the day
    poller. The desk had ONE manual run on 14 Jul 2026 and then sat dead —
    no schedule ever ticked it again, leaving its only trade frozen OPEN."""
    moment = cp.now()
    if moment.weekday() >= 5 or moment.time() < dtime(9, 14):
        return
    flag = cp.ROOT / "data" / "scanners" / ".mill_last_run"
    today = moment.date().isoformat()
    try:
        if flag.exists() and flag.read_text().strip() == today:
            return
    except OSError:
        pass
    out = cp.ROOT / "data" / "scanners" / "nifty-premium-mill-latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [sys.executable, str(cp.ROOT / "scanners" / "run_nifty_premium_mill.py"),
         "--output", str(out)],
        cwd=str(cp.ROOT), capture_output=True, text=True, timeout=300)
    print(f"mill scan: rc={r.returncode} "
          f"{(r.stdout or '')[-200:]}", flush=True)
    # same retry discipline as theta: flag only on a healthy scan
    try:
        import desk_retry
        if r.returncode == 0 and out.exists() \
                and desk_retry.healthy("mill", desk_retry.last_output("mill")):
            flag.write_text(today)
    except OSError:
        pass


def htf_hm_tick() -> None:
    """Intraday HM desk — HTF (weekly+daily) bias, entries on 1H HM.
    Runs during market hours about every 30 minutes."""
    moment = cp.now()
    if moment.weekday() >= 5:
        return
    # runs in BOTH windows: the day poller (market hours, 30-min buckets)
    # and the evening job (~20:30-22:00). Each sets its own bucket flag, so
    # scans fire once per half-hour bucket without double-running.
    in_day = dtime(9, 30) <= moment.time() <= dtime(15, 20)
    in_eve = dtime(15, 30) <= moment.time() <= dtime(22, 0)
    if moment.weekday() >= 5 or not (in_day or in_eve):
        return
    flag = cp.ROOT / "data" / "scanners" / ".htfhm_last_run"
    bucket = moment.strftime("%Y-%m-%d %H:") + ("30" if moment.minute >= 30 else "00")
    try:
        if flag.exists() and flag.read_text().strip() == bucket:
            return
    except OSError:
        pass
    out = cp.ROOT / "data" / "scanners" / "htf-hm-latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [sys.executable, str(cp.ROOT / "scanners" / "cloud_htf_hm.py"),
         "--output", str(out)],
        cwd=str(cp.ROOT), capture_output=True, text=True, timeout=1800)
    print(f"intraday-hm scan: rc={r.returncode} "
          f"{(r.stdout or '')[-300:]}", flush=True)
    if r.returncode == 0 and out.exists():
        try:
            flag.write_text(bucket)
        except OSError:
            pass


def run_day() -> None:
    print("relay: day poller starting", flush=True)
    if another_poller_running():
        print("relay: another poller is alive — exiting", flush=True)
        return
    while True:
        now = cp.now()
        if now.weekday() >= 5 or now.time() >= DAY_END:
            break
        # each step isolated: one failing step (NSE engine, theta, mill...)
        # must never skip the others or — worst — the commit, which is what
        # froze the board for two days (13-15 Oct)
        errs = []
        for name, step in (("nse", nse_tick), ("theta", theta_tick),
                           ("mill", mill_tick), ("btc", btc_tick)):
            try:
                step()
            except Exception as e:
                import traceback
                print(f"day tick: {name} failed: {e}\n"
                      f"{traceback.format_exc()[-500:]}", flush=True)
                errs.append(f"{cp.now().strftime('%H:%M')} {name}: {str(e)[:160]}")
        write_status("day", errs)
        commit_and_push()
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
        # HM Positional desk — its tick gates itself to >=15:35 IST and the
        # day loop ends 15:05, so it was previously UNREACHABLE (the desk
        # only ever scanned when run by hand). Run it here, post-close.
        try:
            htf_hm_tick()
            commit_and_push()
        except Exception as e:
            print(f"htf-hm run failed: {e}", flush=True)
        # BB Trap scan (EOD bhavcopies) — ported from the throttled cron
        try:
            import subprocess
            r = subprocess.run([sys.executable,
                                str(cp.ROOT / "scanners" / "fetch_bhavcopies.py")],
                               cwd=str(cp.ROOT), capture_output=True, text=True,
                               timeout=25 * 60)
            print(f"bhavcopy fetch: {r.stdout.strip()}", flush=True)
            if r.returncode == 0:
                # rebuild the avg20 volume cache from the fresh bhavcopies so
                # the OL/OH entry cap never depends on a throttled Yahoo call
                try:
                    cp.refresh_vol_cache_from_bhav(cp.ROOT / "bhav")
                    commit_and_push()
                except Exception as e:
                    print(f"vol cache rebuild failed: {e}", flush=True)
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
    last_eod = 0.0
    while cp.now().time() < EVE_END:
        errs = []
        try:
            gc_tick()
        except Exception as e:
            print(f"evening gc tick failed: {e}", flush=True)
            errs.append(f"gc: {str(e)[:160]}")
        # NIFTY Long-Term Premium + Future Arbitrage: these two EOD-bhavcopy
        # desks were only ever called from the scheduled `gc` cron branch,
        # which this relay's long-running jobs starve (concurrency group), so
        # both sat frozen since 7 Oct. Same guards as there: weekday, >=18:45
        # IST, done_date-guarded, retried until the bhavcopy lands.
        if time.time() - last_eod > 900:
            last_eod = time.time()
            for mod_name in ("cloud_nifty_ltp", "cloud_future_arb"):
                try:
                    __import__(mod_name).tick()
                except Exception as e:
                    print(f"evening {mod_name} failed: {e}", flush=True)
                    errs.append(f"{mod_name}: {str(e)[:160]}")
        write_status("evening", errs)
        commit_and_push()
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
        errs = []
        try:
            gc_tick()
        except Exception as e:
            print(f"gcwatch tick failed: {e}", flush=True)
            errs.append(f"gc: {str(e)[:160]}")
        write_status("gcwatch", errs)
        commit_and_push()
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
