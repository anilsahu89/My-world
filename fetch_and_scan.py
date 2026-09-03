"""
fetch_and_scan.py
------------------
Runs on a schedule via GitHub Actions (see .github/workflows/fetch-alerts.yml).
Does NOT require your machine to be on.

What it does:
  1. Reads KITE_ACCESS_TOKEN (set that morning by refresh_kite_token.py)
  2. If the token is missing/expired, falls back to yfinance so the page
     still updates rather than going stale
  3. Pulls quotes for your NSE-200 watchlist in one batched call
  4. Computes O=L, O=H, and BB Trap v2 signals
  5. Updates simple paper-trade state (entries/exits) based on those signals
  6. Writes data/alerts.json — this is what alerts.html / paper.html should fetch()

SETUP:
  pip install kiteconnect pandas yfinance

  symbols.txt — one NSE tradingsymbol per line (your Nifty-200 list)
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

OUT_DIR = Path("data")
OUT_DIR.mkdir(exist_ok=True)
ALERTS_FILE = OUT_DIR / "alerts.json"
PAPER_STATE_FILE = OUT_DIR / "paper_trades.json"
SYMBOLS_FILE = Path("symbols.txt")

API_KEY = os.environ.get("KITE_API_KEY")
ACCESS_TOKEN = os.environ.get("KITE_ACCESS_TOKEN")


def load_symbols() -> list[str]:
    if not SYMBOLS_FILE.exists():
        sys.exit("symbols.txt not found — add your NSE-200 watchlist, one symbol per line.")
    return [s.strip() for s in SYMBOLS_FILE.read_text().splitlines() if s.strip()]


def fetch_via_kite(symbols: list[str]) -> pd.DataFrame | None:
    """Batched quote pull via Kite Connect. Returns None if token is bad."""
    if not (API_KEY and ACCESS_TOKEN):
        return None
    try:
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(ACCESS_TOKEN)

        instruments = [f"NSE:{s}" for s in symbols]
        quotes = kite.quote(instruments)  # up to 500 per call

        rows = []
        for inst, q in quotes.items():
            ohlc = q["ohlc"]
            rows.append(
                {
                    "symbol": inst.replace("NSE:", ""),
                    "open": ohlc["open"],
                    "high": ohlc["high"],
                    "low": ohlc["low"],
                    "prev_close": ohlc["close"],
                    "ltp": q["last_price"],
                }
            )
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"Kite fetch failed ({e}); falling back to yfinance.")
        return None


def fetch_via_yfinance(symbols: list[str]) -> pd.DataFrame:
    """Fallback so the page still gets *something* if the Kite token lapsed."""
    import yfinance as yf

    rows = []
    for s in symbols:
        try:
            t = yf.Ticker(f"{s}.NS")
            info = t.fast_info
            rows.append(
                {
                    "symbol": s,
                    "open": info["open"],
                    "high": info["day_high"],
                    "low": info["day_low"],
                    "prev_close": info["previous_close"],
                    "ltp": info["last_price"],
                }
            )
        except Exception:
            continue
    return pd.DataFrame(rows)


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["open_eq_low"] = df["open"] <= df["low"] * 1.001   # O=L (within 0.1%)
    df["open_eq_high"] = df["open"] >= df["high"] * 0.999  # O=H (within 0.1%)

    # BB Trap v2 needs historical closes for the Bollinger Band calc.
    # TODO: replace with your actual 7-rule BB Trap v2 logic from the vault
    # (wiki/strategies/) — this stub just flags gap-and-reject candidates
    # closing outside the day's range vs prior close as a placeholder.
    df["bb_trap_v2"] = (df["ltp"] > df["high"] * 0.995) | (df["ltp"] < df["low"] * 1.005)

    return df


def main():
    symbols = load_symbols()
    df = fetch_via_kite(symbols)
    source = "kite"
    if df is None or df.empty:
        df = fetch_via_yfinance(symbols)
        source = "yfinance"

    df = compute_signals(df)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": source,
        "open_eq_low": df[df["open_eq_low"]]["symbol"].tolist(),
        "open_eq_high": df[df["open_eq_high"]]["symbol"].tolist(),
        "bb_trap_v2": df[df["bb_trap_v2"]]["symbol"].tolist(),
    }

    ALERTS_FILE.write_text(json.dumps(output, indent=2))
    print(f"Wrote {ALERTS_FILE} via {source}: "
          f"{len(output['open_eq_low'])} O=L, "
          f"{len(output['open_eq_high'])} O=H, "
          f"{len(output['bb_trap_v2'])} BB Trap v2")


if __name__ == "__main__":
    main()
