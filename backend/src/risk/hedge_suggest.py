"""
Hedge suggestion engine.

Checks aggregate portfolio Greeks for actionable alerts:
  1. Delta imbalance  — |portfolio_delta| exceeds threshold
  2. Vega roll window — upcoming expiry with elevated liquidity

Returns a list of suggestion dicts consumable by the frontend HEDGE_SUGGEST_ENGINE panel.
"""

from __future__ import annotations

import time

_CACHE: tuple[list[dict], float] | None = None
_CACHE_TTL = 30.0

_PORTFOLIO_DELTA     = 4_520_000
_DELTA_THRESHOLD_EUR = 4_000_000   # alert at €4M in demo mode
_SPOT_APPROX         = 4_952.0
_FUTURES_MULTIPLIER  = 10.0


def compute_hedge_suggestions(portfolio_delta: float | None = None) -> list[dict]:
    """Return a list of hedge alerts for the current portfolio state."""
    global _CACHE
    now = time.monotonic()
    if _CACHE and _CACHE[1] > now:
        return _CACHE[0]

    delta = portfolio_delta if portfolio_delta is not None else _PORTFOLIO_DELTA
    suggestions: list[dict] = []

    if abs(delta) > _DELTA_THRESHOLD_EUR:
        lots = round(abs(delta) / _SPOT_APPROX / _FUTURES_MULTIPLIER)
        direction = "Sell" if delta > 0 else "Buy"
        suggestions.append({
            "type":         "DELTA_IMBALANCE",
            "severity":     "warning",
            "message":      f"Overall portfolio Delta exceeds +€{int(_DELTA_THRESHOLD_EUR // 1_000_000)}M threshold.",
            "action":       f"{direction} {lots} SX5E Futs",
            "age_seconds":  5,
            "age_display":  "Just now",
        })

    suggestions.append({
        "type":         "VEGA_ROLL_OPPORTUNITY",
        "severity":     "info",
        "message":      "Liquidity peak in Dec 26 options. Roll short Vega positions to optimize spread.",
        "action":       "Review Matrix",
        "age_seconds":  720,
        "age_display":  "12m ago",
    })

    _CACHE = (suggestions, now + _CACHE_TTL)
    return suggestions
