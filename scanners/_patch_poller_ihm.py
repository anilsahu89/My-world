#!/usr/bin/env python3
from pathlib import Path
p = Path(__file__).resolve().parent / "market_poller.py"
t = p.read_text()
if "Intraday HM desk" in t and "bucket" in t:
    print("poller already patched")
else:
    old_start = "def htf_hm_tick() -> None:"
    idx = t.find(old_start)
    if idx < 0:
        raise SystemExit("htf_hm_tick not found")
    rest = t[idx+1:]
    next_def = rest.find("\ndef ")
    end = idx + 1 + next_def
    new = 'def htf_hm_tick() -> None:\n    """Intraday HM desk — HTF (weekly+daily) bias, entries on 1H HM.\n    Runs during market hours about every 30 minutes."""\n    moment = cp.now()\n    if moment.weekday() >= 5:\n        return\n    if moment.time() < dtime(9, 30) or moment.time() > dtime(15, 20):\n        return\n    flag = cp.ROOT / "data" / "scanners" / ".htfhm_last_run"\n    bucket = moment.strftime("%Y-%m-%d %H:") + ("30" if moment.minute >= 30 else "00")\n    try:\n        if flag.exists() and flag.read_text().strip() == bucket:\n            return\n    except OSError:\n        pass\n    out = cp.ROOT / "data" / "scanners" / "htf-hm-latest.json"\n    out.parent.mkdir(parents=True, exist_ok=True)\n    r = subprocess.run(\n        [sys.executable, str(cp.ROOT / "scanners" / "cloud_htf_hm.py"),\n         "--output", str(out)],\n        cwd=str(cp.ROOT), capture_output=True, text=True, timeout=1800)\n    print(f"intraday-hm scan: rc={r.returncode} "\n          f"{(r.stdout or "")[-300:]}", flush=True)\n    if r.returncode == 0 and out.exists():\n        try:\n            flag.write_text(bucket)\n        except OSError:\n            pass\n\n'
    t = t[:idx] + new + t[end:]
    p.write_text(t)
    print("poller patched")
