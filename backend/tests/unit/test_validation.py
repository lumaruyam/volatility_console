"""
Comprehensive tests for Step 14: Validation framework and anomaly detection.

Acceptance criterion (PLAN):
  Failing underlyings identifiable within minutes from QC report.
"""

from __future__ import annotations

import math
import statistics

import pytest

from src.qc.validation import (
    DailyQCReport,
    ValidationCheckResult,
    build_triage_table,
    check_calendar_sanity,
    check_collector_continuity,
    check_forward_stability,
    check_greek_sanity,
    check_iv_solver_convergence,
    check_option_chain_coverage,
    check_scenario_completeness,
    check_surface_fit_error,
    check_underlying_quote_health,
    robust_zscore,
    run_daily_qc,
)
from src.qc.anomaly import (
    AnomalyResult,
    anomaly_summary,
    detect_anomaly,
    filter_anomalies,
    run_anomaly_detection,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

CFG = {
    "version": "1.0",
    "max_collector_gap_seconds": 30,
    "session_window_seconds": 27000,
    "max_spread_pct": 0.25,
    "max_stale_ratio": 0.10,
    "min_calls_per_maturity": 3,
    "min_puts_per_maturity": 3,
    "max_forward_deviation_pct": 0.005,
    "min_iv_convergence_ratio": 0.95,
    "max_rmse": 0.02,
    "calendar_sanity_tolerance": 1e-6,
    "greek_sanity_tolerance": 0.01,
    "anomaly_zscore_threshold": 3.0,
    "min_baseline_length": 3,
}

DATE = "2025-01-15"
UND = "ESTX50"


def _vcheck(status="pass", severity="info", reason="", val=None, thresh=None):
    return ValidationCheckResult(
        check_name="dummy", target_key=UND,
        status=status, severity=severity,
        measured_value=val, threshold=thresh,
        threshold_version="1.0",
        reason_code=reason,
    )


def _iv_rows(n_total=100, n_converged=98, expiry="2025-03-21", opt_type="C"):
    rows = []
    for i in range(n_total):
        rows.append({
            "converged": i < n_converged,
            "expiry_str": expiry,
            "option_type": "C" if i % 2 == 0 else "P",
            "qc_status": "usable",
            "implied_vol": 0.20 + i * 0.001,
        })
    return rows


def _surface_rows(maturities=None, rmse=0.005, expiry="2025-03-21"):
    mats = maturities or [0.25, 0.50, 1.0]
    return [
        {"expiry_str": expiry, "maturity_years": T,
         "atm_total_variance": 0.04 * T, "rmse": rmse}
        for T in mats
    ]


def _pricing_rows(n=10, delta_diff=0.001):
    return [
        {"analytic_delta": 0.50, "fd_delta": 0.50 + delta_diff}
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# TestRobustZscore
# ---------------------------------------------------------------------------

class TestRobustZscore:
    def test_single_value(self):
        assert robust_zscore([5.0]) == [0.0]

    def test_empty(self):
        assert robust_zscore([]) == []

    def test_two_values_symmetric(self):
        z = robust_zscore([0.0, 10.0])
        assert z[0] == pytest.approx(-z[1], abs=1e-10)

    def test_all_identical(self):
        assert robust_zscore([3.0, 3.0, 3.0, 3.0]) == [0.0, 0.0, 0.0, 0.0]

    def test_outlier_high_zscore(self):
        # Varied baseline so MAD > 0; last value is a clear outlier
        values = [1.0, 1.1, 0.9, 1.05, 0.95, 1.02, 0.98, 1.08, 0.92, 100.0]
        z = robust_zscore(values)
        assert abs(z[-1]) > 5.0

    def test_median_zero_zscore(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        z = robust_zscore(values)
        median_idx = 2  # value=3.0
        assert z[median_idx] == pytest.approx(0.0, abs=1e-10)

    def test_formula_matches(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        med = statistics.median(values)
        mad = statistics.median([abs(v - med) for v in values])
        expected = [(v - med) / (1.4826 * mad) for v in values]
        result = robust_zscore(values)
        for r, e in zip(result, expected):
            assert r == pytest.approx(e, rel=1e-8)

    def test_length_preserved(self):
        values = [1.0, 2.0, 5.0, 10.0, 100.0]
        assert len(robust_zscore(values)) == len(values)


# ---------------------------------------------------------------------------
# TestDailyQCReport
# ---------------------------------------------------------------------------

class TestDailyQCReport:
    def _report(self, statuses):
        checks = [_vcheck(status=s) for s in statuses]
        return DailyQCReport(run_id="r1", trade_date=DATE, underlying=UND, checks=checks)

    def test_pass_count(self):
        r = self._report(["pass", "pass", "warn"])
        assert r.pass_count == 2

    def test_warn_count(self):
        r = self._report(["pass", "warn", "warn", "fail"])
        assert r.warn_count == 2

    def test_fail_count(self):
        r = self._report(["fail", "pass", "fail"])
        assert r.fail_count == 2

    def test_overall_pass(self):
        assert self._report(["pass", "pass"]).overall_status == "pass"

    def test_overall_warn(self):
        assert self._report(["pass", "warn"]).overall_status == "warn"

    def test_overall_fail(self):
        assert self._report(["pass", "warn", "fail"]).overall_status == "fail"

    def test_fail_overrides_warn(self):
        assert self._report(["warn", "warn", "fail"]).overall_status == "fail"

    def test_failures_helper(self):
        r = self._report(["pass", "fail", "warn", "fail"])
        assert len(r.failures()) == 2
        assert all(c.status == "fail" for c in r.failures())

    def test_warnings_helper(self):
        r = self._report(["pass", "warn", "warn"])
        assert len(r.warnings()) == 2

    def test_empty_checks(self):
        r = self._report([])
        assert r.overall_status == "pass"
        assert r.pass_count == 0


# ---------------------------------------------------------------------------
# TestValidationCheckResult
# ---------------------------------------------------------------------------

class TestValidationCheckResult:
    def test_fields(self):
        c = _vcheck("fail", "critical", "SPREAD_TOO_WIDE", 0.30, 0.25)
        assert c.status == "fail"
        assert c.severity == "critical"
        assert c.reason_code == "SPREAD_TOO_WIDE"
        assert c.measured_value == pytest.approx(0.30)

    def test_reason_code_empty_on_pass(self):
        c = _vcheck("pass")
        assert c.reason_code == ""


# ---------------------------------------------------------------------------
# TestCollectorContinuity
# ---------------------------------------------------------------------------

class TestCollectorContinuity:
    def _events(self, timestamps):
        return [{"timestamp": t} for t in timestamps]

    def test_no_events_fail(self):
        r = check_collector_continuity([], 27000, CFG, DATE)
        assert r.status == "fail"
        assert r.reason_code == "NO_EVENTS"

    def test_single_event_warn(self):
        r = check_collector_continuity(self._events([100.0]), 27000, CFG, DATE)
        assert r.status == "warn"
        assert r.reason_code == "INSUFFICIENT_EVENTS"

    def test_small_gaps_pass(self):
        ts = [float(i * 10) for i in range(100)]  # 10s gaps < 30s threshold
        r = check_collector_continuity(self._events(ts), 27000, CFG, DATE)
        assert r.status == "pass"

    def test_large_gap_fail(self):
        ts = [0.0, 10.0, 20.0, 80.0, 90.0]  # 60s gap
        r = check_collector_continuity(self._events(ts), 27000, CFG, DATE)
        assert r.status == "fail"
        assert r.reason_code == "GAP_TOO_LARGE"

    def test_measured_value_is_max_gap(self):
        ts = [0.0, 5.0, 10.0, 45.0]  # max gap = 35s
        r = check_collector_continuity(self._events(ts), 27000, CFG, DATE)
        assert r.measured_value == pytest.approx(35.0)

    def test_threshold_from_config(self):
        r = check_collector_continuity(self._events([0.0, 5.0]), 27000, CFG, DATE)
        assert r.threshold == pytest.approx(30.0)

    def test_exactly_at_threshold_passes(self):
        ts = [0.0, 30.0]
        r = check_collector_continuity(self._events(ts), 27000, CFG, DATE)
        assert r.status == "pass"

    def test_check_name(self):
        r = check_collector_continuity([], 27000, CFG, DATE)
        assert r.check_name == "collector_continuity"


# ---------------------------------------------------------------------------
# TestUnderlyingQuoteHealth
# ---------------------------------------------------------------------------

class TestUnderlyingQuoteHealth:
    def _rows(self, spread_pcts, stale_flags=None):
        rows = []
        for i, sp in enumerate(spread_pcts):
            rows.append({
                "spread_pct": sp,
                "is_stale": (stale_flags or [False] * len(spread_pcts))[i],
            })
        return rows

    def test_no_snapshots_fail(self):
        r = check_underlying_quote_health([], CFG, UND)
        assert r.status == "fail"
        assert r.reason_code == "NO_SNAPSHOTS"

    def test_good_spreads_pass(self):
        r = check_underlying_quote_health(self._rows([0.05, 0.08, 0.06]), CFG, UND)
        assert r.status == "pass"

    def test_high_spread_fail(self):
        r = check_underlying_quote_health(self._rows([0.30, 0.35]), CFG, UND)
        assert r.status == "fail"
        assert r.reason_code == "SPREAD_TOO_WIDE"

    def test_high_stale_ratio_warn(self):
        rows = self._rows([0.05] * 10, stale_flags=[True] * 2 + [False] * 8)
        r = check_underlying_quote_health(rows, CFG, UND)
        assert r.status == "warn"
        assert r.reason_code == "HIGH_STALE_RATIO"

    def test_spread_checked_before_stale(self):
        rows = self._rows([0.50] * 5, stale_flags=[True] * 5)
        r = check_underlying_quote_health(rows, CFG, UND)
        assert r.reason_code == "SPREAD_TOO_WIDE"

    def test_check_name(self):
        r = check_underlying_quote_health([], CFG, UND)
        assert r.check_name == "underlying_quote_health"


# ---------------------------------------------------------------------------
# TestOptionChainCoverage
# ---------------------------------------------------------------------------

class TestOptionChainCoverage:
    def _iv_rows_for(self, n_calls, n_puts, expiry="2025-03-21", status="usable"):
        rows = [{"expiry_str": expiry, "option_type": "C", "qc_status": status}
                for _ in range(n_calls)]
        rows += [{"expiry_str": expiry, "option_type": "P", "qc_status": status}
                 for _ in range(n_puts)]
        return rows

    def test_sufficient_coverage_pass(self):
        rows = self._iv_rows_for(5, 5)
        r = check_option_chain_coverage(rows, [], CFG, UND, "2025-03-21")
        assert r.status == "pass"

    def test_insufficient_calls_fail(self):
        rows = self._iv_rows_for(1, 5)
        r = check_option_chain_coverage(rows, [], CFG, UND, "2025-03-21")
        assert r.status == "fail"
        assert r.reason_code == "INSUFFICIENT_CHAIN"

    def test_insufficient_puts_fail(self):
        rows = self._iv_rows_for(5, 1)
        r = check_option_chain_coverage(rows, [], CFG, UND, "2025-03-21")
        assert r.status == "fail"

    def test_wrong_expiry_excluded(self):
        rows = self._iv_rows_for(10, 10, expiry="2025-06-20")
        r = check_option_chain_coverage(rows, [], CFG, UND, "2025-03-21")
        assert r.status == "fail"

    def test_rejected_rows_excluded(self):
        rows = self._iv_rows_for(5, 5, status="reject")
        r = check_option_chain_coverage(rows, [], CFG, UND, "2025-03-21")
        assert r.status == "fail"

    def test_caution_rows_included(self):
        rows = self._iv_rows_for(3, 3, status="caution")
        r = check_option_chain_coverage(rows, [], CFG, UND, "2025-03-21")
        assert r.status == "pass"

    def test_check_name(self):
        r = check_option_chain_coverage([], [], CFG, UND, "2025-03-21")
        assert r.check_name == "option_chain_coverage"


# ---------------------------------------------------------------------------
# TestForwardStability
# ---------------------------------------------------------------------------

class TestForwardStability:
    def _rows(self, forwards):
        return [{"forward": f} for f in forwards]

    def test_no_data_fail(self):
        r = check_forward_stability([], CFG, UND)
        assert r.status == "fail"
        assert r.reason_code == "NO_FORWARD_DATA"

    def test_one_row_warn(self):
        r = check_forward_stability(self._rows([5000.0]), CFG, UND)
        assert r.status == "warn"
        assert r.reason_code == "INSUFFICIENT_FORWARD_CANDIDATES"

    def test_stable_forwards_pass(self):
        forwards = [5000.0, 5001.0, 4999.5]  # < 0.5% deviation
        r = check_forward_stability(self._rows(forwards), CFG, UND)
        assert r.status == "pass"

    def test_unstable_forwards_fail(self):
        forwards = [5000.0, 4800.0]  # 4% deviation >> 0.5% threshold
        r = check_forward_stability(self._rows(forwards), CFG, UND)
        assert r.status == "fail"
        assert r.reason_code == "FORWARD_UNSTABLE"

    def test_zero_forward_fail(self):
        r = check_forward_stability(self._rows([0.0, 0.0]), CFG, UND)
        assert r.status == "fail"
        assert r.reason_code == "ZERO_FORWARD"

    def test_measured_value_is_max_dev(self):
        forwards = [5000.0, 5025.0]  # 25/5000 = 0.5% med ≈ 5012.5, max_dev ≈ 0.25%
        r = check_forward_stability(self._rows(forwards), CFG, UND)
        assert r.measured_value is not None
        assert 0 <= r.measured_value <= 1.0

    def test_check_name(self):
        r = check_forward_stability([], CFG, UND)
        assert r.check_name == "forward_stability"


# ---------------------------------------------------------------------------
# TestIvSolverConvergence
# ---------------------------------------------------------------------------

class TestIvSolverConvergence:
    def test_no_rows_fail(self):
        r = check_iv_solver_convergence([], CFG, UND)
        assert r.status == "fail"
        assert r.reason_code == "NO_IV_POINTS"

    def test_all_converged_pass(self):
        rows = [{"converged": True}] * 100
        r = check_iv_solver_convergence(rows, CFG, UND)
        assert r.status == "pass"

    def test_low_ratio_fail(self):
        rows = [{"converged": i < 50} for i in range(100)]  # 50% convergence
        r = check_iv_solver_convergence(rows, CFG, UND)
        assert r.status == "fail"
        assert r.reason_code == "LOW_CONVERGENCE_RATIO"

    def test_measured_value_is_ratio(self):
        rows = [{"converged": i < 96} for i in range(100)]
        r = check_iv_solver_convergence(rows, CFG, UND)
        assert r.measured_value == pytest.approx(0.96)

    def test_exactly_at_threshold_passes(self):
        n = 100
        thresh = int(CFG["min_iv_convergence_ratio"] * n)
        rows = [{"converged": i < thresh} for i in range(n)]
        r = check_iv_solver_convergence(rows, CFG, UND)
        assert r.status == "pass"

    def test_check_name(self):
        r = check_iv_solver_convergence([], CFG, UND)
        assert r.check_name == "iv_solver_convergence"

    def test_context_has_counts(self):
        rows = [{"converged": True}] * 80 + [{"converged": False}] * 20
        r = check_iv_solver_convergence(rows, CFG, UND)
        assert r.context.get("n_total") == 100
        assert r.context.get("n_converged") == 80


# ---------------------------------------------------------------------------
# TestSurfaceFitError
# ---------------------------------------------------------------------------

class TestSurfaceFitError:
    EXP = "2025-03-21"

    def test_no_surface_fail(self):
        r = check_surface_fit_error([], CFG, UND, self.EXP)
        assert r.status == "fail"
        assert r.reason_code == "NO_SURFACE_FIT"

    def test_good_rmse_pass(self):
        rows = [{"expiry_str": self.EXP, "rmse": 0.005}]
        r = check_surface_fit_error(rows, CFG, UND, self.EXP)
        assert r.status == "pass"

    def test_high_rmse_fail(self):
        rows = [{"expiry_str": self.EXP, "rmse": 0.05}]
        r = check_surface_fit_error(rows, CFG, UND, self.EXP)
        assert r.status == "fail"
        assert r.reason_code == "HIGH_SURFACE_RMSE"

    def test_wrong_expiry_excluded(self):
        rows = [{"expiry_str": "2025-06-20", "rmse": 0.001}]
        r = check_surface_fit_error(rows, CFG, UND, self.EXP)
        assert r.status == "fail"
        assert r.reason_code == "NO_SURFACE_FIT"

    def test_missing_rmse_warn(self):
        rows = [{"expiry_str": self.EXP}]  # no "rmse" key
        r = check_surface_fit_error(rows, CFG, UND, self.EXP)
        assert r.status == "warn"
        assert r.reason_code == "RMSE_MISSING"

    def test_measured_value_is_max_rmse(self):
        rows = [{"expiry_str": self.EXP, "rmse": 0.01},
                {"expiry_str": self.EXP, "rmse": 0.018}]
        r = check_surface_fit_error(rows, CFG, UND, self.EXP)
        assert r.measured_value == pytest.approx(0.018)

    def test_check_name(self):
        r = check_surface_fit_error([], CFG, UND, self.EXP)
        assert r.check_name == "surface_fit_error"


# ---------------------------------------------------------------------------
# TestCalendarSanity
# ---------------------------------------------------------------------------

class TestCalendarSanity:
    def _rows(self, maturities_and_variances):
        return [{"maturity_years": T, "atm_total_variance": w}
                for T, w in maturities_and_variances]

    def test_no_data_warn(self):
        r = check_calendar_sanity([], CFG, UND)
        assert r.status == "warn"
        assert r.reason_code == "NO_SURFACE_DATA"

    def test_one_slice_warn(self):
        r = check_calendar_sanity(self._rows([(0.25, 0.04)]), CFG, UND)
        assert r.status == "warn"
        assert r.reason_code == "INSUFFICIENT_SLICES"

    def test_monotone_variance_pass(self):
        rows = self._rows([(0.25, 0.04), (0.5, 0.08), (1.0, 0.16)])
        r = check_calendar_sanity(rows, CFG, UND)
        assert r.status == "pass"

    def test_violation_fail(self):
        rows = self._rows([(0.25, 0.04), (0.5, 0.08), (1.0, 0.05)])  # 1.0 dips
        r = check_calendar_sanity(rows, CFG, UND)
        assert r.status == "fail"
        assert r.reason_code == "CALENDAR_VIOLATION"

    def test_violation_count_in_context(self):
        rows = self._rows([(0.25, 0.08), (0.5, 0.04), (1.0, 0.02)])  # 2 violations
        r = check_calendar_sanity(rows, CFG, UND)
        assert r.context.get("n_violations") == 2

    def test_tiny_numerical_noise_not_violation(self):
        rows = self._rows([(0.25, 0.04), (0.5, 0.04 - 1e-10)])
        r = check_calendar_sanity(rows, CFG, UND)
        assert r.status == "pass"

    def test_check_name(self):
        r = check_calendar_sanity([], CFG, UND)
        assert r.check_name == "calendar_sanity"


# ---------------------------------------------------------------------------
# TestGreekSanity
# ---------------------------------------------------------------------------

class TestGreekSanity:
    def test_no_data_warn(self):
        r = check_greek_sanity([], CFG, UND)
        assert r.status == "warn"
        assert r.reason_code == "NO_PRICING_DATA"

    def test_good_agreement_pass(self):
        rows = [{"analytic_delta": 0.50, "fd_delta": 0.501}]  # 0.001 < 0.01
        r = check_greek_sanity(rows, CFG, UND)
        assert r.status == "pass"

    def test_large_discrepancy_fail(self):
        rows = [{"analytic_delta": 0.50, "fd_delta": 0.55}]  # 0.05 > 0.01
        r = check_greek_sanity(rows, CFG, UND)
        assert r.status == "fail"
        assert r.reason_code == "GREEK_DISCREPANCY"

    def test_missing_fields_warn(self):
        rows = [{"other_field": 1.0}]  # no analytic_delta or fd_delta
        r = check_greek_sanity(rows, CFG, UND)
        assert r.status == "warn"
        assert r.reason_code == "GREEK_COMPARISON_UNAVAILABLE"

    def test_measured_value_is_max_diff(self):
        rows = [{"analytic_delta": 0.50, "fd_delta": 0.503},
                {"analytic_delta": 0.60, "fd_delta": 0.607}]
        r = check_greek_sanity(rows, CFG, UND)
        assert r.measured_value == pytest.approx(0.007, abs=1e-9)

    def test_check_name(self):
        r = check_greek_sanity([], CFG, UND)
        assert r.check_name == "greek_sanity"


# ---------------------------------------------------------------------------
# TestScenarioCompleteness
# ---------------------------------------------------------------------------

class TestScenarioCompleteness:
    EXPECTED = ["dn10", "dn5", "flat", "up5", "up10"]

    def test_all_present_pass(self):
        rows = [{"scenario_id": s} for s in self.EXPECTED]
        r = check_scenario_completeness(rows, self.EXPECTED, CFG)
        assert r.status == "pass"

    def test_missing_scenarios_fail(self):
        rows = [{"scenario_id": "dn10"}, {"scenario_id": "flat"}]
        r = check_scenario_completeness(rows, self.EXPECTED, CFG)
        assert r.status == "fail"
        assert r.reason_code == "MISSING_SCENARIOS"

    def test_missing_listed_in_context(self):
        rows = [{"scenario_id": "dn10"}]
        r = check_scenario_completeness(rows, self.EXPECTED, CFG)
        for s in ["dn5", "flat", "up5", "up10"]:
            assert s in r.context.get("missing", [])

    def test_empty_expected_pass(self):
        r = check_scenario_completeness([], [], CFG)
        assert r.status == "pass"

    def test_extra_scenario_still_passes(self):
        rows = [{"scenario_id": s} for s in self.EXPECTED + ["extra"]]
        r = check_scenario_completeness(rows, self.EXPECTED, CFG)
        assert r.status == "pass"

    def test_check_name(self):
        r = check_scenario_completeness([], self.EXPECTED, CFG)
        assert r.check_name == "scenario_completeness"


# ---------------------------------------------------------------------------
# TestBuildTriageTable
# ---------------------------------------------------------------------------

class TestBuildTriageTable:
    def _report(self, statuses_and_severities):
        checks = [
            ValidationCheckResult(
                check_name=f"check_{i}", target_key=UND,
                status=s, severity=sev,
                measured_value=None, threshold=None,
                threshold_version="1.0", reason_code=f"REASON_{i}",
            )
            for i, (s, sev) in enumerate(statuses_and_severities)
        ]
        return DailyQCReport(run_id="r1", trade_date=DATE, underlying=UND, checks=checks)

    def test_excludes_pass_checks(self):
        report = self._report([("pass", "info"), ("fail", "critical")])
        table = build_triage_table([report])
        assert all(r["status"] != "pass" for r in table)

    def test_includes_warn_and_fail(self):
        report = self._report([("warn", "warn"), ("fail", "critical"), ("pass", "info")])
        table = build_triage_table([report])
        assert len(table) == 2

    def test_sorted_critical_first(self):
        report = self._report([("warn", "warn"), ("fail", "critical")])
        table = build_triage_table([report])
        assert table[0]["severity"] == "critical"

    def test_fail_before_warn_same_severity(self):
        report = self._report([("warn", "critical"), ("fail", "critical")])
        table = build_triage_table([report])
        assert table[0]["status"] == "fail"

    def test_has_required_fields(self):
        report = self._report([("fail", "critical")])
        row = build_triage_table([report])[0]
        for f in ("trade_date", "underlying", "check_name", "status",
                  "severity", "reason_code", "measured_value", "threshold"):
            assert f in row

    def test_multi_report(self):
        r1 = self._report([("fail", "critical")])
        r2 = self._report([("warn", "warn"), ("warn", "warn")])
        table = build_triage_table([r1, r2])
        assert len(table) == 3

    def test_empty_reports(self):
        assert build_triage_table([]) == []

    def test_all_pass_empty_table(self):
        report = self._report([("pass", "info"), ("pass", "info")])
        assert build_triage_table([report]) == []


# ---------------------------------------------------------------------------
# TestRunDailyQc
# ---------------------------------------------------------------------------

class TestRunDailyQc:
    def _all_data(self, **overrides):
        base = {
            "raw_events": [{"timestamp": float(i * 10)} for i in range(100)],
            "snapshots": [{"spread_pct": 0.05, "is_stale": False}] * 20,
            "iv_points": _iv_rows(100, 96),
            "forward_rows": [{"forward": 5000.0}, {"forward": 5002.0}],
            "surface_params": _surface_rows(),
            "pricing_rows": _pricing_rows(delta_diff=0.001),
            "scenario_results": [{"scenario_id": s} for s in
                                  ["dn10", "dn5", "flat", "up5", "up10"]],
        }
        base.update(overrides)
        return base

    def test_returns_daily_qc_report(self):
        r = run_daily_qc(DATE, UND, "run1", self._all_data(), CFG,
                         expected_scenarios=["dn10", "dn5", "flat", "up5", "up10"])
        assert isinstance(r, DailyQCReport)

    def test_check_names_present(self):
        r = run_daily_qc(DATE, UND, "run1", self._all_data(), CFG)
        names = {c.check_name for c in r.checks}
        for expected in ("collector_continuity", "underlying_quote_health",
                         "iv_solver_convergence", "forward_stability",
                         "calendar_sanity", "greek_sanity"):
            assert expected in names

    def test_all_good_data_passes(self):
        r = run_daily_qc(DATE, UND, "run1", self._all_data(), CFG,
                         expected_scenarios=["dn10", "dn5", "flat", "up5", "up10"])
        assert r.overall_status == "pass", \
            f"Expected pass, got {r.overall_status}: {[c.reason_code for c in r.failures()]}"

    def test_missing_iv_points_fails(self):
        data = self._all_data(iv_points=[])
        r = run_daily_qc(DATE, UND, "run1", data, CFG)
        assert r.overall_status == "fail"

    def test_collector_gap_propagates(self):
        events = [{"timestamp": 0.0}, {"timestamp": 200.0}]  # 200s gap
        data = self._all_data(raw_events=events)
        r = run_daily_qc(DATE, UND, "run1", data, CFG)
        names = {c.check_name: c for c in r.checks}
        assert names["collector_continuity"].status == "fail"

    def test_missing_scenario_fails(self):
        data = self._all_data(scenario_results=[{"scenario_id": "dn10"}])
        r = run_daily_qc(DATE, UND, "run1", data, CFG,
                         expected_scenarios=["dn10", "up10"])
        assert r.overall_status == "fail"

    def test_no_scenario_check_when_none(self):
        r = run_daily_qc(DATE, UND, "run1", self._all_data(), CFG,
                         expected_scenarios=None)
        names = {c.check_name for c in r.checks}
        assert "scenario_completeness" not in names

    def test_trade_date_stored(self):
        r = run_daily_qc(DATE, UND, "run1", self._all_data(), CFG)
        assert r.trade_date == DATE

    def test_underlying_stored(self):
        r = run_daily_qc(DATE, UND, "run1", self._all_data(), CFG)
        assert r.underlying == UND


# ---------------------------------------------------------------------------
# TestDetectAnomaly
# ---------------------------------------------------------------------------

class TestDetectAnomaly:
    def _baseline(self, n=10, value=1.0):
        return [value] * n

    def test_returns_anomaly_result(self):
        r = detect_anomaly("metric", "K1", 1.0, self._baseline(), CFG)
        assert isinstance(r, AnomalyResult)

    def test_normal_value_not_anomaly(self):
        baseline = list(range(1, 11))  # 1..10
        r = detect_anomaly("metric", "K1", 5.0, baseline, CFG)
        assert not r.is_anomaly

    def test_extreme_value_is_anomaly(self):
        # Varied baseline (non-zero MAD required for z-score to be meaningful)
        baseline = [1.0 + i * 0.05 for i in range(10)]  # 1.0..1.45
        r = detect_anomaly("metric", "K1", 1000.0, baseline, CFG)
        assert r.is_anomaly

    def test_insufficient_baseline_not_anomaly(self):
        r = detect_anomaly("metric", "K1", 99.0, [1.0, 2.0], CFG)
        assert not r.is_anomaly
        assert r.context.get("reason") == "INSUFFICIENT_BASELINE"

    def test_baseline_median_stored(self):
        baseline = [2.0, 4.0, 6.0, 8.0, 10.0]
        r = detect_anomaly("m", "k", 6.0, baseline, CFG)
        assert r.baseline_median == pytest.approx(6.0)

    def test_zscore_zero_for_median_value(self):
        baseline = [1.0, 2.0, 3.0, 4.0, 5.0]
        med = statistics.median(baseline)
        r = detect_anomaly("m", "k", med, baseline, CFG)
        assert r.zscore == pytest.approx(0.0, abs=1e-8)

    def test_critical_severity_for_large_zscore(self):
        baseline = [1.0 + i * 0.05 for i in range(10)]  # non-zero MAD
        r = detect_anomaly("m", "k", 10000.0, baseline, CFG)
        assert r.severity == "critical"

    def test_warn_severity_at_threshold(self):
        baseline = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
        med = statistics.median(baseline)
        mad = statistics.median([abs(v - med) for v in baseline])
        # z = threshold pushes to just above threshold but below 2*threshold
        val = med + (CFG["anomaly_zscore_threshold"] + 0.1) * 1.4826 * mad
        r = detect_anomaly("m", "k", val, baseline, CFG)
        assert r.is_anomaly
        assert r.severity == "warn"

    def test_metric_and_key_stored(self):
        r = detect_anomaly("iv_ratio", "AAPL", 0.95, self._baseline(), CFG)
        assert r.metric_name == "iv_ratio"
        assert r.target_key == "AAPL"

    def test_current_value_stored(self):
        r = detect_anomaly("m", "k", 42.0, self._baseline(), CFG)
        assert r.current_value == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# TestRunAnomalyDetection
# ---------------------------------------------------------------------------

class TestRunAnomalyDetection:
    def test_returns_list(self):
        daily = {"metric_a": {"K1": 1.0}}
        baseline = {"metric_a": {"K1": [1.0] * 5}}
        results = run_anomaly_detection(daily, baseline, CFG)
        assert isinstance(results, list)

    def test_one_result_per_metric_key(self):
        daily = {"m1": {"K1": 1.0, "K2": 2.0}, "m2": {"K1": 3.0}}
        baseline = {"m1": {"K1": [1.0] * 5, "K2": [2.0] * 5},
                    "m2": {"K1": [3.0] * 5}}
        results = run_anomaly_detection(daily, baseline, CFG)
        assert len(results) == 3

    def test_missing_baseline_handled(self):
        daily = {"metric": {"K1": 1.0}}
        results = run_anomaly_detection(daily, {}, CFG)
        assert len(results) == 1
        assert not results[0].is_anomaly  # insufficient baseline

    def test_anomaly_detected_in_batch(self):
        baseline_vals = [0.95 + i * 0.001 for i in range(10)]  # non-zero MAD
        daily = {"iv_ratio": {"AAPL": 0.0001}}  # extreme outlier vs ~0.954
        baseline = {"iv_ratio": {"AAPL": baseline_vals}}
        results = run_anomaly_detection(daily, baseline, CFG)
        assert results[0].is_anomaly

    def test_empty_daily_metrics(self):
        assert run_anomaly_detection({}, {}, CFG) == []


# ---------------------------------------------------------------------------
# TestFilterAnomalies
# ---------------------------------------------------------------------------

class TestFilterAnomalies:
    def _result(self, is_anomaly, zscore):
        return AnomalyResult(
            metric_name="m", target_key="k",
            current_value=0.0, zscore=zscore,
            baseline_median=0.0, baseline_mad=1.0,
            is_anomaly=is_anomaly, severity="warn",
            threshold_zscore=3.0,
        )

    def test_returns_only_anomalies(self):
        results = [self._result(True, 5.0), self._result(False, 0.5),
                   self._result(True, 4.0)]
        filtered = filter_anomalies(results)
        assert len(filtered) == 2
        assert all(r.is_anomaly for r in filtered)

    def test_sorted_by_abs_zscore(self):
        results = [self._result(True, 4.0), self._result(True, -6.0),
                   self._result(True, 5.0)]
        filtered = filter_anomalies(results)
        assert [abs(r.zscore) for r in filtered] == [6.0, 5.0, 4.0]

    def test_empty_input(self):
        assert filter_anomalies([]) == []


# ---------------------------------------------------------------------------
# TestAnomalySummary
# ---------------------------------------------------------------------------

class TestAnomalySummary:
    def _result(self, is_anomaly=False, zscore=0.0, severity="info"):
        return AnomalyResult(
            metric_name="m", target_key="k",
            current_value=0.0, zscore=zscore,
            baseline_median=0.0, baseline_mad=None,
            is_anomaly=is_anomaly, severity=severity,
            threshold_zscore=3.0,
        )

    def test_total_checked(self):
        results = [self._result()] * 5
        s = anomaly_summary(results)
        assert s["total_checked"] == 5

    def test_n_anomalies(self):
        results = [self._result(True), self._result(False), self._result(True)]
        s = anomaly_summary(results)
        assert s["n_anomalies"] == 2

    def test_n_critical(self):
        results = [self._result(True, severity="critical"),
                   self._result(True, severity="warn"),
                   self._result(False)]
        s = anomaly_summary(results)
        assert s["n_critical"] == 1

    def test_worst_by_abs_zscore(self):
        results = [self._result(True, zscore=4.0), self._result(True, zscore=-7.0)]
        s = anomaly_summary(results)
        assert s["worst_zscore"] == pytest.approx(-7.0)

    def test_no_anomalies_worst_none(self):
        results = [self._result(False)] * 3
        s = anomaly_summary(results)
        assert s["worst_metric"] is None

    def test_empty_results(self):
        s = anomaly_summary([])
        assert s["total_checked"] == 0
        assert s["n_anomalies"] == 0


# ---------------------------------------------------------------------------
# TestAcceptanceCriterion
# ---------------------------------------------------------------------------

class TestAcceptanceCriterion:
    """PLAN: Failing underlyings identifiable within minutes from QC report."""

    def test_triage_table_identifies_failures_by_underlying(self):
        good = DailyQCReport("r1", DATE, "AAPL",
                             [_vcheck("pass")])
        bad = DailyQCReport("r2", DATE, "ESTX50",
                            [_vcheck("fail", "critical", "SPREAD_TOO_WIDE")])
        table = build_triage_table([good, bad])
        assert len(table) == 1
        assert table[0]["underlying"] == "ESTX50"
        assert table[0]["reason_code"] == "SPREAD_TOO_WIDE"

    def test_triage_is_sorted_critical_first(self):
        report = DailyQCReport("r1", DATE, UND, [
            ValidationCheckResult("c1", UND, "warn", "warn",  0.5, 0.3, "1.0", reason_code="W"),
            ValidationCheckResult("c2", UND, "fail", "critical", 0.9, 0.5, "1.0", reason_code="F"),
        ])
        table = build_triage_table([report])
        assert table[0]["severity"] == "critical"

    def test_reason_code_always_set_on_fail(self):
        rows = [{"converged": False}] * 10
        r = check_iv_solver_convergence(rows, CFG, UND)
        assert r.status == "fail"
        assert r.reason_code != ""

    def test_run_daily_qc_overall_status_immediately_visible(self):
        """overall_status is a single derived field — no need to scan all checks."""
        data = {
            "raw_events": [{"timestamp": float(i)} for i in range(100)],
            "snapshots": [{"spread_pct": 0.05, "is_stale": False}],
            "iv_points": [],   # will fail
            "forward_rows": [{"forward": 5000.0}, {"forward": 5001.0}],
            "surface_params": [],
            "pricing_rows": [],
            "scenario_results": [],
        }
        report = run_daily_qc(DATE, UND, "r1", data, CFG)
        assert report.overall_status == "fail"
        assert len(report.failures()) >= 1

    def test_anomaly_summary_identifies_worst_metric(self):
        baseline = [0.95 + i * 0.001 for i in range(10)]  # non-zero MAD
        daily = {"iv_convergence_ratio": {"ESTX50": 0.30}}  # severe drop
        results = run_anomaly_detection(daily, {"iv_convergence_ratio": {"ESTX50": baseline}}, CFG)
        s = anomaly_summary(results)
        assert s["worst_metric"] == "iv_convergence_ratio"
        assert s["n_anomalies"] == 1

    def test_deterministic_checks(self):
        """Same inputs always produce same check results."""
        rows = _iv_rows(100, 90)
        r1 = check_iv_solver_convergence(rows, CFG, UND)
        r2 = check_iv_solver_convergence(rows, CFG, UND)
        assert r1.status == r2.status
        assert r1.measured_value == pytest.approx(r2.measured_value)
