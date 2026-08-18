#!/usr/bin/env python3
"""
Live Open=Low Scanner — Real-Time Intraday
Uses yfinance to get 5-minute candle data for NSE 200 stocks.
Runs at 9:30 AM to find Open=Low signals with volume confirmation.

Usage:
  python3 live_scan_open_low.py              # Scan all NSE 200
  python3 live_scan_open_low.py --top 5      # Show top 5
  python3 live_scan_open_low.py --watch      # Live mode (refresh every 2 min)
"""

import csv, time, sys, argparse
from pathlib import Path
from datetime import datetime, date
import warnings
warnings.filterwarnings("ignore")

NSE200_FILE = Path(__file__).parent / "data" / "nse200_symbols.csv"
INVESTMENT = 10000
OL_DIFF_MAX = 0.10  # 10 paisa
SL_PCT = 0.5
MIN_VOL_MULT = 1.5
VOL_LOOKBACK_DAYS = 20


def load_nse200():
    syms = []
    with NSE200_FILE.open() as f:
        next(f)
        for line in f:
            s = line.strip()
            if s:
                syms.append(s)
    return syms


def fetch_intraday(symbol):
    """Fetch 5-min candle data for a stock using yfinance."""
    import yfinance as yf
    ticker = f"{symbol}.NS"
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d", interval="5m")
        if hist.empty:
            return None
        return hist
    except Exception:
        return None


def fetch_daily_volume(symbol):
    """Fetch 20-day average daily volume."""
    import yfinance as yf
    ticker = f"{symbol}.NS"
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1mo", interval="1d")
        if hist.empty:
            return 0
        vols = hist["Volume"].values[-VOL_LOOKBACK_DAYS:]
        return sum(vols) / len(vols) if len(vols) > 0 else 0
    except Exception:
        return 0


def analyze_stock(symbol, hist, avg_daily_vol):
    """Check if stock has Open=Low signal today."""
    if hist is None or hist.empty:
        return None

    # Filter to today's candles only
    today = date.today()
    today_data = hist[hist.index.date == today]

    if today_data.empty:
        # Try last available date (might be today in IST)
        today_data = hist.tail(50)

    if len(today_data) < 2:
        return None

    # Today's open = first candle's open
    day_open = float(today_data["Open"].iloc[0])
    # Day's low so far = min of all lows
    day_low = float(today_data["Low"].min())
    # Day's high so far
    day_high = float(today_data["High"].max())
    # Current price = last close
    current_price = float(today_data["Close"].iloc[-1])
    # Volume so far = sum of all candle volumes
    day_volume = int(today_data["Volume"].sum())

    if day_open <= 0 or day_low <= 0:
        return None

    # Open=Low check (strict: diff <= 0.10 paisa)
    diff = abs(day_open - day_low)
    if diff > OL_DIFF_MAX:
        return None

    # Price filter
    if day_open < 50:
        return None

    # Volume ratio (compare partial day volume to avg daily vol)
    # Estimate: if we're at 1PM (4hrs into 6.25hr session), scale up
    vol_ratio = day_volume / avg_daily_vol if avg_daily_vol > 0 else 1

    # Calculate trade details
    shares = int(INVESTMENT / day_open)
    if shares == 0:
        return None

    sl_price = day_open * (1 - SL_PCT / 100)
    current_pnl = shares * (current_price - day_open)
    current_pnl_pct = (current_price - day_open) / day_open * 100

    # Number of candles
    num_candles = len(today_data)

    return {
        "symbol": symbol,
        "open": round(day_open, 2),
        "low": round(day_low, 2),
        "high": round(day_high, 2),
        "current": round(current_price, 2),
        "diff": round(diff, 2),
        "volume": day_volume,
        "vol_ratio": round(vol_ratio, 2),
        "shares": shares,
        "sl_price": round(sl_price, 2),
        "current_pnl": round(current_pnl, 2),
        "current_pnl_pct": round(current_pnl_pct, 2),
        "num_candles": num_candles,
        "passes_vol": vol_ratio >= MIN_VOL_MULT,
        "sl_hit": current_price <= sl_price,
    }


def scan_all(symbols, batch_size=10):
    """Scan all NSE 200 stocks."""
    results = []
    total = len(symbols)

    for i in range(0, total, batch_size):
        batch = symbols[i:i + batch_size]
        for sym in batch:
            # First fetch intraday data
            hist = fetch_intraday(sym)
            if hist is None:
                continue

            # Only fetch daily vol if it passes the OL check (save time)
            today = date.today()
            today_data = hist[hist.index.date == today]
            if today_data.empty:
                today_data = hist.tail(50)

            if len(today_data) < 2:
                continue

            day_open = float(today_data["Open"].iloc[0])
            day_low = float(today_data["Low"].min())

            if day_open <= 0 or abs(day_open - day_low) > OL_DIFF_MAX:
                continue

            # Passes OL check — now get volume
            avg_vol = fetch_daily_volume(sym)
            result = analyze_stock(sym, hist, avg_vol)
            if result:
                results.append(result)

        progress = min(i + batch_size, total)
        print(f"\r  Scanning... {progress}/{total} ({len(results)} signals found)", end="", flush=True)

    print()
    return results


