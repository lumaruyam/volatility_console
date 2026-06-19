"""
PnL attribution — decompose today's portfolio PnL into Greek components.

When live position data is unavailable, falls back to estimating the realized
spot move from yfinance and applying it to the portfolio's aggregate Greeks.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

log = logging.getLogger(__name__)

_CACHE: tuple[dict, float] | None = None
_CACHE_TTL = 120.0


def compute_pnl_attribution(
    portfolio_greeks: dict | None = None,
    spot_ticker: str = "^STOXX50E",
) -> dict:
    """
    Attribution: ΔV ≈ Δ·dS + ½Γ·dS² + V·dσ + Θ·dt

    Uses yesterday's realized spot move (IBKR primary via data_fetcher, yfinance fallback).
    Falls back to synthetic estimates when market data is unavailable.
    """
    global _CACHE
    now = time.monotonic()
    if _CACHE and _CACHE[1] > now:
        return _CACHE[0]

    if portfolio_greeks is None:
        from src.risk.portfolio_state import get_portfolio_greeks
        portfolio_greeks = get_portfolio_greeks()
    greeks = portfolio_greeks

    try:
        dS_pct, d_sigma = _fetch_realized_moves(spot_ticker)
    except Exception as exc:
        log.warning("pnl_attribution: realized move fetch failed (%s) — using defaults", exc)
        dS_pct, d_sigma = 0.005, 0.01    # +0.5% spot, +1 vol point

    spot_approx = 4_952.0
    dS = spot_approx * dS_pct

    delta_pnl = greeks["portfolio_delta"] / spot_approx * dS
    gamma_pnl = 0.5 * greeks["dollar_gamma"] / (spot_approx ** 2) * dS ** 2
    vega_pnl  = greeks.get("dollar_vega", greeks.get("vega", 0)) * d_sigma
    theta_pnl = greeks["theta"]
    rho_pnl   = greeks.get("rho", 45_100) * 0.001   # approx: 10bps rate move

    result = {
        "delta_pnl": round(delta_pnl),
        "gamma_pnl": round(gamma_pnl),
        "vega_pnl":  round(vega_pnl),
        "theta_pnl": round(theta_pnl),
        "rho_pnl":   round(rho_pnl),
    }
    _CACHE = (result, now + _CACHE_TTL)
    return result


def _fetch_realized_moves(spot_ticker: str) -> tuple[float, float]:
    """Return (dS_pct, d_sigma_vol_pts) from the last two bars."""
    from src.historical.data_fetcher import fetch_history
    end   = date.today().isoformat()
    start = (date.today() - timedelta(days=7)).isoformat()
    df    = fetch_history(spot_ticker, start=start, end=end)

    if df.empty or len(df) < 2:
        raise ValueError("Not enough bars")

    closes = df["Close"].dropna()
    dS_pct = float((closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2])
    d_sigma = 0.01    # yfinance has no IV; assume 1 vol point daily move
    return dS_pct, d_sigma
