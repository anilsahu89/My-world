#!/usr/bin/env python3
"""One-shot patch: RVOL, F3 done-date, persist-before-notify, strip non-trade TG."""
from pathlib import Path
import re

p = Path(__file__).resolve().parent / "cloud_papertrade.py"
text = p.read_text()
changed = []

old = (
    "NSE_RVOL_OL = 1.5              # O=L long  (was 3.0 on the old scale)\n"
    "NSE_RVOL_OH = 1.2              # O=H short (was 1.5 on the old scale)"
)
new = (
    "NSE_RVOL_OL = 1.0              # O=L long  (lowered 15 Oct from 1.5 — restart flow)\n"
    "NSE_RVOL_OH = 0.8              # O=H short (lowered 15 Oct from 1.2 — restart flow)"
)
if old in text:
    text = text.replace(old, new, 1)
    changed.append("rvol")
elif "NSE_RVOL_OL = 1.0" in text:
    changed.append("rvol-already")
else:
    raise SystemExit("RVOL block not found")

old_f3 = '''def f3_scan(state: dict, quotes: dict, moment: datetime) -> None:
    """First-3-candle momentum longs (Rita's rule) once a day in the window."""
    today = moment.date().isoformat()
    if state.get("f3_done_date") == today or moment.weekday() >= 5:
        return
    if not (F3_WINDOW[0] <= moment.time() <= F3_WINDOW[1]):
        return
    state["f3_done_date"] = today
    if nifty_bias() != 1:
        return
    symbols = nse_symbols()
    bars = yf.download([f"{s}.NS" for s in symbols], period="5d", interval="1h",
                       group_by="ticker", progress=False, threads=True, auto_adjust=False)
    picks = []'''

new_f3 = '''def f3_scan(state: dict, quotes: dict, moment: datetime) -> None:
    """First-3-candle momentum longs (Rita's rule) once a day in the window."""
    today = moment.date().isoformat()
    if state.get("f3_done_date") == today or moment.weekday() >= 5:
        return
    if not (F3_WINDOW[0] <= moment.time() <= F3_WINDOW[1]):
        return
    # Do NOT set f3_done_date yet: NIFTY gate / empty Yahoo bars must allow
    # retries inside the 12:20-13:30 window (was marking done immediately and
    # skipping the whole day on a red NIFTY tick).
    if nifty_bias() != 1:
        print("f3_scan: NIFTY bias not long — retry later in window", flush=True)
        return
    symbols = nse_symbols()
    try:
        bars = yf.download([f"{s}.NS" for s in symbols], period="5d", interval="1h",
                           group_by="ticker", progress=False, threads=True, auto_adjust=False)
    except Exception as e:
        print(f"f3_scan: hourly bars download failed — retry later: {e}", flush=True)
        return
    if bars is None or getattr(bars, "empty", False):
        print("f3_scan: empty hourly bars — retry later", flush=True)
        return
    picks = []'''

if old_f3 in text:
    text = text.replace(old_f3, new_f3, 1)
    changed.append("f3-header")
elif "retry later in window" in text:
    changed.append("f3-header-already")
else:
    raise SystemExit("f3_scan header not found")

old_after = '''    picks.sort(reverse=True)
    stamp = moment.strftime("%H:%M:%S")
    already = {t["symbol"] for t in state["trades"]
               if t.get("date") == today and t.get("setup") == "f3"}
    for drive, symbol, _, sl in [p for p in picks if p[1] not in already][:F3_MAX_TRADES]:'''

new_after = '''    picks.sort(reverse=True)
    # Successful scan — mark done even if 0 picks (avoid re-download every tick).
    state["f3_done_date"] = today
    stamp = moment.strftime("%H:%M:%S")
    already = {t["symbol"] for t in state["trades"]
               if t.get("date") == today and t.get("setup") == "f3"}
    for drive, symbol, _, sl in [p for p in picks if p[1] not in already][:F3_MAX_TRADES]:'''

if old_after in text:
    text = text.replace(old_after, new_after, 1)
    changed.append("f3-done")
elif "Successful scan — mark done" in text:
    changed.append("f3-done-already")
else:
    raise SystemExit("f3 after-picks block not found")

if "def _persist_nse(" not in text:
    needle = (
        "def write_json(path: Path, value) -> None:\n"
        "    path.write_text(json.dumps(value, indent=1, ensure_ascii=False) + \"\\n\")\n"
    )
    # actual file uses real newline in the string written
    needle = 'def write_json(path: Path, value) -> None:\n    path.write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\\n")\n'
    # Fix: the source has + "\n") literally
    for cand in [
        'def write_json(path: Path, value) -> None:\n    path.write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n")\n',
    ]:
        if cand in text:
            text = text.replace(
                cand,
                cand + '\n\ndef _persist_nse(state: dict) -> None:\n'
                '    """Write ledger to disk BEFORE Telegram so an alert never outlives the trade."""\n'
                '    try:\n'
                '        write_json(NSE_STATE, state)\n'
                '    except Exception as e:\n'
                '        print(f"persist nse state failed: {e}", flush=True)\n',
                1,
            )
            changed.append("persist-helper")
            break
    else:
        raise SystemExit("write_json block not found for persist helper")

for label, old_n, new_n in [
    ("ol-open",
     '            state["next_id"] += 1\n            try:\n                telegram_notify.notify(\n                    f"⚡ OPEN · O=L/O=H ({setup.upper()})\n"',
     '            state["next_id"] += 1\n            _persist_nse(state)\n            try:\n                telegram_notify.notify(\n                    f"⚡ OPEN · O=L/O=H ({setup.upper()})\n"'),
    ("f3-open",
     '        state["next_id"] += 1\n        try:\n            telegram_notify.notify(\n                f"⚡ OPEN · First-3 momentum\n"',
     '        state["next_id"] += 1\n        _persist_nse(state)\n        try:\n            telegram_notify.notify(\n                f"⚡ OPEN · First-3 momentum\n"'),
]:
    if old_n in text:
        text = text.replace(old_n, new_n, 1)
        changed.append(label)
    elif "_persist_nse(state)" in text:
        changed.append(label + "-maybe")
    else:
        raise SystemExit(f"{label} block not found")

pat = re.compile(
    r"    try:\n"
    r"        import telegram_notify\n"
    r"        telegram_notify\.notify\(\n"
    r".*?Quotes on the DELAYED Yahoo fallback.*?"
    r"    except Exception:\n"
    r"        pass",
    re.S,
)
text, n = pat.subn(
    '    print("feed warning: delayed Yahoo fallback (market hours): " + " + ".join(down), flush=True)\n',
    text, count=1)
if n:
    changed.append("strip-yahoo")

pat2 = re.compile(
    r"                    try:\n"
    r"                        import telegram_notify\n"
    r"                        telegram_notify\.notify\(\n"
    r".*?Candle Swing EOD scan failed.*?"
    r"                    except Exception:\n"
    r"                        pass",
    re.S,
)
text, n2 = pat2.subn(
    '                    print(f"swing EOD failed (telegram suppressed): {e}", flush=True)\n',
    text, count=1)
if n2:
    changed.append("strip-swing")

p.write_text(text)
print("changed:", changed)
assert "NSE_RVOL_OL = 1.0" in text
assert "retry later in window" in text
assert text.count("_persist_nse(state)") >= 2
print("OK")
