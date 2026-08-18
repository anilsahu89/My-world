#!/usr/bin/env python3
"""
Wrapper: Future Arbitrage scanner → JSON output
Runs engine.py in dry-run mode, captures signals and summary, writes JSON.

Output format:
{
  "strategy": "future-arbitrage",
  "scanned_at": "2026-08-10 18:30:00 IST",
  "status": "success|no_signals|error",
  "scan_date": "2026-08-10",
  "preset": "v2.1",
  "signals": 3,
  "summary": { ... },
  "table": { "headers": [...], "rows": [[...], ...] },
  "text_report": "..."
}
"""

import argparse
import csv
import io
import json
import os
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

# Ensure scanners dir is on the Python path
SCANNER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCANNER_DIR))

import config as config_mod


def main():
    parser = argparse.ArgumentParser(description="Future Arbitrage scanner wrapper")
    parser.add_argument("--output", required=True, help="Path to write JSON output")
    args = parser.parse_args()

    today = date.today()
    # Skip weekends
    if today.weekday() >= 5:
        _write_output(args.output, {
            "strategy": "future-arbitrage",
            "scanned_at": _now_str(),
            "status": "no_signals",
            "scan_date": today.isoformat(),
            "message": "Market closed — weekend",
            "signals": 0,
            "table": {"headers": [], "rows": []},
        })
        return

    output = {
        "strategy": "future-arbitrage",
        "scanned_at": _now_str(),
        "scan_date": today.isoformat(),
    }

    try:
        # Ensure data/raw exists for bhavcopy downloads
        raw_dir = SCANNER_DIR / "data" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        # Load config — set paths relative to scanner dir
        cfg = config_mod.load()
        cfg.trades_dir = str(SCANNER_DIR / "papertrades")
        cfg.raw_dir = str(SCANNER_DIR / "data" / "raw")
        cfg.results_dir = str(SCANNER_DIR / "results")

        # Import engine after config paths are set
        import engine

        # Run in dry-run mode to avoid writing paper trades
        result = engine.run(today, cfg, dry_run=True)

        output["status"] = "success"
        output["preset"] = result.get("preset", "v2.1")
        output["signals"] = len(result.get("candidates", []))
        output["summary"] = {
            "scan_date": today.isoformat(),
            "candidates": len(result.get("candidates", [])),
            "open_positions": result.get("open_count", 0),
            "day_realized_pnl": round(result.get("day_realized", 0), 2),
            "realized_pnl_cumulative": round(result.get("realized_cum", 0), 2),
            "open_mtm": round(result.get("mtm", 0), 2),
            "kill_switch": result.get("kill_switch", False),
            "closed_today": len(result.get("closed_rows", [])),
        }

        # Convert candidates to table format
        candidates = result.get("candidates", [])
        if candidates:
            headers = list(vars(candidates[0]).keys()) if candidates else []
            rows = []
            for c in candidates:
                row = {}
                for k, v in vars(c).items():
                    if isinstance(v, date):
                        row[k] = v.isoformat()
                    elif isinstance(v, float):
                        row[k] = round(v, 4)
                    else:
                        row[k] = str(v) if v is not None else ""
                rows.append(list(row.values()))
            output["table"] = {"headers": headers, "rows": rows}

            # Build text report
            text_lines = [
                f"Future Arbitrage Scan — {today.isoformat()} (dry-run)",
                f"Preset: {result.get('preset', 'v2.1')}",
                f"Candidates found: {len(candidates)}",
                f"Open positions: {result.get('open_count', 0)}",
                f"Day realized P&L: ₹{result.get('day_realized', 0):,.2f}",
                "",
            ]
            for i, c in enumerate(candidates[:10], 1):
                text_lines.append(
                    f"  {i}. {c.symbol} | Spot: {c.spot:.2f} | "
                    f"Basis: {c.basis_pct:.2f}% | Spread: {c.spread_value:.2f} | "
                    f"Ratio: {c.basis_spread_ratio:.2f}"
                )
            output["text_report"] = "\n".join(text_lines)
        else:
            output["table"] = {"headers": [], "rows": []}
            output["text_report"] = f"No future arbitrage signals found for {today.isoformat()}."

    except Exception as e:
        output["status"] = "error"
        output["error"] = str(e)
        output["traceback"] = traceback.format_exc()
        output["signals"] = 0
        output["table"] = {"headers": [], "rows": []}

    _write_output(args.output, output)


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")


def _write_output(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"✅ Written: {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
