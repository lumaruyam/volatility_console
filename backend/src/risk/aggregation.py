"""
Greeks aggregation and per-position risk.

Design rules:
  - Line-level outputs are always stored; aggregates are a view over lines.
  - Signed Greeks: positive quantity = long = positive delta for calls.
  - Broker Greeks are diagnostics only — never the source of truth.
  - compute_local_pnl_attribution: dS in currency units, d_sigma in vol points.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from src.risk.models import Position, PositionRisk, RiskAggregates

logger = logging.getLogger(__name__)

# Re-export for legacy imports.
__all__ = [
    "Position", "PositionRisk", "RiskAggregates",
    "compute_position_risk", "aggregate_risk",
    "reconcile_with_broker_greeks", "compute_local_pnl_attribution",
]


# ---------------------------------------------------------------------------
# Per-position risk
# ---------------------------------------------------------------------------

def compute_position_risk(
    position: Position,
    analytics_snapshot: dict,
    pricer: Callable,
    config: dict,
) -> PositionRisk:
    """
    Compute risk for one position line.

    Args:
        position:           Position record (portfolio_id, contract_key, quantity, …).
        analytics_snapshot: Dict with at minimum:
                              S, K, T, r, q, sigma, option_type
                            Optional keys:
                              multiplier (default 100), forward, snapshot_ts
        pricer:             Callable(inputs) → PricingResult.
                            Pass price_european or price_american from the pricing engine.
        config:             Risk config dict (analytics_version, …).

    Returns:
        PositionRisk with raw and signed dollar Greeks.
        All Greek signs include the position quantity (long = positive delta for calls).
    """
    from src.pricing.european import EuropeanInputs

    S = analytics_snapshot["S"]
    multiplier = float(analytics_snapshot.get("multiplier", 100.0))
    forward = analytics_snapshot.get("forward", S)
    snapshot_ts = analytics_snapshot.get("snapshot_ts", 0.0)
    Q = position.quantity

    inputs = EuropeanInputs(
        S=S,
        K=analytics_snapshot["K"],
        T=analytics_snapshot["T"],
        r=analytics_snapshot["r"],
        q=analytics_snapshot["q"],
        sigma=analytics_snapshot["sigma"],
        option_type=analytics_snapshot["option_type"],
        multiplier=multiplier,
    )
    result = pricer(inputs)

    analytics_version = config.get("analytics_version", "1.0")

    return PositionRisk(
        portfolio_id=position.portfolio_id,
        contract_key=position.contract_key,
        underlying_symbol=position.underlying_symbol,
        quantity=Q,
        multiplier=multiplier,
        snapshot_ts=snapshot_ts,
        spot=S,
        forward=float(forward),
        implied_vol=analytics_snapshot["sigma"],
        maturity_years=analytics_snapshot["T"],
        model_price=result.price,
        market_value=result.price * Q * multiplier,
        delta=result.delta,
        gamma=result.gamma,
        vega_per_point=result.vega,       # already per vol-point from pricer
        theta_per_day=result.theta,       # already per calendar day from pricer
        dollar_delta=result.delta * S * Q * multiplier,
        dollar_gamma=result.gamma * S ** 2 * Q * multiplier,
        dollar_vega=result.vega * 0.01 * Q * multiplier,
        analytics_version=analytics_version,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_risk(
    position_risks: list[PositionRisk],
    group_keys: list[str],
) -> list[RiskAggregates]:
    """
    Aggregate position-level risk by each group key.

    Args:
        position_risks: List of PositionRisk rows.
        group_keys:     Attribute names on PositionRisk to group by.
                        E.g. ["underlying_symbol", "portfolio_id"].
                        Each key produces its own set of aggregates.

    Returns:
        One RiskAggregates per (group_key, group_value) combination.
        Aggregates reconcile to the sum of the corresponding line-level rows.
    """
    if not position_risks:
        return []

    results: list[RiskAggregates] = []

    for key in group_keys:
        buckets: dict[str, list[PositionRisk]] = {}
        for r in position_risks:
            val = str(getattr(r, key, "unknown"))
            buckets.setdefault(val, []).append(r)

        for val, rows in buckets.items():
            # portfolio_id for the aggregate: use common value if uniform, else "mixed"
            pids = {r.portfolio_id for r in rows}
            agg_pid = next(iter(pids)) if len(pids) == 1 else "mixed"
            ts = rows[0].snapshot_ts

            results.append(RiskAggregates(
                portfolio_id=agg_pid,
                group_key=key,
                group_value=val,
                valuation_ts=ts,
                net_delta=sum(r.delta * r.quantity * r.multiplier for r in rows),
                net_gamma=sum(r.gamma * r.quantity * r.multiplier for r in rows),
                net_vega=sum(r.vega_per_point * r.quantity * r.multiplier for r in rows),
                net_theta=sum(r.theta_per_day * r.quantity * r.multiplier for r in rows),
                net_dollar_delta=sum(r.dollar_delta for r in rows),
                net_dollar_gamma=sum(r.dollar_gamma for r in rows),
                net_dollar_vega=sum(r.dollar_vega for r in rows),
                net_market_value=sum(r.market_value for r in rows),
                position_count=len(rows),
                analytics_version=rows[0].analytics_version,
            ))

    return results


# ---------------------------------------------------------------------------
# Broker reconciliation diagnostics
# ---------------------------------------------------------------------------

def reconcile_with_broker_greeks(
    position_risks: list[PositionRisk],
    broker_greeks: dict,
) -> list[dict]:
    """
    Compare platform Greeks with broker-returned Greeks.

    Args:
        position_risks: Line-level PositionRisk rows.
        broker_greeks:  contract_key → {"delta": …, "vega": …, …}.
                        Broker Greeks are diagnostics only — never the source of truth.

    Returns:
        List of discrepancy records. One entry per (position, Greek) where a
        broker value exists.  Fields: contract_key, greek, platform_value,
        broker_value, abs_diff, rel_diff_pct.
    """
    records = []
    for row in position_risks:
        bg = broker_greeks.get(row.contract_key)
        if not bg:
            continue
        for greek in ("delta", "vega"):
            platform_val = getattr(row, greek if greek != "vega" else "vega_per_point", None)
            broker_val = bg.get(greek)
            if platform_val is None or broker_val is None:
                continue
            abs_diff = platform_val - broker_val
            rel_diff_pct = (abs_diff / broker_val * 100.0) if broker_val != 0 else float("nan")
            records.append({
                "contract_key": row.contract_key,
                "greek": greek,
                "platform_value": platform_val,
                "broker_value": broker_val,
                "abs_diff": abs_diff,
                "rel_diff_pct": rel_diff_pct,
            })
    return records


# ---------------------------------------------------------------------------
# Local PnL attribution
# ---------------------------------------------------------------------------

def compute_local_pnl_attribution(
    position_risks: list[PositionRisk],
    dS: float,
    d_sigma: float,
    dt_days: float,
) -> dict:
    """
    ΔV ≈ Δ·dS + ½·Γ·dS² + ν·dσ + Θ·dt  (Greek approximation).

    Args:
        position_risks: Line-level PositionRisk rows.
        dS:             Spot move in currency units (e.g. 25.0 for +25 index points).
        d_sigma:        Vol move in vol points (e.g. 1.0 for +1 vol point).
        dt_days:        Time elapsed in calendar days.

    Returns:
        Dict with per-Greek and total approximate PnL.

    PnL formulas (using stored dollar Greeks):
      delta_pnl = (dollar_delta / spot) × dS    [= delta × Q × M × dS]
      gamma_pnl = 0.5 × (dollar_gamma / spot²) × dS²
      vega_pnl  = dollar_vega × d_sigma
      theta_pnl = theta_per_day × quantity × multiplier × dt_days
    """
    delta_pnl = sum(r.dollar_delta / r.spot * dS for r in position_risks)
    gamma_pnl = sum(0.5 * r.dollar_gamma / (r.spot ** 2) * dS ** 2
                    for r in position_risks)
    vega_pnl = sum(r.dollar_vega * d_sigma for r in position_risks)
    theta_pnl = sum(r.theta_per_day * r.quantity * r.multiplier * dt_days
                    for r in position_risks)
    total = delta_pnl + gamma_pnl + vega_pnl + theta_pnl

    return {
        "delta_pnl": delta_pnl,
        "gamma_pnl": gamma_pnl,
        "vega_pnl": vega_pnl,
        "theta_pnl": theta_pnl,
        "total_approx_pnl": total,
    }
