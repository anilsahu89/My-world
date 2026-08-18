"""Paper-trade ledger.

The schema is frozen to match the existing
`papertrades/future_arbitaage_open_YYYY-MM-DD.csv` so that
`monitor_papertrades.py` (which reads via csv.DictReader) keeps working
unchanged. Do NOT reorder or rename these columns.

One ledger file = one entry day's OPEN positions (entries opened on that
day). Exits are written back into the same row when the exit rule fires
on a later run. A row's lifecycle: OPEN -> CLOSED.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

# Exact 25-column header from the existing ledger. Field order matters.
LEDGER_FIELDS = [
    "paper_entry_date",
    "source_price_date",
    "strategy",
    "symbol",
    "action_current_month",
    "current_expiry",
    "entry_current_future",
    "action_next_month",
    "next_expiry",
    "entry_next_future",
    "entry_spot",
    "spot_minus_current",
    "current_minus_next",
    "lot",
    "entry_spread_value",
    "sl_rule",
    "status",
    "exit_date",
    "exit_reason",
    "exit_spot",
    "exit_current_future",
    "exit_next_future",
    "paper_pnl",
    "notes",
]

STRATEGY_NAME = "Future Arbitaage"  # preserved misspelling for compatibility
ACTION_BUY = "BUY"
ACTION_SELL = "SELL"
SL_RULE = "exit if current future >= spot"
EXIT_REASON_CONVERGENCE = "SL_current_future_matched_or_crossed_spot"
EXIT_REASON_EXPIRY = "current_month_expiry"


def ledger_path(trades_dir: str | Path, entry_date: date) -> Path:
    """Path for a given entry day's ledger file."""
    return Path(trades_dir) / f"future_arbitaage_open_{entry_date.isoformat()}.csv"


def ensure_ledger(path: Path) -> None:
    """Create the ledger with a header if it does not exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", newline="") as fp:
            csv.DictWriter(fp, fieldnames=LEDGER_FIELDS).writeheader()


def load_all(path: Path) -> list[dict]:
    """Read every row (OPEN + CLOSED) from a ledger file."""
    if not path.exists():
        return []
    with path.open(newline="") as fp:
        return list(csv.DictReader(fp))


def load_open(path: Path) -> list[dict]:
    """Read only OPEN rows."""
    return [r for r in load_all(path) if r.get("status") == "OPEN"]


def open_symbols(path: Path) -> set[str]:
    """Set of symbols with an OPEN row in this ledger."""
    return {r["symbol"] for r in load_open(path)}


def append_entry(
    path: Path,
    *,
    paper_entry_date: date,
    source_price_date: date,
    symbol: str,
    current_expiry: date,
    entry_current_future: float,
    next_expiry: date,
    entry_next_future: float,
    entry_spot: float,
    lot: int,
    notes: str = "",
) -> None:
    """Append a new OPEN row. Idempotency (symbol already open) is the
    caller's responsibility — see engine.py."""
    ensure_ledger(path)
    basis = entry_spot - entry_current_future
    spread = entry_current_future - entry_next_future
    row = {field: "" for field in LEDGER_FIELDS}
    row.update(
        {
            "paper_entry_date": paper_entry_date.isoformat(),
            "source_price_date": source_price_date.isoformat(),
            "strategy": STRATEGY_NAME,
            "symbol": symbol,
            "action_current_month": ACTION_BUY,
            "current_expiry": current_expiry.isoformat(),
            "entry_current_future": f"{entry_current_future:.4f}",
            "action_next_month": ACTION_SELL,
            "next_expiry": next_expiry.isoformat(),
            "entry_next_future": f"{entry_next_future:.4f}",
            "entry_spot": f"{entry_spot:.4f}",
            "spot_minus_current": f"{basis:.4f}",
            "current_minus_next": f"{spread:.4f}",
            "lot": str(lot),
            "entry_spread_value": f"{spread * lot:.2f}",
            "sl_rule": SL_RULE,
            "status": "OPEN",
            "notes": notes,
        }
    )
    with path.open("a", newline="") as fp:
        csv.DictWriter(fp, fieldnames=LEDGER_FIELDS).writerow(row)


def mark_closed(
    path: Path,
    symbol: str,
    *,
    exit_date: date,
    exit_reason: str,
    exit_spot: float,
    exit_current_future: float,
    exit_next_future: float,
    paper_pnl: float,
) -> bool:
    """Flip the OPEN row for `symbol` to CLOSED, filling exit fields.

    Returns True if a row was closed, False if no OPEN row for the symbol
    existed. Rewrites the whole file (rows are small).
    """
    rows = load_all(path)
    closed = False
    for row in rows:
        if row.get("status") == "OPEN" and row.get("symbol") == symbol:
            row["status"] = "CLOSED"
            row["exit_date"] = exit_date.isoformat()
            row["exit_reason"] = exit_reason
            row["exit_spot"] = f"{exit_spot:.4f}"
            row["exit_current_future"] = f"{exit_current_future:.4f}"
            row["exit_next_future"] = f"{exit_next_future:.4f}"
            row["paper_pnl"] = f"{paper_pnl:.2f}"
            closed = True
            break
    if closed:
        with path.open("w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=LEDGER_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return closed


def all_ledgers(trades_dir: str | Path) -> list[Path]:
    """Every ledger file in trades_dir, sorted by name (== by date)."""
    base = Path(trades_dir)
    if not base.exists():
        return []
    return sorted(base.glob("future_arbitaage_open_*.csv"))
