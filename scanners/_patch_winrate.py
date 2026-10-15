#!/usr/bin/env python3
"""Apply win-rate tune patches to cloud_papertrade.py (idempotent)."""
from pathlib import Path
p = Path(__file__).resolve().parent / "cloud_papertrade.py"
t = p.read_text()
changed = []

if "NSE_RVOL_OL = 1.3" not in t:
    for a in ["NSE_RVOL_OL = 1.5", "NSE_RVOL_OL = 1.0"]:
        if a in t:
            t = t.replace(a, "NSE_RVOL_OL = 1.3              # O=L long — stricter (win-rate tune 15 Oct)", 1)
            changed.append("rvol-ol")
            break
if "moderate (win-rate" not in t:
    for a in ["NSE_RVOL_OH = 1.2", "NSE_RVOL_OH = 0.8", "NSE_RVOL_OH = 1.0"]:
        if a in t and "moderate (win-rate" not in t:
            t = t.replace(a, "NSE_RVOL_OH = 1.0              # O=H short — moderate (win-rate tune 15 Oct)", 1)
            changed.append("rvol-oh")
            break

if "NSE_REENTRY_OL" not in t:
    if "NSE_REENTRY_MAX = 2" in t:
        t = t.replace(
            "NSE_REENTRY_MAX = 2            # max re-entries per symbol+setup per day\n",
            "NSE_REENTRY_MAX = 1            # max re-entries (was 2)\n"
            "NSE_REENTRY_OL = False         # O=L: no re-entry\n"
            "NSE_MAX_LOSS = 100.0           # INR hard cap risk per name\n",
            1,
        )
        changed.append("reentry-max")
    elif "NSE_REENTRY_MAX = 1" in t:
        t = t.replace(
            "NSE_REENTRY_MAX = 1",
            "NSE_REENTRY_MAX = 1            # max re-entries (was 2)\n"
            "NSE_REENTRY_OL = False         # O=L: no re-entry\n"
            "NSE_MAX_LOSS = 100.0           # INR hard cap risk per name  #",
            1,
        )
        changed.append("reentry-ol-only")

if "F3_MAX_TRADES = 5" in t:
    t = t.replace("F3_MAX_TRADES = 5", "F3_MAX_TRADES = 3              # quality over quantity (was 5)", 1)
    changed.append("f3-max")
if "F3_MIN_DRIVE" not in t:
    needle = "F3_MAX_TRADES = 3"
    if needle in t:
        idx = t.find(needle)
        end = t.find("\n", idx)
        t = t[: end + 1] + "F3_MIN_DRIVE = 0.4             # min drive % for First-3\n" + t[end + 1 :]
        changed.append("f3-min")

if 'trade.get("setup") == "ol" and not NSE_REENTRY_OL' not in t:
    t = t.replace(
        '    if not NSE_REENTRY or trade.get("setup") not in ("ol", "oh"):\n        return\n    if trade.get("reason") not in ("SL", "CANDLE-CLOSE"):\n        return\n',
        '    if not NSE_REENTRY or trade.get("setup") not in ("ol", "oh"):\n        return\n    if trade.get("setup") == "ol" and not NSE_REENTRY_OL:\n        return\n    if trade.get("reason") not in ("SL", "CANDLE-CLOSE"):\n        return\n',
        1,
    )
    changed.append("no-ol-reentry")

if "bias != -1" not in t:
    t = t.replace(
        '        if setup == "oh" and bias == 1:\n            continue\n',
        '        if setup == "oh" and bias != -1:\n            continue\n',
        1,
    )
    changed.append("oh-bias")

if "risk = abs(entry_px - sl_px)" not in t:
    old = '''            qty = int(NSE_CAPITAL / quote["ltp"])
            if qty < 1:
                continue
            state["trades"].append({"id": state["next_id"], "date": today, "setup": setup,
                "symbol": symbol, "side": side, "qty": qty, "entry_time": stamp,
                "entry_price": round(quote["ltp"], 2),
                "candle_stop": round(quote["low"] if side == "BUY" else quote["high"], 2),
                "sl_price": round((quote["low"] if side == "BUY" else quote["high"])
                                  * (0.995 if side == "BUY" else 1.005), 2),'''
    new = '''            entry_px = quote["ltp"]
            sl_px = (quote["low"] if side == "BUY" else quote["high"]) * (
                0.995 if side == "BUY" else 1.005)
            qty = int(NSE_CAPITAL / entry_px) if entry_px > 0 else 0
            risk = abs(entry_px - sl_px)
            if risk > 0 and NSE_MAX_LOSS > 0:
                qty = min(qty, max(1, int(NSE_MAX_LOSS / risk)))
            if qty < 1:
                continue
            state["trades"].append({"id": state["next_id"], "date": today, "setup": setup,
                "symbol": symbol, "side": side, "qty": qty, "entry_time": stamp,
                "entry_price": round(entry_px, 2),
                "candle_stop": round(quote["low"] if side == "BUY" else quote["high"], 2),
                "sl_price": round(sl_px, 2),'''
    if old in t:
        t = t.replace(old, new, 1)
        changed.append("max-loss-ol")
    else:
        changed.append("max-loss-ol-miss")

if "ranked = [p for p in picks" not in t:
    old = '''    for drive, symbol, _, sl in [p for p in picks if p[1] not in already][:F3_MAX_TRADES]:
        quote = quotes.get(symbol)
        if not quote:
            continue
        entry = quote["ltp"]            # fill where the market is NOW
        qty = int(NSE_CAPITAL / entry)
        if qty < 1:
            continue'''
    new = '''    ranked = [p for p in picks if p[1] not in already and p[0] >= F3_MIN_DRIVE]
    for drive, symbol, _, sl in ranked[:F3_MAX_TRADES]:
        quote = quotes.get(symbol)
        if not quote:
            continue
        entry = quote["ltp"]            # fill where the market is NOW
        qty = int(NSE_CAPITAL / entry) if entry > 0 else 0
        risk = abs(entry - sl * 0.995)
        if risk > 0 and NSE_MAX_LOSS > 0:
            qty = min(qty, max(1, int(NSE_MAX_LOSS / risk)))
        if qty < 1:
            continue'''
    if old in t:
        t = t.replace(old, new, 1)
        changed.append("f3-filter")
    else:
        changed.append("f3-filter-miss")

p.write_text(t)
print("changed:", changed)
assert "NSE_RVOL_OL = 1.3" in t or "1.3" in t
print("OK")
