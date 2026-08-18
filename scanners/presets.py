"""Rule presets for the Future Arbitrage strategy.

Two frozen configurations backed by ARBITAGE_2_0_RULES.md:

- V20: the default, backtested rule set (276 trades, PF 2.68 over 2.3 years).
- V21: the stricter "winning-rate" filter — fewer trades, higher selectivity.

Both reuse `backtest_future_arbitaage.StrategyRules` so the entry check
(`passes_entry_rules`) stays the single source of truth.
"""

from __future__ import annotations

import backtest_future_arbitaage as bt

DEFAULT = "v2.1"

# Arbitrage 2.0 — the baseline. Mirrors bt.StrategyRules defaults and the
# 2.3-year backtest in wiki/trade-reviews/arbitrage-2-0-2yr3-backtest.md.
V20 = bt.StrategyRules(
    name="Arbitage 2.0",
    min_basis=10.0,
    min_basis_pct=0.0,
    max_spread_to_basis=1.0,
    min_spread_value=2000.0,
    min_current_volume=100,
    min_next_volume=100,
    min_days_to_expiry=0,
    max_days_to_expiry=999,
)

# Arbitrage 2.1 — winning-rate variant. From ARBITAGE_2_0_RULES.md:
#   spot-current gap >= Rs 20
#   spot-current gap >= 2x current-next gap   (max_spread_to_basis = 0.5)
#   spot-current gap >= 1.5% of spot
#   next-month volume >= 500 contracts
#   prefer 7-21 days before current-month expiry
#   skip if current-next gap > 50% of spot-current gap (same as the 2x rule)
V21 = bt.StrategyRules(
    name="Arbitage 2.1",
    min_basis=20.0,
    min_basis_pct=1.5,
    max_spread_to_basis=0.5,
    min_spread_value=2000.0,
    min_current_volume=100,
    min_next_volume=500,
    min_days_to_expiry=7,
    max_days_to_expiry=21,
)

_PRESETS = {"v2.0": V20, "v2": V20, "2.0": V20, "v2.1": V21, "v21": V21, "2.1": V21}


def get_preset(name: str | None = None) -> bt.StrategyRules:
    """Resolve a preset by name. Falls back to DEFAULT on None/empty.

    Raises ValueError on an unrecognized name so a typo surfaces loudly
    rather than silently using the wrong rule set.
    """
    if not name:
        return _PRESETS[DEFAULT]
    key = name.strip().lower()
    if key not in _PRESETS:
        known = ", ".join(sorted({k for k in _PRESETS}))
        raise ValueError(f"unknown preset {name!r}; known: {known}")
    return _PRESETS[key]
