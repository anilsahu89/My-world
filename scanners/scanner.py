"""Daily scanner — the pure-data core of the automation.

Given a trading day and a rule preset, this returns the ranked list of
candidate spreads that pass every entry rule. It mirrors the candidate
selection in `backtest_future_arbitaage.run_backtest` so a scan for a
given day reproduces the backtester's `signals.csv` rows for that day.

No orders, no ledger writes — just data. This is intentionally separate
from the engine so it can be unit-checked against backtest output and
reused unchanged when live execution arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import backtest_future_arbitaage as bt


@dataclass
class Candidate:
    """A spread that passed every entry rule on `scan_date`."""

    rank: int
    symbol: str
    scan_date: date
    spot: float
    curr_expiry: date
    next_expiry: date
    curr_future: float
    next_future: float
    basis: float  # spot - current
    spread: float  # current - next
    basis_spread_ratio: float
    basis_pct: float
    spread_value: float  # spread * lot, one lot
    lot: int
    curr_volume: int
    next_volume: int
    days_to_expiry: int


def scan_day(
    day: date,
    rules: bt.StrategyRules,
    raw_dir: str | Path,
    *,
    exclude: set[str] | None = None,
) -> list[Candidate]:
    """Scan a single day and return ranked candidates.

    - Downloads the day's FO+CM bhavcopy if not cached (via bt.download).
    - Applies bt.passes_entry_rules with `rules`.
    - Ranks by (basis_spread_ratio desc, basis desc, spread asc) — the
      exact ordering the backtester uses.

    `exclude` is a set of symbols to skip (e.g. already-open positions).
    Returns [] if the bhavcopy for `day` is unavailable (weekend, holiday,
    or NSE hasn't published yet).
    """
    exclude = exclude or set()
    raw_dir = Path(raw_dir)

    # Ensure both archives are cached. load_day expects them present.
    ymd = day.strftime("%Y%m%d")
    bt.download(bt.FO_URL.format(yyyymmdd=ymd), raw_dir / f"fo_{ymd}.zip")
    bt.download(bt.CM_URL.format(yyyymmdd=ymd), raw_dir / f"cm_{ymd}.zip")

    loaded = bt.load_day(day, raw_dir)
    if loaded is None:
        return []
    spots, futures_by_symbol = loaded

    ranked_keys = []  # (sort_key, symbol, curr, nxt, spot)
    for symbol, quotes in futures_by_symbol.items():
        if symbol in exclude or symbol not in spots:
            continue
        # Same filtering the backtester applies: live (expiry >= today),
        # traded, and at least two expiries available.
        live = [q for q in quotes if q.expiry >= day and q.volume > 0]
        if len(live) < 2:
            continue
        curr, nxt = live[0], live[1]
        spot = spots[symbol]
        if not bt.passes_entry_rules(spot, curr, nxt, rules, day):
            continue
        basis = spot - curr.close
        spread = curr.close - nxt.close
        ratio = basis / spread if spread else 999.0
        # sort key mirrors backtest_future_arbitaage: ratio desc, basis
        # desc, spread asc (encoded as -spread desc).
        ranked_keys.append((ratio, basis, -spread, symbol, curr, nxt, spot))

    ranked_keys.sort(reverse=True)
    candidates = []
    for rank, (_ratio, basis, neg_spread, symbol, curr, nxt, spot) in enumerate(
        ranked_keys, 1
    ):
        spread = -neg_spread
        candidates.append(
            Candidate(
                rank=rank,
                symbol=symbol,
                scan_date=day,
                spot=spot,
                curr_expiry=curr.expiry,
                next_expiry=nxt.expiry,
                curr_future=curr.close,
                next_future=nxt.close,
                basis=basis,
                spread=spread,
                basis_spread_ratio=basis / spread if spread else 999.0,
                basis_pct=basis / spot * 100 if spot else 0.0,
                spread_value=spread * curr.lot,
                lot=curr.lot,
                curr_volume=curr.volume,
                next_volume=nxt.volume,
                days_to_expiry=(curr.expiry - day).days,
            )
        )
    return candidates
