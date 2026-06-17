"""
European option pricer — Black-Scholes with continuous dividend/carry yield.

All downstream analytics must call these functions rather than reimplementing
the formulas elsewhere.

Unit conventions (explicit and stable):
  delta        ∂V/∂S                  dimensionless, range [−1, 1]
  gamma        ∂²V/∂S²                per unit of S
  vega         ∂V/∂σ per 1 percentage-point move in vol (= raw_vega / 100)
  theta        ∂V/∂t per calendar day (negative for long options in normal regimes)
  dollar_gamma Γ × S² × multiplier
  dollar_vega  vega × 0.01 × multiplier  (P&L of one contract per 1 vol-point move;
               the 0.01 converts from "per 1 pct-pt" to "per 0.01 absolute σ")
  rho          ∂V/∂r  call: K·T·e^(−rT)·N(d2)   put: −K·T·e^(−rT)·N(−d2)
  dollar_rho   rho × 0.0001 × multiplier  (P&L of one contract per 1 bp rate move)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm

from src.pricing.models import PricingResult

N = norm.cdf
n = norm.pdf


@dataclass(frozen=True)
class EuropeanInputs:
    S: float           # Reference spot price
    K: float           # Strike price
    T: float           # Maturity in years (act/365)
    r: float           # Risk-free rate (continuous, annual)
    q: float           # Dividend / carry yield (continuous, annual)
    sigma: float       # Implied volatility
    option_type: str   # "C" (call) or "P" (put)
    multiplier: float = 100.0


# Backward-compatibility alias — downstream code that imports EuropeanResult still works.
EuropeanResult = PricingResult


def price_european(inputs: EuropeanInputs) -> PricingResult:
    """
    Price a European option and compute all first/second-order Greeks.

    C = S·e^(−qT)·N(d1) − K·e^(−rT)·N(d2)
    P = K·e^(−rT)·N(−d2) − S·e^(−qT)·N(−d1)
    d1 = [ln(S/K) + (r − q + σ²/2)·T] / (σ√T)
    d2 = d1 − σ√T
    """
    S, K, T, r, q, sigma = inputs.S, inputs.K, inputs.T, inputs.r, inputs.q, inputs.sigma

    if T <= 0:
        price = _intrinsic_value(S, K, inputs.option_type)
        return PricingResult(
            price=price,
            delta=_intrinsic_delta(S, K, inputs.option_type),
            gamma=0.0, vega=0.0, theta=0.0,
            dollar_gamma=0.0, dollar_vega=0.0,
            model_name="black_scholes",
            rho=0.0, dollar_rho=0.0, d1=0.0, d2=0.0,
        )

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)

    if inputs.option_type == "C":
        price = S * disc_q * N(d1) - K * disc_r * N(d2)
        delta = disc_q * N(d1)
        rho = K * T * disc_r * N(d2)
    elif inputs.option_type == "P":
        price = K * disc_r * N(-d2) - S * disc_q * N(-d1)
        delta = -disc_q * N(-d1)
        rho = -K * T * disc_r * N(-d2)
    else:
        raise ValueError(f"option_type must be 'C' or 'P', got {inputs.option_type!r}")

    gamma = disc_q * n(d1) / (S * sigma * sqrt_T)

    raw_vega = S * disc_q * n(d1) * sqrt_T    # Per 1 unit of σ (per 100% vol move)
    vega = raw_vega / 100.0                     # Per 1 percentage-point vol move

    if inputs.option_type == "C":
        theta_annual = (
            -S * disc_q * n(d1) * sigma / (2 * sqrt_T)
            - r * K * disc_r * N(d2)
            + q * S * disc_q * N(d1)
        )
    else:
        theta_annual = (
            -S * disc_q * n(d1) * sigma / (2 * sqrt_T)
            + r * K * disc_r * N(-d2)
            - q * S * disc_q * N(-d1)
        )
    theta = theta_annual / 365.0

    dollar_gamma = gamma * S ** 2 * inputs.multiplier
    dollar_vega = vega * 0.01 * inputs.multiplier
    dollar_rho = rho * 0.0001 * inputs.multiplier

    return PricingResult(
        price=price, delta=delta, gamma=gamma,
        vega=vega, theta=theta,
        dollar_gamma=dollar_gamma, dollar_vega=dollar_vega,
        model_name="black_scholes",
        rho=rho, dollar_rho=dollar_rho, d1=d1, d2=d2,
    )


@dataclass(frozen=True)
class Black76Inputs:
    F: float           # Forward price (from put-call parity or futures market)
    K: float           # Strike price
    T: float           # Maturity in years (act/365)
    r: float           # Risk-free rate (continuous, annual) — for discounting only
    sigma: float       # Implied volatility
    option_type: str   # "C" (call) or "P" (put)
    multiplier: float = 100.0


def price_black76(inputs: Black76Inputs) -> PricingResult:
    """
    Black-76 model: European option on a forward/futures contract.

    C = e^(−rT) · [F·N(d1) − K·N(d2)]
    P = e^(−rT) · [K·N(−d2) − F·N(−d1)]
    d1 = [ln(F/K) + σ²T/2] / (σ√T)
    d2 = d1 − σ√T

    Use instead of price_european when the underlying is a futures/forward
    and you want to avoid specifying a dividend yield. Delta here is
    sensitivity to the forward F (delta_F), not to spot.
    """
    F, K, T, r, sigma = inputs.F, inputs.K, inputs.T, inputs.r, inputs.sigma

    disc_r = math.exp(-r * T)

    if T <= 0:
        price = _intrinsic_value(F, K, inputs.option_type)
        return PricingResult(
            price=price,
            delta=_intrinsic_delta(F, K, inputs.option_type),
            gamma=0.0, vega=0.0, theta=0.0,
            dollar_gamma=0.0, dollar_vega=0.0,
            model_name="black76",
            rho=0.0, dollar_rho=0.0, d1=0.0, d2=0.0,
        )

    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    if inputs.option_type == "C":
        price = disc_r * (F * N(d1) - K * N(d2))
        delta = disc_r * N(d1)
        # theta: option loses value as time passes; r·price captures the funding cost
        theta_annual = -disc_r * F * n(d1) * sigma / (2 * sqrt_T) + r * price
        rho = -T * price
    elif inputs.option_type == "P":
        price = disc_r * (K * N(-d2) - F * N(-d1))
        delta = -disc_r * N(-d1)
        theta_annual = -disc_r * F * n(d1) * sigma / (2 * sqrt_T) - r * price
        rho = -T * price
    else:
        raise ValueError(f"option_type must be 'C' or 'P', got {inputs.option_type!r}")

    gamma = disc_r * n(d1) / (F * sigma * sqrt_T)

    raw_vega = disc_r * F * n(d1) * sqrt_T
    vega = raw_vega / 100.0

    theta = theta_annual / 365.0

    dollar_gamma = gamma * F ** 2 * inputs.multiplier
    dollar_vega = vega * 0.01 * inputs.multiplier
    dollar_rho = rho * 0.0001 * inputs.multiplier

    return PricingResult(
        price=price, delta=delta, gamma=gamma,
        vega=vega, theta=theta,
        dollar_gamma=dollar_gamma, dollar_vega=dollar_vega,
        model_name="black76",
        rho=rho, dollar_rho=dollar_rho, d1=d1, d2=d2,
    )


def local_pnl_approximation(
    result: PricingResult,
    dS: float,
    d_sigma_pct: float,
    dt_days: float,
) -> float:
    """
    Local PnL approximation from Greeks:
      ΔV ≈ Δ·dS + ½·Γ·dS² + vega·dσ_pct + Θ·dt

    Args:
        result:       PricingResult from price_european
        dS:           spot move in currency units
        d_sigma_pct:  vol move in percentage points (e.g. 1.0 = 1 vol point)
        dt_days:      time elapsed in calendar days
    """
    return (
        result.delta * dS
        + 0.5 * result.gamma * dS ** 2
        + result.vega * d_sigma_pct
        + result.theta * dt_days
    )


def _intrinsic_value(S: float, K: float, option_type: str) -> float:
    if option_type == "C":
        return max(S - K, 0.0)
    return max(K - S, 0.0)


def _intrinsic_delta(S: float, K: float, option_type: str) -> float:
    if option_type == "C":
        return 1.0 if S > K else 0.0
    return -1.0 if S < K else 0.0
