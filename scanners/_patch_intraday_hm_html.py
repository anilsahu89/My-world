#!/usr/bin/env python3
from pathlib import Path
p = Path("paper.html")
t = p.read_text()
pairs = [
 ("📈 HM Positional", "📈 Intraday HM"),
 ("Hilega-Milega Weekly", "HTF bias + 1H Hilega-Milega"),
 ("Loading HM Positional desk…", "Loading Intraday HM desk…"),
 ("HM Positional: ₹20k × 5 open = ₹1L", "Intraday HM: ₹10k × 5 open = ₹1L"),
 ("HM Positional", "Intraday HM"),
 ("Could not load HM Positional data yet.", "Could not load Intraday HM data yet."),
]
for a,b in pairs:
    if a in t:
        t = t.replace(a,b); print("ok", a[:40])
p.write_text(t)
print("html done")
