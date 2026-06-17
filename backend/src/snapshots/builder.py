"""
Market-state snapshot builder.

Pure functions: raw events in, snapshots out.
Do NOT call external services from inside this module.
That purity makes replay easy and enables unit testing with synthetic event streams.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from src.collectors.raw_collector import RawEvent
from src.snapshots.models import MarketStateSnapshot, OptionRow, UnderlyingState
from src.utils.date_utils import from_utc_epoch, year_fraction

logger = logging.getLogger(__name__)

# Reference spot fallback priority
SPOT_FALLBACK_CHAIN = ["mid", "last", "close", "carry_forward"]


def build_snapshot(events: list[RawEvent], snapshot_ts: float,
                    config: dict, debug: bool = False) -> MarketStateSnapshot:
    """
    Build one MarketStateSnapshot from a list of raw events.

    Args:
        events: Raw events prior to snapshot_ts
        snapshot_ts: Target snapshot timestamp (UTC epoch)
        config: Snapshot configuration (staleness thresholds, etc.)
        debug: If True, emit debug logs for each contract

    Returns:
        MarketStateSnapshot deterministic given same inputs.

    Config keys:
        underlying_symbol: str
        underlying_key: str
        max_underlying_age_seconds: int (default 30)
        max_option_age_seconds: int (default 60)
        max_spread_pct_for_mid: float (default 0.05)
        session_open: bool (default True)
        option_contracts: list of OptionContract or instrument_key strings (default [])
        snapshot_date: date — date of the snapshot (default derived from snapshot_ts)
        prior_close: float | None
        carry_forward_spot: float | None
    """
    state_by_key = _latest_by_field_before(events, snapshot_ts)
    underlying_key = config["underlying_key"]

    underlying = _build_underlying_state(
        state_by_key.get(underlying_key, {}),
        underlying_key,
        snapshot_ts,
        config,
    )

    option_rows = _build_option_rows(state_by_key, snapshot_ts, config, debug=debug)
    flags = _derive_state_flags(underlying, option_rows, config)

    return MarketStateSnapshot(
        snapshot_ts=snapshot_ts,
        underlying_state=underlying,
        option_rows=option_rows,
        flags=flags,
    )


def _latest_by_field_before(events: list[RawEvent],
                              cutoff_ts: float) -> dict[str, dict[str, RawEvent]]:
    """
    For each instrument_key, return the most recent RawEvent per field_name
    that occurred at or before cutoff_ts.

    Returns: {instrument_key: {field_name: RawEvent}}
    """
    result: dict[str, dict[str, RawEvent]] = {}
    eligible = sorted(
        [e for e in events if e.receipt_ts <= cutoff_ts],
        key=lambda e: e.receipt_ts,
    )
    for event in eligible:
        result.setdefault(event.instrument_key, {})
        result[event.instrument_key][event.field_name] = event
    return result


def _build_underlying_state(fields: dict[str, RawEvent], instrument_key: str,
                              snapshot_ts: float, config: dict) -> UnderlyingState:
    """
    Build UnderlyingState with reference spot selection and fallback labeling.
    Never hide which fallback was used — reference_type must always be set.
    """
    bid = _get_field_value(fields, "bid")
    ask = _get_field_value(fields, "ask")
    last = _get_field_value(fields, "last")
    volume = _get_field_value(fields, "volume")

    reference_spot, reference_type = choose_reference_spot(bid, ask, last, config)
    spread_pct = _compute_spread_pct(bid, ask)
    max_age = config.get("max_underlying_age_seconds", 30)
    age = _compute_quote_age(fields, snapshot_ts)
    is_stale = age is None or age > max_age

    return UnderlyingState(
        instrument_key=instrument_key,
        snapshot_ts=snapshot_ts,
        bid=bid,
        ask=ask,
        last=last,
        volume=volume,
        reference_spot=reference_spot,
        reference_type=reference_type,
        spread_pct=spread_pct,
        is_market_open=config.get("session_open", True),
        is_stale=is_stale,
        quote_age_seconds=age,
    )


def choose_reference_spot(bid: Optional[float], ask: Optional[float],
                            last: Optional[float], config: dict) -> tuple[float, str]:
    """
    Select reference spot using priority chain.
    Returns (reference_spot, reference_type).

    Priority: mid → last → close → carry_forward
    Each fallback is labeled — never hidden.
    """
    max_spread_pct = config.get("max_spread_pct_for_mid", 0.05)

    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else float("inf")
        if spread_pct <= max_spread_pct:
            return mid, "mid"

    if last is not None and last > 0:
        return last, "last"

    close = config.get("prior_close")
    if close is not None and close > 0:
        return float(close), "close"

    carry_forward = config.get("carry_forward_spot")
    if carry_forward is not None and carry_forward > 0:
        logger.warning("Using carry_forward spot for %s", config.get("underlying_key"))
        return float(carry_forward), "carry_forward"

    raise ValueError("No valid reference spot available; all fallbacks exhausted")


def _build_option_rows(state_by_key: dict, snapshot_ts: float,
                        config: dict, debug: bool = False) -> list[OptionRow]:
    """
    Build an OptionRow for every option contract in config["option_contracts"].

    Entries in option_contracts can be OptionContract objects or instrument_key
    strings — both are supported so this function is usable with the universe
    master or with raw string keys recovered from replay.
    """
    option_contracts = config.get("option_contracts", [])
    rows: list[OptionRow] = []
    for contract in option_contracts:
        key = contract if isinstance(contract, str) else contract.instrument_key
        fields = state_by_key.get(key, {})
        row = build_option_row(key, fields, snapshot_ts, config, debug=debug)
        if row is not None:
            rows.append(row)
    return rows


def build_option_row(instrument_key: str, fields: dict[str, RawEvent],
                      snapshot_ts: float, config: dict,
                      debug: bool = False) -> Optional[OptionRow]:
    """
    Build one OptionRow from field observations for a contract.

    Returns None only if instrument_key cannot be parsed (structural error).
    An OptionRow with all None quote fields is valid — it means no data arrived.

    Key format: SYMBOL|OPT|EXCHANGE|CURRENCY|YYYYMMDD|STRIKE|RIGHT|MULTIPLIER
    """
    parts = instrument_key.split("|")
    if len(parts) != 8:
        logger.warning("Cannot parse option key (expected 8 parts): %s", instrument_key)
        return None

    underlying_symbol = parts[0]
    expiry_yyyymmdd = parts[4]
    strike_str = parts[5]
    option_right = parts[6]
    multiplier_str = parts[7]

    try:
        expiry_date = datetime.strptime(expiry_yyyymmdd, "%Y%m%d").date()
        strike = float(strike_str) if strike_str else None
        multiplier = float(multiplier_str) if multiplier_str else 100.0
    except (ValueError, TypeError) as exc:
        logger.warning("Failed to parse option key fields %s: %s", instrument_key, exc)
        return None

    expiry_str = expiry_date.isoformat()

    bid = _get_field_value(fields, "bid")
    ask = _get_field_value(fields, "ask")
    last = _get_field_value(fields, "last")
    volume = _get_field_value(fields, "volume")
    open_interest = _get_field_value(fields, "open_interest")

    mid: Optional[float] = None
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        mid = (bid + ask) / 2.0

    spread_pct = _compute_spread_pct(bid, ask)
    age = _compute_quote_age(fields, snapshot_ts)
    max_age = config.get("max_option_age_seconds", 60)
    is_stale = age is None or age > max_age

    snapshot_date = config.get("snapshot_date") or from_utc_epoch(snapshot_ts).date()
    maturity_years: Optional[float] = None
    if expiry_date >= snapshot_date:
        maturity_years = year_fraction(snapshot_date, expiry_date)

    if debug:
        logger.debug(
            "option_row key=%s bid=%s ask=%s mid=%s age=%.1fs stale=%s",
            instrument_key, bid, ask, mid, age if age is not None else -1, is_stale,
        )

    return OptionRow(
        instrument_key=instrument_key,
        snapshot_ts=snapshot_ts,
        underlying_symbol=underlying_symbol,
        expiry_str=expiry_str,
        strike=strike,
        option_right=option_right,
        multiplier=multiplier,
        bid=bid,
        ask=ask,
        last=last,
        mid=mid,
        volume=volume,
        open_interest=open_interest,
        spread_pct=spread_pct,
        quote_age_seconds=age,
        is_stale=is_stale,
        maturity_years=maturity_years,
    )


def _derive_state_flags(underlying: UnderlyingState, option_rows: list[OptionRow],
                         config: dict) -> dict:
    """
    Compute snapshot-level quality flags:
      session_open, stale_underlying, data_complete,
      option_coverage_pct, completeness_by_maturity, option_count.
    """
    completeness_by_maturity: dict[str, dict] = {}
    for row in option_rows:
        bucket = completeness_by_maturity.setdefault(
            row.expiry_str, {"total": 0, "with_quotes": 0}
        )
        bucket["total"] += 1
        if row.bid is not None or row.ask is not None or row.last is not None:
            bucket["with_quotes"] += 1

    for bucket in completeness_by_maturity.values():
        n = bucket["total"]
        bucket["coverage_pct"] = round(bucket["with_quotes"] / n, 4) if n > 0 else 0.0

    total = len(option_rows)
    with_quotes = sum(
        1 for r in option_rows
        if r.bid is not None or r.ask is not None or r.last is not None
    )
    coverage_pct = round(with_quotes / total, 4) if total > 0 else 0.0

    return {
        "session_open": config.get("session_open", True),
        "stale_underlying": underlying.is_stale,
        "data_complete": total > 0 and with_quotes == total,
        "option_coverage_pct": coverage_pct,
        "completeness_by_maturity": completeness_by_maturity,
        "option_count": total,
        "options_with_quotes": with_quotes,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_field_value(fields: dict[str, RawEvent], field_name: str) -> Optional[float]:
    event = fields.get(field_name)
    return event.field_value if event is not None else None


def _compute_spread_pct(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid if mid > 0 else None


def _compute_quote_age(fields: dict[str, RawEvent], snapshot_ts: float) -> Optional[float]:
    if not fields:
        return None
    latest_ts = max(e.receipt_ts for e in fields.values())
    return snapshot_ts - latest_ts
