#!/usr/bin/env python3
"""
Wrapper: BB Trap Positional scanner → JSON output
Runs scan_bb_trap_v2.py, captures stdout, parses tabular data, writes JSON.

Output format:
{
  "strategy": "bb-trap-positional",
  "scanned_at": "2026-08-10 18:30:00 IST",
  "status": "success|no_signals|error",
  "scan_date": "2026-08-10",
  "signals": { "short": 2, "long": 1 },
  "table": { "headers": [...], "rows": [[...], ...] },
  "text_report": "..."
}
"""

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

SCANNER_DIR = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="BB Trap Positional scanner wrapper")
    parser.add_argument("--output", required=True, help="Path to write JSON output")
    args = parser.parse_args()

    today = date.today()
    output = {
        "strategy": "bb-trap-positional",
        "scanned_at": _now_str(),
        "scan_date": today.isoformat(),
    }

    try:
        # Ensure data/raw exists for bhavcopy downloads
        raw_dir = SCANNER_DIR / "data" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        # Run the scanner, capture stdout + stderr
        cmd = [
            sys.executable, str(SCANNER_DIR / "scan_bb_trap_v2.py"),
            "--shorts-only",
            "--top", "20",
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(SCANNER_DIR),
        )

        stdout = result.stdout
        stderr = result.stderr

        if result.returncode != 0:
            # Scanner may return non-zero for "no data" — check if it's an error
            if "No data available" in stderr or "No data" in stderr:
                output["status"] = "no_signals"
                output["signals"] = {"short": 0, "long": 0}
                output["table"] = {"headers": [], "rows": []}
                output["text_report"] = f"No BB Trap signals found for {today.isoformat()}.\n{stderr.strip()}"
            else:
                output["status"] = "error"
                output["error"] = stderr or stdout
                output["signals"] = {"short": 0, "long": 0}
                output["table"] = {"headers": [], "rows": []}
        else:
            # Parse the stdout for tabular signal data
            short_count, long_count, headers, rows = _parse_stdout(stdout)

            output["status"] = "success" if (short_count + long_count) > 0 else "no_signals"
            output["signals"] = {"short": short_count, "long": long_count}
            output["table"] = {"headers": headers, "rows": rows}
            output["text_report"] = stdout.strip() if stdout.strip() else f"No BB Trap v2 signals found for {today.isoformat()}."

    except subprocess.TimeoutExpired:
        output["status"] = "error"
        output["error"] = "Scanner timed out after 180 seconds"
        output["signals"] = {"short": 0, "long": 0}
        output["table"] = {"headers": [], "rows": []}
    except Exception as e:
        output["status"] = "error"
        output["error"] = str(e)
        output["traceback"] = traceback.format_exc()
        output["signals"] = {"short": 0, "long": 0}
        output["table"] = {"headers": [], "rows": []}

    _write_output(args.output, output)


def _parse_stdout(text):
    """Parse the formatted table output from scan_bb_trap_v2.py"""
    short_count = 0
    long_count = 0
    headers = []
    rows = []

    lines = text.split("\n")

    # Find the SHORT SETUPS section
    in_short = False
    in_long = False
    header_found = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        if "SHORT SETUPS" in stripped:
            in_short = True
            in_long = False
            header_found = False
            continue
        elif "LONG SETUPS" in stripped:
            in_short = False
            in_long = True
            header_found = False
            continue
        elif "No BB Trap" in stripped:
            break

        if not in_short and not in_long:
            continue

        # Skip separator lines
        if stripped.startswith("---") or stripped.startswith("="):
            continue

        # The header line contains "Symbol" and "Entry"
        if not header_found and "Symbol" in stripped and "Entry" in stripped:
            headers = stripped.split()
            header_found = True
            continue

        if header_found and stripped and stripped[0].isdigit():
            # Data row — parse values
            parts = stripped.split()
            if len(parts) >= 8:
                rows.append(parts[:len(headers)] if len(parts) >= len(headers) else parts)
                if in_short:
                    short_count += 1
                elif in_long:
                    long_count += 1

    # Default headers if parsing didn't find them
    if not headers:
        headers = ["#", "Symbol", "Entry", "SL", "Target", "R:R", "Wick%", "Vol_x", "RSI", "Range", "Score"]

    return short_count, long_count, headers, rows


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")


def _write_output(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"✅ Written: {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
