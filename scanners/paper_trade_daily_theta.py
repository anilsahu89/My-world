#!/usr/bin/env python3
"""
Nifty Daily Theta — Paper Trade Tracker
Sells a hedged credit spread Mon-Wed, exits at 50% profit or SL or expiry.

Rules (DTE fix, 30 Sep 2026 — NIFTY weeklies expire TUESDAYS now; the
1-year real-premium backtest says the juice is in 1-4 DTE entries, i.e.
Monday = 1 DTE and Friday = 4 DTE under the new calendar):
  - Entry: Mon, Tue, Wed, Fri after 1 PM (the 1-4 DTE gate does the work)
  - VIX < 22 (skip if higher)
  - VIX regime (falling vs 9d avg) RECORDED on the trade, not a veto —
    owner directive 6 Oct 2026: daily frequency first, win-rate tuning
    later. (1y backtest had shown the falling-VIX filter helps: worst
    -₹2,145 vs -₹7,312, PF 2.78→5.85 — data kept on each trade so the
    filter can be re-enabled selectively later)
  - Above 20 SMA → Bull Put | Below → Bear Call
  - Sell 300 OTM, Buy 400 OTM (100pt spread)
  - Nearest expiry 1-4 DTE
  - Credit ≥ ₹3
  - Exit: 50% profit OR strike hit (SL) OR expiry
  - Max 1 open position at a time
"""

import csv, io, math, urllib.request, zipfile
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from collections import defaultdict

IST = timezone(timedelta(hours=5, minutes=30))


def ist_today() -> date:
    return datetime.now(IST).date()

import telegram_notify

BASE = Path(__file__).parent
RAW_DIR = BASE / "data" / "raw"
TRADES_FILE = BASE / "papertrades" / "daily_theta_trades.csv"
SPOT_VIX_FILE = BASE / "data" / "nifty_spot_vix.csv"
NIFTY_OPTS_FILE = BASE / "data" / "nifty_options_daily.csv"

NIFTY_LOT = 75
BB_PERIOD = 20
SHORT_OTM = 300
SPREAD_WIDTH = 100
VIX_MAX = 22
VIX_SMA_PERIOD = 9  # falling-VIX gate: no fresh entries when VIX > its own 9-day avg
PROFIT_PCT = 0.50
MIN_CREDIT = 3.0
MAX_DTE = 8   # owner 6 Oct: cover every day — NIFTY weeklies expire TUESDAYS,
              # so a 1-4 window made Tue (expiry, 0 DTE) / Wed (6) / Thu (5)
              # structurally untradeable; 8 lets Wed/Thu sell next week's
              # spread and Tue to roll into it
ENTRY_DAYS = [0, 1, 2, 4]  # Mon-Wed + Fri; 1-4 DTE gate filters to Mon/Fri


