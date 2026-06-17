"""
American option pricer — CRR (Cox-Ross-Rubinstein) binomial tree.

Backward induction:
  V_{n,j} = max(Φ(S_{n,j}), e^(-r·Δt) · [p·V_{n+1,j+1} + (1-p)·V_{n+1,j}])

where:
  Φ(S) = intrinsic value (early exercise)
  p    = (e^((r-q)·Δt) - d) / (u - d)   risk-neutral up probability
  u    = e^(σ√Δt),  d = 1/u

Greeks computed via finite difference on the tree output.

Unit conventions (match european.py):
  vega         ∂V/∂σ per 1 percentage-point vol move (FD bump ÷ bump_size ÷ 100)
  dollar_vega  vega × 0.01 × multiplier  (P&L of one contract per 1 vol-point move)

Benchmark: American call with q=0 must match European price (no early exercise
premium). American put always ≥ European put.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from src.pricing.models import PricingResult


@dataclass(frozen=True)
class AmericanInputs:
    S: float
    K: float
    T: float
    r: float
    q: float
    sigma: float
    option_type: str     # "C" or "P"
    n_steps: int = 200
    multiplier: float = 100.0


# Backward-compatibility alias.
AmericanResult = PricingResult


def price_american(inputs: AmericanInputs) -> PricingResult:
    """
    Price an American option using the CRR binomial tree.

    Finite-difference Greeks (bump-and-reprice on the same tree):
      Δ = (V(S+δS) − V(S−δS)) / (2·δS)
      Γ = (V(S+δS) − 2V(S) + V(S−δS)) / δS²
      Θ = (V(T−δt) − V(T)) / δt  [per calendar day]
      ν = (V(σ+δσ) − V(σ)) / δσ   → normalized per 1 percentage-point vol move
    """
    price = _crr_tree(
        inputs.S, inputs.K, inputs.T, inputs.r, inputs.q,
        inputs.sigma, inputs.option_type, inputs.n_steps, american=True,
    )

    dS = inputs.S * 0.001
    dt = 1.0 / 365.0
    dsigma = 0.001  # 0.1 percentage point

    price_up = _crr_tree(inputs.S + dS, inputs.K, inputs.T, inputs.r, inputs.q,
                          inputs.sigma, inputs.option_type, inputs.n_steps, american=True)
    price_dn = _crr_tree(inputs.S - dS, inputs.K, inputs.T, inputs.r, inputs.q,
                          inputs.sigma, inputs.option_type, inputs.n_steps, american=True)
    price_fwd = _crr_tree(inputs.S, inputs.K, max(inputs.T - dt, 1e-6), inputs.r, inputs.q,
                           inputs.sigma, inputs.option_type, inputs.n_steps, american=True)
    price_vega_bump = _crr_tree(inputs.S, inputs.K, inputs.T, inputs.r, inputs.q,
                                 inputs.sigma + dsigma, inputs.option_type, inputs.n_steps,
                                 american=True)

    delta = (price_up - price_dn) / (2 * dS)
    gamma = (price_up - 2 * price + price_dn) / (dS ** 2)
    theta = (price_fwd - price) / dt / 365.0   # per calendar day
    vega = (price_vega_bump - price) / dsigma / 100.0  # per 1 percentage-point vol move

    dollar_gamma = gamma * inputs.S ** 2 * inputs.multiplier
    dollar_vega = vega * 0.01 * inputs.multiplier

    return PricingResult(
        price=price, delta=delta, gamma=gamma,
        vega=vega, theta=theta,
        dollar_gamma=dollar_gamma, dollar_vega=dollar_vega,
        model_name="crr_binomial",
        n_steps=inputs.n_steps,
    )


def _crr_tree(
    S: float, K: float, T: float, r: float, q: float,
    sigma: float, option_type: str, n_steps: int, american: bool = True,
) -> float:
    """CRR binomial tree. american=False gives European price (for convergence tests)."""
    if T <= 0:
        return max(S - K, 0.0) if option_type == "C" else max(K - S, 0.0)

    dt = T / n_steps
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    disc = math.exp(-r * dt)
    p = (math.exp((r - q) * dt) - d) / (u - d)
    q_prob = 1.0 - p

    j_arr = np.arange(n_steps + 1)
    S_T = S * (u ** (n_steps - j_arr)) * (d ** j_arr)

    if option_type == "C":
        V = np.maximum(S_T - K, 0.0)
    else:
        V = np.maximum(K - S_T, 0.0)

    for step in range(n_steps - 1, -1, -1):
        j_arr_step = np.arange(step + 1)
        S_step = S * (u ** (step - j_arr_step)) * (d ** j_arr_step)
        V = disc * (p * V[:step + 1] + q_prob * V[1:step + 2])
        if american:
            intrinsic = (np.maximum(S_step - K, 0.0) if option_type == "C"
                         else np.maximum(K - S_step, 0.0))
            V = np.maximum(V, intrinsic)

    return float(V[0])
