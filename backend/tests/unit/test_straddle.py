"""
Comprehensive tests for Step 16: ATR Straddle strategy and paper execution.

Acceptance criterion (PLAN):
  Straddle opens/rolls without errors in paper account; positions reconcile.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest

from src.strategy.straddle import (
    ReconciliationReport,
    StraddleLeg,
    StraddlePosition,
    compute_position_size,
    open_straddle,
    reconcile_positions,
    roll_straddle,
    select_atm_strike,
    select_expiry,
    should_roll,
)
from src.execution.order_manager import (
    OrderManager,
    OrderRequest,
    OrderResult,
    ReadOnlyModeError,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

CFG = {
    "version": "1.0",
    "target_dte_months": 12,
    "roll_dte_days": 270,
    "atm_delta_target": 0.50,
    "max_delta_deviation": 0.10,
    "sizing_mode": "fixed_notional",
    "notional": 100_000.0,
    "multiplier": 10.0,
    "order_type": "LMT",
    "time_in_force": "DAY",
}

TRADE_DATE = "2025-01-15"
EXPIRY_12M = "2026-01-16"
EXPIRY_6M = "2025-07-18"
SPOT = 5000.0
SIGMA = 0.20


def _call_row(strike=5000.0, delta=0.52, expiry=EXPIRY_12M,
              price=150.0, key=None) -> dict:
    return {
        "contract_key": key or f"ESTX50_{expiry}_C_{int(strike)}",
        "option_type": "C",
        "strike": strike,
        "expiry_str": expiry,
        "delta": delta,
        "mid_price": price,
    }


def _put_row(strike=5000.0, delta=-0.48, expiry=EXPIRY_12M,
             price=145.0, key=None) -> dict:
    return {
        "contract_key": key or f"ESTX50_{expiry}_P_{int(strike)}",
        "option_type": "P",
        "strike": strike,
        "expiry_str": expiry,
        "delta": delta,
        "mid_price": price,
    }


def _chain(n_strikes=5, base_strike=5000.0, step=50.0, expiry=EXPIRY_12M) -> list[dict]:
    rows = []
    for i in range(n_strikes):
        K = base_strike + (i - n_strikes // 2) * step
        # Delta decreasing from ~0.70 to ~0.30 across strikes
        delta_c = 0.70 - i * 0.10
        rows.append(_call_row(strike=K, delta=delta_c, expiry=expiry))
        rows.append(_put_row(strike=K, delta=-(1 - delta_c), expiry=expiry))
    return rows


def _open_position(trade_date=TRADE_DATE, expiry=EXPIRY_12M,
                   strike=5000.0, qty=1.0) -> StraddlePosition:
    call = _call_row(strike=strike, expiry=expiry)
    put = _put_row(strike=strike, expiry=expiry)
    return open_straddle(call, put, SPOT, SIGMA, trade_date, CFG)


def _mock_session(status="submitted", order_id="ord001",
                  filled_price=150.0) -> MagicMock:
    session = MagicMock()
    session.place_order.return_value = {
        "order_id": order_id,
        "status": status,
        "filled_price": filled_price,
        "filled_quantity": 1.0,
    }
    session.cancel_order.return_value = {"success": True}
    session.get_order_status.return_value = {
        "contract_key": "K1",
        "action": "BUY",
        "quantity": 1.0,
        "status": status,
        "filled_price": filled_price,
        "filled_quantity": 1.0,
    }
    return session


# ===========================================================================
# TestStraddleLeg
# ===========================================================================

class TestStraddleLeg:
    def test_fields(self):
        leg = StraddleLeg("K1", "C", 5000.0, EXPIRY_12M, 2.0, 150.0, 10.0)
        assert leg.contract_key == "K1"
        assert leg.option_type == "C"
        assert leg.strike == 5000.0
        assert leg.quantity == 2.0
        assert leg.open_price == 150.0
        assert leg.multiplier == 10.0

    def test_notional_value(self):
        leg = StraddleLeg("K1", "C", 5000.0, EXPIRY_12M, 2.0, 150.0, 10.0)
        assert leg.notional_value == pytest.approx(150.0 * 2.0 * 10.0)

    def test_default_currency_eur(self):
        leg = StraddleLeg("K1", "C", 5000.0, EXPIRY_12M, 1.0, 100.0)
        assert leg.currency == "EUR"

    def test_default_multiplier(self):
        leg = StraddleLeg("K1", "C", 5000.0, EXPIRY_12M, 1.0, 100.0)
        assert leg.multiplier == 10.0


# ===========================================================================
# TestStraddlePosition
# ===========================================================================

class TestStraddlePosition:
    def _pos(self, expiry=EXPIRY_12M) -> StraddlePosition:
        return _open_position(expiry=expiry)

    def test_has_call_and_put(self):
        pos = self._pos()
        assert pos.call_leg.option_type == "C"
        assert pos.put_leg.option_type == "P"

    def test_legs_property(self):
        pos = self._pos()
        legs = pos.legs
        assert len(legs) == 2
        types = {l.option_type for l in legs}
        assert types == {"C", "P"}

    def test_strike_from_call_leg(self):
        pos = self._pos()
        assert pos.strike == pos.call_leg.strike

    def test_status_open_at_creation(self):
        assert self._pos().status == "open"

    def test_dte_calculation(self):
        pos = _open_position(trade_date="2025-01-15", expiry="2026-01-15")
        assert pos.dte("2025-01-15") == 365

    def test_dte_zero_at_expiry(self):
        pos = _open_position(trade_date="2025-01-15", expiry="2026-01-15")
        assert pos.dte("2026-01-15") == 0

    def test_dte_never_negative(self):
        pos = _open_position(trade_date="2025-01-15", expiry="2025-06-15")
        assert pos.dte("2026-01-01") == 0

    def test_position_id_generated(self):
        pos = self._pos()
        assert len(pos.position_id) > 0

    def test_unique_position_ids(self):
        p1, p2 = self._pos(), self._pos()
        assert p1.position_id != p2.position_id

    def test_underlying_stored(self):
        pos = self._pos()
        assert pos.underlying == "ESTX50"

    def test_config_version_stored(self):
        pos = self._pos()
        assert pos.config_version == "1.0"


# ===========================================================================
# TestSelectAtmStrike
# ===========================================================================

class TestSelectAtmStrike:
    def test_returns_closest_to_target_delta(self):
        chain = _chain(n_strikes=5)
        best = select_atm_strike(chain, CFG, option_type="C")
        assert best is not None
        assert abs(abs(best["delta"]) - 0.50) < 0.15

    def test_returns_none_on_empty_chain(self):
        assert select_atm_strike([], CFG, option_type="C") is None

    def test_only_correct_option_type(self):
        chain = _chain()
        call_best = select_atm_strike(chain, CFG, "C")
        put_best = select_atm_strike(chain, CFG, "P")
        assert call_best["option_type"] == "C"
        assert put_best["option_type"] == "P"

    def test_returns_none_outside_max_deviation(self):
        chain = [_call_row(delta=0.10)]  # |0.10 - 0.50| = 0.40 > max_dev=0.10
        assert select_atm_strike(chain, CFG, "C") is None

    def test_within_deviation_limit_returned(self):
        chain = [_call_row(delta=0.55)]  # |0.55 - 0.50| = 0.05 < 0.10
        assert select_atm_strike(chain, CFG, "C") is not None

    def test_missing_delta_rows_skipped(self):
        chain = [{"option_type": "C", "strike": 5000.0, "delta": None,
                  "contract_key": "K1", "expiry_str": EXPIRY_12M}]
        assert select_atm_strike(chain, CFG, "C") is None

    def test_exact_atm_selected(self):
        chain = [
            _call_row(strike=4900.0, delta=0.60),
            _call_row(strike=5000.0, delta=0.50),
            _call_row(strike=5100.0, delta=0.40),
        ]
        best = select_atm_strike(chain, CFG, "C")
        assert best["strike"] == pytest.approx(5000.0)


# ===========================================================================
# TestSelectExpiry
# ===========================================================================

class TestSelectExpiry:
    def test_returns_closest_to_target(self):
        expiries = ["2025-06-20", "2025-12-19", "2026-06-19"]
        # 12 months from 2025-01-15 ≈ 2026-01-15 → closest is 2025-12-19
        best = select_expiry(expiries, "2025-01-15", CFG)
        assert best == "2025-12-19"

    def test_returns_none_on_empty(self):
        assert select_expiry([], TRADE_DATE, CFG) is None

    def test_excludes_past_expiries(self):
        expiries = ["2024-12-20", "2026-01-16"]
        best = select_expiry(expiries, "2025-01-15", CFG)
        assert best == "2026-01-16"

    def test_all_past_returns_none(self):
        expiries = ["2024-01-01", "2024-06-01"]
        assert select_expiry(expiries, "2025-01-15", CFG) is None

    def test_single_future_expiry_returned(self):
        best = select_expiry([EXPIRY_12M], TRADE_DATE, CFG)
        assert best == EXPIRY_12M

    def test_custom_target_dte(self):
        cfg = {**CFG, "target_dte_months": 6}
        expiries = ["2025-07-18", "2026-01-16"]
        # 6 months from 2025-01-15 ≈ 2025-07-15 → closest is 2025-07-18
        best = select_expiry(expiries, "2025-01-15", cfg)
        assert best == "2025-07-18"


# ===========================================================================
# TestComputePositionSize
# ===========================================================================

class TestComputePositionSize:
    def test_fixed_notional_basic(self):
        # 100_000 / (5000 * 10) = 2.0
        qty = compute_position_size(SPOT, SIGMA, CFG)
        assert qty == pytest.approx(2.0)

    def test_fixed_notional_floors_down(self):
        cfg = {**CFG, "notional": 150_000.0}
        # 150_000 / (5000 * 10) = 3.0 exactly
        qty = compute_position_size(SPOT, SIGMA, cfg)
        assert qty == pytest.approx(3.0)

    def test_minimum_quantity_is_one(self):
        cfg = {**CFG, "notional": 1.0}  # tiny notional
        qty = compute_position_size(SPOT, SIGMA, cfg)
        assert qty == pytest.approx(1.0)

    def test_vol_adjusted_mode(self):
        cfg = {**CFG, "sizing_mode": "vol_adjusted"}
        qty = compute_position_size(SPOT, SIGMA, cfg)
        # 100_000 / (5000 * 0.20 * 10) = 10.0
        assert qty == pytest.approx(10.0)

    def test_vol_adjusted_higher_vol_lower_qty(self):
        cfg = {**CFG, "sizing_mode": "vol_adjusted"}
        qty_low = compute_position_size(SPOT, 0.10, cfg)
        qty_high = compute_position_size(SPOT, 0.40, cfg)
        assert qty_low > qty_high

    def test_fixed_and_vol_both_return_positive(self):
        for mode in ("fixed_notional", "vol_adjusted"):
            cfg = {**CFG, "sizing_mode": mode}
            assert compute_position_size(SPOT, SIGMA, cfg) >= 1.0

    def test_result_is_integer(self):
        qty = compute_position_size(SPOT, SIGMA, CFG)
        assert qty == math.floor(qty)


# ===========================================================================
# TestShouldRoll
# ===========================================================================

class TestShouldRoll:
    def test_roll_when_dte_at_threshold(self):
        pos = _open_position(trade_date="2025-01-15", expiry="2025-12-12")
        # DTE from "2025-09-15" to "2025-12-12" ≈ 88 days — well below 270
        assert should_roll(pos, "2025-09-15", CFG)

    def test_no_roll_when_dte_above_threshold(self):
        pos = _open_position(trade_date="2025-01-15", expiry=EXPIRY_12M)
        # DTE from trade_date to EXPIRY_12M ≈ 365 days > 270
        assert not should_roll(pos, TRADE_DATE, CFG)

    def test_roll_exactly_at_threshold(self):
        # 270 days from "2025-01-15" = "2025-10-12"
        pos = _open_position(trade_date="2025-01-15", expiry="2025-10-12")
        assert should_roll(pos, "2025-01-15", CFG)

    def test_no_roll_when_status_closed(self):
        pos = _open_position()
        # Simulate a closed position
        from dataclasses import replace
        closed = StraddlePosition(
            position_id=pos.position_id, underlying=pos.underlying,
            call_leg=pos.call_leg, put_leg=pos.put_leg,
            open_date=pos.open_date, target_expiry=pos.target_expiry,
            status="closed", notional=pos.notional,
        )
        assert not should_roll(closed, "2025-10-01", CFG)

    def test_custom_roll_threshold(self):
        cfg = {**CFG, "roll_dte_days": 30}
        pos = _open_position(trade_date="2025-01-15", expiry="2025-03-15")
        # DTE ≈ 59 days > 30 → no roll yet
        assert not should_roll(pos, TRADE_DATE, cfg)
        # DTE ≈ 15 days < 30 → roll
        assert should_roll(pos, "2025-02-28", cfg)


# ===========================================================================
# TestOpenStraddle
# ===========================================================================

class TestOpenStraddle:
    def test_returns_straddle_position(self):
        assert isinstance(_open_position(), StraddlePosition)

    def test_call_leg_correct_type(self):
        pos = _open_position()
        assert pos.call_leg.option_type == "C"

    def test_put_leg_correct_type(self):
        pos = _open_position()
        assert pos.put_leg.option_type == "P"

    def test_both_legs_same_strike(self):
        pos = _open_position(strike=5000.0)
        assert pos.call_leg.strike == pos.put_leg.strike == pytest.approx(5000.0)

    def test_both_legs_same_expiry(self):
        pos = _open_position(expiry=EXPIRY_12M)
        assert pos.call_leg.expiry_str == pos.put_leg.expiry_str == EXPIRY_12M

    def test_quantity_from_config(self):
        pos = _open_position()
        # 100_000 / (5000 * 10) = 2.0
        assert pos.call_leg.quantity == pytest.approx(2.0)
        assert pos.put_leg.quantity == pytest.approx(2.0)

    def test_open_price_from_row(self):
        call = _call_row(price=175.0)
        put = _put_row(price=160.0)
        pos = open_straddle(call, put, SPOT, SIGMA, TRADE_DATE, CFG)
        assert pos.call_leg.open_price == pytest.approx(175.0)
        assert pos.put_leg.open_price == pytest.approx(160.0)

    def test_open_date_stored(self):
        pos = _open_position(trade_date=TRADE_DATE)
        assert pos.open_date == TRADE_DATE

    def test_target_expiry_stored(self):
        pos = _open_position(expiry=EXPIRY_12M)
        assert pos.target_expiry == EXPIRY_12M

    def test_status_open(self):
        assert _open_position().status == "open"

    def test_underlying_default(self):
        call = _call_row()
        put = _put_row()
        pos = open_straddle(call, put, SPOT, SIGMA, TRADE_DATE, CFG)
        assert pos.underlying == "ESTX50"

    def test_contract_keys_different(self):
        pos = _open_position()
        assert pos.call_leg.contract_key != pos.put_leg.contract_key

    def test_notional_stored(self):
        pos = _open_position()
        assert pos.notional == pytest.approx(CFG["notional"])


# ===========================================================================
# TestRollStraddle
# ===========================================================================

class TestRollStraddle:
    def test_returns_two_positions(self):
        old = _open_position()
        call = _call_row(key="NEW_CALL")
        put = _put_row(key="NEW_PUT")
        closed, new = roll_straddle(old, call, put, SPOT, SIGMA, TRADE_DATE, CFG)
        assert isinstance(closed, StraddlePosition)
        assert isinstance(new, StraddlePosition)

    def test_old_position_status_closed(self):
        old = _open_position()
        call = _call_row(key="NC")
        put = _put_row(key="NP")
        closed, _ = roll_straddle(old, call, put, SPOT, SIGMA, TRADE_DATE, CFG)
        assert closed.status == "closed"

    def test_new_position_status_open(self):
        old = _open_position()
        call = _call_row(key="NC")
        put = _put_row(key="NP")
        _, new = roll_straddle(old, call, put, SPOT, SIGMA, TRADE_DATE, CFG)
        assert new.status == "open"

    def test_old_position_id_preserved(self):
        old = _open_position()
        call = _call_row(key="NC")
        put = _put_row(key="NP")
        closed, _ = roll_straddle(old, call, put, SPOT, SIGMA, TRADE_DATE, CFG)
        assert closed.position_id == old.position_id

    def test_new_position_id_differs(self):
        old = _open_position()
        call = _call_row(key="NC")
        put = _put_row(key="NP")
        _, new = roll_straddle(old, call, put, SPOT, SIGMA, TRADE_DATE, CFG)
        assert new.position_id != old.position_id

    def test_new_legs_are_new_contracts(self):
        old = _open_position()
        new_call = _call_row(key="BRAND_NEW_CALL")
        new_put = _put_row(key="BRAND_NEW_PUT")
        _, new = roll_straddle(old, new_call, new_put, SPOT, SIGMA, TRADE_DATE, CFG)
        assert new.call_leg.contract_key == "BRAND_NEW_CALL"
        assert new.put_leg.contract_key == "BRAND_NEW_PUT"

    def test_underlying_preserved(self):
        old = _open_position()
        _, new = roll_straddle(old, _call_row(key="NC"), _put_row(key="NP"),
                               SPOT, SIGMA, TRADE_DATE, CFG)
        assert new.underlying == old.underlying


# ===========================================================================
# TestReconcilePositions
# ===========================================================================

class TestReconcilePositions:
    def _leg(self, key, qty=2.0, opt="C") -> StraddleLeg:
        return StraddleLeg(key, opt, 5000.0, EXPIRY_12M, qty, 150.0)

    def test_perfect_match_is_reconciled(self):
        legs = [self._leg("C1", 2.0, "C"), self._leg("P1", 2.0, "P")]
        broker = {"C1": 2.0, "P1": 2.0}
        r = reconcile_positions(legs, broker, TRADE_DATE)
        assert r.is_reconciled
        assert len(r.matching) == 2

    def test_missing_in_broker_detected(self):
        legs = [self._leg("C1"), self._leg("P1")]
        broker = {"C1": 2.0}  # P1 missing
        r = reconcile_positions(legs, broker, TRADE_DATE)
        assert "P1" in r.missing_in_broker
        assert not r.is_reconciled

    def test_extra_in_broker_detected(self):
        legs = [self._leg("C1")]
        broker = {"C1": 2.0, "EXTRA": 1.0}
        r = reconcile_positions(legs, broker, TRADE_DATE)
        assert "EXTRA" in r.extra_in_broker
        assert not r.is_reconciled

    def test_quantity_mismatch_detected(self):
        legs = [self._leg("C1", qty=2.0)]
        broker = {"C1": 3.0}
        r = reconcile_positions(legs, broker, TRADE_DATE)
        assert len(r.quantity_mismatches) == 1
        assert r.quantity_mismatches[0]["contract_key"] == "C1"
        assert r.quantity_mismatches[0]["expected_qty"] == pytest.approx(2.0)
        assert r.quantity_mismatches[0]["broker_qty"] == pytest.approx(3.0)

    def test_tolerance_hides_small_diff(self):
        legs = [self._leg("C1", qty=2.0)]
        broker = {"C1": 2.0001}
        r = reconcile_positions(legs, broker, TRADE_DATE, quantity_tolerance=0.01)
        assert r.is_reconciled

    def test_empty_legs_empty_broker_reconciled(self):
        r = reconcile_positions([], {}, TRADE_DATE)
        assert r.is_reconciled

    def test_trade_date_stored(self):
        r = reconcile_positions([], {}, TRADE_DATE)
        assert r.trade_date == TRADE_DATE

    def test_report_type(self):
        r = reconcile_positions([], {}, TRADE_DATE)
        assert isinstance(r, ReconciliationReport)


# ===========================================================================
# TestOrderRequest
# ===========================================================================

class TestOrderRequest:
    def test_valid_buy_lmt(self):
        req = OrderRequest("K1", "BUY", 2.0, "LMT", limit_price=150.0)
        assert req.action == "BUY"
        assert req.quantity == 2.0
        assert req.limit_price == 150.0

    def test_invalid_action_raises(self):
        with pytest.raises(ValueError, match="action"):
            OrderRequest("K1", "HOLD", 1.0, "MKT")

    def test_invalid_order_type_raises(self):
        with pytest.raises(ValueError, match="order_type"):
            OrderRequest("K1", "BUY", 1.0, "IOC")

    def test_zero_quantity_raises(self):
        with pytest.raises(ValueError, match="quantity"):
            OrderRequest("K1", "BUY", 0.0, "MKT")

    def test_negative_quantity_raises(self):
        with pytest.raises(ValueError, match="quantity"):
            OrderRequest("K1", "BUY", -1.0, "MKT")

    def test_lmt_without_price_raises(self):
        with pytest.raises(ValueError, match="limit_price"):
            OrderRequest("K1", "BUY", 1.0, "LMT", limit_price=None)

    def test_mkt_without_price_ok(self):
        req = OrderRequest("K1", "BUY", 1.0, "MKT")
        assert req.limit_price is None

    def test_default_time_in_force(self):
        req = OrderRequest("K1", "BUY", 1.0, "MKT")
        assert req.time_in_force == "DAY"


# ===========================================================================
# TestOrderResult
# ===========================================================================

class TestOrderResult:
    def _result(self, status="submitted") -> OrderResult:
        return OrderResult("ord1", "K1", "BUY", 2.0, status, filled_price=150.0)

    def test_is_filled_true(self):
        assert self._result("filled").is_filled

    def test_is_filled_false(self):
        assert not self._result("submitted").is_filled

    def test_is_submitted_true(self):
        assert self._result("submitted").is_submitted

    def test_fields(self):
        r = self._result("filled")
        assert r.order_id == "ord1"
        assert r.contract_key == "K1"
        assert r.action == "BUY"
        assert r.quantity == pytest.approx(2.0)
        assert r.filled_price == pytest.approx(150.0)


# ===========================================================================
# TestOrderManager
# ===========================================================================

class TestOrderManager:
    def _om(self, read_only=False, status="submitted") -> tuple[OrderManager, MagicMock]:
        session = _mock_session(status=status)
        return OrderManager(session, read_only=read_only, paper_trading=True), session

    def _req(self, action="BUY", key="K1", qty=2.0) -> OrderRequest:
        return OrderRequest(key, action, qty, "LMT", limit_price=150.0)

    def test_place_order_returns_result(self):
        om, _ = self._om()
        result = om.place_order(self._req())
        assert isinstance(result, OrderResult)

    def test_place_order_sets_order_id(self):
        om, _ = self._om()
        result = om.place_order(self._req())
        assert result.order_id == "ord001"

    def test_place_order_status_submitted(self):
        om, _ = self._om(status="submitted")
        result = om.place_order(self._req())
        assert result.status == "submitted"

    def test_read_only_raises_on_place(self):
        om, _ = self._om(read_only=True)
        with pytest.raises(ReadOnlyModeError):
            om.place_order(self._req())

    def test_broker_exception_returns_rejected(self):
        om, session = self._om()
        session.place_order.side_effect = RuntimeError("broker down")
        result = om.place_order(self._req())
        assert result.status == "rejected"
        assert "broker down" in result.message

    def test_cancel_order_returns_true(self):
        om, _ = self._om()
        assert om.cancel_order("ord001") is True

    def test_cancel_order_read_only_raises(self):
        om, _ = self._om(read_only=True)
        with pytest.raises(ReadOnlyModeError):
            om.cancel_order("ord001")

    def test_cancel_order_exception_returns_false(self):
        om, session = self._om()
        session.cancel_order.side_effect = RuntimeError("timeout")
        assert om.cancel_order("ord001") is False

    def test_get_order_status_returns_result(self):
        om, _ = self._om(status="filled")
        result = om.get_order_status("ord001")
        assert isinstance(result, OrderResult)
        assert result.status == "filled"

    def test_get_order_status_exception_returns_rejected(self):
        om, session = self._om()
        session.get_order_status.side_effect = RuntimeError("timeout")
        result = om.get_order_status("ord001")
        assert result.status == "rejected"

    def test_paper_trading_does_not_raise(self):
        om, _ = self._om()
        om.place_order(self._req())  # should warn but not raise


# ===========================================================================
# TestOpenStraddleOrders
# ===========================================================================

class TestOpenStraddleOrders:
    def test_returns_two_results(self):
        session = _mock_session()
        om = OrderManager(session, read_only=False, paper_trading=True)
        pos = _open_position()
        results = om.open_straddle_orders(pos, CFG)
        assert len(results) == 2

    def test_both_are_buy_orders(self):
        session = _mock_session()
        om = OrderManager(session, read_only=False)
        pos = _open_position()
        results = om.open_straddle_orders(pos, CFG)
        calls = session.place_order.call_args_list
        for c in calls:
            assert c.kwargs["action"] == "BUY"

    def test_read_only_raises(self):
        session = _mock_session()
        om = OrderManager(session, read_only=True)
        pos = _open_position()
        with pytest.raises(ReadOnlyModeError):
            om.open_straddle_orders(pos, CFG)

    def test_order_results_have_status(self):
        session = _mock_session(status="submitted")
        om = OrderManager(session, read_only=False)
        results = om.open_straddle_orders(_open_position(), CFG)
        assert all(r.status == "submitted" for r in results)


# ===========================================================================
# TestCloseStraddleOrders
# ===========================================================================

class TestCloseStraddleOrders:
    def test_returns_two_results(self):
        session = _mock_session()
        om = OrderManager(session, read_only=False)
        pos = _open_position()
        close_prices = {pos.call_leg.contract_key: 140.0,
                        pos.put_leg.contract_key: 135.0}
        results = om.close_straddle_orders(pos, close_prices, CFG)
        assert len(results) == 2

    def test_both_are_sell_orders(self):
        session = _mock_session()
        om = OrderManager(session, read_only=False)
        pos = _open_position()
        close_prices = {pos.call_leg.contract_key: 140.0,
                        pos.put_leg.contract_key: 135.0}
        om.close_straddle_orders(pos, close_prices, CFG)
        calls = session.place_order.call_args_list
        for c in calls:
            assert c.kwargs["action"] == "SELL"

    def test_read_only_raises(self):
        session = _mock_session()
        om = OrderManager(session, read_only=True)
        pos = _open_position()
        with pytest.raises(ReadOnlyModeError):
            om.close_straddle_orders(pos, {}, CFG)


# ===========================================================================
# TestStrategyYamlLoads
# ===========================================================================

class TestStrategyYamlLoads:
    def test_yaml_loads(self):
        import yaml
        from pathlib import Path
        path = Path("configs/strategy.yaml")
        if not path.exists():
            pytest.skip("configs/strategy.yaml not found")
        cfg = yaml.safe_load(path.read_text())
        assert cfg.get("strategy") == "atr_straddle"
        assert "straddle" in cfg
        assert cfg["straddle"]["roll_dte_days"] == 270
        assert cfg["straddle"]["multiplier"] == 10.0

    def test_yaml_version_present(self):
        import yaml
        from pathlib import Path
        path = Path("configs/strategy.yaml")
        if not path.exists():
            pytest.skip("configs/strategy.yaml not found")
        cfg = yaml.safe_load(path.read_text())
        assert "version" in cfg


# ===========================================================================
# TestAcceptanceCriterion
# ===========================================================================

class TestAcceptanceCriterion:
    """PLAN: Straddle opens/rolls without errors; positions reconcile."""

    def test_straddle_opens_without_error(self):
        chain = _chain(n_strikes=5)
        call_best = select_atm_strike(chain, CFG, "C")
        put_best = select_atm_strike(chain, CFG, "P")
        assert call_best is not None
        assert put_best is not None
        pos = open_straddle(call_best, put_best, SPOT, SIGMA, TRADE_DATE, CFG)
        assert pos.status == "open"
        assert pos.call_leg.option_type == "C"
        assert pos.put_leg.option_type == "P"

    def test_orders_placed_for_open(self):
        session = _mock_session(status="submitted")
        om = OrderManager(session, read_only=False, paper_trading=True)
        pos = _open_position()
        results = om.open_straddle_orders(pos, CFG)
        assert all(r.status == "submitted" for r in results)
        assert session.place_order.call_count == 2

    def test_positions_reconcile_after_open(self):
        pos = _open_position()
        broker = {
            pos.call_leg.contract_key: pos.call_leg.quantity,
            pos.put_leg.contract_key: pos.put_leg.quantity,
        }
        report = reconcile_positions(pos.legs, broker, TRADE_DATE)
        assert report.is_reconciled

    def test_roll_produces_new_open_position(self):
        old = _open_position(trade_date="2025-01-15", expiry="2025-06-20")
        # DTE ≈ 155 days < 270 → should roll
        assert should_roll(old, "2025-01-15", CFG)

        new_call = _call_row(key="NEW_CALL_2026", expiry=EXPIRY_12M)
        new_put = _put_row(key="NEW_PUT_2026", expiry=EXPIRY_12M)
        closed, new = roll_straddle(old, new_call, new_put, SPOT, SIGMA,
                                    "2025-01-15", CFG)
        assert closed.status == "closed"
        assert new.status == "open"

    def test_new_position_reconciles(self):
        old = _open_position(trade_date="2025-01-15", expiry="2025-06-20")
        new_call = _call_row(key="NEW_C")
        new_put = _put_row(key="NEW_P")
        _, new_pos = roll_straddle(old, new_call, new_put, SPOT, SIGMA,
                                   "2025-01-15", CFG)
        broker = {
            new_pos.call_leg.contract_key: new_pos.call_leg.quantity,
            new_pos.put_leg.contract_key: new_pos.put_leg.quantity,
        }
        report = reconcile_positions(new_pos.legs, broker, "2025-01-15")
        assert report.is_reconciled

    def test_missing_broker_position_not_reconciled(self):
        pos = _open_position()
        # Broker only reports the call, not the put
        broker = {pos.call_leg.contract_key: pos.call_leg.quantity}
        report = reconcile_positions(pos.legs, broker, TRADE_DATE)
        assert not report.is_reconciled
        assert pos.put_leg.contract_key in report.missing_in_broker
