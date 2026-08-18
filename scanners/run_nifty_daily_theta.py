#!/usr/bin/env python3
"""
Wrapper: Nifty Daily Theta scanner → JSON output
Runs paper_trade_daily_theta.py, captures stdout, parses report, writes JSON.

The daily theta is a paper-trade tracker — it checks for signals Mon-Wed
and updates a local CSV. We capture its stdout report and the current trade state.

Output format:
{
  "strategy": "nifty-daily-theta",
  "scanned_at": "2026-08-10 09:15:00 IST",
  "status": "success|no_signal|error",
  "scan_date": "2026-08-10",
  "report": { ... },
  "trades": [...],
  "text_report": "..."
}
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

SCANNER_DIR = Path(__file__).resolve().parent
TRADES_FILE = SCANNER_DIR / "papertrades" / "daily_theta_trades.csv"


def main():
    parser = argparse.ArgumentParser(description="Nifty Daily Theta scanner wrapper")
    parser.add_argument("--output", required=True, help="Path to write JSON output")
    args = parser.parse_args()

    today = date.today()
    output = {
        "strategy": "nifty-daily-theta",
        "scanned_at": _now_str(),
        "scan_date": today.isoformat(),
    }

    try:
        # Ensure directories exist
        raw_dir = SCANNER_DIR / "data" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (SCANNER_DIR / "papertrades").mkdir(parents=True, exist_ok=True)

        # Run the scanner, capture stdout
        cmd = [sys.executable, str(SCANNER_DIR / "paper_trade_daily_theta.py")]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(SCANNER_DIR),
        )

        stdout = result.stdout
        stderr = result.stderr

        # Always read the trades file for current state
        trades = _read_trades_csv()

        output["trades"] = trades
        output["trade_count"] = len(trades)

        if result.returncode != 0:
            output["status"] = "error"
            output["error"] = stderr or stdout
        else:
            # Parse report summary from stdout
            report = _parse_report(stdout)
            output["status"] = "success"
            output["report"] = report
            output["text_report"] = stdout.strip() if stdout.strip() else "Daily Theta check complete."

            if "SIGNAL" in stdout.upper() or "ENTRY" in stdout.upper():
                output["status"] = "new_signal"

    except subprocess.TimeoutExpired:
        output["status"] = "error"
        output["error"] = "Scanner timed out after 120 seconds"
        output["trades"] = _read_trades_csv()
    except Exception as e:
        output["status"] = "error"
        output["error"] = str(e)
        output["traceback"] = traceback.format_exc()
        output["trades"] = _read_trades_csv()

    _write_output(args.output, output)


def _read_trades_csv():
    """Read the daily theta trades CSV"""
    if not TRADES_FILE.exists():
        return []
    try:
        with TRADES_FILE.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return list(reader)
    except Exception:
        return []


def _parse_report(text):
    """Extract key metrics from the stdout report"""
    report = {}
    lines = text.split("\n")
    for line in lines:
        stripped = line.strip()
        if "Total Trades" in stripped or "total trades" in stripped.lower():
            report["total_trades"] = stripped.split(":")[-1].strip()
        elif "Win Rate" in stripped or "win rate" in stripped.lower():
            report["win_rate"] = stripped.split(":")[-1].strip()
        elif "Net P&L" in stripped or "net pnl" in stripped.lower():
            report["net_pnl"] = stripped.split(":")[-1].strip()
        elif "Open Position" in stripped or "open position" in stripped.lower():
            report["open_position"] = stripped.split(":")[-1].strip()
        elif "Realized P&L" in stripped or "realized" in stripped.lower():
            report["realized_pnl"] = stripped.split(":")[-1].strip()
    return report


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")


def _write_output(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"✅ Written: {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
