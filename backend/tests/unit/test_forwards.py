"""Unit tests for forward curve and implied carry engine.

Acceptance criterion: Forward stable across small strike-set perturbations.
"""

from __future__ import annotations

import math
import uuid
from datetime import date
from typing import Optional

import pytest

from src.collectors.raw_collector import RawEvent
from src.forwards.engine import (
    EPSILON,
    _annotate_residuals,
    _build_candidates,
    _compute_confidence_score,
    _fallback_forward,
    _interpolate_missing,
    _liquidity_weight,
    _reject_outliers,
    _weighted_aggregate,
    compute_carry_diagnostics,
    estimate_forward,
    estimate_forward_curve,
)
from src.forwards.models import (
    CarryDiagnostics,
    ForwardCandidate,
    ForwardDiagnostics,
    ForwardResult,
)
from src.snapshots.builder import build_snapshot
from src.snapshots.models import MarketStateSnapshot, OptionRow, UnderlyingState

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SNAP_TS = 1_000.0
RATE = 0.05
UNDERLYING_KEY = "SPY|STK|SMART|USD"
SNAP_DATE = date(2026, 1, 2)

CONFIG = {
    "max_spread_pct": 0.30,
    "max_robust_zscore": 3.5,
}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_candidate(
    strike: float,
    forward_est: float,
    weight: float = 1.0,
    spread_call: float = 0.05,
    spread_put: float = 0.05,
    residual: Optional[float] = None,
) -> ForwardCandidate:
    return ForwardCandidate(
        strike=strike,
        maturity_years=1.0,
        call_mid=5.0,
        put_mid=5.0,
        forward_estimate=forward_est,
        weight=weight,
        spread_pct_call=spread_call,
        spread_pct_put=spread_put,
        parity_residual=residual,
    )


def _make_option_row(
    symbol: str,
    expiry: str,
    strike: float,
    right: str,
    bid: float,
    ask: float,
    maturity_years: float = 1.0,
    spread_pct: Optional[float] = None,
) -> OptionRow:
    mid = (bid + ask) / 2.0
    sp = spread_pct if spread_pct is not None else ((ask - bid) / mid if mid > 0 else None)
    return OptionRow(
        instrument_key=f"{symbol}|OPT|SMART|USD|{expiry}|{strike:g}|{right}|100",
        snapshot_ts=SNAP_TS,
        underlying_symbol=symbol,
        expiry_str=expiry,
        strike=strike,
        option_right=right,
        multiplier=100,
        bid=bid,
        ask=ask,
        last=None,
        mid=mid,
        volume=None,
        open_interest=None,
        spread_pct=sp,
        quote_age_seconds=5.0,
        is_stale=False,
        maturity_years=maturity_years,
    )


def _make_underlying_state(spot: float = 450.0) -> UnderlyingState:
    return UnderlyingState(
        instrument_key=UNDERLYING_KEY,
        snapshot_ts=SNAP_TS,
        bid=spot - 0.5,
        ask=spot + 0.5,
        last=spot,
        volume=5_000_000.0,
        reference_spot=spot,
        reference_type="mid",
        spread_pct=0.002,
        is_market_open=True,
        is_stale=False,
        quote_age_seconds=5.0,
    )


def _make_snapshot(
    spot: float = 450.0,
    strikes: list[float] | None = None,
    expiry_str: str = "2027-01-02",
    maturity_years: float = 1.0,
    call_premium: float = 5.0,    # call_mid = spot/K proxy; use fixed for clarity
    put_premium: float = 5.0,
    spread_pct: float = 0.02,
) -> MarketStateSnapshot:
    if strikes is None:
        strikes = [440.0, 445.0, 450.0, 455.0, 460.0]
    option_rows: list[OptionRow] = []
    for K in strikes:
        half_spread_call = call_premium * spread_pct / 2
        half_spread_put = put_premium * spread_pct / 2
        option_rows.append(_make_option_row(
            "SPY", expiry_str, K, "C",
            bid=call_premium - half_spread_call, ask=call_premium + half_spread_call,
            maturity_years=maturity_years, spread_pct=spread_pct,
        ))
        option_rows.append(_make_option_row(
            "SPY", expiry_str, K, "P",
            bid=put_premium - half_spread_put, ask=put_premium + half_spread_put,
            maturity_years=maturity_years, spread_pct=spread_pct,
        ))
    return MarketStateSnapshot(
        snapshot_ts=SNAP_TS,
        underlying_state=_make_underlying_state(spot),
        option_rows=option_rows,
    )


