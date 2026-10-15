#!/usr/bin/env python3
from pathlib import Path
import base64
root = Path(__file__).resolve().parent
parts = sorted(root.glob("_hmb_desk.b64.*"))
data = base64.b64decode("".join(p.read_text().strip() for p in parts))
root.joinpath("hm_btc_gold_desk.py").write_bytes(data)
print("desk", len(data))
assert b"pnl_inr" in data
