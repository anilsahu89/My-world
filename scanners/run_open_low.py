#!/usr/bin/env python3
"""
Wrapper: Open=Low Intraday scanner → JSON output
Runs live_scan_open_low.py, captures stdout, parses tabular data, writes JSON.
Requires yfinance (installed in CI only for this scanner).

Output format:
{
  "strategy": "open-low-intraday",
  "scanned_at": "2026-08-10 09:30:00 IST",
  "status": "success|no_signals|error",
  "scan_date": "2026-08-10",
  "signals": 5,
  "table": { "headers": [...], "rows": [[...], ...] },
  "text_report": "..."
}
"""

import argparse
import json
import subprocess
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

SCANNER_DIR = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Open=Low Intraday scanner wrapper")
    parser.add_argument("--output", required=True, help="Path to write JSON output")
    args = parser.parse_args()

    today = date.today()
    output = {
        "strategy": "open-low-intraday",
        "scanned_at": _now_str(),
        "scan_date": today.isoformat(),
    }

    try:
        # Ensure data dir exists
        data_dir = SCANNER_DIR / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Run the scanner, capture stdout
        cmd = [
            sys.executable, str(SCANNER_DIR / "live_scan_open_low.py"),
            "--top", "20",
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min timeout — yfinance can be slow
            cwd=str(SCANNER_DIR),
        )

        stdout = result.stdout
        stderr = result.stderr

        if result.returncode != 0:
            output["status"] = "error"
            output["error"] = stderr or stdout
            output["signals"] = 0
            output["table"] = {"headers": [], "rows": []}
        else:
            # Parse the stdout for tabular signal data
            signal_count, headers, rows = _parse_stdout(stdout)

            output["status"] = "success" if signal_count > 0 else "no_signals"
            output["signals"] = signal_count
            output["table"] = {"headers": headers, "rows": rows}
            output["text_report"] = stdout.strip() if stdout.strip() else f"No Open=Low signals found for {today.isoformat()}."

    except subprocess.TimeoutExpired:
        output["status"] = "error"
        output["error"] = "Scanner timed out after 300 seconds"
        output["signals"] = 0
        output["table"] = {"headers": [], "rows": []}
    except Exception as e:
        output["status"] = "error"
        output["error"] = str(e)
        output["traceback"] = traceback.format_exc()
        output["signals"] = 0
        output["table"] = {"headers": [], "rows": []}

    _write_output(args.output, output)


def _parse_stdout(text):
    """Parse the formatted table output from live_scan_open_low.py"""
    signal_count = 0
    headers = []
    rows = []

    lines = text.split("\n")
    in_table = False
    header_found = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Find the main signals section
        if "OPEN=LOW SIGNALS" in stripped or "STRONG OPEN=LOW" in stripped:
            in_table = True
            header_found = False
            continue
        elif "OTHER OPEN=LOW" in stripped or "VOLUME" in stripped:
            in_table = False
            continue
        elif "No Open=Low" in stripped:
            return 0, [], []

        if not in_table:
            continue

        # Skip separator lines
        if stripped.startswith("---") or stripped.startswith("=") or stripped.startswith("Live Open"):
            continue

        # The header line contains "Symbol" and "Open"
        if not header_found and "Symbol" in stripped and "Open" in stripped:
            headers = stripped.split()
            header_found = True
            continue

        if header_found and stripped and stripped[0].isdigit():
            parts = stripped.split()
            if len(parts) >= 4:
                rows.append(parts[:len(headers)] if len(parts) >= len(headers) else parts)
                signal_count += 1

    # Default headers
    if not headers:
        headers = ["#", "Symbol", "Open", "High", "Low", "Close", "Vol_x", "P&L%"]

    return signal_count, headers, rows


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")


def _write_output(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"✅ Written: {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
