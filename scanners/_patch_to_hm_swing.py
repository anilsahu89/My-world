#!/usr/bin/env python3
"""Surgical: convert cloud_swing.py candlestick patterns → weekly HM buy/sell."""
from pathlib import Path
import re

p = Path(__file__).resolve().parent / "cloud_swing.py"
t = p.read_text()

if "HM Swing desk" not in t and '"""HM Swing' not in t:
    t = re.sub(
        r'^"""[\s\S]*?"""',
        '"""HM Swing desk — weekly Hilega-Milega (replaces CS-MARU/PINS/ENGF).\n\n'
        'BUY  (HM-BUY):  RSI9>50, red WMA21(RSI)<RSI, green WMA3(RSI)>red\n'
        'SELL (HM-SELL): RSI9<50, red>RSI, green<red (new-low / sell zone)\n'
        'Signal newly true on weekly bar → fill next open. SL=3-week extreme, 2R, 15 sessions.\n'
        '"""',
        t,
        count=1,
    )

t = re.sub(
    r'CFG = \{[^}]+\}',
    'CFG = {"end_date": "2026-12-31", "capital_inr": 10_000.0,\n'
    '       "max_open": 5, "rr": 2.0, "max_sessions": 15, "min_spread": 3.0,\n'
    '       "max_new_per_day": 3}',
    t,
    count=1,
)
t = re.sub(
    r'ENABLED_SETUPS = \{[^}]+\}',
    'ENABLED_SETUPS = {"HM-BUY", "HM-SELL"}',
    t,
    count=1,
)

new_detect = '''
def rsi9(c):
    import numpy as np
    d = c.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ma_up = up.ewm(alpha=1/9, min_periods=9, adjust=False).mean()
    ma_dn = dn.ewm(alpha=1/9, min_periods=9, adjust=False).mean()
    rs = ma_up / ma_dn.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def wma(s, n):
    import numpy as np
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: (x * w).sum() / w.sum(), raw=True)


def weekly_hm(df):
    if df is None or len(df) < 80:
        return None
    w = df.resample("W-FRI").agg(
        {"Open": "first", "High": "max", "Low": "min",
         "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    if len(w) < 30:
        return None
    r = rsi9(w["Close"])
    w = w.assign(rsi=r, red=wma(r, 21), green=wma(r, 3)).dropna(
        subset=["rsi", "red", "green"])
    return w if len(w) >= 5 else None


def detect(df):
    """Weekly HM newly-true signals. Returns list of (setup, side, sl_px)."""
    w = weekly_hm(df)
    if w is None or len(w) < 4:
        return []
    last, prev = w.iloc[-1], w.iloc[-2]
    r, red, g = float(last["rsi"]), float(last["red"]), float(last["green"])
    r1, red1, g1 = float(prev["rsi"]), float(prev["red"]), float(prev["green"])
    spread = max(r, red, g) - min(r, red, g)
    if spread < CFG.get("min_spread", 3.0):
        return []
    lo3 = float(w["Low"].iloc[-3:].min())
    hi3 = float(w["High"].iloc[-3:].max())
    buy_now = r > 50 and red < r and g > red
    buy_prev = r1 > 50 and red1 < r1 and g1 > red1
    sell_now = r < 50 and red > r and g < red
    sell_prev = r1 < 50 and red1 > r1 and g1 < red1
    sigs = []
    if buy_now and not buy_prev:
        sigs.append(("HM-BUY", "BUY", lo3 * 0.995))
    if sell_now and not sell_prev:
        sigs.append(("HM-SELL", "SELL", hi3 * 1.005))
    return sigs

'''

t2 = re.sub(
    r'\ndef detect\(df: pd\.DataFrame\)[\s\S]*?\n    return sigs\n',
    '\n' + new_detect + '\n',
    t,
    count=1,
)
if t2 == t:
    t2 = re.sub(
        r'\ndef detect\(df\)[\s\S]*?\n    return sigs\n',
        '\n' + new_detect + '\n',
        t,
        count=1,
    )
t = t2

t = t.replace('period="1y"', 'period="2y"')
t = t.replace('auto_adjust=False', 'auto_adjust=True')
t = t.replace('prio = {"CS-PINS": 0, "CS-ENGF": 1, "CS-MARU": 9}', 'prio = {"HM-BUY": 0, "HM-SELL": 1}')
t = t.replace('prio = {"CS-MARU": 0, "CS-PINS": 1, "CS-ENGF": 2}', 'prio = {"HM-BUY": 0, "HM-SELL": 1}')
t = t.replace('"TIME10"', '"TIME15"')
t = t.replace('Candle Swing', 'HM Swing')

if 'candlestick setups' in t:
    t = re.sub(
        r'"rule":\s*"[^"]*(?:\\.[^"]*)*"',
        '"rule": "HM Swing (weekly): BUY RSI9>50 red<RSI green>red · '
        'SELL RSI9<50 red>RSI green<red · next open · 3w extreme SL · 2R · 15 sessions · max 5"',
        t,
        count=1,
    )
if '"note":' in t:
    t = re.sub(
        r'"note":\s*"[^"]*(?:\\.[^"]*)*"',
        '"note": "Candlestick CS-MARU/PINS/ENGF retired 15 Oct 2026. Same HM lines as HM Positional."',
        t,
        count=1,
    )

p.write_text(t)
print("patched", p)
assert "HM-BUY" in t and "HM-SELL" in t
print("OK", len(t))
