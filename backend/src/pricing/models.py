"""Unified pricing result model shared by European and American pricers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PricingResult:
    """Option pricing output from any model.

    Field conventions (stable across model_name values):
      price        — option fair value in currency units
      delta        — ∂V/∂S              dimensionless, range [−1, 1]
      gamma        — ∂²V/∂S²            per unit of S, always ≥ 0
      vega         — ∂V/∂σ per 1 percentage-point move in vol  (= raw_vega / 100)
      theta        — ∂V/∂t per calendar day                     typically < 0
      dollar_gamma — Γ × S² × multiplier
      dollar_vega  — vega × 0.01 × multiplier  (P&L per contract per 1 vol-point move)
      model_name   — "black_scholes" | "crr_binomial"

    Optional fields (None when not applicable to the model):
      rho          — ∂V/∂r  (European BS only)
                     call: K·T·e^(-rT)·N(d2)   put: −K·T·e^(-rT)·N(−d2)
      dollar_rho   — rho × 0.0001 × multiplier  (P&L per contract per 1 bp rate move)
      d1, d2       — BS intermediate values (European only)
      n_steps      — binomial tree depth (American only)
    """
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    dollar_gamma: float
    dollar_vega: float
    model_name: str
    rho: Optional[float] = None
    dollar_rho: Optional[float] = None
    d1: Optional[float] = None
    d2: Optional[float] = None
    n_steps: Optional[int] = None
