"""IV solver data models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PricingInputs:
    """Market and contract inputs required for Black-Scholes pricing/IV inversion."""
    S: float          # Reference spot price
    K: float          # Strike price
    T: float          # Time to expiry in years (act/365)
    r: float          # Risk-free rate (continuous, annual)
    q: float          # Dividend / carry yield (continuous, annual)
    option_type: str  # "C" (call) or "P" (put)


@dataclass(frozen=True)
class IvSolveResult:
    """Full diagnostic output of one implied-volatility solve.

    Fields
    ------
    implied_vol     Solved IV in annualized decimal form (e.g. 0.20 = 20 vol).
                    None when converged=False.
    converged       True only when the solver found a root within tolerance.
    iterations      Number of objective evaluations performed.
    residual        |BS_price(iv) − market_price| at the solution; nan on failure.
    lower_bound     Left bracket (vol units) used by the solver.
    upper_bound     Right bracket (vol units) used by the solver.
    failure_reason  Stable uppercase code when converged=False, e.g.:
                      "BELOW_INTRINSIC"       market_price < intrinsic value
                      "ABOVE_THEORETICAL_MAX" market_price > discounted forward
                      "BRACKET_FAILED"        objective has same sign at both bounds
                      "SOLVER_EXCEPTION:..."  unexpected numerical error
                    None when converged=True.
    model_name      "black_scholes" | "bs_american_proxy"
    """
    contract_key: str
    snapshot_ts: float
    market_price: float
    implied_vol: Optional[float]
    converged: bool
    iterations: int
    residual: float
    lower_bound: float
    upper_bound: float
    failure_reason: Optional[str]
    model_name: str = "black_scholes"
    model_version: str = "1.0"
