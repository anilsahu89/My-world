#!/usr/bin/env python3
from pathlib import Path
import base64
root = Path(__file__).resolve().parent
parts = sorted(root.glob("_hm_swing.b64.*"))
assert parts, "missing b64 parts"
data = base64.b64decode("".join(p.read_text().strip() for p in parts))
out = root / "cloud_swing.py"
out.write_bytes(data)
print(f"wrote {out} ({len(data)} bytes)")
assert b"HM-BUY" in data and b"HM-SELL" in data
