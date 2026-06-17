"""
UAM — Utilisation des Actifs Margés.

Requirement from professor: shock portfolio ±5% spot / ±20% vol, compute
margin requirement ratio.

Convention
----------
Four instantaneous scenarios are evaluated using the local Greek approximation
(delta, gamma, vega; theta dt=0):

  scenario        spot_shift    vol_shift
  up_vol_up       +spot_pct     +vol_abs
  up_vol_dn       +spot_pct     -vol_abs
  dn_vol_up       -spot_pct     +vol_abs
  dn_vol_dn       -spot_pct     -vol_abs

worst_case_pnl    = min across all four scenarios
margin_requirement = |worst_case_pnl|
uam_ratio          = margin_requirement / portfolio_gross_value

A UAM ratio above 1.0 means the estimated margin call exceeds the portfolio
gross value — a practical warning threshold for the professor's review.

Greek approximation path (speed):
  ΔV ≈ Δ·dS + ½·Γ·dS² + ν·dσ     (dt = 0 for instantaneous shock)
"""

from __future__ import annotations

import logging

from src.risk.models import PositionRisk, UAMResult

logger = logging.getLogger(__name__)


def compute_uam(
    position_risks: list[PositionRisk],
    config: dict,
    portfolio_id: str = "",
    snapshot_ts: float = 0.0,
) -> UAMResult:
    """
    Compute UAM (Utilisation des Actifs Margés) for a portfolio.

    Args:
        position_risks: Line-level PositionRisk rows for the portfolio.
        config:         UAM config dict.
                          spot_shock_pct   default 0.05  (5%)
                          vol_shock_abs    default 0.20  (20 vol points)
                          config_version   default "1.0"
        portfolio_id:   Label for the result.
        snapshot_ts:    Timestamp of the snapshot used.

    Returns:
        UAMResult with all four scenario PnLs, worst case, margin requirement,
        and UAM ratio.
    """
    spot_shock_pct = config.get("spot_shock_pct", 0.05)
    vol_shock_abs = config.get("vol_shock_abs", 0.20)
    config_version = config.get("config_version", "1.0")

    if not position_risks:
        return UAMResult(
            portfolio_id=portfolio_id,
            snapshot_ts=snapshot_ts,
            scenario_pnls={},
            worst_case_pnl=0.0,
            margin_requirement=0.0,
            portfolio_gross_value=0.0,
            uam_ratio=0.0,
            spot_shock_pct=spot_shock_pct,
            vol_shock_abs=vol_shock_abs,
            config_version=config_version,
        )

    ref_spot = position_risks[0].spot
    dS_up = ref_spot * spot_shock_pct
    dS_dn = -ref_spot * spot_shock_pct

    scenarios = {
        "up_vol_up": (dS_up, vol_shock_abs),
        "up_vol_dn": (dS_up, -vol_shock_abs),
        "dn_vol_up": (dS_dn, vol_shock_abs),
        "dn_vol_dn": (dS_dn, -vol_shock_abs),
    }

    scenario_pnls: dict[str, float] = {}
    for label, (dS, d_sigma) in scenarios.items():
        pnl = _approx_pnl(position_risks, dS, d_sigma)
        scenario_pnls[label] = pnl
        logger.debug("uam.scenario label=%s dS=%.1f d_sigma=%.2f pnl=%.2f",
                     label, dS, d_sigma, pnl)

    worst_case_pnl = min(scenario_pnls.values())
    margin_requirement = abs(worst_case_pnl)
    portfolio_gross_value = sum(abs(r.market_value) for r in position_risks)
    uam_ratio = (margin_requirement / portfolio_gross_value
                 if portfolio_gross_value > 0 else 0.0)

    logger.info(
        "uam portfolio=%s worst_pnl=%.2f margin=%.2f gross_val=%.2f ratio=%.4f",
        portfolio_id, worst_case_pnl, margin_requirement,
        portfolio_gross_value, uam_ratio,
    )

    return UAMResult(
        portfolio_id=portfolio_id,
        snapshot_ts=snapshot_ts,
        scenario_pnls=scenario_pnls,
        worst_case_pnl=worst_case_pnl,
        margin_requirement=margin_requirement,
        portfolio_gross_value=portfolio_gross_value,
        uam_ratio=uam_ratio,
        spot_shock_pct=spot_shock_pct,
        vol_shock_abs=vol_shock_abs,
        config_version=config_version,
    )


def _approx_pnl(
    position_risks: list[PositionRisk],
    dS: float,
    d_sigma: float,
) -> float:
    """
    Greek approximation of instantaneous portfolio PnL (dt = 0):
      ΔV ≈ Δ·dS + ½·Γ·dS² + ν·dσ
    """
    total = 0.0
    for r in position_risks:
        delta_pnl = r.dollar_delta / r.spot * dS
        gamma_pnl = 0.5 * r.dollar_gamma / (r.spot ** 2) * dS ** 2
        vega_pnl = r.dollar_vega * d_sigma
        total += delta_pnl + gamma_pnl + vega_pnl
    return total
