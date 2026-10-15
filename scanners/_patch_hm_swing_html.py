#!/usr/bin/env python3
from pathlib import Path
p = Path("paper.html")
t = p.read_text()
pairs = [
    ("🕯️ Candle Swing", "🕯️ HM Swing"),
    ("🕯️ Candlestick Swing — top-3 patterns", "🕯️ HM Swing — weekly Hilega-Milega (buy & sell)"),
    ("Loading candlestick swing desk…", "Loading HM Swing desk…"),
    ("No open swing positions.", "No open HM Swing positions."),
    ("No closed swing trades yet.", "No closed HM Swing trades yet."),
    ("Candlestick swing desk — data/swing_picks.json (EOD desk, marks daily)",
     "HM Swing desk — weekly Hilega-Milega buy/sell (data/swing_picks.json)"),
    ("Candle Swing &amp; GC sized within ₹1L", "HM Swing &amp; GC sized within ₹1L"),
    ("No open swing positions \\u2014 signals fill at the next session\\u2019s open.",
     "No open HM Swing positions \\u2014 weekly signals fill at the next session open."),
]
for a, b in pairs:
    if a in t:
        t = t.replace(a, b)
        print("ok", a[:40])
    else:
        print("skip", a[:40])
p.write_text(t)
print("paper.html done")
