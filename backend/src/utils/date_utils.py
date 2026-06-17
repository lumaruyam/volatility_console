"""Date and time utilities — calendars, year fractions, timestamp normalization."""

from __future__ import annotations

from datetime import date, datetime, timezone


def year_fraction(start: date, end: date, day_count: str = "act/365") -> float:
    """
    Compute year fraction from start to end using specified day-count convention.

    Supported conventions:
    - "act/365": actual days / 365
    - "act/360": actual days / 360
    - "act/act": actual days / actual days in year

    Always document which convention is used — store it alongside T.
    """
    days = (end - start).days
    if day_count == "act/365":
        return days / 365.0
    elif day_count == "act/360":
        return days / 360.0
    elif day_count == "act/act":
        year_days = 366 if _is_leap_year(start.year) else 365
        return days / year_days
    raise ValueError(f"Unknown day_count convention: {day_count!r}")


def to_utc_epoch(dt: datetime) -> float:
    """Convert a datetime (aware or naive UTC) to UTC epoch seconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def from_utc_epoch(epoch: float) -> datetime:
    """Convert UTC epoch seconds to timezone-aware datetime."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def utc_now_epoch() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def is_trading_day(d: date, calendar: str = "US") -> bool:
    """
    Return True if d is a trading day for the given calendar.
    Basic implementation: Monday–Friday, no holidays.
    TODO: integrate with a proper holiday calendar.
    """
    return d.weekday() < 5   # Mon=0 … Fri=4


def next_trading_day(d: date, calendar: str = "US") -> date:
    """Return next trading day after d."""
    from datetime import timedelta
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate, calendar):
        candidate += timedelta(days=1)
    return candidate


def _is_leap_year(year: int) -> bool:
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
