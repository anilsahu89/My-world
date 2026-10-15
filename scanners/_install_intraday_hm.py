#!/usr/bin/env python3
from pathlib import Path
import base64
root = Path(__file__).resolve().parent
parts = sorted(root.glob("_ihm_c*.b64"))
assert len(parts) == 8, parts
data = base64.b64decode("".join(p.read_text().strip() for p in parts))
(root / "cloud_htf_hm.py").write_bytes(data)
print("wrote", len(data))
assert b"Intraday HM" in data
