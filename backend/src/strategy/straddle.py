"""
ATR Straddle strategy — professor requirement.

Buy 1 ATM call + 1 ATM put on Euro Stoxx 50.
Roll when the existing position reaches 9-month maturity (roll_dte_days ≈ 270).

Strike selection: option with |delta| closest to atm_delta_target (≈ 0.50).
Expiry selection: available expiry closest to target_dte_months months out.
Position sizing: fixed notional OR vol-adjusted (configurable).

Pure functions — no IBKR session; OrderManager handles actual placement.

Acceptance criterion: straddle opens/rolls without errors; positions reconcile.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class StraddleLeg:
    """One leg (call or put) of an ATR Straddle position."""
    contract_key: str
    option_type: str       # "C" | "P"
    strike: float
    expiry_str: str        # "YYYY-MM-DD"
    quantity: float        # positive = long
    open_price: float      # mid-price at open
    multiplier: float = 10.0
    currency: str = "EUR"

    @property
    def notional_value(self) -> float:
        return self.open_price * self.quantity * self.multiplier


@dataclass
class StraddlePosition:
    """
    A live ATR Straddle — two legs (call + put) on the same underlying and expiry.
    position_id is generated at open and never changes; used for reconciliation.
    """
    position_id: str
    underlying: str
    call_leg: StraddleLeg
    put_leg: StraddleLeg
    open_date: str          # "YYYY-MM-DD"
    target_expiry: str      # expiry selected at open
    status: str             # "open" | "rolling" | "closed"
    notional: float
    config_version: str = "1.0"

    @property
    def legs(self) -> list[StraddleLeg]:
        return [self.call_leg, self.put_leg]

    @property
    def strike(self) -> float:
        """Both legs share the same (ATM) strike."""
        return self.call_leg.strike

    def dte(self, as_of_date: str) -> int:
        """Calendar days remaining to expiry as of a given date."""
        expiry = date.fromisoformat(self.target_expiry)
        ref = date.fromisoformat(as_of_date)
        return max(0, (expiry - ref).days)


# ---------------------------------------------------------------------------
# Strike and expiry selection
# ---------------------------------------------------------------------------

def select_atm_strike(
    chain_rows: list[dict],
    config: dict,
    option_type: str = "C",
) -> Optional[dict]:
    """
    Choose the chain row with |delta| closest to atm_delta_target.

    chain_rows: list of dicts with "strike", "delta", "option_type", "expiry_str".
    Returns the best row, or None if chain is empty.
    """
    target_delta = float(config.get("atm_delta_target", 0.50))
    max_dev = float(config.get("max_delta_deviation", 0.10))

    candidates = [
        r for r in chain_rows
        if r.get("option_type") == option_type
        and r.get("delta") is not None
    ]
    if not candidates:
        return None

    best = min(candidates, key=lambda r: abs(abs(float(r["delta"])) - target_delta))
    if abs(abs(float(best["delta"])) - target_delta) > max_dev:
        return None
    return best


def select_expiry(
    available_expiries: list[str],
    trade_date: str,
    config: dict,
) -> Optional[str]:
    """
    Choose the expiry closest to target_dte_months months from trade_date.

    available_expiries: list of "YYYY-MM-DD" strings.
    Returns the best expiry string, or None if list is empty.
    """
    target_months = int(config.get("target_dte_months", 12))
    ref = date.fromisoformat(trade_date)
    target_date = ref + timedelta(days=target_months * 30)

    candidates = [e for e in available_expiries if date.fromisoformat(e) > ref]
    if not candidates:
        return None

    return min(candidates, key=lambda e: abs((date.fromisoformat(e) - target_date).days))


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def compute_position_size(
    spot: float,
    sigma: float,
    config: dict,
) -> float:
    """
    Return the quantity (number of contracts) for one leg.

    Modes (config.sizing_mode):
      fixed_notional:  quantity = floor(notional / (spot * multiplier))
      vol_adjusted:    quantity = floor(notional / (spot * sigma * multiplier))
                        keeps dollar-vega approximately constant across vols.

    Always returns at least 1.0.
    """
    notional = float(config.get("notional", 100_000.0))
    multiplier = float(config.get("multiplier", 10.0))
    mode = config.get("sizing_mode", "fixed_notional")

    if mode == "vol_adjusted":
        denom = spot * max(sigma, 1e-4) * multiplier
    else:
        denom = spot * multiplier

    return max(1.0, math.floor(notional / denom))


# ---------------------------------------------------------------------------
# Roll decision
# ---------------------------------------------------------------------------

def should_roll(
    position: StraddlePosition,
    trade_date: str,
    config: dict,
) -> bool:
    """
    Return True when the straddle should be rolled.
    Triggers when remaining DTE ≤ roll_dte_days (≈ 9 months = 270 days).
    """
    if position.status != "open":
        return False
    threshold = int(config.get("roll_dte_days", 270))
    return position.dte(trade_date) <= threshold


# ---------------------------------------------------------------------------
# Open and roll
# ---------------------------------------------------------------------------

def open_straddle(
    call_row: dict,
    put_row: dict,
    spot: float,
    sigma: float,
    trade_date: str,
    config: dict,
    underlying: str = "ESTX50",
) -> StraddlePosition:
    """
    Construct a StraddlePosition from selected call and put chain rows.

    call_row / put_row must have: "contract_key", "strike", "expiry_str",
                                   "mid_price" (or "price"), "option_type".
    """
    mult = float(config.get("multiplier", 10.0))
    qty = compute_position_size(spot, sigma, config)
    position_id = str(uuid.uuid4())[:8]

    def _leg(row: dict, opt_type: str) -> StraddleLeg:
        price = float(row.get("mid_price") or row.get("price") or 0.0)
        return StraddleLeg(
            contract_key=row["contract_key"],
            option_type=opt_type,
            strike=float(row["strike"]),
            expiry_str=row["expiry_str"],
            quantity=qty,
            open_price=price,
            multiplier=mult,
        )

    call_leg = _leg(call_row, "C")
    put_leg = _leg(put_row, "P")

    return StraddlePosition(
        position_id=position_id,
        underlying=underlying,
        call_leg=call_leg,
        put_leg=put_leg,
        open_date=trade_date,
        target_expiry=call_leg.expiry_str,
        status="open",
        notional=float(config.get("notional", 100_000.0)),
        config_version=config.get("version", "1.0"),
    )


def roll_straddle(
    old_position: StraddlePosition,
    call_row: dict,
    put_row: dict,
    spot: float,
    sigma: float,
    trade_date: str,
    config: dict,
) -> tuple[StraddlePosition, StraddlePosition]:
    """
    Roll the straddle: mark old position as 'closed' and open a new one.

    Returns (closed_position, new_position).
    The caller is responsible for submitting close orders (sell old legs)
    and open orders (buy new legs) via OrderManager.
    """
    closed = StraddlePosition(
        position_id=old_position.position_id,
        underlying=old_position.underlying,
        call_leg=old_position.call_leg,
        put_leg=old_position.put_leg,
        open_date=old_position.open_date,
        target_expiry=old_position.target_expiry,
        status="closed",
        notional=old_position.notional,
        config_version=old_position.config_version,
    )

    new_position = open_straddle(
        call_row=call_row,
        put_row=put_row,
        spot=spot,
        sigma=sigma,
        trade_date=trade_date,
        config=config,
        underlying=old_position.underlying,
    )

    return closed, new_position


# ---------------------------------------------------------------------------
# Position reconciliation
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationReport:
    """
    Compares expected straddle legs vs broker-reported positions.
    Used to verify that all orders were filled and recorded correctly.
    """
    trade_date: str
    matching: list[str]           # contract_keys that agree
    missing_in_broker: list[str]  # expected but not in broker
    extra_in_broker: list[str]    # in broker but not expected
    quantity_mismatches: list[dict]  # {contract_key, expected_qty, broker_qty}

    @property
    def is_reconciled(self) -> bool:
        return (
            not self.missing_in_broker
            and not self.extra_in_broker
            and not self.quantity_mismatches
        )


def reconcile_positions(
    expected_legs: list[StraddleLeg],
    broker_positions: dict[str, float],
    trade_date: str,
    quantity_tolerance: float = 0.0,
) -> ReconciliationReport:
    """
    Compare expected straddle legs vs broker-reported quantities.

    expected_legs:    List of StraddleLeg from open/roll logic.
    broker_positions: {contract_key: signed_quantity} from broker API.
    quantity_tolerance: Allowable absolute difference before flagging mismatch.

    Returns ReconciliationReport with matching / missing / extra / mismatches.
    """
    expected_map = {leg.contract_key: leg.quantity for leg in expected_legs}
    expected_keys = set(expected_map)
    broker_keys = set(broker_positions)

    missing_in_broker = sorted(expected_keys - broker_keys)
    extra_in_broker = sorted(broker_keys - expected_keys)
    common_keys = expected_keys & broker_keys

    matching: list[str] = []
    quantity_mismatches: list[dict] = []

    for key in sorted(common_keys):
        exp_qty = expected_map[key]
        brk_qty = broker_positions[key]
        if abs(exp_qty - brk_qty) <= quantity_tolerance:
            matching.append(key)
        else:
            quantity_mismatches.append({
                "contract_key": key,
                "expected_qty": exp_qty,
                "broker_qty": brk_qty,
                "diff": brk_qty - exp_qty,
            })

    return ReconciliationReport(
        trade_date=trade_date,
        matching=matching,
        missing_in_broker=missing_in_broker,
        extra_in_broker=extra_in_broker,
        quantity_mismatches=quantity_mismatches,
    )
