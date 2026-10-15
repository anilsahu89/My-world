#!/usr/bin/env python3
from pathlib import Path
p = Path(__file__).resolve().parent / "cloud_papertrade.py"
t = p.read_text()
old = "NSE_RVOL_OL = 1.3              # O=L long — stricter (win-rate tune 15 Oct)              # O=L long  (was 3.0 on the old scale)"
new = "NSE_RVOL_OL = 1.3              # O=L long — stricter (win-rate tune 15 Oct)"
if old in t:
    p.write_text(t.replace(old, new, 1))
    print("cleaned")
else:
    print("already clean or different")
