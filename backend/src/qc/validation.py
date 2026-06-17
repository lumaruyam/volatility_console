"""
Validation framework — named QC checks over day-level analytics data.

Each check returns a ValidationCheckResult with:
  status           "pass" | "warn" | "fail"
  severity         "info" | "warn" | "critical"
  measured_value   the observed quantity
  threshold        the configured limit tested against
  reason_code      stable uppercase tag (empty on pass)
  context          supporting detail dict

Design rules (same as checks.py):
  - Pure functions: same inputs → same output.
  - Never silently hide a failure; always return a result even on missing data.
  - reason_code must be non-empty on any non-pass result.
  - Triage table collects every failure with reason code and context.

Acceptance criterion: failing underlyings identifiable within minutes from QC report.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

@dataclass
class ValidationCheckResult:
    check_name: str
    target_key: str
    status: str          # "pass" | "warn" | "fail"
    severity: str        # "info" | "warn" | "critical"
    measured_value: Optional[float]
    threshold: Optional[float]
    threshold_version: str
    context: dict = field(default_factory=dict)
    reason_code: str = ""


@dataclass
class DailyQCReport:
    run_id: str
    trade_date: str
    underlying: str
    checks: list[ValidationCheckResult]

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "pass")

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail")

    @property
    def overall_status(self) -> str:
        if self.fail_count > 0:
            return "fail"
        if self.warn_count > 0:
            return "warn"
        return "pass"

    def failures(self) -> list[ValidationCheckResult]:
        return [c for c in self.checks if c.status == "fail"]

    def warnings(self) -> list[ValidationCheckResult]:
        return [c for c in self.checks if c.status == "warn"]


# ---------------------------------------------------------------------------
# Robust z-score
# ---------------------------------------------------------------------------

def robust_zscore(values: list[float]) -> list[float]:
    """
    z_i = (x_i - median(x)) / (1.4826 * MAD(x))
    Returns zeros when len < 2 or MAD ≈ 0 (all values identical).
    """
    if len(values) < 2:
        return [0.0] * len(values)
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad < 1e-10:
        return [0.0] * len(values)
    return [(v - med) / (1.4826 * mad) for v in values]


# ---------------------------------------------------------------------------
# Named validation checks
# ---------------------------------------------------------------------------

def check_collector_continuity(
    events: list,
    session_window_seconds: float,
    config: dict,
    trade_date: str,
) -> ValidationCheckResult:
    """
    No unexplained gap longer than max_collector_gap_seconds during the liquid session.
    events: list of dicts with "timestamp" key (numeric seconds).
    """
    max_gap = float(config.get("max_collector_gap_seconds", 30))
    ver = config.get("version", "1.0")

    if not events:
        return ValidationCheckResult(
            check_name="collector_continuity", target_key=trade_date,
            status="fail", severity="critical", measured_value=None,
            threshold=max_gap, threshold_version=ver,
            reason_code="NO_EVENTS",
            context={"session_window_seconds": session_window_seconds},
        )

    timestamps = sorted(e["timestamp"] for e in events if "timestamp" in e)
    if len(timestamps) < 2:
        return ValidationCheckResult(
            check_name="collector_continuity", target_key=trade_date,
            status="warn", severity="warn", measured_value=None,
            threshold=max_gap, threshold_version=ver,
            reason_code="INSUFFICIENT_EVENTS",
            context={"n_events": len(timestamps)},
        )

    gaps = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    max_observed_gap = max(gaps)
    status = "pass" if max_observed_gap <= max_gap else "fail"
    severity = "info" if status == "pass" else "critical"

    return ValidationCheckResult(
        check_name="collector_continuity", target_key=trade_date,
        status=status, severity=severity,
        measured_value=max_observed_gap, threshold=max_gap,
        threshold_version=ver,
        reason_code="" if status == "pass" else "GAP_TOO_LARGE",
        context={"max_gap_seconds": max_observed_gap, "n_events": len(timestamps),
                 "n_gaps": len(gaps)},
    )


def check_underlying_quote_health(
    snapshot_rows: list,
    config: dict,
    underlying: str,
) -> ValidationCheckResult:
    """
    Mean spread percentage and stale ratio both below configured thresholds.
    snapshot_rows: list of dicts with "spread_pct" and "is_stale" keys.
    """
    max_spread = float(config.get("max_spread_pct", 0.25))
    max_stale = float(config.get("max_stale_ratio", 0.10))
    ver = config.get("version", "1.0")

    if not snapshot_rows:
        return ValidationCheckResult(
            check_name="underlying_quote_health", target_key=underlying,
            status="fail", severity="critical", measured_value=None,
            threshold=max_spread, threshold_version=ver,
            reason_code="NO_SNAPSHOTS",
        )

    spreads = [r["spread_pct"] for r in snapshot_rows if r.get("spread_pct") is not None]
    stale_flags = [bool(r.get("is_stale", False)) for r in snapshot_rows]
    stale_ratio = sum(stale_flags) / len(stale_flags) if stale_flags else 0.0
    mean_spread = statistics.mean(spreads) if spreads else None

    if mean_spread is not None and mean_spread > max_spread:
        return ValidationCheckResult(
            check_name="underlying_quote_health", target_key=underlying,
            status="fail", severity="critical",
            measured_value=mean_spread, threshold=max_spread,
            threshold_version=ver, reason_code="SPREAD_TOO_WIDE",
            context={"mean_spread_pct": mean_spread, "stale_ratio": stale_ratio},
        )
    if stale_ratio > max_stale:
        return ValidationCheckResult(
            check_name="underlying_quote_health", target_key=underlying,
            status="warn", severity="warn",
            measured_value=stale_ratio, threshold=max_stale,
            threshold_version=ver, reason_code="HIGH_STALE_RATIO",
            context={"stale_ratio": stale_ratio, "mean_spread_pct": mean_spread},
        )
    return ValidationCheckResult(
        check_name="underlying_quote_health", target_key=underlying,
        status="pass", severity="info",
        measured_value=mean_spread, threshold=max_spread,
        threshold_version=ver,
        context={"mean_spread_pct": mean_spread, "stale_ratio": stale_ratio},
    )


def check_option_chain_coverage(
    iv_point_rows: list,
    instrument_master: list,
    config: dict,
    underlying: str,
    expiry_str: str,
) -> ValidationCheckResult:
    """
    Minimum count of eligible calls and puts per monitored maturity.
    iv_point_rows: list of dicts with "expiry_str", "option_type", "qc_status".
    """
    min_calls = int(config.get("min_calls_per_maturity", 5))
    min_puts = int(config.get("min_puts_per_maturity", 5))
    ver = config.get("version", "1.0")
    target_key = f"{underlying}/{expiry_str}"

    usable = [
        r for r in iv_point_rows
        if r.get("expiry_str") == expiry_str
        and r.get("qc_status") in ("usable", "caution")
    ]
    n_calls = sum(1 for r in usable if r.get("option_type") == "C")
    n_puts = sum(1 for r in usable if r.get("option_type") == "P")
    min_observed = min(n_calls, n_puts)
    min_required = min(min_calls, min_puts)

    if n_calls < min_calls or n_puts < min_puts:
        return ValidationCheckResult(
            check_name="option_chain_coverage", target_key=target_key,
            status="fail", severity="critical",
            measured_value=float(min_observed), threshold=float(min_required),
            threshold_version=ver, reason_code="INSUFFICIENT_CHAIN",
            context={"n_calls": n_calls, "n_puts": n_puts,
                     "min_calls": min_calls, "min_puts": min_puts,
                     "expiry_str": expiry_str},
        )
    return ValidationCheckResult(
        check_name="option_chain_coverage", target_key=target_key,
        status="pass", severity="info",
        measured_value=float(min_observed), threshold=float(min_required),
        threshold_version=ver,
        context={"n_calls": n_calls, "n_puts": n_puts},
    )


def check_forward_stability(
    forward_rows: list,
    config: dict,
    underlying: str,
) -> ValidationCheckResult:
    """
    Max percentage deviation of forward candidates from their median is within tolerance.
    forward_rows: list of dicts with "forward" key (numeric).
    """
    max_dev_pct = float(config.get("max_forward_deviation_pct", 0.005))
    ver = config.get("version", "1.0")

    if not forward_rows:
        return ValidationCheckResult(
            check_name="forward_stability", target_key=underlying,
            status="fail", severity="critical", measured_value=None,
            threshold=max_dev_pct, threshold_version=ver,
            reason_code="NO_FORWARD_DATA",
        )

    forwards = [float(r["forward"]) for r in forward_rows if r.get("forward") is not None]

    if len(forwards) < 2:
        return ValidationCheckResult(
            check_name="forward_stability", target_key=underlying,
            status="warn", severity="warn",
            measured_value=None, threshold=max_dev_pct,
            threshold_version=ver, reason_code="INSUFFICIENT_FORWARD_CANDIDATES",
            context={"n_candidates": len(forwards)},
        )

    med = statistics.median(forwards)
    if med == 0.0:
        return ValidationCheckResult(
            check_name="forward_stability", target_key=underlying,
            status="fail", severity="critical", measured_value=0.0,
            threshold=max_dev_pct, threshold_version=ver,
            reason_code="ZERO_FORWARD",
        )

    max_dev = max(abs(f - med) / abs(med) for f in forwards)
    status = "pass" if max_dev <= max_dev_pct else "fail"
    severity = "info" if status == "pass" else "critical"

    return ValidationCheckResult(
        check_name="forward_stability", target_key=underlying,
        status=status, severity=severity,
        measured_value=max_dev, threshold=max_dev_pct,
        threshold_version=ver,
        reason_code="" if status == "pass" else "FORWARD_UNSTABLE",
        context={"median_forward": med, "max_deviation_pct": max_dev,
                 "n_candidates": len(forwards)},
    )


def check_iv_solver_convergence(
    iv_point_rows: list,
    config: dict,
    underlying: str,
) -> ValidationCheckResult:
    """
    Convergence ratio above threshold; residual distribution acceptable.
    iv_point_rows: list of dicts with "converged" bool key.
    """
    min_ratio = float(config.get("min_iv_convergence_ratio", 0.97))
    ver = config.get("version", "1.0")

    if not iv_point_rows:
        return ValidationCheckResult(
            check_name="iv_solver_convergence", target_key=underlying,
            status="fail", severity="critical", measured_value=0.0,
            threshold=min_ratio, threshold_version=ver,
            reason_code="NO_IV_POINTS",
        )

    n_total = len(iv_point_rows)
    n_converged = sum(1 for r in iv_point_rows if r.get("converged", False))
    ratio = n_converged / n_total
    status = "pass" if ratio >= min_ratio else "fail"
    severity = "info" if status == "pass" else ("warn" if ratio >= min_ratio * 0.9 else "critical")

    return ValidationCheckResult(
        check_name="iv_solver_convergence", target_key=underlying,
        status=status, severity=severity,
        measured_value=ratio, threshold=min_ratio,
        threshold_version=ver,
        reason_code="" if status == "pass" else "LOW_CONVERGENCE_RATIO",
        context={"n_converged": n_converged, "n_total": n_total},
    )


def check_surface_fit_error(
    surface_param_rows: list,
    config: dict,
    underlying: str,
    expiry_str: str,
) -> ValidationCheckResult:
    """
    Root-mean-square fit error below threshold for the given maturity slice.
    surface_param_rows: list of dicts with "expiry_str" and "rmse" keys.
    """
    max_rmse = float(config.get("max_rmse", 0.02))
    ver = config.get("version", "1.0")
    target_key = f"{underlying}/{expiry_str}"

    rows = [r for r in surface_param_rows if r.get("expiry_str") == expiry_str]
    if not rows:
        return ValidationCheckResult(
            check_name="surface_fit_error", target_key=target_key,
            status="fail", severity="critical", measured_value=None,
            threshold=max_rmse, threshold_version=ver,
            reason_code="NO_SURFACE_FIT",
            context={"expiry_str": expiry_str},
        )

    rmse_vals = [float(r["rmse"]) for r in rows if r.get("rmse") is not None]
    if not rmse_vals:
        return ValidationCheckResult(
            check_name="surface_fit_error", target_key=target_key,
            status="warn", severity="warn", measured_value=None,
            threshold=max_rmse, threshold_version=ver,
            reason_code="RMSE_MISSING",
        )

    max_obs = max(rmse_vals)
    status = "pass" if max_obs <= max_rmse else "fail"
    severity = "info" if status == "pass" else "critical"

    return ValidationCheckResult(
        check_name="surface_fit_error", target_key=target_key,
        status=status, severity=severity,
        measured_value=max_obs, threshold=max_rmse,
        threshold_version=ver,
        reason_code="" if status == "pass" else "HIGH_SURFACE_RMSE",
        context={"max_rmse": max_obs, "n_slices": len(rmse_vals)},
    )


def check_calendar_sanity(
    surface_param_rows: list,
    config: dict,
    underlying: str,
) -> ValidationCheckResult:
    """
    Total variance must not decrease across successive maturities.
    surface_param_rows: list of dicts with "maturity_years" and "atm_total_variance".
    """
    tolerance = float(config.get("calendar_sanity_tolerance", 1e-6))
    ver = config.get("version", "1.0")

    if not surface_param_rows:
        return ValidationCheckResult(
            check_name="calendar_sanity", target_key=underlying,
            status="warn", severity="warn", measured_value=None,
            threshold=None, threshold_version=ver,
            reason_code="NO_SURFACE_DATA",
        )

    sorted_rows = sorted(surface_param_rows, key=lambda r: r.get("maturity_years", 0.0))
    pairs = [
        (r.get("maturity_years"), r.get("atm_total_variance"))
        for r in sorted_rows
        if r.get("maturity_years") is not None and r.get("atm_total_variance") is not None
    ]

    if len(pairs) < 2:
        return ValidationCheckResult(
            check_name="calendar_sanity", target_key=underlying,
            status="warn", severity="warn", measured_value=None,
            threshold=None, threshold_version=ver,
            reason_code="INSUFFICIENT_SLICES",
            context={"n_slices": len(pairs)},
        )

    violations = [
        {"from_T": pairs[i][0], "to_T": pairs[i + 1][0],
         "from_w": pairs[i][1], "to_w": pairs[i + 1][1]}
        for i in range(len(pairs) - 1)
        if pairs[i + 1][1] < pairs[i][1] - tolerance
    ]

    n_viol = len(violations)
    status = "pass" if n_viol == 0 else "fail"
    severity = "info" if status == "pass" else "critical"

    return ValidationCheckResult(
        check_name="calendar_sanity", target_key=underlying,
        status=status, severity=severity,
        measured_value=float(n_viol), threshold=0.0,
        threshold_version=ver,
        reason_code="" if status == "pass" else "CALENDAR_VIOLATION",
        context={"n_violations": n_viol, "violations": violations[:5]},
    )


def check_greek_sanity(
    pricing_rows: list,
    config: dict,
    underlying: str,
) -> ValidationCheckResult:
    """
    Analytic and finite-difference deltas agree within tolerance.
    pricing_rows: list of dicts with "analytic_delta" and "fd_delta" keys.
    """
    tolerance = float(config.get("greek_sanity_tolerance", 0.01))
    ver = config.get("version", "1.0")

    if not pricing_rows:
        return ValidationCheckResult(
            check_name="greek_sanity", target_key=underlying,
            status="warn", severity="warn", measured_value=None,
            threshold=tolerance, threshold_version=ver,
            reason_code="NO_PRICING_DATA",
        )

    diffs = []
    for row in pricing_rows:
        a = row.get("analytic_delta")
        f = row.get("fd_delta")
        if a is not None and f is not None:
            diffs.append(abs(float(a) - float(f)))

    if not diffs:
        return ValidationCheckResult(
            check_name="greek_sanity", target_key=underlying,
            status="warn", severity="warn", measured_value=None,
            threshold=tolerance, threshold_version=ver,
            reason_code="GREEK_COMPARISON_UNAVAILABLE",
        )

    max_diff = max(diffs)
    n_viol = sum(1 for d in diffs if d > tolerance)
    status = "pass" if n_viol == 0 else "fail"
    severity = "info" if status == "pass" else "critical"

    return ValidationCheckResult(
        check_name="greek_sanity", target_key=underlying,
        status=status, severity=severity,
        measured_value=max_diff, threshold=tolerance,
        threshold_version=ver,
        reason_code="" if status == "pass" else "GREEK_DISCREPANCY",
        context={"max_diff": max_diff, "n_violations": n_viol, "n_compared": len(diffs)},
    )


def check_scenario_completeness(
    scenario_rows: list,
    expected_scenarios: list,
    config: dict,
) -> ValidationCheckResult:
    """
    All configured scenarios executed and stored with no missing results.
    scenario_rows: list of dicts with "scenario_id" key.
    """
    ver = config.get("version", "1.0")
    executed_ids = {r.get("scenario_id") for r in scenario_rows}
    missing = [s for s in expected_scenarios if s not in executed_ids]

    if missing:
        return ValidationCheckResult(
            check_name="scenario_completeness", target_key="portfolio",
            status="fail", severity="critical",
            measured_value=float(len(executed_ids)),
            threshold=float(len(expected_scenarios)),
            threshold_version=ver, reason_code="MISSING_SCENARIOS",
            context={"missing": missing, "executed": sorted(executed_ids)},
        )
    return ValidationCheckResult(
        check_name="scenario_completeness", target_key="portfolio",
        status="pass", severity="info",
        measured_value=float(len(executed_ids)),
        threshold=float(len(expected_scenarios)),
        threshold_version=ver,
    )


# ---------------------------------------------------------------------------
# Triage table
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2}
_STATUS_ORDER = {"fail": 0, "warn": 1, "pass": 2}


def build_triage_table(reports: list[DailyQCReport]) -> list[dict]:
    """
    Flatten all warn/fail checks from a list of DailyQCReports into a triage table.
    Sorted by severity (critical first) then status (fail before warn).
    Every row has: trade_date, underlying, check_name, status, severity,
                   reason_code, measured_value, threshold, context.
    """
    rows = []
    for report in reports:
        for check in report.checks:
            if check.status in ("warn", "fail"):
                rows.append({
                    "trade_date": report.trade_date,
                    "underlying": report.underlying,
                    "run_id": report.run_id,
                    "check_name": check.check_name,
                    "target_key": check.target_key,
                    "status": check.status,
                    "severity": check.severity,
                    "reason_code": check.reason_code,
                    "measured_value": check.measured_value,
                    "threshold": check.threshold,
                    "context": check.context,
                })
    rows.sort(key=lambda r: (
        _SEVERITY_ORDER.get(r["severity"], 9),
        _STATUS_ORDER.get(r["status"], 9),
    ))
    return rows


# ---------------------------------------------------------------------------
# Daily QC orchestration
# ---------------------------------------------------------------------------

def run_daily_qc(
    trade_date: str,
    underlying: str,
    run_id: str,
    all_data: dict,
    config: dict,
    expected_scenarios: Optional[list[str]] = None,
) -> DailyQCReport:
    """
    Run the full QC suite for one underlying on one trade date.

    all_data keys consumed:
      raw_events         list[dict] with "timestamp" (collector continuity)
      snapshots          list[dict] with "spread_pct", "is_stale" (quote health)
      iv_points          list[dict] with "converged", "expiry_str", "option_type", "qc_status"
      forward_rows       list[dict] with "forward"
      surface_params     list[dict] with "expiry_str", "rmse", "maturity_years", "atm_total_variance"
      pricing_rows       list[dict] with "analytic_delta", "fd_delta"
      scenario_results   list[dict] with "scenario_id"

    Returns DailyQCReport with all check results.
    """
    checks: list[ValidationCheckResult] = []
    ver = config.get("version", "1.0")

    # 1. Collector continuity
    raw_events = all_data.get("raw_events", [])
    session_window = float(config.get("session_window_seconds", 27000))
    checks.append(check_collector_continuity(raw_events, session_window, config, trade_date))

    # 2. Underlying quote health
    snapshots = all_data.get("snapshots", [])
    checks.append(check_underlying_quote_health(snapshots, config, underlying))

    # 3. IV solver convergence
    iv_points = all_data.get("iv_points", [])
    checks.append(check_iv_solver_convergence(iv_points, config, underlying))

    # 4. Forward stability
    forward_rows = all_data.get("forward_rows", [])
    checks.append(check_forward_stability(forward_rows, config, underlying))

    # 5. Surface fit error and calendar sanity (per expiry aggregated to worst)
    surface_params = all_data.get("surface_params", [])
    checks.append(check_calendar_sanity(surface_params, config, underlying))

    expiries = sorted({r.get("expiry_str") for r in surface_params if r.get("expiry_str")})
    if expiries:
        # Report worst slice only in the daily summary
        fit_checks = [
            check_surface_fit_error(surface_params, config, underlying, exp)
            for exp in expiries
        ]
        worst_fit = max(fit_checks, key=lambda c: (
            _STATUS_ORDER.get(c.status, 9),  # lower number = worse
            -(c.measured_value or 0.0),
        ), default=None)
        if worst_fit is not None:
            checks.append(worst_fit)
    else:
        checks.append(check_surface_fit_error(surface_params, config, underlying, ""))

    # 6. Greek sanity
    pricing_rows = all_data.get("pricing_rows", [])
    checks.append(check_greek_sanity(pricing_rows, config, underlying))

    # 7. Scenario completeness (optional — skip if no expected list)
    if expected_scenarios is not None:
        scenario_results = all_data.get("scenario_results", [])
        checks.append(check_scenario_completeness(scenario_results, expected_scenarios, config))

    logger.info(
        "qc.daily underlying=%s date=%s pass=%d warn=%d fail=%d",
        underlying, trade_date,
        sum(1 for c in checks if c.status == "pass"),
        sum(1 for c in checks if c.status == "warn"),
        sum(1 for c in checks if c.status == "fail"),
    )

    return DailyQCReport(
        run_id=run_id,
        trade_date=trade_date,
        underlying=underlying,
        checks=checks,
    )
