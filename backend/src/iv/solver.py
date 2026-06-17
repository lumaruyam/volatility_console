"""
Implied volatility inversion engine.

Uses a bracketed root solver (Brent's method) as the primary safe path.
Scalar path is maximally readable and covered by tests.
Vectorized batch wrapper calls the scalar path.

Failed solves return structured diagnostics — never silent NaN.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from scipy.optimize import brentq
from scipy.stats import norm

from src.iv.models import IvSolveResult, PricingInputs

logger = logging.getLogger(__name__)

N = norm.cdf    # Standard normal CDF


# ---------------------------------------------------------------------------
# European pricer (Black-Scholes with carry)
# ---------------------------------------------------------------------------

def bs_price(S: float, K: float, T: float, r: float, q: float,
             sigma: float, option_type: str) -> float:
    """
    Black-Scholes price with continuous dividend/carry yield q.

    C = S*e^(-qT)*N(d1) - K*e^(-rT)*N(d2)
    P = K*e^(-rT)*N(-d2) - S*e^(-qT)*N(-d1)
    d1 = [ln(S/K) + (r - q + σ²/2)*T] / (σ√T)
    d2 = d1 - σ√T
    """
    if T <= 0:
        return _intrinsic_value(S, K, option_type)
    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma}")

    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == "C":
        return S * math.exp(-q * T) * N(d1) - K * math.exp(-r * T) * N(d2)
    elif option_type == "P":
        return K * math.exp(-r * T) * N(-d2) - S * math.exp(-q * T) * N(-d1)
    else:
        raise ValueError(f"option_type must be 'C' or 'P', got {option_type!r}")


def bs_vega(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """Vega = ∂V/∂σ = S * e^(-qT) * N'(d1) * √T"""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return S * math.exp(-q * T) * norm.pdf(d1) * math.sqrt(T)


# ---------------------------------------------------------------------------
# IV solver — scalar (primary safe path)
# ---------------------------------------------------------------------------

def solve_iv(market_price: float, inputs: PricingInputs, config: dict,
             contract_key: str = "", snapshot_ts: float = 0.0) -> IvSolveResult:
    """
    Solve for implied volatility from a market price using Brent's method.

    Pre-checks intrinsic and no-arbitrage bounds before entering the root finder.
    Returns structured diagnostics even on success.

    Args:
        market_price: Observed option mid price
        inputs: PricingInputs (S, K, T, r, q, option_type)
        config: iv_solver config dict (lower_vol, upper_vol, price_tolerance, max_iterations)
        contract_key: For diagnostics
        snapshot_ts: For diagnostics

    Returns:
        IvSolveResult with full convergence metadata
    """
    lower_vol = config.get("lower_vol", 0.0001)
    upper_vol = config.get("upper_vol", 5.0)
    tol = config.get("price_tolerance", 1e-6)
    max_iter = config.get("max_iterations", 100)

    intrinsic = _intrinsic_value(inputs.S, inputs.K, inputs.option_type)
    if market_price < intrinsic - tol:
        return IvSolveResult(
            contract_key=contract_key, snapshot_ts=snapshot_ts,
            market_price=market_price, implied_vol=None,
            converged=False, iterations=0, residual=float("nan"),
            lower_bound=lower_vol, upper_bound=upper_vol,
            failure_reason="BELOW_INTRINSIC",
        )

    max_price = _theoretical_max(inputs)
    if market_price > max_price + tol:
        return IvSolveResult(
            contract_key=contract_key, snapshot_ts=snapshot_ts,
            market_price=market_price, implied_vol=None,
            converged=False, iterations=0, residual=float("nan"),
            lower_bound=lower_vol, upper_bound=upper_vol,
            failure_reason="ABOVE_THEORETICAL_MAX",
        )

    iteration_count = [0]

    def objective(sigma: float) -> float:
        iteration_count[0] += 1
        return bs_price(inputs.S, inputs.K, inputs.T, inputs.r, inputs.q,
                        sigma, inputs.option_type) - market_price

    try:
        f_low = objective(lower_vol)
        f_high = objective(upper_vol)
        if f_low * f_high > 0:
            return IvSolveResult(
                contract_key=contract_key, snapshot_ts=snapshot_ts,
                market_price=market_price, implied_vol=None,
                converged=False, iterations=iteration_count[0], residual=float("nan"),
                lower_bound=lower_vol, upper_bound=upper_vol,
                failure_reason="BRACKET_FAILED",
            )

        iv = brentq(objective, lower_vol, upper_vol, xtol=1e-8, rtol=tol, maxiter=max_iter)
        residual = abs(objective(iv))
        return IvSolveResult(
            contract_key=contract_key, snapshot_ts=snapshot_ts,
            market_price=market_price, implied_vol=iv,
            converged=True, iterations=iteration_count[0], residual=residual,
            lower_bound=lower_vol, upper_bound=upper_vol,
            failure_reason=None,
        )
    except Exception as exc:
        return IvSolveResult(
            contract_key=contract_key, snapshot_ts=snapshot_ts,
            market_price=market_price, implied_vol=None,
            converged=False, iterations=iteration_count[0], residual=float("nan"),
            lower_bound=lower_vol, upper_bound=upper_vol,
            failure_reason=f"SOLVER_EXCEPTION:{exc}",
        )


