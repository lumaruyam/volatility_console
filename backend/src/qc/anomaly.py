"""
Anomaly detection against a rolling baseline.

Uses the robust z-score to flag metrics that deviate from their recent history.
The rolling baseline is computed from prior days' values; today's value is
compared against it — not included in the baseline itself.

Design rules:
  - Pure functions: no side effects, deterministic.
  - Never suppress an anomaly silently: every outlier gets a flagged AnomalyResult.
  - Minimum baseline length is enforced; insufficient history → is_anomaly=False.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional

from src.qc.validation import robust_zscore


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class AnomalyResult:
    metric_name: str
    target_key: str
    current_value: float
    zscore: float
    baseline_median: Optional[float]
    baseline_mad: Optional[float]
    is_anomaly: bool
    severity: str           # "info" | "warn" | "critical"
    threshold_zscore: float
    context: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Single-metric anomaly detection
# ---------------------------------------------------------------------------

def detect_anomaly(
    metric_name: str,
    target_key: str,
    current_value: float,
    baseline_values: list[float],
    config: dict,
) -> AnomalyResult:
    """
    Detect whether current_value is anomalous vs its rolling baseline.

    z = (current - median(baseline)) / (1.4826 * MAD(baseline))

    Args:
        metric_name:      Name of the metric (e.g. "iv_convergence_ratio").
        target_key:       Underlying or contract identifier.
        current_value:    Today's value.
        baseline_values:  Prior days' values (today excluded).
        config:
          anomaly_zscore_threshold        default 3.5
          anomaly_critical_zscore_mult    default 2.0  (multiplier above threshold → critical)
          min_baseline_length             default 5

    Returns:
        AnomalyResult — always returned even when baseline is insufficient.
    """
    threshold = float(config.get("anomaly_zscore_threshold", 3.5))
    critical_mult = float(config.get("anomaly_critical_zscore_mult", 2.0))
    min_len = int(config.get("min_baseline_length", 5))

    if len(baseline_values) < min_len:
        return AnomalyResult(
            metric_name=metric_name,
            target_key=target_key,
            current_value=current_value,
            zscore=0.0,
            baseline_median=None,
            baseline_mad=None,
            is_anomaly=False,
            severity="info",
            threshold_zscore=threshold,
            context={"reason": "INSUFFICIENT_BASELINE",
                     "n_baseline": len(baseline_values),
                     "min_required": min_len},
        )

    med = statistics.median(baseline_values)
    mad = statistics.median([abs(v - med) for v in baseline_values])

    if mad < 1e-10:
        zscore = 0.0
    else:
        zscore = (current_value - med) / (1.4826 * mad)

    abs_z = abs(zscore)
    is_anomaly = abs_z > threshold
    if abs_z > threshold * critical_mult:
        severity = "critical"
    elif is_anomaly:
        severity = "warn"
    else:
        severity = "info"

    return AnomalyResult(
        metric_name=metric_name,
        target_key=target_key,
        current_value=current_value,
        zscore=zscore,
        baseline_median=med,
        baseline_mad=mad,
        is_anomaly=is_anomaly,
        severity=severity,
        threshold_zscore=threshold,
        context={"baseline_n": len(baseline_values)},
    )


# ---------------------------------------------------------------------------
# Batch anomaly detection
# ---------------------------------------------------------------------------

def run_anomaly_detection(
    daily_metrics: dict[str, dict[str, float]],
    rolling_baseline: dict[str, dict[str, list[float]]],
    config: dict,
) -> list[AnomalyResult]:
    """
    Detect anomalies across all metrics and targets in one pass.

    Args:
        daily_metrics:     {metric_name: {target_key: current_value}}
        rolling_baseline:  {metric_name: {target_key: [historical_values]}}
        config:            Anomaly detection config.

    Returns:
        List of AnomalyResult, one per (metric_name, target_key) pair.
    """
    results: list[AnomalyResult] = []
    for metric_name, key_values in daily_metrics.items():
        for target_key, current_value in key_values.items():
            baseline = (
                rolling_baseline
                .get(metric_name, {})
                .get(target_key, [])
            )
            result = detect_anomaly(
                metric_name=metric_name,
                target_key=target_key,
                current_value=current_value,
                baseline_values=baseline,
                config=config,
            )
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# Anomaly summary helpers
# ---------------------------------------------------------------------------

def filter_anomalies(results: list[AnomalyResult]) -> list[AnomalyResult]:
    """Return only flagged anomalies, sorted by |z-score| descending."""
    flagged = [r for r in results if r.is_anomaly]
    return sorted(flagged, key=lambda r: abs(r.zscore), reverse=True)


def anomaly_summary(results: list[AnomalyResult]) -> dict:
    """
    High-level summary over a batch of AnomalyResults.
    Returns counts and the worst anomaly by |z-score|.
    """
    flagged = [r for r in results if r.is_anomaly]
    critical = [r for r in flagged if r.severity == "critical"]
    worst = max(flagged, key=lambda r: abs(r.zscore)) if flagged else None
    return {
        "total_checked": len(results),
        "n_anomalies": len(flagged),
        "n_critical": len(critical),
        "worst_metric": worst.metric_name if worst else None,
        "worst_target": worst.target_key if worst else None,
        "worst_zscore": worst.zscore if worst else None,
    }
