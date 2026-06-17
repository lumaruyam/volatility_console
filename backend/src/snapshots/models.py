"""
Market-state snapshot data models.

Snapshots are the deterministic, time-aligned inputs to all downstream analytics.
Must be reproducible from raw events given the same parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class UnderlyingState:
    """Reference price and quality metadata for one underlying at one snapshot time."""
    instrument_key: str
    snapshot_ts: float          # UTC epoch
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    volume: Optional[float]
    reference_spot: float
    reference_type: str         # "mid" | "last" | "close" | "fallback"
    spread_pct: Optional[float]
    is_market_open: bool
    is_stale: bool
    quote_age_seconds: Optional[float]


@dataclass(frozen=True)
class OptionRow:
    """One option quote at one snapshot time."""
    instrument_key: str
    snapshot_ts: float
    underlying_symbol: str
    expiry_str: str             # ISO date string
    strike: float
    option_right: str           # "C" or "P"
    multiplier: float
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    mid: Optional[float]        # (bid+ask)/2 when both positive
    volume: Optional[float]
    open_interest: Optional[float]
    spread_pct: Optional[float]
    quote_age_seconds: Optional[float]
    is_stale: bool
    maturity_years: Optional[float]  # Filled by forward engine


@dataclass
class MarketStateSnapshot:
    """
    Smallest coherent state used by downstream analytics.
    Contains underlying state + all eligible option rows for that timestamp.
    """
    snapshot_ts: float
    underlying_state: UnderlyingState
    option_rows: list[OptionRow]
    flags: dict = field(default_factory=dict)   # e.g. {"session_open": True, "data_complete": True}
    snapshot_version: str = "1.0"

    def get_call(self, strike: float, expiry_str: str) -> Optional[OptionRow]:
        for row in self.option_rows:
            if row.strike == strike and row.expiry_str == expiry_str and row.option_right == "C":
                return row
        return None

    def get_put(self, strike: float, expiry_str: str) -> Optional[OptionRow]:
        for row in self.option_rows:
            if row.strike == strike and row.expiry_str == expiry_str and row.option_right == "P":
                return row
        return None

    def get_options_by_expiry(self, expiry_str: str) -> list[OptionRow]:
        return [r for r in self.option_rows if r.expiry_str == expiry_str]
