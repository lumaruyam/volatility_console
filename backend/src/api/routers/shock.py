"""
Shock reprice router — Page 5: ShockSimulator.

Builds a 3×3 scenario matrix:
  rows: Spot −5% / Base / Spot +5%   (plus user's manual spot offset)
  cols: Vol −30 / ATM / Vol +30      (plus user's manual vol offset)

PnL approximation: ΔV ≈ Δ·dS + ½Γ·dS² + V·dσ + ρ·dr
"""

from __future__ import annotations

import math

from fastapi import APIRouter
from pydantic import BaseModel

from src.risk.portfolio_state import get_portfolio_greeks, PORTFOLIO_NAV, PORTFOLIO_SPOT, NAV_TOTAL

router = APIRouter()

_BASE_SPOT_PCT = [-5.0, 0.0, 5.0]
_BASE_VOL_PCT  = [-30.0, 0.0, 30.0]
_SPOT_LABELS   = ["Spot −5%", "Spot Unchanged (Base)", "Spot +5%"]
_VOL_LABELS    = ["−30 ΔVol Shock", "ATM Baseline", "+30 ΔVol Shock"]

_METHODOLOGY_SLUGS = {
    "Parallel Grid Shift":          "parallel_grid_shift",
    "Historical Copula Resampling": "historical_copula",
    "VIX-Indexed Skew Stressing":   "vix_indexed_skew",
}


class LiquidityImpactRequest(BaseModel):
    spot: float = 0.0   # spot shock %
    vol: float  = 0.0   # vol shock pts
    portfolio: str = "SX5E_STRADDLE"


class RepriceRequest(BaseModel):
    spot_stress: float = 0.0          # manual offset, fraction (−0.05 = −5%)
    vol_stress: float = 0.0           # manual offset, fraction (0.20 = +20%)
    rate_stress_bps: float = 0.0      # manual offset, bps
    methodology: str = "parallel_grid_shift"
    active_methods: int = 1           # count of active methodology toggles


@router.get("/surface-before-after")
def surface_before_after(
    spot: float = 0.0,
    vol: float  = 0.0,
    portfolio: str = "SX5E_STRADDLE",
) -> dict:
    """ATM vol term structure before and after a combined spot/vol shock."""
    maturities   = ["1M", "3M", "6M", "12M", "18M", "24M"]
    base_atm_vol = [0.152, 0.162, 0.170, 0.178, 0.185, 0.190]
    vol_shift    = vol * 0.01   # vol shock is in vol pts (e.g. 10 → +0.10)
    # Spot shock steepens the skew slightly (−5% spot → +1.5% ATM vol)
    spot_atm_adj = -spot * 0.003

    after = [round(v + vol_shift + spot_atm_adj, 4) for v in base_atm_vol]
    return {
        "maturities":    maturities,
        "atm_vol_before": base_atm_vol,
        "atm_vol_after":  after,
    }


@router.post("/liquidity-impact")
def liquidity_impact(body: LiquidityImpactRequest) -> list[dict]:
    """Estimated bid-ask spread widening and volume impact under a shock scenario."""
    abs_shock = abs(body.spot) + abs(body.vol) * 0.5
    multiplier = 1.0 + abs_shock * 0.06   # 6% spread widening per unit of combined shock

    rows = [
        {"contract": "SX5E 4000P DEC26", "pre_spread_pct": 2.1,  "volume_impact_pct": -42},
        {"contract": "ASML 900C SEP26",  "pre_spread_pct": 5.9,  "volume_impact_pct": -61},
        {"contract": "SX5E 4400C DEC26", "pre_spread_pct": 1.8,  "volume_impact_pct": -35},
        {"contract": "MC.PA 500P SEP26", "pre_spread_pct": 8.2,  "volume_impact_pct": -74},
        {"contract": "SX5E 3800P MAR27", "pre_spread_pct": 3.4,  "volume_impact_pct": -48},
    ]
    for row in rows:
        row["post_spread_pct"] = round(row["pre_spread_pct"] * multiplier, 2)
        row["volume_impact_pct"] = (
            0 if abs_shock == 0
            else round(row["volume_impact_pct"] * (1.0 + abs_shock * 0.02), 1)
        )
    return rows


@router.post("/reprice")
def reprice(body: RepriceRequest) -> dict:
    """
    Returns the full 3×3 scenario matrix with pnl_eur and nav_bps per cell,
    plus footer stats (aggregate_shift_pct, active_methods, rate_bps).
    """
    g    = get_portfolio_greeks()
    spot = PORTFOLIO_SPOT

    # Convert manual offsets from fractions/bps to percentages used in display
    user_spot_pct = body.spot_stress * 100    # −0.05 → −5.0
    user_vol_pct  = body.vol_stress  * 100    # 0.20  → 20.0
    user_rate_bps = body.rate_stress_bps

    def approx_pnl(total_spot_pct: float, total_vol_pct: float) -> int:
        dS_pct  = total_spot_pct / 100.0
        d_sigma = total_vol_pct  / 100.0   # vol-point move as fraction
        dS      = spot * dS_pct
        pnl     = (
            g["portfolio_delta"] / spot * dS              # delta
            + 0.5 * g["dollar_gamma"] / spot**2 * dS**2  # gamma
            + g["vega"] * d_sigma                          # vega
            + g["rho"] * user_rate_bps * 0.0001           # rho (bp sensitivity)
        )
        return int(pnl)

    matrix: list[list[dict]] = []
    for s_base in _BASE_SPOT_PCT:
        row: list[dict] = []
        for v_base in _BASE_VOL_PCT:
            tot_s = s_base + user_spot_pct
            tot_v = v_base + user_vol_pct
            pnl   = approx_pnl(tot_s, tot_v)
            nav_bps = round(pnl / NAV_TOTAL * 10_000, 1) if NAV_TOTAL > 0 else 0.0
            row.append({
                "spot_pct": round(tot_s, 1),
                "vol_pct":  round(tot_v, 1),
                "pnl_eur":  pnl,
                "nav_bps":  nav_bps,
            })
        matrix.append(row)

    # Composite shift magnitude (Euclidean in (spot, 0.4×vol) space)
    aggregate_shift_pct = round(
        math.sqrt(user_spot_pct**2 + (user_vol_pct * 0.4)**2), 2
    )

    return {
        "scenario_matrix":      matrix,
        "spot_row_labels":      _SPOT_LABELS,
        "vol_col_labels":       _VOL_LABELS,
        "base_portfolio_value": PORTFOLIO_NAV,
        "nav_total":            NAV_TOTAL,
        "aggregate_shift_pct":  aggregate_shift_pct,
        "active_methods":       max(1, body.active_methods),
        "rate_bps":             int(user_rate_bps),
    }