def _make_parity_snapshot(
    spot: float = 450.0,
    target_fwd: float = 450.0,
    strikes: list[float] | None = None,
    expiry_str: str = "2027-01-02",
    maturity_years: float = 1.0,
    rate: float = 0.0,
    spread_pct: float = 0.02,
) -> MarketStateSnapshot:
    """Create a snapshot where every call-put pair respects put-call parity at target_fwd.

    C(K) − P(K) = e^(−rT) * (F − K)  →  all strikes produce the same forward estimate.
    base_mid is a fixed option premium level to keep all mids positive.
    """
    if strikes is None:
        strikes = [440.0, 445.0, 450.0, 455.0, 460.0]
    disc = math.exp(rate * maturity_years)
    base_mid = 15.0  # enough headroom so both C and P stay positive
    option_rows: list[OptionRow] = []
    for K in strikes:
        parity_diff = (target_fwd - K) / disc   # C_mid − P_mid
        call_mid = base_mid + parity_diff / 2
        put_mid = base_mid - parity_diff / 2
        if call_mid <= 0 or put_mid <= 0:
            continue  # skip ITM strikes where premium would go negative
        half_c = call_mid * spread_pct / 2
        half_p = put_mid * spread_pct / 2
        option_rows.append(_make_option_row(
            "SPY", expiry_str, K, "C",
            bid=call_mid - half_c, ask=call_mid + half_c,
            maturity_years=maturity_years, spread_pct=spread_pct,
        ))
        option_rows.append(_make_option_row(
            "SPY", expiry_str, K, "P",
            bid=put_mid - half_p, ask=put_mid + half_p,
            maturity_years=maturity_years, spread_pct=spread_pct,
        ))
    return MarketStateSnapshot(
        snapshot_ts=SNAP_TS,
        underlying_state=_make_underlying_state(spot),
        option_rows=option_rows,
    )


def _make_forward_result(
    maturity_years: float = 1.0,
    expiry_str: str = "2027-01-02",
    forward: float = 450.0,
    confidence: float = 0.8,
    fallback: str = "none",
) -> ForwardResult:
    return ForwardResult(
        underlying=UNDERLYING_KEY,
        snapshot_ts=SNAP_TS,
        maturity_years=maturity_years,
        expiry_str=expiry_str,
        chosen_forward=forward,
        weighted_mean_forward=forward,
        median_forward=forward,
        confidence_score=confidence,
        candidates_before_filter=5,
        candidates_after_filter=5,
        fallback_used=fallback,
    )


# ---------------------------------------------------------------------------
# ForwardDiagnostics
# ---------------------------------------------------------------------------


class TestForwardDiagnostics:
    def test_instantiation(self) -> None:
        diag = ForwardDiagnostics(
            candidates_accepted=[_make_candidate(450.0, 450.0)],
            candidates_rejected=[],
            weighted_mean=450.0,
            median=450.0,
            confidence_score=0.9,
            forward_range=0.0,
        )
        assert diag.confidence_score == 0.9
        assert len(diag.candidates_accepted) == 1
        assert len(diag.candidates_rejected) == 0

    def test_forward_range_nonzero(self) -> None:
        diag = ForwardDiagnostics(
            candidates_accepted=[
                _make_candidate(440.0, 449.0),
                _make_candidate(460.0, 451.0),
            ],
            candidates_rejected=[],
            weighted_mean=450.0,
            median=450.0,
            confidence_score=0.8,
            forward_range=2.0,
        )
        assert diag.forward_range == 2.0


# ---------------------------------------------------------------------------
# _liquidity_weight
# ---------------------------------------------------------------------------


class TestLiquidityWeight:
    def test_tighter_spread_gives_higher_weight(self) -> None:
        assert _liquidity_weight(0.01, 0.01) > _liquidity_weight(0.20, 0.20)

    def test_zero_spread_uses_epsilon(self) -> None:
        w = _liquidity_weight(0.0, 0.0)
        assert w == pytest.approx(1.0 / EPSILON, rel=1e-3)

    def test_symmetric_call_put(self) -> None:
        assert _liquidity_weight(0.05, 0.05) == pytest.approx(_liquidity_weight(0.05, 0.05))

    def test_positive(self) -> None:
        assert _liquidity_weight(0.10, 0.15) > 0


# ---------------------------------------------------------------------------
# _reject_outliers
# ---------------------------------------------------------------------------


