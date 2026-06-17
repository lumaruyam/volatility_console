"""
Single source of truth for portfolio-level Greeks, NAV, and reference spot.

Both risk.py and shock.py import from here so the values can never desync.

To swap in live data: replace get_portfolio_greeks() internals with a call to
src.risk.aggregation.aggregate_risk() once IBKR positions are available
(integration.md Priority 7).
"""

from __future__ import annotations

# Reference SX5E level used for Greek approximations (updated when positions reload)
PORTFOLIO_SPOT: float = 4_952.0

# Portfolio NAV (EUR) — used for VaR scaling and UAM% ratio
PORTFOLIO_NAV: float = 12_500_000

# Broader book NAV (EUR) — denominator for shock reprice bps calculation
NAV_TOTAL: float = 26_000_000


def get_portfolio_greeks() -> dict:
    """
    Aggregate EUR-denominated portfolio Greeks.

    Keys
    ----
    portfolio_delta  EUR delta (dV/dS · S, signed)
    gamma            Raw gamma (dΔ/dS per index point)
    dollar_gamma     ½Γ·S²  (dollar-gamma, EUR)
    vega             EUR vega per vol point (dV/dσ)
    theta            EUR daily theta
    rho              EUR rho per basis point

    When live IBKR positions are available, replace the return statement with:
        return aggregate_risk(positions)   # from src.risk.aggregation
    """
    return {
        "portfolio_delta": 4_520_000,
        "gamma":          -1_240_000,
        "dollar_gamma":     -385_200,
        "vega":             850_400,
        "theta":            -12_500,
        "rho":               45_100,
    }
