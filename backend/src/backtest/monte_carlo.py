"""
GBM Monte Carlo simulation.

Generates n_paths terminal return samples over a 1-year horizon using
Geometric Brownian Motion, then computes the 95th-percentile loss (VaR).
"""

from __future__ import annotations

import math
import time

import numpy as np

_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 300.0


def run_monte_carlo(
    n_paths: int = 500,
    strategy_id: str = "VOL_CARRY_01",
) -> dict:
    """
    Returns:
        simulation_path_terminal_returns: list of n_paths terminal returns (as %)
        var_95_pct: 5th-percentile terminal return (i.e., 95% VaR, as %)
    """
    cache_key = f"{n_paths}:{strategy_id}"
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]

    # GBM parameters — strategy-specific drift/vol
    params = {
        "VOL_CARRY_01":   (0.12, 0.18),   # (ann_mu, ann_sigma)
        "SX5E_STRADDLE":  (0.10, 0.20),
        "DISPERSION_Q3":  (0.09, 0.15),
    }
    ann_mu, ann_sig = params.get(strategy_id, (0.12, 0.18))

    T_years = 1.0                      # 1-year horizon
    rng = np.random.default_rng(seed=abs(hash(strategy_id)) % 2**31)

    # Terminal log-return under GBM: ln(S_T/S_0) ~ N((μ-½σ²)·T, σ²·T)
    drift   = (ann_mu - 0.5 * ann_sig**2) * T_years
    diffuse = ann_sig * math.sqrt(T_years) * rng.standard_normal(n_paths)
    terminal_log_rets = drift + diffuse
    terminal_rets_pct = (np.exp(terminal_log_rets) - 1.0) * 100.0

    var_95_pct = float(np.percentile(terminal_rets_pct, 5))

    result = {
        "simulation_path_terminal_returns": [round(float(r), 2) for r in terminal_rets_pct],
        "var_95_pct": round(var_95_pct, 2),
    }
    _CACHE[cache_key] = (result, now + _CACHE_TTL)
    return result