class TestRejectOutliers:
    def test_removes_extreme_outlier(self) -> None:
        candidates = [_make_candidate(k, 450.0 + (k - 450) * 0.1) for k in range(445, 456)]
        candidates.append(_make_candidate(999, 900.0))  # extreme outlier
        cleaned = _reject_outliers(candidates, CONFIG)
        assert not any(c.forward_estimate == 900.0 for c in cleaned)

    def test_single_candidate_unchanged(self) -> None:
        c = [_make_candidate(450.0, 450.0)]
        assert _reject_outliers(c, CONFIG) == c

    def test_identical_values_no_rejection(self) -> None:
        candidates = [_make_candidate(k, 450.0) for k in range(440, 460)]
        assert len(_reject_outliers(candidates, CONFIG)) == len(candidates)

    def test_tight_group_survives(self) -> None:
        candidates = [_make_candidate(k, 450.0 + k * 0.01) for k in range(10)]
        assert len(_reject_outliers(candidates, CONFIG)) == len(candidates)

    def test_custom_zscore_threshold(self) -> None:
        candidates = [_make_candidate(k, 450.0 + k) for k in range(5)]
        candidates.append(_make_candidate(99, 460.0))
        # With a tight threshold, more are rejected
        strict = {**CONFIG, "max_robust_zscore": 0.5}
        cleaned = _reject_outliers(candidates, strict)
        assert len(cleaned) <= len(candidates)


# ---------------------------------------------------------------------------
# _weighted_aggregate
# ---------------------------------------------------------------------------


class TestWeightedAggregate:
    def test_uniform_weights_equals_simple_mean(self) -> None:
        candidates = [
            _make_candidate(440.0, 448.0, weight=1.0),
            _make_candidate(450.0, 450.0, weight=1.0),
            _make_candidate(460.0, 452.0, weight=1.0),
        ]
        chosen, wmean, median = _weighted_aggregate(candidates)
        assert wmean == pytest.approx(450.0, abs=1e-6)
        assert median == pytest.approx(450.0, abs=1e-6)

    def test_higher_weight_pulls_mean(self) -> None:
        candidates = [
            _make_candidate(440.0, 448.0, weight=1.0),
            _make_candidate(460.0, 452.0, weight=9.0),
        ]
        _, wmean, _ = _weighted_aggregate(candidates)
        # Weighted: (448*1 + 452*9) / 10 = 451.6
        assert wmean == pytest.approx(451.6, abs=1e-4)

    def test_single_candidate(self) -> None:
        c = [_make_candidate(450.0, 451.0, weight=2.0)]
        chosen, wmean, median = _weighted_aggregate(c)
        assert chosen == pytest.approx(451.0)
        assert median == pytest.approx(451.0)


# ---------------------------------------------------------------------------
# _annotate_residuals
# ---------------------------------------------------------------------------


class TestAnnotateResiduals:
    def test_residuals_populated(self) -> None:
        candidates = [_make_candidate(450.0, 451.0), _make_candidate(460.0, 449.0)]
        annotated = _annotate_residuals(candidates, 450.0)
        assert annotated[0].parity_residual == pytest.approx(1.0)
        assert annotated[1].parity_residual == pytest.approx(-1.0)

    def test_returns_new_objects(self) -> None:
        c = _make_candidate(450.0, 450.0)
        annotated = _annotate_residuals([c], 450.0)
        assert annotated[0] is not c

    def test_chosen_forward_has_zero_residual(self) -> None:
        c = _make_candidate(450.0, 451.5)
        annotated = _annotate_residuals([c], 451.5)
        assert annotated[0].parity_residual == pytest.approx(0.0)

    def test_other_fields_preserved(self) -> None:
        c = _make_candidate(450.0, 451.0, weight=3.5)
        annotated = _annotate_residuals([c], 451.0)
        assert annotated[0].weight == pytest.approx(3.5)
        assert annotated[0].strike == 450.0


# ---------------------------------------------------------------------------
# _compute_confidence_score
# ---------------------------------------------------------------------------


class TestComputeConfidenceScore:
    def test_returns_zero_for_empty(self) -> None:
        assert _compute_confidence_score([], [_make_candidate(450.0, 450.0)]) == 0.0

    def test_returns_between_zero_and_one(self) -> None:
        accepted = [_make_candidate(k, 450.0 + k * 0.1) for k in range(5)]
        score = _compute_confidence_score(accepted, accepted)
        assert 0.0 <= score <= 1.0

    def test_more_candidates_increases_coverage_component(self) -> None:
        all_c = [_make_candidate(k, 450.0) for k in range(10)]
        # All accepted: coverage=1.0
        score_all = _compute_confidence_score(all_c, all_c)
        # Half accepted: coverage=0.5
        score_half = _compute_confidence_score(all_c[:5], all_c)
        assert score_all >= score_half

    def test_tighter_cluster_higher_score(self) -> None:
        # Tight cluster (narrow spread)
        tight = [_make_candidate(k, 450.0 + k * 0.001) for k in range(5)]
        score_tight = _compute_confidence_score(tight, tight)
        # Loose cluster (wide spread)
        loose = [_make_candidate(k, 450.0 + k * 10.0) for k in range(5)]
        score_loose = _compute_confidence_score(loose, loose)
        assert score_tight > score_loose


