"""Canonical instrument master — contract definitions and key construction.

The canonical key must remain meaningful even if the broker session changes.
Broker contract IDs are stored alongside normalized records for audit but are
never used as the primary identifier.

Key format:
  Underlying: SYMBOL|SEC_TYPE|EXCHANGE|CURRENCY
  Option:     SYMBOL|OPT|EXCHANGE|CURRENCY|YYYYMMDD|STRIKE|RIGHT|MULTIPLIER

STRIKE is formatted with :g (strips trailing zeros: 450.0→"450", 450.5→"450.5")
so it is identical to the CanonicalContract format from the connectivity layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class UnderlyingContract:
    """Canonical representation of a tradable underlying."""

    symbol: str
    sec_type: str           # STK | IND | FUT
    exchange: str
    currency: str
    broker_id: int | None = None
    broker_payload: dict[str, Any] | None = None
    description: str | None = None
    as_of_date: date | None = None

    @property
    def instrument_key(self) -> str:
        return f"{self.symbol}|{self.sec_type}|{self.exchange}|{self.currency}"

    @classmethod
    def from_key(cls, key: str, **kwargs: Any) -> "UnderlyingContract":
        parts = key.split("|")
        if len(parts) != 4:
            raise ValueError(f"Invalid underlying key: {key!r}")
        return cls(
            symbol=parts[0], sec_type=parts[1], exchange=parts[2], currency=parts[3], **kwargs
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_key": self.instrument_key,
            "symbol": self.symbol,
            "sec_type": self.sec_type,
            "exchange": self.exchange,
            "currency": self.currency,
            "broker_id": self.broker_id,
            "broker_payload": self.broker_payload,
            "description": self.description,
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UnderlyingContract":
        return cls(
            symbol=d["symbol"],
            sec_type=d["sec_type"],
            exchange=d["exchange"],
            currency=d["currency"],
            broker_id=d.get("broker_id"),
            broker_payload=d.get("broker_payload"),
            description=d.get("description"),
            as_of_date=date.fromisoformat(d["as_of_date"]) if d.get("as_of_date") else None,
        )


@dataclass(frozen=True)
class OptionContract:
    """Canonical representation of a single-leg equity/index option."""

    underlying_symbol: str
    sec_type: str = "OPT"
    exchange: str = "SMART"
    currency: str = "USD"
    expiry: date | None = None
    strike: float | None = None
    right: str | None = None       # "C" or "P"
    multiplier: int = 100
    trading_class: str | None = None
    broker_id: int | None = None
    broker_payload: dict[str, Any] | None = None
    as_of_date: date | None = None

    @property
    def instrument_key(self) -> str:
        """Stable composite key — identical to CanonicalContract format."""
        expiry_str = self.expiry.strftime("%Y%m%d") if self.expiry else ""
        strike_str = f"{self.strike:g}" if self.strike is not None else ""
        return (
            f"{self.underlying_symbol}|{self.sec_type}|{self.exchange}|"
            f"{self.currency}|{expiry_str}|{strike_str}|{self.right or ''}|{self.multiplier}"
        )

    @property
    def dte(self) -> int | None:
        """Days to expiry relative to as_of_date."""
        if self.expiry is None or self.as_of_date is None:
            return None
        return (self.expiry - self.as_of_date).days

    @property
    def maturity_label(self) -> str:
        return self.expiry.strftime("%Y%m%d") if self.expiry else "NONE"

    @classmethod
    def from_key(cls, key: str, as_of_date: date | None = None) -> "OptionContract":
        """Round-trip: parse instrument_key back into an OptionContract."""
        parts = key.split("|")
        if len(parts) != 8:
            raise ValueError(f"Invalid option key: {key!r}")
        expiry = datetime.strptime(parts[4], "%Y%m%d").date() if parts[4] else None
        strike = float(parts[5]) if parts[5] else None
        multiplier = int(parts[7]) if parts[7] else 100
        return cls(
            underlying_symbol=parts[0],
            sec_type=parts[1],
            exchange=parts[2],
            currency=parts[3],
            expiry=expiry,
            strike=strike,
            right=parts[6] or None,
            multiplier=multiplier,
            as_of_date=as_of_date,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_key": self.instrument_key,
            "underlying_symbol": self.underlying_symbol,
            "sec_type": self.sec_type,
            "exchange": self.exchange,
            "currency": self.currency,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "strike": self.strike,
            "right": self.right,
            "multiplier": self.multiplier,
            "trading_class": self.trading_class,
            "broker_id": self.broker_id,
            "broker_payload": self.broker_payload,
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OptionContract":
        return cls(
            underlying_symbol=d["underlying_symbol"],
            sec_type=d.get("sec_type", "OPT"),
            exchange=d.get("exchange", "SMART"),
            currency=d.get("currency", "USD"),
            expiry=date.fromisoformat(d["expiry"]) if d.get("expiry") else None,
            strike=d.get("strike"),
            right=d.get("right"),
            multiplier=d.get("multiplier", 100),
            trading_class=d.get("trading_class"),
            broker_id=d.get("broker_id"),
            broker_payload=d.get("broker_payload"),
            as_of_date=date.fromisoformat(d["as_of_date"]) if d.get("as_of_date") else None,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def validate_option_contract(contract: OptionContract) -> list[str]:
    """Return validation error messages (empty list = valid)."""
    errors: list[str] = []
    if contract.strike is None or contract.strike <= 0:
        errors.append(f"Invalid strike: {contract.strike}")
    if contract.multiplier <= 0:
        errors.append(f"Invalid multiplier: {contract.multiplier}")
    if contract.right not in ("C", "P"):
        errors.append(f"Invalid right: {contract.right!r}")
    if contract.expiry is None:
        errors.append("Missing expiry")
    return errors


def deduplicate_contracts(contracts: list[OptionContract]) -> list[OptionContract]:
    """Remove duplicates by instrument_key, keeping the first occurrence."""
    seen: set[str] = set()
    result: list[OptionContract] = []
    for c in contracts:
        k = c.instrument_key
        if k not in seen:
            seen.add(k)
            result.append(c)
    return result


def filter_by_dte(
    contracts: list[OptionContract],
    session_date: date,
    min_dte: int = 1,
    max_dte: int = 180,
) -> list[OptionContract]:
    """Keep only contracts whose expiry falls within [min_dte, max_dte] of session_date."""
    result: list[OptionContract] = []
    for c in contracts:
        if c.expiry is None:
            continue
        dte = (c.expiry - session_date).days
        if min_dte <= dte <= max_dte:
            result.append(c)
    return result


def filter_by_strike_range(
    contracts: list[OptionContract],
    spot: float,
    range_pct: float = 0.30,
) -> list[OptionContract]:
    """Keep only contracts whose strike is within ±range_pct of spot."""
    lo = spot * (1.0 - range_pct)
    hi = spot * (1.0 + range_pct)
    return [c for c in contracts if c.strike is not None and lo <= c.strike <= hi]


def filter_by_maturity_ladder(
    contracts: list[OptionContract],
    session_date: date,
    maturity_ladder_days: list[int],
    tolerance_days: int = 5,
) -> list[OptionContract]:
    """Keep contracts whose DTE is within tolerance_days of any ladder rung.

    The ladder (e.g. [10, 30, 90, 180, 270, 365, 548, 730, 1095]) defines the
    target maturities. A contract qualifies if |DTE - rung| ≤ tolerance_days
    for at least one rung.
    """
    if not maturity_ladder_days:
        return contracts
    result: list[OptionContract] = []
    for c in contracts:
        if c.expiry is None:
            continue
        dte = (c.expiry - session_date).days
        if any(abs(dte - rung) <= tolerance_days for rung in maturity_ladder_days):
            result.append(c)
    return result


def filter_by_delta_approx(
    contracts: list[OptionContract],
    spot: float,
    approx_vol: float,
    session_date: date,
    delta_range: tuple[float, float] = (-0.30, 0.30),
) -> list[OptionContract]:
    """Keep options whose approximate Black-Scholes delta falls within delta_range.

    Uses a zero-carry, zero-rate Black-Scholes N(d1) approximation:
      Call delta ≈ N(d1),  Put delta ≈ N(d1) − 1
    where d1 = [ln(S/K) + 0.5 σ² T] / (σ √T).

    delta_range=(-0.30, 0.30) keeps OTM calls (delta ∈ (0, 0.30]) and OTM puts
    (delta ∈ [-0.30, 0)). Positive-signed put deltas (as sometimes quoted) are not
    used here — put delta is always negative.

    Contracts with no expiry, no strike, or expired are excluded.
    """
    import math
    from scipy.stats import norm  # scipy is a project dependency

    delta_lo, delta_hi = delta_range
    result: list[OptionContract] = []
    for c in contracts:
        if c.expiry is None or c.strike is None or c.right not in ("C", "P"):
            continue
        dte = (c.expiry - session_date).days
        if dte <= 0:
            continue
        T = dte / 365.0
        try:
            d1 = (math.log(spot / c.strike) + 0.5 * approx_vol ** 2 * T) / (
                approx_vol * math.sqrt(T)
            )
        except (ValueError, ZeroDivisionError):
            continue
        delta = norm.cdf(d1) if c.right == "C" else norm.cdf(d1) - 1.0
        if delta_lo <= delta <= delta_hi:
            result.append(c)
    return result
