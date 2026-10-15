#!/usr/bin/env python3
from pathlib import Path
import base64
root = Path(__file__).resolve().parent
data = base64.b64decode(root.joinpath("_hmb_desk.b64.0").read_text() + root.joinpath("_hmb_desk.b64.1").read_text())
root.joinpath("hm_btc_gold_desk.py").write_bytes(data)
print("desk", len(data))
assert b"pnl_inr" in data
