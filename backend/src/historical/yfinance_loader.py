"""
Yahoo Finance historical data loader.

Use cases:
  - Backfill index / constituent prices for strategy backtesting
  - Provide reference spot prices when IBKR session is unavailable
  - Bootstrap the pipeline before live IBKR entitlements are confirmed

Limitations (per professor):
  - NO options or futures data — use IBKR for those
  - Daily bars only (no intraday)
  - Data quality: may have gaps; always validate before use

Coverage:
  - Euro Stoxx 50:  ^STOXX50E
  - S&P 500:        ^GSPC
  - Constituent stocks: their normal tickers (e.g. AAPL, MC.PA)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Euro Stoxx 50 constituents (as of 2025)
# ---------------------------------------------------------------------------

EURO_STOXX_50_TICKERS: list[str] = [
    "ADS.DE", "AI.PA", "AIR.PA", "ALV.DE", "ASML.AS",
    "BAS.DE", "BAYN.DE", "BBVA.MC", "BMW.DE", "BNP.PA",
    "CRH.L",  "CS.PA",  "DB1.DE", "DBK.DE", "DG.PA",
    "DPW.DE", "DTE.DE", "ENEL.MI","ENI.MI", "EL.PA",
    "FLTR.L", "FRE.DE", "IBE.MC", "IFX.DE", "INGA.AS",
    "ISP.MI", "ITX.MC", "KER.PA", "LIN.DE", "MC.PA",
    "MBG.DE", "MUV2.DE","MRK.DE", "NOKIA.HE","OR.PA",
    "PHIA.AS","PRX.AS", "RMS.PA", "RWE.DE", "SAF.PA",
    "SAN.MC", "SAP.DE", "SIE.DE", "STLAM.MI","SU.PA",
    "TTE.PA", "UCG.MI", "UNA.AS", "VIV.PA", "VOW3.DE",
]

INDEX_TICKERS: dict[str, str] = {
    "EURO_STOXX_50": "^STOXX50E",
    "SP500":         "^GSPC",
    "NASDAQ":        "^IXIC",
    "DAX":           "^GDAXI",
    "CAC40":         "^FCHI",
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class HistoricalBar:
    """One OHLCV bar for one instrument."""
    ticker: str
    date: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: float
    source: str = "yfinance"


# ---------------------------------------------------------------------------
# Core fetch functions
# ---------------------------------------------------------------------------

def fetch_index_history(
    ticker: str,
    start: str,
    end: Optional[str] = None,
    interval: str = "1d",
) -> pd.DataFrame:
    """
    Fetch OHLCV history for one index or ETF.

    Args:
        ticker:   Yahoo Finance ticker, e.g. "^STOXX50E" or "^GSPC"
        start:    ISO date string "YYYY-MM-DD"
        end:      ISO date string, defaults to today
        interval: Bar size — "1d" (daily) is sufficient per professor

    Returns:
        DataFrame with columns [Open, High, Low, Close, Adj Close, Volume]
        Index is DatetimeIndex (UTC-aware).
        Empty DataFrame if fetch fails (never raises — caller decides what to do).

    Example:
        >>> df = fetch_index_history("^STOXX50E", "2020-01-01", "2024-12-31")
        >>> df["Close"].plot()
    """
    end = end or date.today().isoformat()
    log.info("yfinance.fetch ticker=%s start=%s end=%s interval=%s", ticker, start, end, interval)
    try:
        df = yf.download(
            tickers=ticker,
            start=start,
            end=end,
            interval=interval,
            auto_adjust=False,
            progress=False,
        )
        if df.empty:
            log.warning("yfinance.fetch returned empty DataFrame for %s", ticker)
            return df
        df = _flatten_columns(df)
        df.index = pd.to_datetime(df.index, utc=True)
        log.info("yfinance.fetch ok ticker=%s rows=%d", ticker, len(df))
        return df
    except Exception as exc:
        log.error("yfinance.fetch failed ticker=%s error=%s", ticker, exc)
        return pd.DataFrame()


def fetch_constituents_history(
    tickers: list[str],
    start: str,
    end: Optional[str] = None,
    interval: str = "1d",
) -> pd.DataFrame:
    """
    Fetch OHLCV history for multiple tickers in one call (more efficient).

    Returns:
        DataFrame with MultiIndex columns (field, ticker).
        E.g. df["Close"]["AAPL"] gives the AAPL close series.

    Example:
        >>> df = fetch_constituents_history(EURO_STOXX_50_TICKERS, "2020-01-01")
        >>> closes = df["Close"]   # shape: (days, 50)
    """
    end = end or date.today().isoformat()
    log.info("yfinance.fetch_multi n_tickers=%d start=%s end=%s", len(tickers), start, end)
    try:
        df = yf.download(
            tickers=tickers,
            start=start,
            end=end,
            interval=interval,
            auto_adjust=False,
            group_by="column",
            progress=False,
        )
        if df.empty:
            log.warning("yfinance.fetch_multi returned empty DataFrame")
            return df
        df.index = pd.to_datetime(df.index, utc=True)
        log.info("yfinance.fetch_multi ok rows=%d cols=%d", len(df), len(df.columns))
        return df
    except Exception as exc:
        log.error("yfinance.fetch_multi failed error=%s", exc)
        return pd.DataFrame()


def fetch_euro_stoxx_50(
    start: str,
    end: Optional[str] = None,
) -> dict[str, pd.DataFrame]:
    """
    Convenience function: fetch Euro Stoxx 50 index + all 50 constituents.

    Returns:
        {
          "index":        DataFrame (index OHLCV),
          "constituents": DataFrame (multi-ticker close prices),
        }

    Example:
        >>> data = fetch_euro_stoxx_50("2019-01-01", "2024-12-31")
        >>> index_close = data["index"]["Close"]
        >>> constituent_closes = data["constituents"]["Close"]
    """
    log.info("Fetching Euro Stoxx 50 index + 50 constituents from %s to %s", start, end)

    index_df = fetch_index_history(
        INDEX_TICKERS["EURO_STOXX_50"], start=start, end=end
    )
    constituents_df = fetch_constituents_history(
        EURO_STOXX_50_TICKERS, start=start, end=end
    )
    return {
        "index": index_df,
        "constituents": constituents_df,
    }


def fetch_single_ticker(
    ticker: str,
    start: str,
    end: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetch one ticker and return as a clean DataFrame with date column.
    Thin wrapper around fetch_index_history for single-stock use.
    """
    return fetch_index_history(ticker, start=start, end=end)


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def to_historical_bars(df: pd.DataFrame, ticker: str) -> list[HistoricalBar]:
    """
    Convert a single-ticker yfinance DataFrame to list of HistoricalBar.
    Use when you need typed objects instead of a raw DataFrame.
    """
    bars = []
    for ts, row in df.iterrows():
        bars.append(HistoricalBar(
            ticker=ticker,
            date=ts.date() if hasattr(ts, "date") else ts,
            open=float(row.get("Open", float("nan"))),
            high=float(row.get("High", float("nan"))),
            low=float(row.get("Low", float("nan"))),
            close=float(row.get("Close", float("nan"))),
            adj_close=float(row.get("Adj Close", float("nan"))),
            volume=float(row.get("Volume", 0.0)),
        ))
    return bars


