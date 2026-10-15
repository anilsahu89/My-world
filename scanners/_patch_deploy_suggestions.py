#!/usr/bin/env python3
from pathlib import Path
import re

p = Path(__file__).resolve().parent / "cloud_papertrade.py"
text = p.read_text()
changed = []

# RVOL
a = "NSE_RVOL_OL = 1.5              # O=L long  (was 3.0 on the old scale)"
b = "NSE_RVOL_OH = 1.2              # O=H short (was 1.5 on the old scale)"
a2 = "NSE_RVOL_OL = 1.0              # O=L long  (lowered 15 Oct from 1.5 — restart flow)"
b2 = "NSE_RVOL_OH = 0.8              # O=H short (lowered 15 Oct from 1.2 — restart flow)"
if a in text and b in text:
    text = text.replace(a, a2, 1).replace(b, b2, 1)
    changed.append("rvol")
elif "NSE_RVOL_OL = 1.0" in text:
    changed.append("rvol-already")
else:
    raise SystemExit("RVOL not found")

# F3: remove early done_date line
early = "    state[\"f3_done_date\"] = today\n    if nifty_bias() != 1:\n        return\n"
if early in text and "retry later in window" not in text:
    text = text.replace(
        early,
        "    # f3_done_date set only after a successful scan (below)\n"
        "    if nifty_bias() != 1:\n"
        "        print(\"f3_scan: NIFTY bias not long — retry later in window\", flush=True)\n"
        "        return\n",
        1,
    )
    changed.append("f3-delay-done")
elif "retry later in window" in text:
    changed.append("f3-delay-already")
else:
    raise SystemExit("f3 early done not found")

# Wrap yf.download in try if not already
if "hourly bars download failed" not in text:
    old_dl = (
        '    symbols = nse_symbols()\n'
        '    bars = yf.download([f"{s}.NS" for s in symbols], period="5d", interval="1h",\n'
        '                       group_by="ticker", progress=False, threads=True, auto_adjust=False)\n'
        '    picks = []\n'
    )
    new_dl = (
        '    symbols = nse_symbols()\n'
        '    try:\n'
        '        bars = yf.download([f"{s}.NS" for s in symbols], period="5d", interval="1h",\n'
        '                           group_by="ticker", progress=False, threads=True, auto_adjust=False)\n'
        '    except Exception as e:\n'
        '        print(f"f3_scan: hourly bars download failed — retry later: {e}", flush=True)\n'
        '        return\n'
        '    if bars is None or getattr(bars, "empty", False):\n'
        '        print("f3_scan: empty hourly bars — retry later", flush=True)\n'
        '        return\n'
        '    picks = []\n'
    )
    if old_dl in text:
        text = text.replace(old_dl, new_dl, 1)
        changed.append("f3-try-download")
    else:
        raise SystemExit("f3 download block not found")

# Mark done after picks.sort
marker = "    picks.sort(reverse=True)\n    stamp = moment.strftime"
if marker in text and "Successful scan" not in text:
    text = text.replace(
        "    picks.sort(reverse=True)\n    stamp = moment.strftime",
        "    picks.sort(reverse=True)\n"
        "    # Successful scan — mark done even if 0 picks\n"
        "    state[\"f3_done_date\"] = today\n"
        "    stamp = moment.strftime",
        1,
    )
    changed.append("f3-mark-done")
elif "Successful scan" in text or 'state["f3_done_date"] = today' in text:
    changed.append("f3-mark-maybe")
else:
    raise SystemExit("picks.sort block not found")

# persist helper
if "def _persist_nse(" not in text:
    marker = "def write_json(path: Path, value) -> None:"
    i = text.find(marker)
    if i < 0:
        raise SystemExit("write_json missing")
    j = text.find("\n\ndef ", i + 10)
    if j < 0:
        raise SystemExit("after write_json missing")
    helper = (
        "\n\ndef _persist_nse(state: dict) -> None:\n"
        "    \"\"\"Write ledger to disk BEFORE Telegram so an alert never outlives the trade.\"\"\"\n"
        "    try:\n"
        "        write_json(NSE_STATE, state)\n"
        "    except Exception as e:\n"
        "        print(f\"persist nse state failed: {e}\", flush=True)\n"
    )
    text = text[:j] + helper + text[j:]
    changed.append("persist-helper")

for label, needle, insert_line in [
    ("ol",
     '            state["next_id"] += 1\n            try:\n                telegram_notify.notify(\n                    f"⚡ OPEN · O=L/O=H',
     '            state["next_id"] += 1\n            _persist_nse(state)\n            try:\n                telegram_notify.notify(\n                    f"⚡ OPEN · O=L/O=H'),
    ("f3",
     '        state["next_id"] += 1\n        try:\n            telegram_notify.notify(\n                f"⚡ OPEN · First-3 momentum',
     '        state["next_id"] += 1\n        _persist_nse(state)\n        try:\n            telegram_notify.notify(\n                f"⚡ OPEN · First-3 momentum'),
]:
    if needle in text:
        text = text.replace(needle, insert_line, 1)
        changed.append(label + "-persist")
    elif "_persist_nse(state)" in text:
        changed.append(label + "-skip")
    else:
        raise SystemExit(f"{label} notify needle not found")

pat = re.compile(
    r"    try:\n        import telegram_notify\n        telegram_notify\.notify\(\n"
    r".*?Quotes on the DELAYED Yahoo fallback.*?"
    r"    except Exception:\n        pass",
    re.S,
)
text, n = pat.subn(
    '    print("feed warning: delayed Yahoo fallback (market hours): " + " + ".join(down), flush=True)\n',
    text, count=1)
if n:
    changed.append("strip-yahoo")

pat2 = re.compile(
    r"                    try:\n                        import telegram_notify\n                        telegram_notify\.notify\(\n"
    r".*?Candle Swing EOD scan failed.*?"
    r"                    except Exception:\n                        pass",
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
assert text.count("_persist_nse(state)") >= 2
print("OK")
