"""
Named QC check functions — one function per check, never a monolithic if-statement.

Every function returns a QCCheckResult with:
  status       "pass" | "caution" | "reject"
  reason_code  stable uppercase code (e.g. "SPREAD_TOO_WIDE") — key for dashboards/alerts
  measured_value  the observed quantity (None if unavailable)
  threshold    the configured limit that was tested against

Design rules:
  - Pure functions: same inputs → same output (deterministic, unit-testable).
  - No logging inside checks; log at the caller level.
  - Never silently change status; always return a result even when data is missing.
  - reason_code must be non-empty on any non-pass result.

parity_residual check note:
  check_parity_residual() expects a *pre-computed robust z-score*, not a raw residual.
  The caller is responsible for computing z-scores from the population of residuals for
  the same maturity using robust_zscore(). This keeps the check pure and fast.
  Use check_parity_residual_population() when you have raw residuals and want both steps.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional

from src.snapshots.models import OptionRow


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class QCCheckResult:
    check_name: str
    status: str                     # "pass" | "caution" | "reject"
    reason_code: str                # stable uppercase tag
    measured_value: Optional[float]
    threshold: Optional[float]
    context: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Robust z-score utility
# ---------------------------------------------------------------------------


def robust_zscore(values: list[float]) -> list[float]:
    """Compute robust z-scores using Median Absolute Deviation.

    z_i = (x_i − median(x)) / (1.4826 * MAD(x))

    Returns a list of zeros when len < 2 or MAD ≈ 0 (all values identical).
    """
    if len(values) < 2:
        return [0.0] * len(values)
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad < 1e-10:
        return [0.0] * len(values)
    return [(v - med) / (1.4826 * mad) for v in values]


# ---------------------------------------------------------------------------
# Individual checks — 7 named checks per PLAN Step 7
# ---------------------------------------------------------------------------


def check_spread_pct(row: OptionRow, config: dict) -> QCCheckResult:
    """Reject if (ask − bid) / mid > max_spread_pct.  Caution between caution and reject levels."""
    max_reject = config.get("max_spread_pct", 0.25)
    max_caution = config.get("caution_spread_pct", 0.15)
    sp = row.spread_pct

    if sp is None:
        return QCCheckResult("spread_pct", "reject", "SPREAD_UNAVAILABLE",
                             None, max_reject, {})
    if sp >= max_reject:
        return QCCheckResult("spread_pct", "reject", "SPREAD_TOO_WIDE",
                             sp, max_reject, {"bid": row.bid, "ask": row.ask})
    if sp >= max_caution:
        return QCCheckResult("spread_pct", "caution", "SPREAD_ELEVATED",
                             sp, max_caution, {"bid": row.bid, "ask": row.ask})
    return QCCheckResult("spread_pct", "pass", "OK", sp, max_reject, {})


def check_bid_positive(row: OptionRow, config: dict) -> QCCheckResult:
    """Reject if bid is None, zero, or negative — no meaningful quote without a positive bid."""
    if row.bid is None or row.bid <= 0:
        return QCCheckResult("bid_positive", "reject", "BID_NOT_POSITIVE",
                             row.bid, 0.0, {"bid": row.bid})
    return QCCheckResult("bid_positive", "pass", "OK", row.bid, 0.0, {})


def check_quote_age(row: OptionRow, config: dict) -> QCCheckResult:
    """Reject if quote is older than max_quote_age_seconds.  Caution in the aging band."""
    max_age = config.get("max_quote_age_seconds", 60)
    caution_age = config.get("caution_quote_age_seconds", 30)

    if row.quote_age_seconds is None:
        return QCCheckResult("quote_age", "caution", "AGE_UNKNOWN", None, max_age, {})
    if row.quote_age_seconds >= max_age:
        return QCCheckResult("quote_age", "reject", "QUOTE_STALE",
                             row.quote_age_seconds, max_age, {})
    if row.quote_age_seconds >= caution_age:
        return QCCheckResult("quote_age", "caution", "QUOTE_AGING",
                             row.quote_age_seconds, caution_age, {})
    return QCCheckResult("quote_age", "pass", "OK", row.quote_age_seconds, max_age, {})


def check_open_interest(row: OptionRow, config: dict) -> QCCheckResult:
    """Caution (not reject) if open interest is below minimum — illiquid but still tradable."""
    min_oi = config.get("min_open_interest", 10)
    if row.open_interest is None:
        return QCCheckResult("open_interest", "caution", "OI_UNAVAILABLE", None, min_oi, {})
    if row.open_interest < min_oi:
        return QCCheckResult("open_interest", "caution", "OI_LOW",
                             row.open_interest, min_oi, {})
    return QCCheckResult("open_interest", "pass", "OK", row.open_interest, min_oi, {})


def check_crossed_market(row: OptionRow, config: dict) -> QCCheckResult:
    """Reject if bid > ask — crossed market indicates a data error."""
    if row.bid is None or row.ask is None:
        return QCCheckResult("crossed_market", "caution", "QUOTE_INCOMPLETE",
                             None, None, {"bid": row.bid, "ask": row.ask})
    if row.bid > row.ask:
        return QCCheckResult("crossed_market", "reject", "MARKET_CROSSED",
                             row.bid - row.ask, 0.0, {"bid": row.bid, "ask": row.ask})
    return QCCheckResult("crossed_market", "pass", "OK", row.ask - row.bid, 0.0, {})


def check_intrinsic_value(row: OptionRow, config: dict,
                           intrinsic: float) -> QCCheckResult:
    """Reject if mid < intrinsic − tolerance  (no-arbitrage violation)."""
    tolerance = config.get("intrinsic_tolerance", 0.01)
    mid = row.mid
    if mid is None:
        return QCCheckResult("intrinsic_value", "reject", "MID_UNAVAILABLE",
                             None, intrinsic, {})
    if mid < intrinsic - tolerance:
        return QCCheckResult("intrinsic_value", "reject", "BELOW_INTRINSIC",
                             mid, intrinsic,
                             {"intrinsic": intrinsic, "mid": mid,
                              "deficit": intrinsic - mid})
    return QCCheckResult("intrinsic_value", "pass", "OK", mid, intrinsic, {})


def check_parity_residual(parity_zscore: float, config: dict,
                           context: Optional[dict] = None) -> QCCheckResult:
    """Reject if pre-computed robust z-score of a parity residual exceeds threshold.

    The caller must compute robust z-scores from the population of parity residuals
    for the same maturity using robust_zscore() before calling this function.

    z_i = (r_i − median(r)) / (1.4826 * MAD(r))
    """
    max_zscore = config.get("max_parity_residual_zscore", 3.5)
    ctx = context or {}
    if abs(parity_zscore) >= max_zscore:
        return QCCheckResult("parity_residual", "reject", "PARITY_OUTLIER",
                             parity_zscore, max_zscore, ctx)
    return QCCheckResult("parity_residual", "pass", "OK",
                         parity_zscore, max_zscore, ctx)


# ---------------------------------------------------------------------------
# Population-level parity residual helper
# ---------------------------------------------------------------------------


def check_parity_residual_population(
    residuals: list[float],
    instrument_keys: list[str] | None,
    config: dict,
) -> list[QCCheckResult]:
    """Run check_parity_residual over a population of residuals in one call.

    Computes robust z-scores from the residual distribution, then applies
    check_parity_residual per element. Returns one QCCheckResult per element.

    When len(residuals) < 2, z-scores default to 0 (single sample — not comparable).
    """
    if not residuals:
        return []

    keys = instrument_keys or [""] * len(residuals)
    zscores = robust_zscore(residuals)
    max_zscore = config.get("max_parity_residual_zscore", 3.5)

    results: list[QCCheckResult] = []
    for key, r, z in zip(keys, residuals, zscores):
        ctx = {"instrument_key": key, "raw_residual": r}
        results.append(check_parity_residual(z, config, context=ctx))
    return results


# ---------------------------------------------------------------------------
# Re-export facade — PDF Part XII names checks.py as the single import point
# for all QC functions; logic lives in quote_filter.py and validation.py.
# ---------------------------------------------------------------------------

from src.qc.quote_filter import run_quote_qc, filter_chain          # noqa: E402,F401
from src.qc.validation import run_daily_qc, DailyQCReport, build_triage_table  # noqa: E402,F401
