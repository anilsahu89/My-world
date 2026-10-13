#!/usr/bin/env python3
"""Desk retry discipline (owner directive 13 Oct 2026): a desk must keep
retrying until it either takes its trade or records a legitimate rules-based
decision — a data failure must never lock the day.

The theta/mill wrappers run once per weekday via their tick functions. This
module centralises: is the desk still due? / was the last output healthy?
A scan counts UNHEALTHY (and stays retryable) when the wrapper errored or
the report says the options data was unavailable. Rules-based outcomes
(VIX gate, no credit, entry day, expiry settle) are final for the day.
"""
import json
from pathlib import Path

BASE = Path(__file__).parent
OUTS = {
    "theta": BASE.parent / "data" / "scanners" / "nifty-daily-theta-latest.json",
    "mill": BASE.parent / "data" / "scanners" / "nifty-premium-mill-latest.json",
}
BAD_MARKERS = ("no options data", "insufficient history",
               "scanner timed out", "yf chain fallback failed",
               "bhavcopy response not a zip")


def last_output(desk: str):
    p = OUTS[desk]
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def healthy(desk: str, out) -> bool:
    """True when the scan completed with real data (even if it decided not
    to trade). False when it died on missing data — those days must retry."""
    if not out:
        return False
    if out.get("status") == "error":
        return False
    text = (str(out.get("text_report") or "") + " " +
            str(out.get("error") or "")).lower()
    return not any(m in text for m in BAD_MARKERS)
