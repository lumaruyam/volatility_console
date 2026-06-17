"""
Historical simulation Value at Risk.

Method: 252-day rolling window of daily log-returns on the reference index
(SX5E / ^STOXX50E). Returns are scaled to portfolio notional to produce
dollar VaR figures.  Square-root-of-time rule for multi-day VaR.
"""

from __future__ import annotations

import math
import logging
import time
from datetime import date, timedelta
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 300.0   # 5 minutes — daily data changes slowly


def compute_historical_var(
    ticker: str = "^STOXX50E",
    portfolio_value: float = 10_000_000,
    window_days: int = 252,
) -> dict:
    """
    Return VaR estimates at 95% and 99% confidence for 1-day and 7-day horizons.

    Falls back to parametric VaR (sigma = 15%) when yfinance data is unavailable.
    """
    cache_key = f"{ticker}:{window_days}"
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and cached[1] > now:
        base = cached[0]
        return _scale_to_portfolio(base, portfolio_value)

    try:
        from src.historical.data_fetcher import fetch_history
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=window_days * 2)).isoformat()
        df    = fetch_history(ticker, start=start, end=end)

        if df.empty or len(df) < 20:
            raise ValueError("Insufficient data")

        closes   = df["Close"].dropna()
        log_rets = np.log(closes / closes.shift(1)).dropna()
        sample   = log_rets.tail(window_days).values

        var_95_1d = float(np.percentile(sample, 5))   # 5th pctl = 95% loss
        var_99_1d = float(np.percentile(sample, 1))
        var_99_7d = var_99_1d * math.sqrt(7)

        base = {
            "1d_95": var_95_1d,
            "1d_99": var_99_1d,
            "7d_99": var_99_7d,
        }
        _CACHE[cache_key] = (base, now + _CACHE_TTL)

    except Exception as exc:
        log.warning("var.compute failed (%s) — using parametric fallback", exc)
        annual_sigma = 0.15
        daily_sigma  = annual_sigma / math.sqrt(252)
        base = {
            "1d_95": -1.645 * daily_sigma,
            "1d_99": -2.326 * daily_sigma,
            "7d_99": -2.326 * daily_sigma * math.sqrt(7),
        }

    return _scale_to_portfolio(base, portfolio_value)


def _scale_to_portfolio(base_returns: dict, portfolio_value: float) -> dict:
    return {
        "1d_95": round(base_returns["1d_95"] * portfolio_value),
        "1d_99": round(base_returns["1d_99"] * portfolio_value),
        "7d_99": round(base_returns["7d_99"] * portfolio_value),
    }