def solve_iv_american_proxy(
    market_price: float,
    inputs: PricingInputs,
    config: dict,
    contract_key: str = "",
    snapshot_ts: float = 0.0,
) -> IvSolveResult:
    """
    American option proxy IV via European Black-Scholes inversion.

    Convention
    ----------
    We invert the European Black-Scholes price to recover a single implied
    volatility number even for American-style contracts.  This is the standard
    convention on vol desks for two reasons:

    1.  European index options (Euro Stoxx 50, SPX) — exact, since those
        products are always European-style and have no early exercise.
    2.  American equity options — the resulting IV absorbs the early exercise
        premium into the vol number.  For OTM options the premium is small and
        the error is negligible.  For deep-ITM puts or calls on high-dividend
        stocks the absorbed premium can be 1–3 vol points; in those regimes a
        full American pricer inversion (Barone-Adesi-Whaley, Binomial CRR) is
        more accurate.  We document the deviation rather than hide it.

    Concretely:
    - American call, q=0: European price = American price (no early exercise).
    - American put, q>0 call: slight overestimate of "true" American IV because
      European price ≤ American price, so inverting European formula yields a
      higher IV than necessary to replicate the market price in a full model.

    The result is tagged model_name="bs_american_proxy" so downstream consumers
    can filter by model name if they need precision.
    """
    result = solve_iv(
        market_price=market_price,
        inputs=inputs,
        config=config,
        contract_key=contract_key,
        snapshot_ts=snapshot_ts,
    )
    # Repackage with the proxy model name — all other fields identical.
    return IvSolveResult(
        contract_key=result.contract_key,
        snapshot_ts=result.snapshot_ts,
        market_price=result.market_price,
        implied_vol=result.implied_vol,
        converged=result.converged,
        iterations=result.iterations,
        residual=result.residual,
        lower_bound=result.lower_bound,
        upper_bound=result.upper_bound,
        failure_reason=result.failure_reason,
        model_name="bs_american_proxy",
        model_version=result.model_version,
    )


def solve_iv_batch(records: list[dict], config: dict) -> list[IvSolveResult]:
    """
    Vectorized wrapper: solve IV for an entire option chain.
    Each record: {market_price, S, K, T, r, q, option_type, contract_key, snapshot_ts}
    Optional field: american_proxy=True to use the American proxy convention.
    """
    results = []
    for rec in records:
        inputs = PricingInputs(
            S=rec["S"], K=rec["K"], T=rec["T"],
            r=rec["r"], q=rec["q"], option_type=rec["option_type"],
        )
        fn = solve_iv_american_proxy if rec.get("american_proxy") else solve_iv
        result = fn(
            market_price=rec["market_price"],
            inputs=inputs,
            config=config,
            contract_key=rec.get("contract_key", ""),
            snapshot_ts=rec.get("snapshot_ts", 0.0),
        )
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Log-moneyness and total variance helpers
# ---------------------------------------------------------------------------

def log_moneyness(K: float, forward: float) -> float:
    """k = ln(K / F(T))"""
    return math.log(K / forward)


def total_variance(implied_vol: float, maturity_years: float) -> float:
    """w(k, T) = σ_imp² * T"""
    return implied_vol ** 2 * maturity_years


def iv_from_total_variance(total_var: float, maturity_years: float) -> float:
    """σ_imp = sqrt(w / T)"""
    if maturity_years <= 0:
        raise ValueError("maturity_years must be positive")
    return math.sqrt(total_var / maturity_years)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _intrinsic_value(S: float, K: float, option_type: str) -> float:
    if option_type == "C":
        return max(S - K, 0.0)
    elif option_type == "P":
        return max(K - S, 0.0)
    raise ValueError(f"option_type must be 'C' or 'P', got {option_type!r}")


def _theoretical_max(inputs: PricingInputs) -> float:
    """Upper bound: discounted forward for call, discounted strike for put."""
    if inputs.option_type == "C":
        return inputs.S * math.exp(-inputs.q * inputs.T)
    return inputs.K * math.exp(-inputs.r * inputs.T)
