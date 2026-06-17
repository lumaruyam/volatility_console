"""
Pearson correlation matrix from 252-day daily log-returns.
Primary source: IBKR via data_fetcher (≤3Y window). Falls back to yfinance for longer windows.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import numpy as np

log = logging.getLogger(__name__)

# Display ticker → Yahoo Finance ticker
_YF_MAP = {
    "SX5E":  "^STOXX50E",
    "ASML":  "ASML.AS",
    "MC.PA": "MC.PA",
    "SAP":   "SAP.DE",
    "TTE":   "TTE.PA",
    "SPX":   "^GSPC",
    "DAX":   "^GDAXI",
    "NKY":   "^N225",
}

_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 600.0   # 10 minutes

_FALLBACK = {
    "SX5E":  [1.00, 0.82, 0.88, 0.79, 0.65],
    "ASML":  [0.82, 1.00, 0.54, 0.89, 0.45],
    "MC.PA": [0.88, 0.54, 1.00, 0.48, 0.52],
    "SAP":   [0.79, 0.89, 0.48, 1.00, 0.49],
    "TTE":   [0.65, 0.45, 0.52, 0.49, 1.00],
}


def compute_correlation(
    tickers: list[str] | None = None,
    window_days: int = 252,
) -> dict:
    """
    Return Pearson correlation matrix for the given tickers over window_days.
    Falls back to hardcoded matrix when yfinance data is unavailable.
    """
    if tickers is None:
        tickers = ["SX5E", "ASML", "MC.PA", "SAP", "TTE"]

    cache_key = ",".join(tickers) + f":{window_days}"
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]

    try:
        result = _compute_from_market_data(tickers, window_days)
        _CACHE[cache_key] = (result, now + _CACHE_TTL)
        return result
    except Exception as exc:
        log.warning("correlation: market data failed (%s) — using fallback", exc)
        return _fallback_matrix(tickers)


def _compute_from_market_data(tickers: list[str], window_days: int) -> dict:
    from src.historical.data_fetcher import fetch_history
    end   = date.today().isoformat()
    start = (date.today() - timedelta(days=window_days * 2)).isoformat()

    close_series = {}
    for t in tickers:
        yf = _YF_MAP.get(t, t)
        df = fetch_history(yf, start=start, end=end)
        if df.empty or "Close" not in df.columns:
            raise ValueError(f"No data for {t}")
        closes = df["Close"].dropna()
        log_rets = np.log(closes / closes.shift(1)).dropna()
        close_series[t] = log_rets.tail(window_days)

    import pandas as pd
    combined = pd.DataFrame(close_series).dropna()
    if len(combined) < 20:
        raise ValueError("Insufficient overlapping data")

    corr = combined.corr(method="pearson")
    matrix = [[round(corr.loc[t1, t2], 4) for t2 in tickers] for t1 in tickers]
    return {"tickers": tickers, "matrix": matrix}


def _fallback_matrix(tickers: list[str]) -> dict:
    default_tickers = list(_FALLBACK.keys())
    matrix = []
    for t1 in tickers:
        row = []
        for t2 in tickers:
            if t1 == t2:
                row.append(1.0)
            elif t1 in _FALLBACK and t2 in default_tickers:
                idx = default_tickers.index(t2)
                row.append(_FALLBACK[t1][idx] if idx < len(_FALLBACK[t1]) else 0.5)
            else:
                row.append(0.5)
        matrix.append(row)
    return {"tickers": tickers, "matrix": matrix}
