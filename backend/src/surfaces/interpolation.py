"""
Cross-maturity interpolation in total-variance space.

Convention
----------
All interpolation happens in (T, k) → w space where w = σ² * T.
This is the natural space for arbitrage-free surface construction: calendar
monotonicity is expressed simply as w(k, T1) ≤ w(k, T2) for T1 < T2.

interpolate_surface_at():
  Linear interpolation in T between the two neighboring calibrated slices.
  For points outside the calibrated maturity range, the nearest slice is used
  (flat extrapolation) — this is conservative and avoids negative-variance blow-up.

_evaluate_slice_variance():
  Evaluates total variance at a target k for any slice model:
    SVI  — via SVIParameters.total_variance(k)
    spline — via PCHIP interpolation of the stored grid (None if k outside grid range)
    failed — returns None (caller must handle)
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from scipy.interpolate import PchipInterpolator

from src.surfaces.models import SliceFitResult


# ---------------------------------------------------------------------------
# Slice-level evaluation (model-agnostic)
# ---------------------------------------------------------------------------

def evaluate_slice_variance(slice_result: SliceFitResult, k: float) -> Optional[float]:
    """
    Return total variance at log-moneyness k for any slice type.

    Returns None when the slice has no valid model (failed) or when k is
    outside the grid range of a spline slice.
    """
    if slice_result.model == "failed":
        return None

    if slice_result.params is not None:
        # SVI — analytic evaluation, always defined
        w = slice_result.params.total_variance(k)
        return max(w, 0.0)

    # Spline fallback — interpolate from stored grid
    grid_k = slice_result.grid_log_moneyness
    grid_w = slice_result.grid_total_variance
    if not grid_k:
        return None
    k_min, k_max = grid_k[0], grid_k[-1]
    if k < k_min or k > k_max:
        return None
    interp = PchipInterpolator(np.array(grid_k), np.array(grid_w))
    return float(max(interp(k), 0.0))


# ---------------------------------------------------------------------------
# Cross-maturity interpolation
# ---------------------------------------------------------------------------

def interpolate_surface_at(
    slices: list[SliceFitResult],
    maturity_years: float,
    log_moneyness: float,
) -> Optional[float]:
    """
    Interpolate total variance at (maturity_years, log_moneyness).

    Algorithm
    ---------
    1. Filter to slices that have a valid model ("svi" or "spline").
    2. Sort by maturity.
    3. Find the nearest neighbor(s):
       - Exact match within 1e-6 → return that slice's value.
       - Inside the range → linear interpolation between adjacent slices.
       - Outside the range → use the nearest endpoint (flat extrapolation).
    4. Returns None only when no valid slice exists at all.

    Args:
        slices: All calibrated SliceFitResult objects (any order).
        maturity_years: Target maturity in years.
        log_moneyness: Target log-moneyness k = ln(K/F).

    Returns:
        Total variance w at (T, k), or None when interpolation is impossible.
    """
    valid = [
        (s.maturity_years, s)
        for s in slices
        if s.model != "failed"
    ]
    if not valid:
        return None

    valid.sort(key=lambda x: x[0])

    # Exact match
    for T, s in valid:
        if abs(T - maturity_years) < 1e-6:
            return evaluate_slice_variance(s, log_moneyness)

    T_min, s_min = valid[0]
    T_max, s_max = valid[-1]

    # Flat extrapolation below the shortest maturity
    if maturity_years <= T_min:
        return evaluate_slice_variance(s_min, log_moneyness)

    # Flat extrapolation beyond the longest maturity
    if maturity_years >= T_max:
        return evaluate_slice_variance(s_max, log_moneyness)

    # Find bracketing neighbors
    left_T, left_s = valid[0]
    right_T, right_s = valid[1]
    for i in range(1, len(valid)):
        T_i, s_i = valid[i]
        if T_i > maturity_years:
            right_T, right_s = T_i, s_i
            left_T, left_s = valid[i - 1]
            break

    w_left = evaluate_slice_variance(left_s, log_moneyness)
    w_right = evaluate_slice_variance(right_s, log_moneyness)

    if w_left is None and w_right is None:
        return None
    if w_left is None:
        return w_right
    if w_right is None:
        return w_left

    # Linear interpolation in variance space
    lam = (maturity_years - left_T) / (right_T - left_T)
    return (1 - lam) * w_left + lam * w_right


def interpolate_surface_grid(
    slices: list[SliceFitResult],
    maturity_grid: list[float],
    k_grid: list[float],
) -> list[list[Optional[float]]]:
    """
    Evaluate total variance over a 2-D grid of (maturity, k) pairs.

    Returns a 2-D list `result[i][j]` = w at (maturity_grid[i], k_grid[j]).
    None entries indicate missing model coverage.
    """
    return [
        [interpolate_surface_at(slices, T, k) for k in k_grid]
        for T in maturity_grid
    ]