def parse_date(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()

def to_float(v):
    try: return float(v)
    except: return 0.0
def to_int(v):
    try: return int(float(v))
    except: return 0

def read_zip_csv(path):
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names: return
        with zf.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            yield from csv.DictReader(text)


def load_spot_vix(day):
    """Load spot+VIX for a day from flat file or download."""
    if SPOT_VIX_FILE.exists():
        with SPOT_VIX_FILE.open() as f:
            for row in csv.DictReader(f):
                if row["date"] == day.isoformat():
                    return float(row["nifty_spot"]), float(row["vix"])

    # Download from index archive
    ddmmyyyy = day.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
        spot, vix = None, None
        for row in csv.DictReader(io.StringIO(text)):
            name = row.get("Index Name", "")
            close = row.get("Closing Index Value", "")
            if name == "Nifty 50" and close: spot = float(close.replace(",", ""))
            elif name == "India VIX" and close: vix = float(close.replace(",", ""))
        if spot and vix:
            # Append to flat file
            with SPOT_VIX_FILE.open("a", newline="") as f:
                csv.writer(f).writerow([day.isoformat(), spot, vix])
            return spot, vix
    except Exception:
        pass
    # yfinance fallback — NSE archives 403 datacenter IPs regularly
    try:
        import yfinance as yf
        kw = dict(interval="1d", progress=False, threads=False,
                  auto_adjust=False)
        s = yf.download("^NSEI", start=day.isoformat(),
                        end=(day + timedelta(days=1)).isoformat(), **kw)
        v = yf.download("^INDIAVIX", start=day.isoformat(),
                        end=(day + timedelta(days=1)).isoformat(), **kw)
        if s is not None and not s.empty and v is not None and not v.empty:
            spot = float(s["Close"].iloc[-1].iloc[0]
                         if hasattr(s["Close"].iloc[-1], "iloc")
                         else s["Close"].iloc[-1])
            vix = float(v["Close"].iloc[-1].iloc[0]
                        if hasattr(v["Close"].iloc[-1], "iloc")
                        else v["Close"].iloc[-1])
            if spot and vix:
                with SPOT_VIX_FILE.open("a", newline="") as fh:
                    csv.writer(fh).writerow([day.isoformat(), spot, vix])
                return spot, vix
    except Exception:
        pass
    return None, None


def load_spot_history(day, n=25):
    spots = []
    if SPOT_VIX_FILE.exists():
        with SPOT_VIX_FILE.open() as f:
            for row in csv.DictReader(f):
                d = parse_date(row["date"])
                if d < day:
                    spots.append(float(row["nifty_spot"]))
    return spots[-n:]


def load_vix_history(day, n=25):
    vixs = []
    if SPOT_VIX_FILE.exists():
        with SPOT_VIX_FILE.open() as f:
            for row in csv.DictReader(f):
                d = parse_date(row["date"])
                if d < day:
                    vixs.append(float(row["vix"]))
    return vixs[-n:]


def load_nifty_options(day):
    """Load Nifty options for a day from flat file or download FO bhavcopy."""
    # Check flat file first
    if NIFTY_OPTS_FILE.exists():
        opts = {}
        with NIFTY_OPTS_FILE.open() as f:
            for row in csv.DictReader(f):
                if row["date"] == day.isoformat():
                    key = (row["expiry"], float(row["strike"]), row["opt_type"])
                    opts[key] = {"close": float(row["close"]), "vol": int(row["volume"])}
        if opts:
            return opts

    # Download FO bhavcopy
    ymd = day.strftime("%Y%m%d")
    fo_path = RAW_DIR / f"fo_{ymd}.zip"
    if not fo_path.exists():
        url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                fo_path.parent.mkdir(parents=True, exist_ok=True)
                fo_path.write_bytes(resp.read())
        except:
            return {}

    opts = {}
    for row in read_zip_csv(fo_path):
        if row.get("TckrSymb") == "NIFTY" and row.get("FinInstrmTp") == "IDO":
            close = to_float(row.get("ClsPric", 0))
            vol = to_int(row.get("TtlTradgVol", 0))
            if close > 0 and vol > 0:
                key = (row.get("XpryDt", "")[:10], float(row.get("StrkPric", 0)), row.get("OptnTp", ""))
                opts[key] = {"close": close, "vol": vol}
    return opts


def get_nearest_expiry(opts, day, max_days=MAX_DTE):
    expiries = sorted(set(k[0] for k in opts.keys()))
    for exp_str in expiries:
        exp_date = parse_date(exp_str)
        dte = (exp_date - day).days
        if 1 <= dte <= max_days:
            return exp_str, exp_date, dte
    return None, None, None


def find_strike(opts, expiry, opt_type, target, direction):
    available = sorted(set(k[1] for k in opts.keys()
                          if k[0] == expiry and k[2] == opt_type and (expiry, k[1], opt_type) in opts))
    if not available: return None
    if direction == "above":
        for s in available:
            if s >= target: return s
        return available[-1]
    else:
        for s in reversed(available):
            if s <= target: return s
        return available[0]


def init_trades_file():
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not TRADES_FILE.exists():
        fields = ["entry_date", "direction", "opt_type", "short_strike", "long_strike",
                  "expiry", "dte", "spot_entry", "vix_entry", "short_premium", "long_premium",
                  "net_credit", "status", "exit_date", "exit_reason", "exit_credit",
                  "pnl", "holding_days", "spot_exit", "notes"]
        with TRADES_FILE.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def _alert_close(t):
    try:
        pnl = float(t.get("pnl") or 0)
        emoji = "\u2705" if pnl > 0 else "\U0001F6D1"
        telegram_notify.notify(
            emoji + " CLOSE \u00b7 NIFTY Daily Theta (" + str(t.get("exit_reason", "?")) + ")\n"
            + str(t.get("direction")) + " " + str(t.get("short_strike")) + "/"
            + str(t.get("long_strike")) + " " + str(t.get("opt_type"))
            + "\nP&L \u20b9{:,.0f}".format(pnl))
    except Exception:
        pass


def _alert_open(sig):
    try:
        telegram_notify.notify(
            "\U0001F6E1\uFE0F OPEN \u00b7 NIFTY Daily Theta\n"
            + sig["direction"] + " " + str(sig["short_strike"]) + "/"
            + str(sig["long_strike"]) + " " + sig["opt_type"]
            + " \u00b7 exp " + sig["expiry"]
            + "\ncredit \u20b9" + str(sig["net_credit"])
            + " \u00b7 VIX " + str(sig["vix_entry"])
            + " \u00b7 spot {:,.0f}".format(sig["spot_entry"]))
    except Exception:
        pass


def check_signal(day):
    """Check if a trade signal exists on this day."""
    if day.weekday() not in ENTRY_DAYS:
        names = ["Mon", "Tue", "Wed", "Thu", "Fri"][day.weekday()]
        return None, (f"{names} is not an entry day "
                      "(Mon/Tue/Wed/Fri, 1-4 DTE gate) — skip")

    spot, vix = load_spot_vix(day)
    if spot is None:
        return None, "No spot/VIX data"

    if vix > VIX_MAX:
        return None, f"VIX {vix:.1f} > {VIX_MAX} — skip"

    # VIX regime (entries only; open trades are managed as usual). Owner
    # directive 6 Oct: take the daily trade regardless of VIX trend — the
    # regime is RECORDED on the trade instead of vetoing it (win-rate can
    # be tuned later; trade frequency is the primary goal)
    vix_regime = "unknown"
    vix_vals = load_vix_history(day, VIX_SMA_PERIOD - 1) + [vix]
    if len(vix_vals) == VIX_SMA_PERIOD:
        vix_sma = sum(vix_vals) / VIX_SMA_PERIOD
        vix_regime = "falling" if vix <= vix_sma else "rising"

    spot_hist = load_spot_history(day)
    if len(spot_hist) < BB_PERIOD:
        return None, f"Insufficient history ({len(spot_hist)}/{BB_PERIOD})"
    sma = sum(spot_hist[-BB_PERIOD:]) / BB_PERIOD

    opts = load_nifty_options(day)
    if not opts:
        return None, "No options data"

    exp_str, exp_date, dte = get_nearest_expiry(opts, day)
    if not exp_str:
        return None, "No suitable expiry (1-4 DTE)"

    if spot > sma:
        direction, opt_type = "BULL_PUT", "PE"
        short_target = math.floor(spot / 50) * 50 - SHORT_OTM
        long_target = short_target - SPREAD_WIDTH
        short_s = find_strike(opts, exp_str, opt_type, short_target, "below")
        long_s = find_strike(opts, exp_str, opt_type, long_target, "below")
    else:
        direction, opt_type = "BEAR_CALL", "CE"
        short_target = math.ceil(spot / 50) * 50 + SHORT_OTM
        long_target = short_target + SPREAD_WIDTH
        short_s = find_strike(opts, exp_str, opt_type, short_target, "above")
        long_s = find_strike(opts, exp_str, opt_type, long_target, "above")

    if not short_s or not long_s or short_s == long_s:
        return None, "Strikes not available"

    short_key = (exp_str, short_s, opt_type)
    long_key = (exp_str, long_s, opt_type)
    if short_key not in opts or long_key not in opts:
        return None, "Option premiums not available"

    credit = opts[short_key]["close"] - opts[long_key]["close"]
    if credit < MIN_CREDIT:
        return None, f"Credit too low (₹{credit:.1f} < ₹{MIN_CREDIT})"

    signal = {
        "entry_date": day.isoformat(), "direction": direction, "opt_type": opt_type,
        "short_strike": short_s, "long_strike": long_s, "expiry": exp_str, "dte": dte,
        "spot_entry": round(spot, 1), "vix_entry": round(vix, 1),
        "vix_regime": vix_regime,
        "short_premium": round(opts[short_key]["close"], 2),
        "long_premium": round(opts[long_key]["close"], 2),
        "net_credit": round(credit, 2), "sma": round(sma, 1),
    }
    return signal, (f"✅ SIGNAL: {direction} {short_s}/{long_s} {opt_type} | "
                    f"Credit ₹{credit:.1f} | VIX {vix:.1f} ({vix_regime}) | DTE {dte}")


def update_exits(day):
    """Check exits for open positions."""
    if not TRADES_FILE.exists():
        return [], 0.0

    trades = list(csv.DictReader(TRADES_FILE.open()))
    opts = load_nifty_options(day)
    spot, vix = load_spot_vix(day)

    updated = []
    closed_today = []
    day_pnl = 0.0

    for t in trades:
        if t["status"] != "OPEN":
            updated.append(t)
            continue

        short_key = (t["expiry"], float(t["short_strike"]), t["opt_type"])
        long_key = (t["expiry"], float(t["long_strike"]), t["opt_type"])

        short_now = opts.get(short_key, {}).get("close", float(t["short_premium"]))
        long_now = opts.get(long_key, {}).get("close", float(t["long_premium"]))
        current_spread = short_now - long_now
        credit = float(t["net_credit"])

        exit_reason = None
        exit_pnl = None

        exp_date = parse_date(t["expiry"])
        if day >= exp_date and spot:
            exit_reason = "expiry"
            if t["opt_type"] == "PE":
                si = max(float(t["short_strike"]) - spot, 0)
                li = max(float(t["long_strike"]) - spot, 0)
            else:
                si = max(spot - float(t["short_strike"]), 0)
                li = max(spot - float(t["long_strike"]), 0)
            exit_pnl = (credit - (si - li)) * NIFTY_LOT

        elif current_spread <= credit * (1 - PROFIT_PCT):
            exit_reason = "profit_50pct"
            exit_pnl = (credit - current_spread) * NIFTY_LOT

        elif spot and t["opt_type"] == "PE" and spot <= float(t["short_strike"]):
            exit_reason = "sl_strike_hit"
            exit_pnl = (credit - current_spread) * NIFTY_LOT
        elif spot and t["opt_type"] == "CE" and spot >= float(t["short_strike"]):
            exit_reason = "sl_strike_hit"
            exit_pnl = (credit - current_spread) * NIFTY_LOT

        if not exit_reason and short_key in opts and long_key in opts:
            # persist the MTM mark on open rows (portal Open P&L column) —
            # only when today's bhavcopy actually carried these strikes;
            # otherwise leave absent so the portal shows "—" not a fake 0
            t["mark_credit"] = round(current_spread, 2)
            t["live_pnl"] = round((credit - current_spread) * NIFTY_LOT, 2)
            t["mark_date"] = day.isoformat()

        if exit_reason:
            t["status"] = "CLOSED"
            _alert_close(t)
            t["exit_date"] = day.isoformat()
            t["exit_reason"] = exit_reason
            t["exit_credit"] = round(current_spread, 2)
            t["pnl"] = round(exit_pnl, 2)
            t["holding_days"] = (day - parse_date(t["entry_date"])).days
            t["spot_exit"] = round(spot, 1) if spot else ""
            day_pnl += exit_pnl
            closed_today.append(t)

        updated.append(t)

    with TRADES_FILE.open("w", newline="") as f:
        if updated:
            # union of keys across rows: new mark columns on open rows must
            # not break the writer when older rows lack them
            fieldnames = list(dict.fromkeys(
                k for r in updated for k in r.keys()))
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(updated)

    return closed_today, day_pnl


def print_report():
    if not TRADES_FILE.exists():
        print("  No trades file yet.")
        return

    trades = list(csv.DictReader(TRADES_FILE.open()))
    if not trades:
        print("  No trades yet.")
        return

    open_t = [t for t in trades if t["status"] == "OPEN"]
    closed_t = [t for t in trades if t["status"] == "CLOSED"]

    print(f"\n{'='*95}")
    print(f"  💰 NIFTY DAILY THETA — PAPER TRADES")
    print(f"  Rules: Mon/Tue/Wed/Fri 1PM | VIX<{VIX_MAX} (regime recorded) | {SHORT_OTM}OTM | {SPREAD_WIDTH}pt spread | 50% target")
    print(f"{'='*95}")

    if open_t:
        print(f"\n  🟢 OPEN POSITIONS ({len(open_t)}):")
        print(f"  {'Entry':<12} {'Dir':<10} {'Short':>7} {'Long':>7} {'Type':<4} {'Credit':>7} {'DTE':>4} {'Expiry':<12} {'VIX':>5}")
        print("  " + "-" * 80)
        for t in open_t:
            print(f"  {t['entry_date']:<12} {t['direction']:<10} {t['short_strike']:>7} {t['long_strike']:>7} {t['opt_type']:<4} ₹{float(t['net_credit']):>6.1f} {t['dte']:>4} {t['expiry']:<12} {float(t['vix_entry']):>4.1f}")

    if closed_t:
        print(f"\n  🔴 CLOSED TRADES ({len(closed_t)}):")
        print(f"  {'Entry':<12} {'Exit':<12} {'Dir':<10} {'Reason':<14} {'Credit':>7} {'P&L':>9} {'Hold':>5}")
        print("  " + "-" * 75)
        total_pnl = 0
        wins = 0
        for t in closed_t:
            pnl = float(t.get("pnl", 0))
            total_pnl += pnl
            if pnl > 0: wins += 1
            emoji = "✅" if pnl > 0 else "❌"
            print(f"  {t['entry_date']:<12} {t['exit_date']:<12} {t['direction']:<10} {t['exit_reason']:<14} ₹{float(t['net_credit']):>6.1f} ₹{pnl:>8.0f} {t.get('holding_days','?'):>4}d {emoji}")

        wr = wins / len(closed_t) * 100 if closed_t else 0
        print(f"\n  📈 SUMMARY: {len(closed_t)} trades | WR {wr:.0f}% | Net P&L ₹{total_pnl:,.0f}")

    if not open_t and not closed_t:
        print("  No trades yet.")


def find_latest_date():
    d = date.today()
    for _ in range(10):
        if d.weekday() < 5:
            spot, vix = load_spot_vix(d)
            if spot:
                return d
        d -= timedelta(days=1)
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nifty Daily Theta Paper Trade Tracker")
    parser.add_argument("--scan", action="store_true", help="Check signal + update exits")
    parser.add_argument("--report", action="store_true", help="Print report only")
    parser.add_argument("--date", help="Specific date YYYY-MM-DD")
    args = parser.parse_args()

    init_trades_file()

    if args.report or not args.scan:
        print_report()
        return

    day = parse_date(args.date) if args.date else find_latest_date()
    if day is None:
        print("❌ Could not find any available data")
        return

    print(f"\n{'='*95}")
    print(f"  💰 NIFTY DAILY THETA UPDATE — {day.strftime('%a, %d %b %Y')}")
    print(f"{'='*95}")

    # 1. Check exits
    print("\n  Checking open positions...")
    closed, day_pnl = update_exits(day)
    if closed:
        for t in closed:
            pnl = float(t["pnl"])
            emoji = "✅" if pnl > 0 else "❌"
            print(f"  {emoji} EXIT: {t['direction']} {t['short_strike']}/{t['long_strike']} → {t['exit_reason']} | P&L ₹{pnl:,.0f}")
    else:
        print("  No exits today.")

    # 2. Check for new signal (but only if no open position)
    trades = list(csv.DictReader(TRADES_FILE.open())) if TRADES_FILE.exists() else []
    has_open = any(t["status"] == "OPEN" for t in trades)

    print(f"\n  Checking for new signal...")
    if has_open:
        print("  ⏸️  Open position exists — waiting for exit before new entry")
    else:
        signal, msg = check_signal(day)
        print(f"  {msg}")

        if signal:
            signal["status"] = "OPEN"
            signal["exit_date"] = ""
            signal["exit_reason"] = ""
            signal["exit_credit"] = ""
            signal["pnl"] = ""
            signal["holding_days"] = ""
            signal["spot_exit"] = ""
            signal["notes"] = ""

            trades.append(signal)
            _alert_open(signal)
            fields = list(signal.keys())
            with TRADES_FILE.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(trades)

            print(f"\n  ✅ ENTERED: {signal['direction']}")
            print(f"     Sell {signal['short_strike']} {signal['opt_type']} @ ₹{signal['short_premium']}")
            print(f"     Buy  {signal['long_strike']} {signal['opt_type']} @ ₹{signal['long_premium']}")
            print(f"     Net credit: ₹{signal['net_credit']} | DTE: {signal['dte']} | Expiry: {signal['expiry']}")
            print(f"     Max profit: ₹{signal['net_credit'] * NIFTY_LOT:.0f} | Max loss: ₹{(SPREAD_WIDTH - signal['net_credit']) * NIFTY_LOT:.0f}")

    print_report()


if __name__ == "__main__":
    main()
