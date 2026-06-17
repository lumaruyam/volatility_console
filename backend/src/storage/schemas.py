"""
Data schema definitions for all table families.

Rules:
- Never store decimals as formatted strings — use numeric types
- Every derived table references source snapshot_ts
- Keep version fields on all derived analytics tables
- Store UTC timestamps only; document timezone convention
- QC results must point to both failing object and run that produced it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Schema definitions (as Python dataclasses for type safety)
# ---------------------------------------------------------------------------

@dataclass
class InstrumentMasterRow:
    """instrument_master table. PK: instrument_key, as_of_date"""
    instrument_key: str
    as_of_date: str          # ISO date
    underlying_symbol: str
    sec_type: str
    exchange: str
    currency: str
    expiry: Optional[str]    # ISO date, None for underlyings
    strike: Optional[float]
    option_right: Optional[str]
    multiplier: Optional[float]
    contract_id_broker: Optional[str]
    trading_class: Optional[str]
    universe_version: str


@dataclass
class RawMarketEventRow:
    """raw_market_events table. PK: session_id, event_id. IMMUTABLE."""
    session_id: str
    event_id: str
    instrument_key: str
    field_name: str
    field_value: float
    exchange_ts: Optional[float]
    receipt_ts: float        # UTC epoch
    source: str              # "live" | "replay"


@dataclass
class MarketStateSnapshotRow:
    """market_state_snapshots table. PK: snapshot_ts, instrument_key"""
    snapshot_ts: float
    instrument_key: str
    underlying_symbol: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    mid: Optional[float]
    volume: Optional[float]
    open_interest: Optional[float]
    spread_pct: Optional[float]
    reference_spot: Optional[float]
    reference_type: Optional[str]
    quote_age_seconds: Optional[float]
    is_stale: bool
    is_market_open: bool
    maturity_years: Optional[float]
    session_id: str
    snapshot_version: str = "1.0"


@dataclass
class ForwardCurveRow:
    """forward_curve table. PK: snapshot_ts, underlying, maturity"""
    snapshot_ts: float
    underlying: str
    expiry_str: str
    maturity_years: float
    chosen_forward: float
    weighted_mean_forward: float
    median_forward: float
    confidence_score: float
    candidates_count: int
    fallback_used: str
    implied_carry: Optional[float]
    diagnostics_version: str


@dataclass
class IVPointRow:
    """iv_points table. PK: snapshot_ts, contract_key"""
    snapshot_ts: float
    contract_key: str          # = instrument_key
    underlying: str
    expiry_str: str
    maturity_years: float
    strike: float
    option_right: str
    forward: float
    log_moneyness: float
    market_price: float
    implied_vol: float
    total_variance: float
    converged: bool
    solver_residual: float
    iterations: int
    failure_reason: Optional[str]
    model_name: str
    solver_version: str


@dataclass
class SurfaceParametersRow:
    """surface_parameters table. PK: snapshot_ts, underlying, maturity, model_version"""
    snapshot_ts: float
    underlying: str
    expiry_str: str
    maturity_years: float
    model_name: str         # "svi" | "spline"
    model_version: str
    # SVI parameters (None for spline)
    svi_a: Optional[float]
    svi_b: Optional[float]
    svi_rho: Optional[float]
    svi_m: Optional[float]
    svi_sigma: Optional[float]
    fit_rmse: float
    fit_max_error: float
    n_accepted_points: int
    quality_flag: str


@dataclass
class SurfaceGridRow:
    """surface_grid table. PK: snapshot_ts, underlying, maturity, moneyness_bucket"""
    snapshot_ts: float
    underlying: str
    expiry_str: str
    maturity_years: float
    log_moneyness: float
    total_variance: float
    implied_vol: float
    model_name: str
    model_version: str


@dataclass
class PricingResultRow:
    """pricing_results table. PK: snapshot_ts, contract_key, pricer_version"""
    snapshot_ts: float
    contract_key: str
    underlying: str
    pricer_name: str        # "black_scholes" | "crr_binomial"
    pricer_version: str
    model_price: float
    delta: float
    gamma: float
    vega_per_point: float
    theta_per_day: float
    dollar_gamma: float
    dollar_vega: float
    forward_used: float
    sigma_used: float


@dataclass
class PositionRow:
    """positions table. PK: valuation_ts, portfolio_id, contract_key"""
    valuation_ts: float
    portfolio_id: str
    contract_key: str
    quantity: float
    avg_cost: Optional[float]
    currency: str
    position_source: str    # "broker" | "manual" | "hypothetical"


@dataclass
class RiskAggregateRow:
    """risk_aggregates table. PK: valuation_ts, portfolio_id, group_key"""
    valuation_ts: float
    portfolio_id: str
    group_key: str
    net_delta: float
    net_gamma: float
    net_vega: float
    net_theta: float
    net_dollar_delta: float
    net_dollar_gamma: float
    net_dollar_vega: float
    net_market_value: float
    position_count: int
    analytics_version: str
    snapshot_ts_used: float


@dataclass
class ScenarioResultRow:
    """scenario_results table. PK: valuation_ts, portfolio_id, scenario_id, contract_key"""
    valuation_ts: float
    portfolio_id: str
    scenario_id: str
    scenario_version: str
    contract_key: str
    base_price: float
    stressed_price: float
    pnl: float
    method: str           # "full_reprice" | "greek_approx"
    analytics_version: str
    snapshot_ts_used: float


@dataclass
class QCResultRow:
    """qc_results table. PK: run_id, check_name, target_key"""
    run_id: str
    check_name: str
    target_key: str       # underlying, contract_key, or maturity label
    qc_status: str        # "pass" | "warn" | "fail"
    reason_code: str
    measured_value: Optional[float]
    threshold: Optional[float]
    severity: str         # "info" | "warn" | "critical"
    run_ts: float
    threshold_version: str
    context_json: str     # JSON-serialized additional context


# ---------------------------------------------------------------------------
# Table name registry
# ---------------------------------------------------------------------------

TABLE_NAMES = [
    "instrument_master",
    "raw_market_events",
    "market_state_snapshots",
    "forward_curve",
    "iv_points",
    "surface_parameters",
    "surface_grid",
    "pricing_results",
    "positions",
    "risk_aggregates",
    "scenario_results",
    "qc_results",
]
