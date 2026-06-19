"""Basket variance identity — PDF Part II Equation 23.

   sigma2_basket = sum_ij w_i * w_j * sigma_i * sigma_j * rho_ij
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class BasketVarianceResult:
    basket_variance: float
    basket_vol: float
    weighted_component_vars: List[float]
    residual_vs_atm: float   # basket_vol - index_atm_vol (0.0 when index_atm_vol not supplied)
    avg_corr_used: float
    n_constituents: int


def compute_basket_variance(
    weights: List[float],
    vols: List[float],
    corr_matrix: Optional[List[List[float]]] = None,
    avg_corr: Optional[float] = None,
    index_atm_vol: Optional[float] = None,
) -> BasketVarianceResult:
    """Compute basket variance using the correlation-weighted identity.

    Args:
        weights:      Portfolio weights (must sum to 1.0 for well-formed basket).
        vols:         Implied or realised vols per constituent (same length as weights).
        corr_matrix:  Full n×n correlation matrix.  Mutually exclusive with avg_corr.
        avg_corr:     Scalar average pairwise correlation applied to all off-diagonal
                      cells when corr_matrix is omitted.  Defaults to 0.5.
        index_atm_vol: ATM vol of the basket index for dispersion-premium residual.
    """
    n = len(weights)
    if len(vols) != n:
        raise ValueError(f"weights ({n}) and vols ({len(vols)}) must have the same length")

    if corr_matrix is not None:
        if len(corr_matrix) != n or any(len(row) != n for row in corr_matrix):
            raise ValueError("corr_matrix must be n×n where n = len(weights)")
        off_diag = [corr_matrix[i][j] for i in range(n) for j in range(n) if i != j]
        avg_corr_used = sum(off_diag) / len(off_diag) if off_diag else 1.0
    else:
        rho = avg_corr if avg_corr is not None else 0.5
        corr_matrix = [[1.0 if i == j else rho for j in range(n)] for i in range(n)]
        avg_corr_used = rho

    basket_var = sum(
        weights[i] * weights[j] * vols[i] * vols[j] * corr_matrix[i][j]
        for i in range(n)
        for j in range(n)
    )
    basket_vol = math.sqrt(max(0.0, basket_var))

    return BasketVarianceResult(
        basket_variance=basket_var,
        basket_vol=basket_vol,
        weighted_component_vars=[w * w * v * v for w, v in zip(weights, vols)],
        residual_vs_atm=basket_vol - index_atm_vol if index_atm_vol is not None else 0.0,
        avg_corr_used=avg_corr_used,
        n_constituents=n,
    )
