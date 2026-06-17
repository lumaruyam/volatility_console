"""
Volatility surface calibration.

Fits SVI (Stochastic Volatility Inspired) per maturity slice.
Fallback: monotone cubic spline (PCHIP) in total variance space.
Cross-maturity: linear interpolation in total variance space.

SVI: w(k) = a + b*(ρ*(k-m) + sqrt((k-m)² + σ²))

Audit rules (from PLAN):
  Never discard raw solved points after the fit.
  Store: raw_points, accepted_points, rejected_points, fit_params, grid_values.
  qc_status "caution" rows are accepted into the fit (only "reject" is excluded).
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import minimize

from src.surfaces.models import (
    IVPoint,
    SVIParameters,
    SliceFitResult,
    SurfaceFitResult,
)
from src.surfaces.interpolation import (
    evaluate_slice_variance,
    interpolate_surface_at,
    interpolate_surface_grid,
)

logger = logging.getLogger(__name__)

# Re-export models so legacy imports from this module keep working.
__all__ = [
    "IVPoint",
    "SVIParameters",
    "SliceFitResult",
    "SurfaceFitResult",
    "evaluate_slice_variance",
    "interpolate_surface_at",
    "interpolate_surface_grid",
    "fit_surface",
    "fit_slice",
    "check_calendar_monotonicity",
    "plot_slice",
]


# ---------------------------------------------------------------------------
# Surface fitting entry point
# ---------------------------------------------------------------------------

def fit_surface(
    iv_points: list[IVPoint],
    config: dict,
    underlying: str = "",
    snapshot_ts: float = 0.0,
) -> SurfaceFitResult:
    """
    Fit volatility surface across all maturities.

    Steps:
    1. Group points by maturity (expiry_str key).
    2. Fit SVI slice-by-slice (PCHIP fallback for sparse slices).
    3. Check calendar monotonicity across fitted slices.

    Both "usable" and "caution" points are accepted into fits.
    Only "reject" points are excluded and stored separately for audit.
    """
    slices_by_expiry: dict[str, list[IVPoint]] = {}
    for pt in iv_points:
        slices_by_expiry.setdefault(pt.expiry_str, []).append(pt)

    slice_results = []
    for expiry_str, pts in sorted(slices_by_expiry.items()):
        result = fit_slice(pts, expiry_str, config)
        slice_results.append(result)

    calendar_violations = check_calendar_monotonicity(slice_results, config)

    return SurfaceFitResult(
        underlying=underlying,
        snapshot_ts=snapshot_ts,
        slices=slice_results,
        calendar_violations=calendar_violations,
    )


def fit_slice(points: list[IVPoint], expiry_str: str, config: dict) -> SliceFitResult:
    """
    Fit one maturity slice.

    Accepted: qc_status in ("usable", "caution") — caution rows are usable data.
    Rejected: qc_status == "reject" — stored for audit, excluded from fit.

    Uses SVI when len(accepted) >= min_points_per_slice; PCHIP spline otherwise.
    """
    min_points = config.get("min_points_per_slice", 5)
    max_rmse = config.get("max_rmse", 0.02)

    accepted = [p for p in points if p.qc_status in ("usable", "caution")]
    rejected = [p for p in points if p.qc_status == "reject"]
    maturity_years = points[0].maturity_years if points else 0.0

    if len(accepted) < min_points:
        return SliceFitResult(
            expiry_str=expiry_str, maturity_years=maturity_years,
            model="failed", params=None,
            grid_log_moneyness=[], grid_total_variance=[],
            raw_points=points, accepted_points=accepted, rejected_points=rejected,
            rmse=float("nan"), max_error=float("nan"), n_accepted=len(accepted),
            fit_warnings=[f"Only {len(accepted)} accepted points (min={min_points})"],
            quality_flag="sparse",
        )

    k = np.array([p.log_moneyness for p in accepted])
    w = np.array([p.total_variance for p in accepted])
    weights = np.array([p.weight for p in accepted])

    # Try SVI first
    try:
        params = _fit_svi(k, w, weights, config)
        w_fitted = np.array([params.total_variance(ki) for ki in k])
        residuals = w - w_fitted
        rmse = float(np.sqrt(np.mean(residuals ** 2)))
        max_err = float(np.max(np.abs(residuals)))

        if rmse > max_rmse:
            logger.warning("SVI RMSE %.4f > threshold %.4f for %s",
                           rmse, max_rmse, expiry_str)

        grid_k, grid_w = _build_grid(params, config)
        return SliceFitResult(
            expiry_str=expiry_str, maturity_years=maturity_years,
            model="svi", params=params,
            grid_log_moneyness=grid_k, grid_total_variance=grid_w,
            raw_points=points, accepted_points=accepted, rejected_points=rejected,
            rmse=rmse, max_error=max_err, n_accepted=len(accepted),
            quality_flag="ok" if rmse <= max_rmse else "high_rmse",
        )
    except Exception as exc:
        logger.warning("SVI failed for %s: %s — falling back to spline", expiry_str, exc)
        return _fit_spline_fallback(
            points, accepted, rejected, expiry_str, maturity_years, config
        )


# ---------------------------------------------------------------------------
# SVI calibration
# ---------------------------------------------------------------------------

def _fit_svi(
    k: np.ndarray,
    w: np.ndarray,
    weights: np.ndarray,
    config: dict,
) -> SVIParameters:
    """
    Calibrate SVI via weighted least squares.
    w(k) = a + b*(ρ*(k-m) + sqrt((k-m)² + σ²))

    Constraints: b ≥ 0, |ρ| < 1, σ > 0, a + b*σ*sqrt(1-ρ²) ≥ 0.
    """
    def objective(params: np.ndarray) -> float:
        a, b, rho, m, sigma = params
        if b < 0 or abs(rho) >= 1 or sigma <= 0:
            return 1e10
        w_fit = a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + sigma ** 2))
        return float(np.sum(weights * (w - w_fit) ** 2))

    w_med = float(np.median(w))
    x0 = [w_med, 0.1, -0.2, 0.0, 0.1]
    bounds = [(0, None), (0, None), (-0.999, 0.999), (-1.0, 1.0), (1e-4, None)]

    result = minimize(
        objective, x0, bounds=bounds, method="L-BFGS-B",
        options={"maxiter": 1000, "ftol": 1e-10},
    )
    if not result.success:
        raise RuntimeError(f"SVI optimization failed: {result.message}")

    a, b, rho, m, sigma = result.x
    return SVIParameters(a=float(a), b=float(b), rho=float(rho),
                         m=float(m), sigma=float(sigma))


# ---------------------------------------------------------------------------
# Spline fallback
# ---------------------------------------------------------------------------

def _fit_spline_fallback(
    all_points: list[IVPoint],
    accepted: list[IVPoint],
    rejected: list[IVPoint],
    expiry_str: str,
    maturity_years: float,
    config: dict,
) -> SliceFitResult:
    """PCHIP monotone cubic spline fallback for slices too sparse for SVI."""
    if len(accepted) < 2:
        return SliceFitResult(
            expiry_str=expiry_str, maturity_years=maturity_years,
            model="failed", params=None,
            grid_log_moneyness=[], grid_total_variance=[],
            raw_points=all_points, accepted_points=accepted, rejected_points=rejected,
            rmse=float("nan"), max_error=float("nan"), n_accepted=len(accepted),
            fit_warnings=["Insufficient points for spline fallback"],
            quality_flag="failed",
        )

    k = np.array([p.log_moneyness for p in accepted])
    w = np.array([p.total_variance for p in accepted])
    sort_idx = np.argsort(k)
    spline = PchipInterpolator(k[sort_idx], w[sort_idx])

    n_grid = config.get("grid_n_points", 50)
    grid_k_arr = np.linspace(float(np.min(k)), float(np.max(k)), n_grid)
    grid_w_arr = np.maximum(spline(grid_k_arr), 0.0)

    w_fitted = spline(k)
    residuals = w - w_fitted
    rmse = float(np.sqrt(np.mean(residuals ** 2)))
    max_err = float(np.max(np.abs(residuals)))

    return SliceFitResult(
        expiry_str=expiry_str, maturity_years=maturity_years,
        model="spline", params=None,
        grid_log_moneyness=list(grid_k_arr),
        grid_total_variance=list(grid_w_arr),
        raw_points=all_points, accepted_points=accepted, rejected_points=rejected,
        rmse=rmse, max_error=max_err, n_accepted=len(accepted),
        fit_warnings=["SVI failed; spline fallback used"],
        quality_flag="sparse",
    )


# ---------------------------------------------------------------------------
# Grid construction
# ---------------------------------------------------------------------------

def _build_grid(params: SVIParameters, config: dict) -> tuple[list[float], list[float]]:
    """Build a regularized k-grid of total variance values from SVI parameters."""
    k_min = config.get("grid_k_min", -1.5)
    k_max = config.get("grid_k_max", 1.5)
    n_points = config.get("grid_n_points", 100)
    grid_k = list(np.linspace(k_min, k_max, n_points))
    grid_w = [max(params.total_variance(k), 0.0) for k in grid_k]
    return grid_k, grid_w


# ---------------------------------------------------------------------------
# Calendar monotonicity diagnostic
# ---------------------------------------------------------------------------

def check_calendar_monotonicity(
    slices: list[SliceFitResult],
    config: dict,
) -> list[dict]:
    """
    Check ∂w(k,T)/∂T ≥ 0 — total variance must be non-decreasing with maturity.

    Returns a list of violation dicts for the QC report. Each dict has:
      expiry_1, expiry_2, log_moneyness, variance_1, variance_2, deficit
    """
    k_check = config.get("calendar_check_moneyness", [-0.5, 0.0, 0.5])
    tolerance = config.get("calendar_tolerance", 1e-6)

    valid = [
        (s.maturity_years, s)
        for s in slices
        if s.model != "failed"
    ]
    valid.sort(key=lambda x: x[0])

    violations = []
    for i in range(1, len(valid)):
        T_prev, s_prev = valid[i - 1]
        T_curr, s_curr = valid[i]
        for k in k_check:
            w_prev = evaluate_slice_variance(s_prev, k)
            w_curr = evaluate_slice_variance(s_curr, k)
            if w_prev is None or w_curr is None:
                continue
            if w_curr < w_prev - tolerance:
                violations.append({
                    "expiry_1": s_prev.expiry_str,
                    "expiry_2": s_curr.expiry_str,
                    "log_moneyness": k,
                    "variance_1": w_prev,
                    "variance_2": w_curr,
                    "deficit": w_prev - w_curr,
                })
    return violations


# ---------------------------------------------------------------------------
# Plotting helper
# ---------------------------------------------------------------------------

def plot_slice(
    slice_result: SliceFitResult,
    config: dict,
    title: Optional[str] = None,
):
    """
    Plot fitted total variance and raw IV points for one maturity slice.

    Returns a matplotlib Figure. Caller is responsible for plt.show() / saving.

    Raises ImportError when matplotlib is not installed.
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ImportError(
            "matplotlib is required for plot_slice. "
            "Install it with: pip install matplotlib"
        ) from exc

    fig, ax = plt.subplots(figsize=(9, 5))

    # Plot accepted and rejected raw points
    if slice_result.accepted_points:
        ax.scatter(
            [p.log_moneyness for p in slice_result.accepted_points],
            [p.total_variance for p in slice_result.accepted_points],
            color="steelblue", s=40, zorder=5, label="accepted",
        )
    if slice_result.rejected_points:
        ax.scatter(
            [p.log_moneyness for p in slice_result.rejected_points],
            [p.total_variance for p in slice_result.rejected_points],
            color="tomato", s=40, marker="x", zorder=5, label="rejected",
        )

    # Plot fitted model
    if slice_result.grid_log_moneyness and slice_result.grid_total_variance:
        label = f"{slice_result.model} fit (RMSE={slice_result.rmse:.5f})"
        ax.plot(
            slice_result.grid_log_moneyness,
            slice_result.grid_total_variance,
            color="black", linewidth=1.5, label=label,
        )

    ax.set_xlabel("Log-moneyness k = ln(K/F)")
    ax.set_ylabel("Total variance w = σ²T")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    plot_title = title or (
        f"{slice_result.expiry_str} (T={slice_result.maturity_years:.2f}y) "
        f"— {slice_result.model.upper()} | {slice_result.quality_flag}"
    )
    ax.set_title(plot_title)
    fig.tight_layout()
    return fig
