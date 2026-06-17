"""
Forward curve and implied carry engine.

Put-call parity forward: F(T) ≈ K + e^(rT) * (C(K,T) - P(K,T))
Weighted aggregation: F̂(T) = Σ(ω_i * F_i) / Σ(ω_i), ω_i = 1/(SpreadPct_i + ε)
Carry identity: q(T) = r(T) - (1/T) * ln(F(T)/S0)

Fallback policy (in priority order):
  1. Weighted parity estimate  — all normal; fallback_used="none"
  2. Prior-snapshot borrow     — pass prior_forward in config; fallback_used="prior_snapshot"
  3. Neighbor interpolation    — applied by estimate_forward_curve; fallback_used="interpolated"
  4. Mark unusable             — confidence=0, spot used as proxy; fallback_used="unusable"

Rule: every fallback is always logged and labeled — never hidden.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import statistics
from typing import Optional

from src.forwards.models import (
    CarryDiagnostics,
    ForwardCandidate,
    ForwardDiagnostics,
    ForwardResult,
)
from src.snapshots.models import MarketStateSnapshot

logger = logging.getLogger(__name__)

EPSILON = 1e-6   # Avoid division by zero in liquidity weights


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_forward(
    snapshot: MarketStateSnapshot,
    expiry_str: str,
    maturity_years: float,
    rate: float,
    config: dict,
) -> ForwardResult:
    """Estimate the forward price for one maturity from call-put parity.

    Steps:
      1. Identify eligible call-put pairs (spread ≤ max_spread_pct, both mids > 0)
      2. Compute parity forward per strike: F_i = K + e^(rT) * (C_i − P_i)
      3. Weight by liquidity: ω_i = 1 / ((spread_call + spread_put)/2 + ε)
      4. Remove outliers: robust z-score with MAD
      5. Weighted mean → chosen_forward; annotate residuals
      6. Populate ForwardDiagnostics; compute confidence score
    """
    candidates = _build_candidates(snapshot, expiry_str, maturity_years, rate, config)
    underlying_key = snapshot.underlying_state.instrument_key

    if not candidates:
        logger.warning(
            "forward.no_candidates underlying=%s expiry=%s", underlying_key, expiry_str
        )
        return _fallback_forward(snapshot, expiry_str, maturity_years, config)

    cleaned = _reject_outliers(candidates, config)
    rejected = [c for c in candidates if c not in cleaned]

    if not cleaned:
        logger.warning(
            "forward.all_rejected underlying=%s expiry=%s candidates=%d",
            underlying_key, expiry_str, len(candidates),
        )
        return _fallback_forward(snapshot, expiry_str, maturity_years, config)

    chosen, weighted_mean, median = _weighted_aggregate(cleaned)
    annotated = _annotate_residuals(cleaned, chosen)
    confidence = _compute_confidence_score(annotated, candidates)

    fwd_values = [c.forward_estimate for c in annotated]
    forward_range = max(fwd_values) - min(fwd_values) if len(fwd_values) > 1 else 0.0

    diagnostics = ForwardDiagnostics(
        candidates_accepted=annotated,
        candidates_rejected=rejected,
        weighted_mean=weighted_mean,
        median=median,
        confidence_score=confidence,
        forward_range=forward_range,
    )

    return ForwardResult(
        underlying=underlying_key,
        snapshot_ts=snapshot.snapshot_ts,
        maturity_years=maturity_years,
        expiry_str=expiry_str,
        chosen_forward=chosen,
        weighted_mean_forward=weighted_mean,
        median_forward=median,
        confidence_score=confidence,
        candidates_before_filter=len(candidates),
        candidates_after_filter=len(annotated),
        candidates=annotated,
        fallback_used="none",
        diagnostics=diagnostics,
    )


def compute_carry_diagnostics(
    forward_result: ForwardResult,
    spot: float,
    rate: float,
) -> CarryDiagnostics:
    """Implied carry/dividend yield from spot and forward.

    q(T) = r(T) − (1/T) * ln(F(T) / S0)
    """
    T = forward_result.maturity_years
    if T <= 0:
        raise ValueError(f"maturity_years must be positive, got {T}")
    implied_carry = rate - (1.0 / T) * math.log(forward_result.chosen_forward / spot)
    return CarryDiagnostics(
        underlying=forward_result.underlying,
        snapshot_ts=forward_result.snapshot_ts,
        maturity_years=T,
        rate=rate,
        spot=spot,
        forward=forward_result.chosen_forward,
        implied_carry=implied_carry,
    )


def estimate_forward_curve(
    snapshot: MarketStateSnapshot,
    maturities: list[tuple[str, float]],
    rate: float,
    config: dict,
) -> list[ForwardResult]:
    """Build the full forward curve, interpolating any gaps.

    Args:
        snapshot:   MarketStateSnapshot containing all option quotes.
        maturities: List of (expiry_str, maturity_years) pairs in any order.
        rate:       Risk-free rate (annual, continuously compounded).
        config:     forward_engine config dict.

    Returns:
        List of ForwardResult sorted by maturity_years, with unusable maturities
        replaced by neighbor-interpolated results where possible.
    """
    sorted_maturities = sorted(maturities, key=lambda x: x[1])

    # First pass — independent estimate per maturity
    first_pass: list[ForwardResult] = []
    for expiry_str, T in sorted_maturities:
        result = estimate_forward(snapshot, expiry_str, T, rate, config)
        first_pass.append(result)

    # Second pass — fill gaps by neighbor interpolation
    return _interpolate_missing(first_pass)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_candidates(
    snapshot: MarketStateSnapshot,
    expiry_str: str,
    maturity_years: float,
    rate: float,
    config: dict,
) -> list[ForwardCandidate]:
    """Build parity-forward candidates from eligible call-put pairs."""
    options = snapshot.get_options_by_expiry(expiry_str)
    calls = {
        r.strike: r
        for r in options
        if r.option_right == "C" and r.mid is not None and r.mid > 0
    }
    puts = {
        r.strike: r
        for r in options
        if r.option_right == "P" and r.mid is not None and r.mid > 0
    }

    max_spread_pct = config.get("max_spread_pct", 0.30)
    disc = math.exp(rate * maturity_years)

    candidates: list[ForwardCandidate] = []
    for strike, call_row in calls.items():
        put_row = puts.get(strike)
        if put_row is None:
            continue
        if call_row.spread_pct is None or put_row.spread_pct is None:
            continue
        if call_row.spread_pct > max_spread_pct or put_row.spread_pct > max_spread_pct:
            continue

        forward_est = strike + disc * (call_row.mid - put_row.mid)
        weight = _liquidity_weight(call_row.spread_pct, put_row.spread_pct)
        candidates.append(ForwardCandidate(
            strike=strike,
            maturity_years=maturity_years,
            call_mid=call_row.mid,
            put_mid=put_row.mid,
            forward_estimate=forward_est,
            weight=weight,
            spread_pct_call=call_row.spread_pct,
            spread_pct_put=put_row.spread_pct,
        ))
    return candidates


def _liquidity_weight(spread_pct_call: float, spread_pct_put: float) -> float:
    """ω_i = 1 / (average_spread + ε); tighter spreads get higher weight."""
    avg_spread = (spread_pct_call + spread_pct_put) / 2.0
    return 1.0 / (avg_spread + EPSILON)


def _reject_outliers(
    candidates: list[ForwardCandidate],
    config: dict,
) -> list[ForwardCandidate]:
    """Remove outliers via robust z-score with MAD.

    z_i = |F_i − median(F)| / (1.4826 * MAD(F))
    Candidates with z > max_robust_zscore are removed.
    """
    if len(candidates) < 2:
        return candidates

    max_zscore = config.get("max_robust_zscore", 3.5)
    values = [c.forward_estimate for c in candidates]
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])

    if mad < EPSILON:
        return candidates  # All values identical — no outliers to remove

    cleaned: list[ForwardCandidate] = []
    for c in candidates:
        z = abs(c.forward_estimate - med) / (1.4826 * mad)
        if z <= max_zscore:
            cleaned.append(c)
        else:
            logger.debug(
                "forward.outlier_rejected strike=%.2f forward=%.4f z=%.2f",
                c.strike, c.forward_estimate, z,
            )
    return cleaned


def _weighted_aggregate(
    candidates: list[ForwardCandidate],
) -> tuple[float, float, float]:
    """Returns (chosen_forward, weighted_mean, median).

    chosen_forward == weighted_mean.
    """
    total_weight = sum(c.weight for c in candidates)
    weighted_mean = sum(c.forward_estimate * c.weight for c in candidates) / total_weight
    median = statistics.median([c.forward_estimate for c in candidates])
    return weighted_mean, weighted_mean, median


def _annotate_residuals(
    candidates: list[ForwardCandidate],
    chosen_forward: float,
) -> list[ForwardCandidate]:
    """Return new ForwardCandidate objects with parity_residual filled in.

    parity_residual = forward_estimate − chosen_forward
    ForwardCandidate is frozen, so dataclasses.replace creates new objects.
    """
    return [
        dataclasses.replace(c, parity_residual=c.forward_estimate - chosen_forward)
        for c in candidates
    ]


def _compute_confidence_score(
    accepted: list[ForwardCandidate],
    all_candidates: list[ForwardCandidate],
) -> float:
    """Confidence score ∈ [0, 1].

    50% weight on candidate survival rate (accepted / all_before_filter).
    50% weight on how tight the accepted forwards cluster (narrower = higher).
    """
    if not accepted:
        return 0.0
    coverage = len(accepted) / max(len(all_candidates), 1)
    values = [c.forward_estimate for c in accepted]
    spread = max(values) - min(values) if len(values) > 1 else 0.0
    spread_score = 1.0 / (1.0 + spread)
    return min(1.0, 0.5 * coverage + 0.5 * spread_score)


def _fallback_forward(
    snapshot: MarketStateSnapshot,
    expiry_str: str,
    maturity_years: float,
    config: dict,
) -> ForwardResult:
    """Apply the fallback policy when no valid parity estimate is available.

    Priority:
      1. prior_snapshot: config['prior_forward'] float is provided → use it directly.
      2. unusable: no information available; spot used as a carry-free proxy.

    Neighbor interpolation is not attempted here — it requires knowledge of
    adjacent maturities and is handled by estimate_forward_curve.
    """
    underlying = snapshot.underlying_state.instrument_key

    prior = config.get("prior_forward")
    if prior is not None and isinstance(prior, (int, float)) and float(prior) > 0:
        fwd = float(prior)
        logger.info(
            "forward.fallback_prior_snapshot underlying=%s expiry=%s forward=%.4f",
            underlying, expiry_str, fwd,
        )
        return ForwardResult(
            underlying=underlying,
            snapshot_ts=snapshot.snapshot_ts,
            maturity_years=maturity_years,
            expiry_str=expiry_str,
            chosen_forward=fwd,
            weighted_mean_forward=fwd,
            median_forward=fwd,
            confidence_score=0.2,
            candidates_before_filter=0,
            candidates_after_filter=0,
            candidates=[],
            fallback_used="prior_snapshot",
        )

    # Last resort: use spot as a zero-carry proxy, confidence = 0
    spot = snapshot.underlying_state.reference_spot
    logger.warning(
        "forward.unusable underlying=%s expiry=%s using_spot=%.4f",
        underlying, expiry_str, spot,
    )
    return ForwardResult(
        underlying=underlying,
        snapshot_ts=snapshot.snapshot_ts,
        maturity_years=maturity_years,
        expiry_str=expiry_str,
        chosen_forward=spot,
        weighted_mean_forward=spot,
        median_forward=spot,
        confidence_score=0.0,
        candidates_before_filter=0,
        candidates_after_filter=0,
        candidates=[],
        fallback_used="unusable",
    )


def _interpolate_missing(results: list[ForwardResult]) -> list[ForwardResult]:
    """Replace unusable maturities with linearly-interpolated estimates.

    For each unusable slot finds the nearest valid (non-unusable) neighbor on
    each side and linearly interpolates the forward in calendar-time space.
    Only one maturity on one side → use it directly (extrapolation).
    Two valid neighbors → linear interpolation.
    No valid neighbors → leave unusable.
    """
    output = list(results)
    for i, r in enumerate(output):
        if r.fallback_used != "unusable":
            continue

        left: Optional[ForwardResult] = None
        right: Optional[ForwardResult] = None
        for j in range(i - 1, -1, -1):
            if output[j].fallback_used != "unusable":
                left = output[j]
                break
        for j in range(i + 1, len(output)):
            if output[j].fallback_used != "unusable":
                right = output[j]
                break

        if left is None and right is None:
            continue

        output[i] = _interpolate_forward(r, left, right)
    return output


def _interpolate_forward(
    target: ForwardResult,
    left: Optional[ForwardResult],
    right: Optional[ForwardResult],
) -> ForwardResult:
    """Interpolate (or extrapolate) a forward from valid neighbors."""
    T = target.maturity_years

    if left is not None and right is not None:
        tL, tR = left.maturity_years, right.maturity_years
        fL, fR = left.chosen_forward, right.chosen_forward
        alpha = (T - tL) / (tR - tL)
        forward = fL + alpha * (fR - fL)
        confidence = 0.5 * min(left.confidence_score, right.confidence_score)
    elif left is not None:
        forward = left.chosen_forward
        confidence = 0.3 * left.confidence_score
    else:
        assert right is not None
        forward = right.chosen_forward
        confidence = 0.3 * right.confidence_score

    logger.info(
        "forward.interpolated underlying=%s expiry=%s forward=%.4f confidence=%.3f",
        target.underlying, target.expiry_str, forward, confidence,
    )
    return ForwardResult(
        underlying=target.underlying,
        snapshot_ts=target.snapshot_ts,
        maturity_years=T,
        expiry_str=target.expiry_str,
        chosen_forward=forward,
        weighted_mean_forward=forward,
        median_forward=forward,
        confidence_score=confidence,
        candidates_before_filter=0,
        candidates_after_filter=0,
        candidates=[],
        fallback_used="interpolated",
    )