# ---------------------------------------------------------------------------
# estimate_forward — parity formula
# ---------------------------------------------------------------------------


class TestEstimateForward:
    def test_parity_formula_single_pair(self) -> None:
        """F = K + e^(rT) * (C - P) for a single strike."""
        K = 450.0
        T = 1.0
        C = 20.0
        P = 10.0
        # Place one call-put pair at K=450, C-P=10 → F ≈ K + disc*10
        snapshot = _make_snapshot(spot=450.0, strikes=[K], call_premium=C, put_premium=P)
        result = estimate_forward(snapshot, "2027-01-02", T, RATE, CONFIG)
        expected_fwd = K + math.exp(RATE * T) * (C - P)
        assert result.chosen_forward == pytest.approx(expected_fwd, rel=1e-4)

    def test_atm_zero_carry_forward_near_spot(self) -> None:
        """When r=0 and C=P (symmetric), F ≈ ATM strike ≈ spot."""
        spot = 450.0
        snapshot = _make_snapshot(spot=spot, strikes=[450.0], call_premium=10.0, put_premium=10.0)
        result = estimate_forward(snapshot, "2027-01-02", 1.0, rate=0.0, config=CONFIG)
        assert result.chosen_forward == pytest.approx(450.0, abs=0.01)

    def test_fallback_used_none_when_valid(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.fallback_used == "none"

    def test_candidates_before_filter_populated(self) -> None:
        snapshot = _make_snapshot(strikes=[440.0, 450.0, 460.0])
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.candidates_before_filter == 3

    def test_candidates_after_filter_lte_before(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.candidates_after_filter <= result.candidates_before_filter

    def test_residuals_populated_on_candidates(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert all(c.parity_residual is not None for c in result.candidates)

    def test_diagnostics_populated(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.diagnostics is not None
        assert isinstance(result.diagnostics, ForwardDiagnostics)
        assert result.diagnostics.confidence_score == pytest.approx(result.confidence_score)

    def test_confidence_between_0_and_1(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert 0.0 <= result.confidence_score <= 1.0

    def test_no_options_returns_fallback(self) -> None:
        snapshot = _make_snapshot(strikes=[])  # no options at all
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.fallback_used in ("unusable", "prior_snapshot")

    def test_no_matching_expiry_returns_fallback(self) -> None:
        snapshot = _make_snapshot(expiry_str="2027-01-02")
        result = estimate_forward(snapshot, "2028-01-02", 2.0, RATE, CONFIG)  # different expiry
        assert result.fallback_used in ("unusable", "prior_snapshot")

    def test_outlier_strike_rejected(self) -> None:
        """An extreme outlier call-put pair should be removed by MAD rejection."""
        # Build snapshot with 4 normal strikes + 1 extreme strike
        normal_strikes = [440.0, 445.0, 450.0, 455.0]
        snapshot = _make_snapshot(spot=450.0, strikes=normal_strikes)

        # Manually inject an extreme outlier row at strike=999
        from src.snapshots.models import OptionRow
        extreme_call = OptionRow(
            instrument_key="SPY|OPT|SMART|USD|20270102|999|C|100",
            snapshot_ts=SNAP_TS,
            underlying_symbol="SPY",
            expiry_str="2027-01-02",
            strike=999.0,
            option_right="C",
            multiplier=100,
            bid=5.0, ask=6.0, last=None, mid=5.5,
            volume=None, open_interest=None, spread_pct=0.18,
            quote_age_seconds=5.0, is_stale=False, maturity_years=1.0,
        )
        extreme_put = OptionRow(
            instrument_key="SPY|OPT|SMART|USD|20270102|999|P|100",
            snapshot_ts=SNAP_TS,
            underlying_symbol="SPY",
            expiry_str="2027-01-02",
            strike=999.0,
            option_right="P",
            multiplier=100,
            bid=450.0, ask=451.0, last=None, mid=450.5,
            volume=None, open_interest=None, spread_pct=0.002,
            quote_age_seconds=5.0, is_stale=False, maturity_years=1.0,
        )
        snapshot.option_rows.extend([extreme_call, extreme_put])

        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        # The extreme forward (≈999 + disc*(-445)) is very negative, should be rejected
        # Accepted candidates should not include strike=999
        accepted_strikes = {c.strike for c in result.candidates}
        assert 999.0 not in accepted_strikes

    def test_spread_too_wide_excluded(self) -> None:
        """Pairs with spread > max_spread_pct are filtered out."""
        snapshot = _make_snapshot(spread_pct=0.5)  # wider than max 0.30
        tight_config = {**CONFIG, "max_spread_pct": 0.10}
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, tight_config)
        # All pairs excluded → fallback
        assert result.fallback_used in ("unusable", "prior_snapshot")


# ---------------------------------------------------------------------------
# Fallback policy
# ---------------------------------------------------------------------------


class TestFallbackForward:
    def _snapshot(self) -> MarketStateSnapshot:
        return _make_snapshot(strikes=[])  # no options

    def test_prior_snapshot_used_when_configured(self) -> None:
        config = {**CONFIG, "prior_forward": 455.0}
        snapshot = self._snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, config)
        assert result.fallback_used == "prior_snapshot"
        assert result.chosen_forward == pytest.approx(455.0)

    def test_prior_snapshot_confidence_is_low(self) -> None:
        config = {**CONFIG, "prior_forward": 455.0}
        result = estimate_forward(self._snapshot(), "2027-01-02", 1.0, RATE, config)
        assert result.confidence_score == pytest.approx(0.2)

    def test_unusable_when_no_prior(self) -> None:
        result = estimate_forward(self._snapshot(), "2027-01-02", 1.0, RATE, CONFIG)
        assert result.fallback_used == "unusable"

    def test_unusable_confidence_is_zero(self) -> None:
        result = estimate_forward(self._snapshot(), "2027-01-02", 1.0, RATE, CONFIG)
        assert result.confidence_score == 0.0

    def test_unusable_uses_spot_as_proxy(self) -> None:
        snapshot = _make_snapshot(spot=448.0, strikes=[])
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.chosen_forward == pytest.approx(448.0)

    def test_zero_prior_not_used(self) -> None:
        """prior_forward=0 must not be treated as valid."""
        config = {**CONFIG, "prior_forward": 0.0}
        result = estimate_forward(self._snapshot(), "2027-01-02", 1.0, RATE, config)
        assert result.fallback_used == "unusable"

    def test_negative_prior_not_used(self) -> None:
        config = {**CONFIG, "prior_forward": -10.0}
        result = estimate_forward(self._snapshot(), "2027-01-02", 1.0, RATE, config)
        assert result.fallback_used == "unusable"


# ---------------------------------------------------------------------------
# compute_carry_diagnostics
# ---------------------------------------------------------------------------


class TestComputeCarryDiagnostics:
    def test_carry_identity(self) -> None:
        """q(T) = r − (1/T) * ln(F/S)."""
        fwd_result = _make_forward_result(maturity_years=1.0, forward=103.0)
        diag = compute_carry_diagnostics(fwd_result, spot=100.0, rate=0.05)
        expected = 0.05 - math.log(103.0 / 100.0) / 1.0
        assert diag.implied_carry == pytest.approx(expected, abs=1e-8)

    def test_negative_carry_for_high_dividend(self) -> None:
        """F < S implies q > r (high dividend, q positive by convention here)."""
        fwd_result = _make_forward_result(maturity_years=1.0, forward=95.0)
        diag = compute_carry_diagnostics(fwd_result, spot=100.0, rate=0.02)
        expected = 0.02 - math.log(95.0 / 100.0) / 1.0
        assert diag.implied_carry == pytest.approx(expected, abs=1e-8)
        assert diag.implied_carry > 0.02  # q > r when F < S

    def test_zero_carry_when_forward_equals_spot_times_exp_r(self) -> None:
        """When F = S * e^(rT), implied carry = 0."""
        S = 100.0
        r = 0.05
        T = 1.0
        F = S * math.exp(r * T)
        fwd_result = _make_forward_result(maturity_years=T, forward=F)
        diag = compute_carry_diagnostics(fwd_result, spot=S, rate=r)
        assert diag.implied_carry == pytest.approx(0.0, abs=1e-8)

    def test_half_year_maturity(self) -> None:
        T = 0.5
        S = 200.0
        r = 0.03
        F = 201.0
        fwd_result = _make_forward_result(maturity_years=T, forward=F)
        diag = compute_carry_diagnostics(fwd_result, spot=S, rate=r)
        expected = r - (1.0 / T) * math.log(F / S)
        assert diag.implied_carry == pytest.approx(expected, abs=1e-8)

    def test_zero_maturity_raises(self) -> None:
        fwd_result = _make_forward_result(maturity_years=0.0, forward=100.0)
        with pytest.raises(ValueError, match="maturity_years"):
            compute_carry_diagnostics(fwd_result, spot=100.0, rate=0.05)

    def test_diagnostics_fields(self) -> None:
        fwd_result = _make_forward_result(maturity_years=1.0, forward=103.0)
        diag = compute_carry_diagnostics(fwd_result, spot=100.0, rate=0.05)
        assert isinstance(diag, CarryDiagnostics)
        assert diag.underlying == UNDERLYING_KEY
        assert diag.snapshot_ts == SNAP_TS
        assert diag.spot == 100.0
        assert diag.forward == 103.0
        assert diag.rate == 0.05


# ---------------------------------------------------------------------------
# estimate_forward_curve
# ---------------------------------------------------------------------------


class TestEstimateForwardCurve:
    def _snapshot_with_two_expiries(self) -> MarketStateSnapshot:
        rows: list[OptionRow] = []
        for expiry, T in [("2027-01-02", 1.0), ("2027-07-02", 0.5)]:
            for K in [440.0, 450.0, 460.0]:
                rows.append(_make_option_row("SPY", expiry, K, "C",
                                             bid=4.9, ask=5.1, maturity_years=T))
                rows.append(_make_option_row("SPY", expiry, K, "P",
                                             bid=4.9, ask=5.1, maturity_years=T))
        return MarketStateSnapshot(
            snapshot_ts=SNAP_TS,
            underlying_state=_make_underlying_state(),
            option_rows=rows,
        )

    def test_returns_one_result_per_maturity(self) -> None:
        snapshot = self._snapshot_with_two_expiries()
        maturities = [("2027-01-02", 1.0), ("2027-07-02", 0.5)]
        results = estimate_forward_curve(snapshot, maturities, RATE, CONFIG)
        assert len(results) == 2

    def test_sorted_by_maturity(self) -> None:
        snapshot = self._snapshot_with_two_expiries()
        maturities = [("2027-01-02", 1.0), ("2027-07-02", 0.5)]
        results = estimate_forward_curve(snapshot, maturities, RATE, CONFIG)
        assert results[0].maturity_years < results[1].maturity_years

    def test_valid_maturities_have_fallback_none(self) -> None:
        snapshot = self._snapshot_with_two_expiries()
        maturities = [("2027-01-02", 1.0), ("2027-07-02", 0.5)]
        results = estimate_forward_curve(snapshot, maturities, RATE, CONFIG)
        for r in results:
            assert r.fallback_used == "none"

    def test_interpolates_unusable_maturity(self) -> None:
        """Middle maturity with no options gets interpolated from neighbors."""
        rows: list[OptionRow] = []
        for expiry, T in [("2026-07-02", 0.5), ("2027-07-02", 1.5)]:
            for K in [440.0, 450.0, 460.0]:
                rows.append(_make_option_row("SPY", expiry, K, "C",
                                             bid=4.9, ask=5.1, maturity_years=T))
                rows.append(_make_option_row("SPY", expiry, K, "P",
                                             bid=4.9, ask=5.1, maturity_years=T))
        snapshot = MarketStateSnapshot(
            snapshot_ts=SNAP_TS,
            underlying_state=_make_underlying_state(),
            option_rows=rows,
        )
        # Middle maturity has NO options
        maturities = [
            ("2026-07-02", 0.5),
            ("2027-01-02", 1.0),   # no quotes for this expiry → unusable → interpolated
            ("2027-07-02", 1.5),
        ]
        results = estimate_forward_curve(snapshot, maturities, RATE, CONFIG)
        middle = next(r for r in results if r.expiry_str == "2027-01-02")
        assert middle.fallback_used == "interpolated"

    def test_interpolated_forward_between_neighbors(self) -> None:
        """Interpolated forward is between its two neighbors."""
        rows = []
        for expiry, T, call_p, put_p in [
            ("2026-07-02", 0.5, 10.0, 5.0),
            ("2027-07-02", 1.5, 20.0, 5.0),
        ]:
            for K in [450.0]:
                rows.append(_make_option_row("SPY", expiry, K, "C",
                                             bid=call_p - 0.1, ask=call_p + 0.1, maturity_years=T))
                rows.append(_make_option_row("SPY", expiry, K, "P",
                                             bid=put_p - 0.1, ask=put_p + 0.1, maturity_years=T))
        snapshot = MarketStateSnapshot(
            snapshot_ts=SNAP_TS,
            underlying_state=_make_underlying_state(),
            option_rows=rows,
        )
        maturities = [("2026-07-02", 0.5), ("2027-01-02", 1.0), ("2027-07-02", 1.5)]
        results = estimate_forward_curve(snapshot, maturities, RATE, CONFIG)
        left = next(r for r in results if r.expiry_str == "2026-07-02")
        mid = next(r for r in results if r.expiry_str == "2027-01-02")
        right = next(r for r in results if r.expiry_str == "2027-07-02")
        assert min(left.chosen_forward, right.chosen_forward) <= mid.chosen_forward <= max(
            left.chosen_forward, right.chosen_forward
        )

    def test_all_unusable_stays_unusable(self) -> None:
        """If all maturities are unusable there's nothing to interpolate from."""
        snapshot = MarketStateSnapshot(
            snapshot_ts=SNAP_TS,
            underlying_state=_make_underlying_state(),
            option_rows=[],
        )
        maturities = [("2027-01-02", 1.0), ("2027-07-02", 1.5)]
        results = estimate_forward_curve(snapshot, maturities, RATE, CONFIG)
        assert all(r.fallback_used == "unusable" for r in results)

    def test_single_valid_neighbor_extrapolation(self) -> None:
        """Unusable at start/end gets the single neighbor's forward."""
        rows = []
        for K in [450.0]:
            rows.append(_make_option_row("SPY", "2027-01-02", K, "C",
                                         bid=4.9, ask=5.1, maturity_years=1.0))
            rows.append(_make_option_row("SPY", "2027-01-02", K, "P",
                                         bid=4.9, ask=5.1, maturity_years=1.0))
        snapshot = MarketStateSnapshot(
            snapshot_ts=SNAP_TS,
            underlying_state=_make_underlying_state(),
            option_rows=rows,
        )
        maturities = [
            ("2026-07-02", 0.5),   # no quotes → unusable → should extrapolate from right
            ("2027-01-02", 1.0),
        ]
        results = estimate_forward_curve(snapshot, maturities, RATE, CONFIG)
        left = next(r for r in results if r.expiry_str == "2026-07-02")
        right = next(r for r in results if r.expiry_str == "2027-01-02")
        assert left.fallback_used == "interpolated"
        assert left.chosen_forward == pytest.approx(right.chosen_forward)


# ---------------------------------------------------------------------------
# _interpolate_missing
# ---------------------------------------------------------------------------


class TestInterpolateMissing:
    def _unusable(self, T: float, expiry: str) -> ForwardResult:
        return _make_forward_result(T, expiry, forward=0.0, confidence=0.0, fallback="unusable")

    def _valid(self, T: float, expiry: str, fwd: float) -> ForwardResult:
        return _make_forward_result(T, expiry, forward=fwd, confidence=0.8, fallback="none")

    def test_no_unusable_unchanged(self) -> None:
        results = [self._valid(0.5, "2026-07-02", 450.0), self._valid(1.0, "2027-01-02", 455.0)]
        out = _interpolate_missing(results)
        assert [r.fallback_used for r in out] == ["none", "none"]

    def test_interpolation_with_two_neighbors(self) -> None:
        results = [
            self._valid(0.5, "2026-07-02", 450.0),
            self._unusable(1.0, "2027-01-02"),
            self._valid(1.5, "2027-07-02", 460.0),
        ]
        out = _interpolate_missing(results)
        mid = out[1]
        assert mid.fallback_used == "interpolated"
        # Linear interp: 0.5*(450+460)=455
        assert mid.chosen_forward == pytest.approx(455.0, abs=1e-6)

    def test_no_valid_neighbors_stays_unusable(self) -> None:
        results = [self._unusable(1.0, "2027-01-02")]
        out = _interpolate_missing(results)
        assert out[0].fallback_used == "unusable"

    def test_interpolated_confidence_half_of_min_neighbor(self) -> None:
        results = [
            self._valid(0.5, "2026-07-02", 450.0),
            self._unusable(1.0, "2027-01-02"),
            self._valid(1.5, "2027-07-02", 460.0),
        ]
        out = _interpolate_missing(results)
        # Both neighbors have confidence=0.8; interpolated = 0.5*0.8 = 0.4
        assert out[1].confidence_score == pytest.approx(0.4, abs=1e-6)


# ---------------------------------------------------------------------------
# Stability / acceptance criterion
# ---------------------------------------------------------------------------


class TestForwardStability:
    """Acceptance criterion: Forward stable across small strike-set perturbations.

    Uses _make_parity_snapshot so every call-put pair is consistent with the same
    underlying forward — this is the realistic scenario where stability is expected.
    """

    _TARGET_FWD = 452.0

    def test_forward_stable_when_one_strike_added(self) -> None:
        base_strikes = [440.0, 445.0, 450.0, 455.0, 460.0]
        extended_strikes = base_strikes + [462.0]
        snap_base = _make_parity_snapshot(
            spot=450.0, target_fwd=self._TARGET_FWD, strikes=base_strikes
        )
        snap_ext = _make_parity_snapshot(
            spot=450.0, target_fwd=self._TARGET_FWD, strikes=extended_strikes
        )
        r_base = estimate_forward(snap_base, "2027-01-02", 1.0, 0.0, CONFIG)
        r_ext = estimate_forward(snap_ext, "2027-01-02", 1.0, 0.0, CONFIG)
        # All pairs give the same forward estimate — adding one strike changes nothing
        assert abs(r_base.chosen_forward - r_ext.chosen_forward) < 0.01 * r_base.chosen_forward

    def test_forward_stable_when_one_strike_removed(self) -> None:
        full_strikes = [440.0, 445.0, 450.0, 455.0, 460.0]
        snap_full = _make_parity_snapshot(
            spot=450.0, target_fwd=self._TARGET_FWD, strikes=full_strikes
        )
        snap_reduced = _make_parity_snapshot(
            spot=450.0, target_fwd=self._TARGET_FWD, strikes=full_strikes[:-1]
        )
        r_full = estimate_forward(snap_full, "2027-01-02", 1.0, 0.0, CONFIG)
        r_reduced = estimate_forward(snap_reduced, "2027-01-02", 1.0, 0.0, CONFIG)
        assert abs(r_full.chosen_forward - r_reduced.chosen_forward) < 0.01 * r_full.chosen_forward

    def test_same_snapshot_produces_identical_result(self) -> None:
        snapshot = _make_parity_snapshot(
            spot=450.0, target_fwd=self._TARGET_FWD, strikes=[440.0, 450.0, 460.0]
        )
        r1 = estimate_forward(snapshot, "2027-01-02", 1.0, 0.0, CONFIG)
        r2 = estimate_forward(snapshot, "2027-01-02", 1.0, 0.0, CONFIG)
        assert r1.chosen_forward == r2.chosen_forward
        assert r1.confidence_score == r2.confidence_score
        assert r1.fallback_used == r2.fallback_used

    def test_parity_snapshot_gives_correct_forward(self) -> None:
        """Sanity: _make_parity_snapshot with all equal C-P gives back target_fwd."""
        snap = _make_parity_snapshot(
            spot=450.0, target_fwd=self._TARGET_FWD, strikes=[440.0, 450.0, 460.0]
        )
        result = estimate_forward(snap, "2027-01-02", 1.0, 0.0, CONFIG)
        assert result.chosen_forward == pytest.approx(self._TARGET_FWD, abs=0.01)

    def test_curve_stable_across_small_perturbations(self) -> None:
        snap1 = _make_parity_snapshot(
            spot=450.0, target_fwd=self._TARGET_FWD, strikes=[440.0, 450.0, 460.0]
        )
        snap2 = _make_parity_snapshot(
            spot=450.0, target_fwd=self._TARGET_FWD, strikes=[440.0, 450.0, 460.0, 462.0]
        )
        maturities = [("2027-01-02", 1.0)]
        c1 = estimate_forward_curve(snap1, maturities, 0.0, CONFIG)
        c2 = estimate_forward_curve(snap2, maturities, 0.0, CONFIG)
        assert abs(c1[0].chosen_forward - c2[0].chosen_forward) < 0.01 * c1[0].chosen_forward


# ---------------------------------------------------------------------------
# ForwardResult fields
# ---------------------------------------------------------------------------


class TestForwardResultFields:
    def test_underlying_key_stored(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.underlying == UNDERLYING_KEY

    def test_snapshot_ts_stored(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.snapshot_ts == SNAP_TS

    def test_expiry_str_stored(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.expiry_str == "2027-01-02"

    def test_maturity_years_stored(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 0.75, RATE, CONFIG)
        assert result.maturity_years == pytest.approx(0.75)

    def test_diagnostics_version_default(self) -> None:
        snapshot = _make_snapshot()
        result = estimate_forward(snapshot, "2027-01-02", 1.0, RATE, CONFIG)
        assert result.diagnostics_version == "1.0"
