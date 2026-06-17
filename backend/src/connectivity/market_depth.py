"""
L2 order book simulator.
Returns top-of-book bid/ask rows with spread % and wide-spread flag.
IBKR reqMktDepth would replace _simulate_book() when a live session is available.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

_BASE_LEVELS = [
    {"bid": 4201.50, "ask": 4203.00, "bid_size": 150, "ask_size": 200},
    {"bid":   15.20, "ask":   16.10, "bid_size":  50, "ask_size":  10},
    {"bid": 4199.00, "ask": 4205.50, "bid_size": 500, "ask_size": 450},
    {"bid":    0.85, "ask":    0.95, "bid_size":  12, "ask_size": 100},
    {"bid": 4200.00, "ask": 4204.00, "bid_size": 300, "ask_size": 300},
]

_CACHE: tuple[list[dict], float] | None = None
_CACHE_TTL = 1.8   # expire just before the 2s frontend poll cycle


def fetch_order_book(ticker: str = "SX5E") -> list[dict]:
    """Return a simulated L2 order book snapshot for the given ticker."""
    global _CACHE
    now = time.monotonic()
    if _CACHE and _CACHE[1] > now:
        return _CACHE[0]

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    rows = []
    for level in _BASE_LEVELS:
        jitter = random.uniform(-0.002, 0.002)
        bid = round(level["bid"] * (1 + jitter), 2)
        ask = round(level["ask"] * (1 + jitter), 2)
        bid_size = max(1, level["bid_size"] + random.randint(-10, 10))
        ask_size = max(1, level["ask_size"] + random.randint(-10, 10))
        spread_pct = round((ask - bid) / bid * 100, 2) if bid > 0 else 0.0
        rows.append({
            "time":       ts,
            "bid_size":   bid_size,
            "bid":        bid,
            "ask":        ask,
            "ask_size":   ask_size,
            "spread_pct": spread_pct,
            "wide":       spread_pct > 2.0,
        })

    _CACHE = (rows, now + _CACHE_TTL)
    return rows
