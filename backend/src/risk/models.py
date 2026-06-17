"""Risk data models for Step 11."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    """A single options position in the portfolio."""
    portfolio_id: str
    contract_key: str
    underlying_symbol: str
    quantity: float           # Signed: positive = long, negative = short
    avg_cost: Optional[float] = None
    currency: str = "EUR"


@dataclass
class PositionRisk:
    """Per-position risk row — always stored at line level for full audit trail.

    Dollar Greek conventions
    -----------------------
    dollar_delta  = delta  × spot × quantity × multiplier  ($ notional spot exposure)
    dollar_gamma  = gamma  × spot² × quantity × multiplier ($ gamma notional)
    dollar_vega   = vega   × quantity × multiplier         ($ per vol-point, already per-point)

    PnL attribution for a dollar spot move dS and vol move d_sigma (vol points):
      delta_pnl = dollar_delta / spot × dS
      gamma_pnl = 0.5 × dollar_gamma / spot² × dS²
      vega_pnl  = dollar_vega × d_sigma
      theta_pnl = theta_per_day × quantity × multiplier × dt_days
    """
    # -- Identity --
    portfolio_id: str
    contract_key: str
    underlying_symbol: str
    quantity: float
    multiplier: float
    snapshot_ts: float

    # -- Pricing context (for auditability) --
    spot: float
    forward: float
    implied_vol: float
    maturity_years: float

    # -- Model output --
    model_price: float
    market_value: float       # model_price × quantity × multiplier (signed)

    # -- Raw per-option Greeks (from pricer) --
    delta: float
    gamma: float
    vega_per_point: float     # ∂V/∂σ per 1 vol-point
    theta_per_day: float      # ∂V/∂t per calendar day

    # -- Signed positional dollar Greeks --
    dollar_delta: float       # delta × spot × quantity × multiplier
    dollar_gamma: float       # gamma × spot² × quantity × multiplier
    dollar_vega: float        # vega_per_point × quantity × multiplier

    # -- Optional broker comparison (diagnostics only) --
    broker_delta: Optional[float] = None
    broker_vega: Optional[float] = None

    analytics_version: str = "1.0"


@dataclass
class RiskAggregates:
    """Grouped risk — summed from PositionRisk rows.

    group_key   e.g. "underlying_symbol", "portfolio_id"
    group_value e.g. "ESTX50", "PORT_A"
    """
    portfolio_id: str
    group_key: str
    group_value: str
    valuation_ts: float
    net_delta: float
    net_gamma: float
    net_vega: float
    net_theta: float
    net_dollar_delta: float
    net_dollar_gamma: float
    net_dollar_vega: float
    net_market_value: float
    position_count: int
    analytics_version: str = "1.0"


@dataclass
class UAMResult:
    """Utilisation des Actifs Margés — margin-shock stress result.

    Scenarios: ±5% spot × ±20% vol (4 combinations).
    worst_case_pnl   = minimum PnL across all scenarios (most negative)
    margin_requirement = |worst_case_pnl|  (cash needed to cover worst case)
    uam_ratio          = margin_requirement / portfolio_gross_value
                         (higher = more levered relative to margin)
    """
    portfolio_id: str
    snapshot_ts: float
    scenario_pnls: dict[str, float]    # scenario_label → approx PnL
    worst_case_pnl: float
    margin_requirement: float
    portfolio_gross_value: float
    uam_ratio: float
    spot_shock_pct: float
    vol_shock_abs: float
    config_version: str
    analytics_version: str = "1.0"
