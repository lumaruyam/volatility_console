"""Unit tests for Step 7: Quote normalization and QC.

Acceptance criterion: Same quote consistently accepted/rejected under fixed threshold version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from src.qc.checks import (
    QCCheckResult,
    check_bid_positive,
    check_crossed_market,
    check_intrinsic_value,
    check_open_interest,
    check_parity_residual,
    check_parity_residual_population,
    check_quote_age,
    check_spread_pct,
    robust_zscore,
)
from src.qc.quote_filter import (
    QuoteQCOutcome,
    filter_chain,
    run_quote_qc,
    store_rejected_outcomes,
)
from src.snapshots.models import OptionRow

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CONFIG = {
    "max_spread_pct": 0.25,
    "caution_spread_pct": 0.15,
    "max_quote_age_seconds": 60,
    "caution_quote_age_seconds": 30,
    "min_open_interest": 10,
    "intrinsic_tolerance": 0.01,
    "max_parity_residual_zscore": 3.5,
}


def _make_row(**overrides) -> OptionRow:
    defaults = dict(
        instrument_key="SPY|OPT|SMART|USD|20260717|450|C|100",
        snapshot_ts=1_000.0,
        underlying_symbol="SPY",
        expiry_str="2026-07-17",
        strike=450.0,
        option_right="C",
        multiplier=100.0,
        bid=5.0,
        ask=5.5,
        last=5.2,
        mid=5.25,
        volume=100.0,
        open_interest=500.0,
        spread_pct=0.095,
        quote_age_seconds=10.0,
        is_stale=False,
        maturity_years=0.5,
    )
    defaults.update(overrides)
    return OptionRow(**defaults)


# ---------------------------------------------------------------------------
# QCCheckResult model
# ---------------------------------------------------------------------------


class TestQCCheckResult:
    def test_pass_result(self) -> None:
        r = QCCheckResult("spread_pct", "pass", "OK", 0.05, 0.25)
        assert r.status == "pass"
        assert r.reason_code == "OK"

    def test_reject_result_has_nonempty_reason(self) -> None:
        r = QCCheckResult("spread_pct", "reject", "SPREAD_TOO_WIDE", 0.30, 0.25)
        assert r.reason_code != ""

    def test_context_defaults_to_empty_dict(self) -> None:
        r = QCCheckResult("bid_positive", "pass", "OK", 5.0, 0.0)
        assert r.context == {}


# ---------------------------------------------------------------------------
# check_spread_pct
# ---------------------------------------------------------------------------


class TestCheckSpreadPct:
    def test_tight_spread_passes(self) -> None:
        assert check_spread_pct(_make_row(spread_pct=0.05), CONFIG).status == "pass"

    def test_at_reject_boundary_rejects(self) -> None:
        assert check_spread_pct(_make_row(spread_pct=0.25), CONFIG).status == "reject"

    def test_just_below_reject_passes(self) -> None:
        r = check_spread_pct(_make_row(spread_pct=0.249), CONFIG)
        assert r.status in ("pass", "caution")

    def test_above_reject_threshold_rejects(self) -> None:
        r = check_spread_pct(_make_row(spread_pct=0.30), CONFIG)
        assert r.status == "reject"
        assert r.reason_code == "SPREAD_TOO_WIDE"

    def test_elevated_but_below_reject_is_caution(self) -> None:
        r = check_spread_pct(_make_row(spread_pct=0.18), CONFIG)
        assert r.status == "caution"
        assert r.reason_code == "SPREAD_ELEVATED"

    def test_none_spread_rejects(self) -> None:
        r = check_spread_pct(_make_row(spread_pct=None), CONFIG)
        assert r.status == "reject"
        assert r.reason_code == "SPREAD_UNAVAILABLE"

    def test_measured_value_populated(self) -> None:
        r = check_spread_pct(_make_row(spread_pct=0.10), CONFIG)
        assert r.measured_value == pytest.approx(0.10)

    def test_threshold_populated(self) -> None:
        r = check_spread_pct(_make_row(spread_pct=0.10), CONFIG)
        assert r.threshold is not None


# ---------------------------------------------------------------------------
# check_bid_positive
# ---------------------------------------------------------------------------


class TestCheckBidPositive:
    def test_positive_bid_passes(self) -> None:
        assert check_bid_positive(_make_row(bid=1.0), CONFIG).status == "pass"

    def test_zero_bid_rejects(self) -> None:
        r = check_bid_positive(_make_row(bid=0.0), CONFIG)
        assert r.status == "reject"
        assert r.reason_code == "BID_NOT_POSITIVE"

    def test_negative_bid_rejects(self) -> None:
        r = check_bid_positive(_make_row(bid=-0.01), CONFIG)
        assert r.status == "reject"

    def test_none_bid_rejects(self) -> None:
        r = check_bid_positive(_make_row(bid=None), CONFIG)
        assert r.status == "reject"
        assert r.reason_code == "BID_NOT_POSITIVE"

    def test_small_positive_bid_passes(self) -> None:
        assert check_bid_positive(_make_row(bid=0.01), CONFIG).status == "pass"


# ---------------------------------------------------------------------------
# check_quote_age
# ---------------------------------------------------------------------------


class TestCheckQuoteAge:
    def test_fresh_quote_passes(self) -> None:
        assert check_quote_age(_make_row(quote_age_seconds=5.0), CONFIG).status == "pass"

    def test_stale_quote_rejects(self) -> None:
        r = check_quote_age(_make_row(quote_age_seconds=120.0), CONFIG)
        assert r.status == "reject"
        assert r.reason_code == "QUOTE_STALE"

    def test_aging_quote_is_caution(self) -> None:
        r = check_quote_age(_make_row(quote_age_seconds=45.0), CONFIG)
        assert r.status == "caution"
        assert r.reason_code == "QUOTE_AGING"

    def test_none_age_is_caution(self) -> None:
        r = check_quote_age(_make_row(quote_age_seconds=None), CONFIG)
        assert r.status == "caution"
        assert r.reason_code == "AGE_UNKNOWN"

    def test_exactly_at_max_rejects(self) -> None:
        r = check_quote_age(_make_row(quote_age_seconds=60.0), CONFIG)
        assert r.status == "reject"

    def test_measured_value_populated(self) -> None:
        r = check_quote_age(_make_row(quote_age_seconds=25.0), CONFIG)
        assert r.measured_value == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# check_open_interest
# ---------------------------------------------------------------------------


class TestCheckOpenInterest:
    def test_sufficient_oi_passes(self) -> None:
        assert check_open_interest(_make_row(open_interest=500.0), CONFIG).status == "pass"

    def test_low_oi_is_caution_not_reject(self) -> None:
        r = check_open_interest(_make_row(open_interest=5.0), CONFIG)
        assert r.status == "caution"
        assert r.reason_code == "OI_LOW"

    def test_none_oi_is_caution(self) -> None:
        r = check_open_interest(_make_row(open_interest=None), CONFIG)
        assert r.status == "caution"
        assert r.reason_code == "OI_UNAVAILABLE"

    def test_zero_oi_is_caution(self) -> None:
        r = check_open_interest(_make_row(open_interest=0.0), CONFIG)
        assert r.status == "caution"

    def test_oi_at_minimum_passes(self) -> None:
        r = check_open_interest(_make_row(open_interest=10.0), CONFIG)
        assert r.status == "pass"


# ---------------------------------------------------------------------------
# check_crossed_market
# ---------------------------------------------------------------------------


class TestCheckCrossedMarket:
    def test_normal_market_passes(self) -> None:
        assert check_crossed_market(_make_row(bid=5.0, ask=5.5), CONFIG).status == "pass"

    def test_crossed_market_rejects(self) -> None:
        r = check_crossed_market(_make_row(bid=6.0, ask=5.0), CONFIG)
        assert r.status == "reject"
        assert r.reason_code == "MARKET_CROSSED"

    def test_equal_bid_ask_passes(self) -> None:
        assert check_crossed_market(_make_row(bid=5.0, ask=5.0), CONFIG).status == "pass"

    def test_none_bid_is_caution(self) -> None:
        r = check_crossed_market(_make_row(bid=None, ask=5.5), CONFIG)
        assert r.status == "caution"
        assert r.reason_code == "QUOTE_INCOMPLETE"

    def test_none_ask_is_caution(self) -> None:
        r = check_crossed_market(_make_row(bid=5.0, ask=None), CONFIG)
        assert r.status == "caution"

    def test_measured_value_is_spread(self) -> None:
        r = check_crossed_market(_make_row(bid=5.0, ask=5.5), CONFIG)
        assert r.measured_value == pytest.approx(0.5)

    def test_measured_value_positive_for_crossed(self) -> None:
        r = check_crossed_market(_make_row(bid=6.0, ask=5.0), CONFIG)
        assert r.measured_value == pytest.approx(1.0)  # bid - ask = 1


# ---------------------------------------------------------------------------
# check_intrinsic_value
# ---------------------------------------------------------------------------


class TestCheckIntrinsicValue:
    def test_above_intrinsic_passes(self) -> None:
        r = check_intrinsic_value(_make_row(mid=12.0), CONFIG, intrinsic=10.0)
        assert r.status == "pass"

    def test_below_intrinsic_rejects(self) -> None:
        r = check_intrinsic_value(_make_row(mid=5.0), CONFIG, intrinsic=10.0)
        assert r.status == "reject"
        assert r.reason_code == "BELOW_INTRINSIC"

    def test_exactly_at_intrinsic_passes(self) -> None:
        r = check_intrinsic_value(_make_row(mid=10.0), CONFIG, intrinsic=10.0)
        assert r.status == "pass"

    def test_within_tolerance_passes(self) -> None:
        r = check_intrinsic_value(_make_row(mid=9.995), CONFIG, intrinsic=10.0)
        assert r.status == "pass"

    def test_none_mid_rejects(self) -> None:
        r = check_intrinsic_value(_make_row(mid=None), CONFIG, intrinsic=10.0)
        assert r.status == "reject"
        assert r.reason_code == "MID_UNAVAILABLE"

    def test_context_includes_deficit(self) -> None:
        r = check_intrinsic_value(_make_row(mid=7.0), CONFIG, intrinsic=10.0)
        assert r.context.get("deficit") == pytest.approx(3.0)

    def test_zero_intrinsic_always_passes(self) -> None:
        r = check_intrinsic_value(_make_row(mid=0.01), CONFIG, intrinsic=0.0)
        assert r.status == "pass"


# ---------------------------------------------------------------------------
# check_parity_residual (single pre-computed z-score)
# ---------------------------------------------------------------------------


class TestCheckParityResidual:
    def test_small_zscore_passes(self) -> None:
        r = check_parity_residual(0.5, CONFIG)
        assert r.status == "pass"

    def test_large_zscore_rejects(self) -> None:
        r = check_parity_residual(4.0, CONFIG)
        assert r.status == "reject"
        assert r.reason_code == "PARITY_OUTLIER"

    def test_at_threshold_rejects(self) -> None:
        r = check_parity_residual(3.5, CONFIG)
        assert r.status == "reject"

    def test_just_below_threshold_passes(self) -> None:
        r = check_parity_residual(3.49, CONFIG)
        assert r.status == "pass"

    def test_negative_large_zscore_rejects(self) -> None:
        r = check_parity_residual(-4.0, CONFIG)
        assert r.status == "reject"

    def test_zero_zscore_passes(self) -> None:
        assert check_parity_residual(0.0, CONFIG).status == "pass"

    def test_custom_threshold(self) -> None:
        strict = {**CONFIG, "max_parity_residual_zscore": 1.0}
        r = check_parity_residual(1.5, strict)
        assert r.status == "reject"

    def test_context_passed_through(self) -> None:
        ctx = {"instrument_key": "SPY|OPT|SMART|USD|20270102|450|C|100"}
        r = check_parity_residual(0.5, CONFIG, context=ctx)
        assert r.context["instrument_key"] == ctx["instrument_key"]


# ---------------------------------------------------------------------------
# robust_zscore
# ---------------------------------------------------------------------------


class TestRobustZscore:
    def test_outlier_has_high_zscore(self) -> None:
        # Spread ensures MAD > 0; last value is extreme outlier
        values = [98.0, 99.0, 100.0, 101.0, 102.0, 99.5, 100.5, 101.5, 98.5, 500.0]
        zs = robust_zscore(values)
        assert abs(zs[-1]) > 3.5

    def test_normal_values_have_low_zscore(self) -> None:
        values = [99.0, 100.0, 100.5, 101.0, 99.5]
        zs = robust_zscore(values)
        assert all(abs(z) < 3.0 for z in zs)

    def test_identical_values_give_zero(self) -> None:
        values = [100.0] * 5
        zs = robust_zscore(values)
        assert all(z == 0.0 for z in zs)

    def test_single_value_gives_zero(self) -> None:
        assert robust_zscore([42.0]) == [0.0]

    def test_empty_returns_empty(self) -> None:
        assert robust_zscore([]) == []

    def test_length_preserved(self) -> None:
        values = [1.0, 2.0, 3.0, 100.0]
        assert len(robust_zscore(values)) == len(values)

    def test_symmetric_about_median(self) -> None:
        values = [90.0, 95.0, 100.0, 105.0, 110.0]
        zs = robust_zscore(values)
        assert zs[2] == pytest.approx(0.0, abs=1e-8)  # median = 100

    def test_negative_deviations_have_negative_z(self) -> None:
        values = [90.0, 95.0, 100.0, 105.0, 110.0]
        zs = robust_zscore(values)
        assert zs[0] < 0  # 90 < median
        assert zs[-1] > 0  # 110 > median


# ---------------------------------------------------------------------------
# check_parity_residual_population
# ---------------------------------------------------------------------------


class TestCheckParityResidualPopulation:
    def test_outlier_rejected(self) -> None:
        residuals = [0.1, -0.1, 0.05, -0.05, 100.0]  # last is outlier
        keys = [f"key_{i}" for i in range(5)]
        results = check_parity_residual_population(residuals, keys, CONFIG)
        assert len(results) == 5
        assert results[-1].status == "reject"
        assert results[-1].reason_code == "PARITY_OUTLIER"

    def test_tight_population_all_pass(self) -> None:
        residuals = [0.1, -0.1, 0.05, -0.05, 0.02]
        results = check_parity_residual_population(residuals, None, CONFIG)
        assert all(r.status == "pass" for r in results)

    def test_single_residual_passes(self) -> None:
        results = check_parity_residual_population([5.0], ["key_0"], CONFIG)
        assert len(results) == 1
        assert results[0].status == "pass"  # z=0 for single element

    def test_empty_returns_empty(self) -> None:
        assert check_parity_residual_population([], [], CONFIG) == []

    def test_length_matches_input(self) -> None:
        residuals = [0.1, 0.2, 0.3, 10.0]
        results = check_parity_residual_population(residuals, None, CONFIG)
        assert len(results) == len(residuals)

    def test_context_includes_raw_residual(self) -> None:
        residuals = [0.5, -0.5]
        keys = ["key_0", "key_1"]
        results = check_parity_residual_population(residuals, keys, CONFIG)
        assert results[0].context["raw_residual"] == pytest.approx(0.5)
        assert results[0].context["instrument_key"] == "key_0"


# ---------------------------------------------------------------------------
# run_quote_qc
# ---------------------------------------------------------------------------


class TestRunQuoteQC:
    def test_clean_row_is_usable(self) -> None:
        outcome = run_quote_qc(_make_row(), CONFIG)
        assert outcome.overall_status == "usable"
        assert outcome.is_usable

    def test_zero_bid_rejects(self) -> None:
        outcome = run_quote_qc(_make_row(bid=0.0), CONFIG)
        assert outcome.overall_status == "reject"
        assert "BID_NOT_POSITIVE" in outcome.rejection_reasons

    def test_crossed_market_rejects(self) -> None:
        outcome = run_quote_qc(_make_row(bid=6.0, ask=5.0), CONFIG)
        assert outcome.overall_status == "reject"

    def test_low_oi_gives_caution(self) -> None:
        outcome = run_quote_qc(_make_row(open_interest=5.0), CONFIG)
        assert outcome.overall_status == "caution"
        assert "OI_LOW" in outcome.caution_reasons

    def test_intrinsic_check_triggered_when_provided(self) -> None:
        outcome = run_quote_qc(_make_row(mid=5.0), CONFIG, intrinsic_value=10.0)
        assert outcome.overall_status == "reject"
        assert "BELOW_INTRINSIC" in outcome.rejection_reasons

    def test_intrinsic_check_skipped_when_not_provided(self) -> None:
        outcome = run_quote_qc(_make_row(mid=5.0), CONFIG)
        check_names = [c.check_name for c in outcome.checks]
        assert "intrinsic_value" not in check_names

    def test_parity_check_triggered_when_provided(self) -> None:
        outcome = run_quote_qc(_make_row(), CONFIG, parity_zscore=4.0)
        assert outcome.overall_status == "reject"
        assert "PARITY_OUTLIER" in outcome.rejection_reasons

    def test_parity_check_skipped_when_not_provided(self) -> None:
        outcome = run_quote_qc(_make_row(), CONFIG)
        check_names = [c.check_name for c in outcome.checks]
        assert "parity_residual" not in check_names

    def test_instrument_key_stored(self) -> None:
        row = _make_row(instrument_key="SPY|OPT|SMART|USD|20270102|450|C|100")
        outcome = run_quote_qc(row, CONFIG)
        assert outcome.instrument_key == row.instrument_key

    def test_snapshot_ts_stored(self) -> None:
        row = _make_row(snapshot_ts=9_999.0)
        outcome = run_quote_qc(row, CONFIG)
        assert outcome.snapshot_ts == 9_999.0

    def test_five_base_checks_always_run(self) -> None:
        outcome = run_quote_qc(_make_row(), CONFIG)
        assert len(outcome.checks) == 5

    def test_reject_overrides_caution(self) -> None:
        # OI low (caution) + zero bid (reject)
        outcome = run_quote_qc(_make_row(bid=0.0, open_interest=5.0), CONFIG)
        assert outcome.overall_status == "reject"

    def test_multiple_rejects_all_recorded(self) -> None:
        outcome = run_quote_qc(
            _make_row(bid=0.0, spread_pct=0.50, quote_age_seconds=200.0), CONFIG
        )
        assert len(outcome.rejection_reasons) >= 2


# ---------------------------------------------------------------------------
# filter_chain
# ---------------------------------------------------------------------------


class TestFilterChain:
    def test_all_good_quotes_accepted(self) -> None:
        rows = [_make_row() for _ in range(5)]
        accepted, outcomes = filter_chain(rows, CONFIG)
        assert len(accepted) == 5
        assert all(o.overall_status == "usable" for o in outcomes)

    def test_bad_quote_not_in_accepted(self) -> None:
        good = _make_row(instrument_key="SPY|OPT|SMART|USD|20270102|450|C|100")
        bad = _make_row(instrument_key="SPY|OPT|SMART|USD|20270102|455|C|100", bid=0.0)
        accepted, outcomes = filter_chain([good, bad], CONFIG)
        assert len(accepted) == 1
        assert accepted[0].instrument_key == good.instrument_key

    def test_all_outcomes_returned_for_audit(self) -> None:
        rows = [_make_row(), _make_row(bid=0.0)]
        _, outcomes = filter_chain(rows, CONFIG)
        assert len(outcomes) == 2

    def test_intrinsics_applied_per_key(self) -> None:
        key = "SPY|OPT|SMART|USD|20270102|450|C|100"
        row = _make_row(instrument_key=key, mid=5.0)
        intrinsics = {key: 10.0}
        accepted, outcomes = filter_chain([row], CONFIG, intrinsics=intrinsics)
        assert len(accepted) == 0
        assert outcomes[0].overall_status == "reject"

    def test_parity_residuals_population_z_scored(self) -> None:
        """One outlier residual in a population should be rejected; others pass."""
        keys = [f"SPY|OPT|SMART|USD|20270102|{450+i*5}|C|100" for i in range(5)]
        rows = [_make_row(instrument_key=k) for k in keys]
        # Last residual is extreme outlier
        parity_residuals = {k: 0.1 * (i + 1) for i, k in enumerate(keys[:-1])}
        parity_residuals[keys[-1]] = 100.0  # outlier
        accepted, outcomes = filter_chain(rows, CONFIG, parity_residuals=parity_residuals)
        rejected = [o for o in outcomes if o.overall_status == "reject"]
        assert any(keys[-1] == r.instrument_key for r in rejected)

    def test_empty_chain_returns_empty(self) -> None:
        accepted, outcomes = filter_chain([], CONFIG)
        assert accepted == []
        assert outcomes == []

    def test_caution_not_excluded_from_accepted(self) -> None:
        row = _make_row(open_interest=5.0)  # OI low → caution only
        accepted, outcomes = filter_chain([row], CONFIG)
        assert len(accepted) == 1
        assert outcomes[0].overall_status == "caution"

    def test_returns_full_audit_including_rejected(self) -> None:
        rows = [_make_row(bid=0.0) for _ in range(3)]
        accepted, outcomes = filter_chain(rows, CONFIG)
        assert len(accepted) == 0
        assert len(outcomes) == 3


# ---------------------------------------------------------------------------
# store_rejected_outcomes
# ---------------------------------------------------------------------------


class TestStoreRejectedOutcomes:
    def _make_outcome(self, key: str, status: str = "reject") -> QuoteQCOutcome:
        checks = [QCCheckResult("bid_positive", status, "BID_NOT_POSITIVE" if status != "pass" else "OK", 0.0, 0.0)]
        return QuoteQCOutcome(key, 1_000.0, status, checks)

    def test_creates_file_for_rejected(self, tmp_path: Path) -> None:
        outcomes = [self._make_outcome("key_1", "reject")]
        path = store_rejected_outcomes(outcomes, tmp_path, "2026-06-07", 1000.0)
        assert path.exists()

    def test_file_contains_jsonl_lines(self, tmp_path: Path) -> None:
        outcomes = [
            self._make_outcome("key_1", "reject"),
            self._make_outcome("key_2", "caution"),
        ]
        path = store_rejected_outcomes(outcomes, tmp_path, "2026-06-07", 1000.0)
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2

    def test_each_line_is_valid_json(self, tmp_path: Path) -> None:
        outcomes = [self._make_outcome("key_1", "reject")]
        path = store_rejected_outcomes(outcomes, tmp_path, "2026-06-07", 1000.0)
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            assert "instrument_key" in rec
            assert "overall_status" in rec
            assert "rejection_reasons" in rec

    def test_usable_outcomes_not_stored(self, tmp_path: Path) -> None:
        outcomes = [self._make_outcome("key_1", "usable")]
        path = store_rejected_outcomes(outcomes, tmp_path, "2026-06-07", 1000.0)
        assert not path.exists()

    def test_path_includes_trade_date(self, tmp_path: Path) -> None:
        outcomes = [self._make_outcome("key_1", "reject")]
        path = store_rejected_outcomes(outcomes, tmp_path, "2026-06-07", 1000.0)
        assert "dt=2026-06-07" in str(path)

    def test_path_includes_snapshot_ts(self, tmp_path: Path) -> None:
        outcomes = [self._make_outcome("key_1", "reject")]
        path = store_rejected_outcomes(outcomes, tmp_path, "2026-06-07", 1000.0)
        assert "snapshot_ts=1000" in str(path)

    def test_caution_also_stored(self, tmp_path: Path) -> None:
        outcomes = [self._make_outcome("key_1", "caution")]
        path = store_rejected_outcomes(outcomes, tmp_path, "2026-06-07", 1000.0)
        assert path.exists()
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1

    def test_stored_record_has_checks_detail(self, tmp_path: Path) -> None:
        outcomes = [self._make_outcome("key_1", "reject")]
        path = store_rejected_outcomes(outcomes, tmp_path, "2026-06-07", 1000.0)
        rec = json.loads(path.read_text().splitlines()[0])
        assert "checks" in rec
        assert len(rec["checks"]) >= 1
        assert "check_name" in rec["checks"][0]


# ---------------------------------------------------------------------------
# Acceptance criterion: determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Same quote consistently accepted/rejected under fixed threshold version."""

    def test_same_row_same_outcome(self) -> None:
        row = _make_row()
        o1 = run_quote_qc(row, CONFIG)
        o2 = run_quote_qc(row, CONFIG)
        assert o1.overall_status == o2.overall_status
        assert o1.rejection_reasons == o2.rejection_reasons

    def test_same_rejected_row_same_reasons(self) -> None:
        row = _make_row(bid=0.0, spread_pct=0.50)
        o1 = run_quote_qc(row, CONFIG)
        o2 = run_quote_qc(row, CONFIG)
        assert sorted(o1.rejection_reasons) == sorted(o2.rejection_reasons)

    def test_same_chain_same_accepted_set(self) -> None:
        rows = [
            _make_row(instrument_key=f"SPY|OPT|SMART|USD|20270102|{450+i*5}|C|100")
            for i in range(10)
        ]
        rows[3] = _make_row(
            instrument_key="SPY|OPT|SMART|USD|20270102|465|C|100", bid=0.0
        )
        accepted1, _ = filter_chain(rows, CONFIG)
        accepted2, _ = filter_chain(rows, CONFIG)
        keys1 = sorted(r.instrument_key for r in accepted1)
        keys2 = sorted(r.instrument_key for r in accepted2)
        assert keys1 == keys2

    def test_threshold_change_changes_outcome(self) -> None:
        row = _make_row(spread_pct=0.20)
        tight = {**CONFIG, "max_spread_pct": 0.15}
        loose = {**CONFIG, "max_spread_pct": 0.25}
        assert run_quote_qc(row, tight).overall_status == "reject"
        assert run_quote_qc(row, loose).overall_status in ("pass", "usable", "caution")

    def test_parity_outlier_deterministic(self) -> None:
        residuals = [0.1, -0.1, 0.05, -0.05, 50.0]
        keys = [f"key_{i}" for i in range(5)]
        r1 = check_parity_residual_population(residuals, keys, CONFIG)
        r2 = check_parity_residual_population(residuals, keys, CONFIG)
        statuses1 = [r.status for r in r1]
        statuses2 = [r.status for r in r2]
        assert statuses1 == statuses2
