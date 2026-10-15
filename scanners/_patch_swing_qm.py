#!/usr/bin/env python3
from pathlib import Path

s = Path(__file__).resolve().parent / "cloud_swing.py"
t = s.read_text()
if "ENABLED_SETUPS" not in t:
    t = t.replace(
        'CFG = {"end_date": "2026-12-31", "capital_inr": 10_000.0, "capital_usd": 60.0,\n       "max_open": 5, "rr": 2.0, "max_sessions": 10}',
        'CFG = {"end_date": "2026-12-31", "capital_inr": 10_000.0, "capital_usd": 60.0,\n       "max_open": 3, "rr": 2.0, "max_sessions": 10}\nENABLED_SETUPS = {"CS-PINS", "CS-ENGF"}  # CS-MARU paused (0/6 live)',
        1,
    )
if 'prio = {"CS-PINS": 0' not in t:
    t = t.replace(
        '    prio = {"CS-MARU": 0, "CS-PINS": 1, "CS-ENGF": 2}',
        '    prio = {"CS-PINS": 0, "CS-ENGF": 1, "CS-MARU": 9}',
        1,
    )
if "setup not in ENABLED_SETUPS" not in t:
    t = t.replace(
        '        for setup, side, sl in detect(df):\n            if (symbol, setup, d) in existing:\n                continue\n            cands.append((prio[setup], market, symbol, setup, side, sl, d))',
        '        for setup, side, sl in detect(df):\n            if setup not in ENABLED_SETUPS:\n                continue\n            if (symbol, setup, d) in existing:\n                continue\n            cands.append((prio[setup], market, symbol, setup, side, sl, d))',
        1,
    )
if "CS-MARU paused" not in t:
    t = t.replace(
        "top-3 candlestick setups (backtested 5y): MARU-at-low BUY PF 1.39 · ShootingStar+VOL SELL PF 1.15 · Engulf-at-low BUY PF 1.13",
        "CS-PINS + CS-ENGF only (CS-MARU paused 15 Oct — 0/6 live); max 3 open; PINS priority",
        1,
    )
s.write_text(t)
print("swing ok")

q = Path(__file__).resolve().parent / "cloud_qm.py"
t = q.read_text()
if "MAX_OPEN = 10" not in t:
    t = t.replace("MAX_OPEN = 16", "MAX_OPEN = 10               # win-rate/capital tune 15 Oct (was 16)", 1)
if "TIME_STOP_SESSIONS" not in t:
    t = t.replace(
        "RSI_EXIT = 86\nAVG_AT = 0.50",
        "RSI_EXIT = 86\nTIME_STOP_SESSIONS = 45      # free capital if stuck underwater this long\nAVG_AT = 0.50",
        1,
    )
if "TIME45" not in t:
    t = t.replace(
        'if r0 >= RSI_EXIT:\n            _close(t, c, "RSI86")',
        'if r0 >= RSI_EXIT:\n            _close(t, c, "RSI86")\n        elif int(t.get("sessions") or 0) >= TIME_STOP_SESSIONS and float(t.get("pnl") or 0) < 0:\n            _close(t, c, "TIME45")',
        1,
    )
q.write_text(t)
print("qm ok")