def print_results(results, top_n=10):
    # Sort by volume ratio
    results.sort(key=lambda x: x["vol_ratio"], reverse=True)

    print(f"\n{'='*105}")
    print(f"  📊 LIVE OPEN=LOW SCAN — {datetime.now().strftime('%a, %d %b %Y %H:%M')}")
    print(f"  Rule: Open=Low (diff ≤ Rs{OL_DIFF_MAX}) | Vol ≥ {MIN_VOL_MULT}× | SL {SL_PCT}% | Rs{INVESTMENT}/stock")
    print(f"{'='*105}")

    if not results:
        print("\n  No Open=Low signals found right now.")
        return

    # Split into volume-passed and not
    vol_pass = [r for r in results if r["passes_vol"]]
    vol_fail = [r for r in results if not r["passes_vol"]]

    if vol_pass:
        print(f"\n  ✅ SIGNALS WITH VOLUME (Vol ≥ {MIN_VOL_MULT}×) — {len(vol_pass)} stocks:")
        print(f"  {'#':<3} {'Symbol':<14} {'Open':>8} {'Low':>8} {'Diff':>6} {'Curr':>8} {'Vol×':>6} {'SL':>8} {'P&L':>8} {'P&L%':>7} {'Status'}")
        print(f"  {'-'*100}")
        for i, r in enumerate(vol_pass[:top_n], 1):
            status = "🔥 SL HIT" if r["sl_hit"] else ("✅ PROFIT" if r["current_pnl"] > 0 else "⏸️ FLAT")
            print(f"  {i:<3} {r['symbol']:<14} {r['open']:>8.1f} {r['low']:>8.1f} Rs{r['diff']:>4.2f} {r['current']:>8.1f} {r['vol_ratio']:>5.1f}× {r['sl_price']:>8.1f} Rs{r['current_pnl']:>+7.0f} {r['current_pnl_pct']:>+6.2f}% {status}")

        # Top pick details
        top = vol_pass[0]
        print(f"\n  🎯 TOP PICK: {top['symbol']}")
        print(f"     Open: Rs{top['open']:.2f} = Low: Rs{top['low']:.2f} (diff Rs{top['diff']:.2f})")
        print(f"     Current: Rs{top['current']:.2f} | SL: Rs{top['sl_price']:.2f}")
        print(f"     Volume: {top['vol_ratio']:.1f}× average | {top['shares']} shares for Rs{INVESTMENT}")
        print(f"     Current P&L: Rs{top['current_pnl']:+.0f} ({top['current_pnl_pct']:+.2f}%)")

    if vol_fail:
        print(f"\n  📋 OTHER OPEN=LOW (volume < {MIN_VOL_MULT}×) — {len(vol_fail)} stocks:")
        print(f"  {'Symbol':<14} {'Open':>8} {'Low':>8} {'Diff':>6} {'Curr':>8} {'Vol×':>6} {'P&L%':>7}")
        print(f"  {'-'*65}")
        for r in vol_fail[:10]:
            print(f"  {r['symbol']:<14} {r['open']:>8.1f} {r['low']:>8.1f} Rs{r['diff']:>4.2f} {r['current']:>8.1f} {r['vol_ratio']:>5.1f}× {r['current_pnl_pct']:>+6.2f}%")


def main():
    parser = argparse.ArgumentParser(description="Live Open=Low Scanner")
    parser.add_argument("--top", type=int, default=10, help="Show top N results")
    parser.add_argument("--watch", action="store_true", help="Live mode — refresh every 2 min")
    parser.add_argument("--symbols", help="Comma-separated list of specific symbols to scan")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        symbols = load_nse200()

    print(f"Live Open=Low Scanner — {len(symbols)} stocks")

    if args.watch:
        print("Watch mode: refreshing every 2 minutes. Press Ctrl+C to stop.\n")
        try:
            while True:
                results = scan_all(symbols)
                print_results(results, args.top)
                print(f"\n  ⏳ Next scan in 2 minutes... (Ctrl+C to stop)")
                time.sleep(120)
        except KeyboardInterrupt:
            print("\n\n  Stopped.")
    else:
        results = scan_all(symbols)
        print_results(results, args.top)


if __name__ == "__main__":
    main()
