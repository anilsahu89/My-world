#!/usr/bin/env python3
"""Fetch NSE CM bhavcopy zips (last ~140 days) for the BB Trap scan.

Ported verbatim from refresh-bbtrap.yml so the evening relay job can run
the scan without the (throttled) cron workflow. Skips files already on
disk, so each evening only pulls the new day.
"""
import os
import time
import urllib.request
from datetime import date, timedelta

RAW = "bhav"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/126.0 Safari/537.36",
      "Accept": "application/zip", "Referer": "https://www.nseindia.com/"}


def main() -> int:
    os.makedirs(RAW, exist_ok=True)
    end = date.today()
    d = end - timedelta(days=140)
    got = 0
    while d <= end:
        if d.weekday() < 5:
            path = os.path.join(RAW, f"cm_{d:%Y%m%d}.zip")
            if not os.path.exists(path):
                url = (f"https://nsearchives.nseindia.com/content/cm/"
                       f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
                req = urllib.request.Request(url, headers=UA)
                try:
                    with urllib.request.urlopen(req, timeout=25) as r:
                        c = r.read()
                    if c.startswith(b"PK"):
                        open(path, "wb").write(c)
                        got += 1
                    time.sleep(0.8)
                except Exception:
                    pass
        d += timedelta(days=1)
    total = len(os.listdir(RAW))
    print(f"bhavcopies: {total} ({got} new this run)")
    if total < 40:
        print("too few bhavcopies — scan aborted")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
