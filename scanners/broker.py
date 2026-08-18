"""Broker abstraction.

The engine talks only to the `Broker` interface, so the paper-trading and
(future) live paths are interchangeable. Milestone 1 ships:

- `PaperBroker` — records intent in the ledger, no real orders.
- `KiteBroker` — STUB. Raises NotImplementedError. Live execution is
  Milestone 2 and requires enabling the `kite-trader` MCP server.

Why a stub instead of omitting it: it documents the exact contract M2
must satisfy, and lets the engine be written against the final interface
today. When KiteBroker is implemented (using mcp__kite-trader__ tools),
engine.py changes by zero lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import ledger


@dataclass
class Fill:
    """Acknowledgement of an executed spread or exit.

    `order_ids` is opaque to the engine — PaperBroker leaves it empty,
    KiteBroker would populate Kite order/trigger ids for reconciliation.
    """

    symbol: str
    ok: bool
    order_ids: tuple[str, ...] = ()


class Broker:
    """Interface every broker implementation must satisfy."""

    def open_spread(self, candidate, *, lots: int) -> Fill:
        """Buy current-month future, sell next-month future."""
        raise NotImplementedError

    def close_spread(self, position: dict, *, exit_date: date) -> Fill:
        """Reverse an open spread to flat."""
        raise NotImplementedError


class PaperBroker(Broker):
    """Records spreads in the 25-column ledger; places no orders.

    The ledger IS the paper broker's "exchange". Exits are written by the
    engine via ledger.mark_closed (which this broker calls back into).
    """

    def __init__(self, trades_dir: str | Path):
        self.trades_dir = Path(trades_dir)

    def open_spread(self, candidate, *, lots: int) -> Fill:
        path = ledger.ledger_path(self.trades_dir, candidate.scan_date)
        # Idempotency guard: never double-enter the same symbol on the
        # same entry day.
        if candidate.symbol in ledger.open_symbols(path):
            return Fill(symbol=candidate.symbol, ok=False)
        ledger.append_entry(
            path,
            paper_entry_date=candidate.scan_date,
            source_price_date=candidate.scan_date,
            symbol=candidate.symbol,
            current_expiry=candidate.curr_expiry,
            entry_current_future=candidate.curr_future,
            next_expiry=candidate.next_expiry,
            entry_next_future=candidate.next_future,
            entry_spot=candidate.spot,
            lot=candidate.lot * lots,
            notes=f"paper entry; preset basis>={candidate.basis:.2f} "
            f"spread={candidate.spread:.2f} ratio={candidate.basis_spread_ratio:.2f}",
        )
        return Fill(symbol=candidate.symbol, ok=True)

    def close_spread(self, position: dict, *, exit_date: date) -> Fill:
        # The engine computes P&L and reason and calls mark_closed
        # directly; this method is here for interface symmetry. It is a
        # no-op marker so KiteBroker can override it for real sell-backs.
        return Fill(symbol=position["symbol"], ok=True)


class KiteBroker(Broker):
    """STUB — live execution via the self-hosted Kite MCP server.

    Milestone 2 will implement this using the mcp__kite-trader__ tools
    (place_order on NFO for each leg, place_gtt_order with a two-leg OCO
    for the convergence exit, get_positions for reconciliation).

    Prerequisites before this can run:
      1. Enable `kite-trader` MCP via /mcp (currently disabled in config).
      2. Confirm the daily OAuth login produced a fresh access_token
         (kite-mcp-server/daily_login.py, expires ~07:30 IST).
      3. Triple-gate safety in engine.py: --live flag + LIVE_ARMED file
         + kite-trader enabled. None of that exists yet by design.

    Until then, every method fails loudly so no code path can accidentally
    reach a real order.
    """

    _NOT_IMPLEMENTED = (
        "live execution is deferred to Milestone 2. To enable: "
        "(1) start the kite-trader MCP via /mcp, "
        "(2) implement KiteBroker.open_spread/close_spread, "
        "(3) add the --live / LIVE_ARMED safety gate in engine.py."
    )

    def __init__(self, trades_dir: str | Path):
        # Accept the same arg as PaperBroker for interface symmetry; not
        # used until the methods are implemented.
        self.trades_dir = Path(trades_dir)

    def open_spread(self, candidate, *, lots: int) -> Fill:
        raise NotImplementedError(self._NOT_IMPLEMENTED)

    def close_spread(self, position: dict, *, exit_date: date) -> Fill:
        raise NotImplementedError(self._NOT_IMPLEMENTED)


def make_broker(mode: str, trades_dir: str | Path) -> Broker:
    """Factory. `mode` is "paper" (M1) or "kite" (M2, not yet usable)."""
    if mode == "paper":
        return PaperBroker(trades_dir)
    if mode == "kite":
        return KiteBroker(trades_dir)
    raise ValueError(f"unknown broker mode {mode!r}; expected 'paper' or 'kite'")
