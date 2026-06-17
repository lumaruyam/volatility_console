"""Forward curve data models."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ForwardCandidate:
    """One parity-based forward estimate from a single call-put pair."""
    strike: float
    maturity_years: float
    call_mid: float
    put_mid: float
    forward_estimate: float     # K + e^(rT) * (C_mid - P_mid)
    weight: float               # 1 / (SpreadPct + epsilon)
    spread_pct_call: float
    spread_pct_put: float
    parity_residual: Optional[float] = None  # Residual vs chosen forward
    quality_flag: str = "ok"    # "ok" | "outlier" | "illiquid"


@dataclass(frozen=True)
class ForwardDiagnostics:
    """Full audit trail for one maturity's forward estimate.

    Separates accepted vs rejected candidates and exposes all intermediate
    quantities so operators can inspect every step of the aggregation.
    """
    candidates_accepted: list[ForwardCandidate]
    candidates_rejected: list[ForwardCandidate]
    weighted_mean: float
    median: float
    confidence_score: float
    forward_range: float        # max − min of accepted forward estimates; 0 when single candidate


@dataclass(frozen=True)
class ForwardResult:
    """Chosen forward for one maturity, with full diagnostics."""
    underlying: str
    snapshot_ts: float
    maturity_years: float
    expiry_str: str
    chosen_forward: float
    weighted_mean_forward: float
    median_forward: float
    confidence_score: float     # 0.0 (poor) to 1.0 (high)
    candidates_before_filter: int
    candidates_after_filter: int
    candidates: list[ForwardCandidate] = field(default_factory=list)
    fallback_used: str = "none"  # "none" | "interpolated" | "prior_snapshot" | "unusable"
    diagnostics_version: str = "1.0"
    diagnostics: Optional[ForwardDiagnostics] = None


@dataclass(frozen=True)
class CarryDiagnostics:
    """Implied carry/dividend yield derived from spot and forward."""
    underlying: str
    snapshot_ts: float
    maturity_years: float
    rate: float
    spot: float
    forward: float
    implied_carry: float   # q(T) = r(T) - (1/T) * ln(F(T)/S0)
