"""Runtime configuration: position sizing + kill-switch.

Pilot defaults are conservative (Rs 1L capital, 1 lot, max 3 concurrent
positions, Rs 15K/day loss kill-switch). Every value is overridable from
`config.toml` in the project root; the file is optional.

Stdlib only. Uses `tomllib` (3.11+) when present, otherwise a tiny
built-in parser that handles the flat `key = value` style our
config.toml actually uses. Matches the existing scripts'
zero-third-party-dependency convention.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import presets

try:  # Python 3.11+
    import tomllib  # type: ignore
    _HAVE_TOMLLIB = True
except ModuleNotFoundError:  # pragma: no cover - depends on runtime
    _HAVE_TOMLLIB = False


@dataclass
class Config:
    # Strategy / rule preset name (resolved via presets.get_preset).
    preset: str = presets.DEFAULT

    # Pilot sizing — deliberately small for live validation.
    capital: float = 100_000.0
    lots_per_signal: int = 1
    max_concurrent: int = 3

    # Kill-switch: once realized losses for the day hit this, stop opening
    # new positions for the rest of that day.
    max_loss_per_day: float = 15_000.0

    # Paths (relative to project root, resolved by load()).
    trades_dir: str = "papertrades"
    raw_dir: str = "data/raw"
    results_dir: str = "results"

    @property
    def rules(self):
        """Resolve the preset into a bt.StrategyRules instance."""
        return presets.get_preset(self.preset)


def project_root() -> Path:
    return Path(__file__).resolve().parent


def _parse_flat_toml(text: str) -> dict:
    """Minimal parser for the flat `key = value` subset of TOML.

    Handles strings ("..."), ints, floats, booleans, and `#` comments.
    Good enough for our config.toml; tomllib is preferred when available.
    """
    data: dict = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        data[key] = _coerce(value)
    return data


def _coerce(value: str):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def load(config_path: str | Path | None = None) -> Config:
    """Load Config, applying optional TOML overrides on top of defaults.

    Unknown keys are ignored (forward-compatible). A missing config file
    simply yields the pilot defaults.
    """
    cfg = Config()
    root = project_root()
    path = Path(config_path) if config_path else root / "config.toml"
    if path.exists():
        if _HAVE_TOMLLIB:
            with path.open("rb") as fp:
                data = tomllib.load(fp)
        else:
            data = _parse_flat_toml(path.read_text())
        # Only copy keys that map to a Config field.
        valid = {f.name for f in fields(Config)}
        for key, value in data.items():
            if key in valid:
                setattr(cfg, key, value)
    return cfg
