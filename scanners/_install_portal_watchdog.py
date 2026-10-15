#!/usr/bin/env python3
"""Decode scanners/_portal_watchdog.b64.* into portal_watchdog.py if missing/outdated."""
from pathlib import Path
import base64

ROOT = Path(__file__).resolve().parent
parts = sorted(ROOT.glob("_portal_watchdog.b64.*"))
if not parts:
    raise SystemExit("no b64 parts")
b = "".join(p.read_text().strip() for p in parts)
data = base64.b64decode(b)
out = ROOT / "portal_watchdog.py"
out.write_bytes(data)
print(f"wrote {out} ({len(data)} bytes)")
