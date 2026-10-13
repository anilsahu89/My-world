#!/usr/bin/env python3
"""Manual close requests from the portal.

The browser cannot edit engine state directly; a Close button on a desk's
Open-positions box commits `data/close_requests/<desk>.json` (a JSON array
of trade refs — ids, symbols, or "open") via the GitHub API. Each desk
consumes its file at the start of its next scan: matching OPEN trades are
closed at that scan's own price with reason MANUAL, and the file is reset
to []. Desks commit these files through their normal commit_state path.
"""
import json
from pathlib import Path

BASE = Path(__file__).parent
REQ_DIR = BASE.parent / "data" / "close_requests"


def _path(desk: str) -> Path:
    REQ_DIR.mkdir(parents=True, exist_ok=True)
    return REQ_DIR / f"{desk}.json"


def pending(desk: str) -> list[str]:
    """Refs currently requested (does not consume)."""
    p = _path(desk)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return [str(x) for x in (data if isinstance(data, list) else [])]
    except Exception:
        return []


def consume(desk: str) -> list[str]:
    """Return pending refs and reset the file to [] (committed by the desk)."""
    refs = pending(desk)
    if refs:
        try:
            _path(desk).write_text("[]\n")
        except OSError:
            pass
    return refs


def wants(desk: str, ref) -> bool:
    """Convenience for single-position desks: any request at all."""
    return bool(pending(desk))
