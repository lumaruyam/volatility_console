"""Volatility surface data models."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IVPoint:
    """One implied volatility observation in (log-moneyness, total-variance) space."""
    contract_key: str
    snapshot_ts: float
    expiry_str: str
    maturity_years: float
    strike: float
    forward: float
    log_moneyness: float          # k = ln(K/F)
    implied_vol: float
    total_variance: float         # w = σ² * T
    weight: float = 1.0           # Fit weight (e.g. 1/spread)
    qc_status: str = "usable"     # "usable" | "caution" | "reject"


@dataclass(frozen=True)
class SVIParameters:
    """SVI slice parameters: w(k) = a + b*(ρ*(k-m) + sqrt((k-m)² + σ²))

    Constraints for a valid (no-arbitrage) SVI slice:
      b >= 0
      |ρ| < 1
      σ > 0
      a + b*σ*sqrt(1 - ρ²) >= 0  (non-negative variance at ATM)
    """
    a: float      # Level
    b: float      # Angle
    rho: float    # Skew (-1 < ρ < 1)
    m: float      # Translation (ATM shift)
    sigma: float  # Curvature

    def total_variance(self, k: float) -> float:
        """Evaluate SVI total variance w(k) at log-moneyness k."""
        return self.a + self.b * (
            self.rho * (k - self.m) + math.sqrt((k - self.m) ** 2 + self.sigma ** 2)
        )

    def implied_vol(self, k: float, maturity_years: float) -> float:
        """σ_imp = sqrt(w(k) / T). Returns nan when w ≤ 0."""
        w = self.total_variance(k)
        if w <= 0 or maturity_years <= 0:
            return float("nan")
        return math.sqrt(w / maturity_years)


@dataclass
class SliceFitResult:
    """Calibration result for one maturity slice."""
    expiry_str: str
    maturity_years: float
    model: str                      # "svi" | "spline" | "failed"
    params: Optional[SVIParameters]  # None for spline / failed
    grid_log_moneyness: list[float]
    grid_total_variance: list[float]
    raw_points: list[IVPoint]
    accepted_points: list[IVPoint]
    rejected_points: list[IVPoint]
    rmse: float
    max_error: float
    n_accepted: int
    fit_warnings: list[str] = field(default_factory=list)
    quality_flag: str = "ok"        # "ok" | "high_rmse" | "sparse" | "failed"


@dataclass
class SurfaceFitResult:
    """Full fitted volatility surface across all maturities."""
    underlying: str
    snapshot_ts: float
    slices: list[SliceFitResult]
    calendar_violations: list[dict]  # {expiry_1, expiry_2, log_moneyness, variance_1, variance_2, deficit}
    model_version: str = "1.0"