def get_close_series(df: pd.DataFrame, adjusted: bool = True) -> pd.Series:
    """
    Extract a clean close price series from a yfinance DataFrame.

    Args:
        adjusted: If True, use Adj Close (corrects for splits/dividends).
                  Use True for strategy backtesting.
                  Use False to match raw exchange prices.
    """
    col = "Adj Close" if adjusted else "Close"
    if col not in df.columns:
        raise KeyError(f"Column '{col}' not found. Available: {list(df.columns)}")
    return df[col].dropna()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_history(df: pd.DataFrame, ticker: str,
                      min_rows: int = 5) -> list[str]:
    """
    Basic sanity checks on a fetched DataFrame.
    Returns list of warning strings (empty = ok).
    """
    warnings = []
    if df.empty:
        warnings.append(f"{ticker}: empty DataFrame")
        return warnings
    if len(df) < min_rows:
        warnings.append(f"{ticker}: only {len(df)} rows (expected >= {min_rows})")
    if df["Close"].isnull().sum() > 0:
        n = df["Close"].isnull().sum()
        warnings.append(f"{ticker}: {n} NaN close values")
    if (df["Close"] <= 0).any():
        warnings.append(f"{ticker}: non-positive close prices detected")
    return warnings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns from single-ticker yfinance download."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]
    return df
