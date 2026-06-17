"""
IBKR historical data loader.

Mirrors the interface of yfinance_loader.py but delegates all data fetching
to IbkrAdapter.get_historical_bars(). Requires an active IB Gateway / TWS
session managed by the caller.

Primary/fallback pattern (unchanged from previous version):
    from src.historical.ibkr_loader import fetch_with_fallback
    bars, source = fetch_with_fallback("^STOXX50E", start="2024-01-01")

Adapter-based pattern (new, mirrors yfinance_loader interface):
    from src.connectivity.ibkr_adapter import IbkrAdapter
    from src.historical.ibkr_loader import fetch_index_history

    adapter = IbkrAdapter(config)
    adapter.connect()
    df = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")

Pacing limits:
    IBKR allows ~60 identical requests per 10 min.
    fetch_constituents_history() enforces a 0.5 s delay between tickers.
    50 constituents × 0.5 s ≈ 25 s total.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from src.historical.yfinance_loader import (
    EURO_STOXX_50_TICKERS,
    HistoricalBar,
    fetch_index_history as _yf_fetch,
    to_historical_bars as _yf_to_bars,
    validate_history,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract mapping: Yahoo-Finance ticker → IBKR (symbol, sec_type, exchange, currency)
# ---------------------------------------------------------------------------

IBKR_INDEX_MAP: dict[str, tuple[str, str, str, str]] = {
    "^STOXX50E": ("ESTX50", "IND", "EUREX",  "EUR"),
    "ESTX50":    ("ESTX50", "IND", "EUREX",  "EUR"),
    "^GSPC":     ("SPX",    "IND", "CBOE",   "USD"),
    "^GDAXI":    ("DAX",    "IND", "DTB",    "EUR"),
    "^FCHI":     ("CAC40",  "IND", "MONEP",  "EUR"),
    "^IXIC":     ("COMP",   "IND", "NASDAQ", "USD"),
}

_INTERVAL_TO_BAR_SIZE: dict[str, str] = {
    "1d": "1 day",
    "1h": "1 hour",
    "5m": "5 mins",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dates_to_duration(start: str, end: Optional[str] = None) -> str:
    """Map a date range to the smallest IBKR duration string that covers it.

    Thresholds (per spec):
        > 2 years  → "3 Y"
        > 1 year   → "2 Y"
        > 6 months → "1 Y"
        else       → "6 M"
    """
    end_date   = date.fromisoformat(end)   if end   else date.today()
    start_date = date.fromisoformat(start)
    days = (end_date - start_date).days
    if days > 730:
        return "3 Y"
    if days > 365:
        return "2 Y"
    if days > 182:
        return "1 Y"
    return "6 M"


def _yf_ticker_to_ibkr_symbol(ticker: str) -> str:
    """Strip Yahoo Finance exchange suffix to get the IBKR base symbol.

    Examples:
        "ADS.DE"   → "ADS"
        "MC.PA"    → "MC"
        "NOKIA.HE" → "NOKIA"
        "AAPL"     → "AAPL"
    """
    return ticker.split(".")[0]


def _bars_to_df(bars: list[dict]) -> pd.DataFrame:
    """Convert IbkrAdapter bar dicts → tidy OHLCV DataFrame.

    Adapter returns lowercase keys (date, open, high, low, close, volume).
    Renames to Title-Case matching yfinance convention.
    Adj Close is set equal to Close (IBKR returns adjusted prices by default).
    """
    if not bars:
        return pd.DataFrame()

    df = pd.DataFrame(bars)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("date").sort_index()
    df = df.rename(columns={
        "open":   "Open",
        "high":   "High",
        "low":    "Low",
        "close":  "Close",
        "volume": "Volume",
    })
    df["Adj Close"] = df["Close"]

    keep = [c for c in ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
            if c in df.columns]
    return df[keep]


# ---------------------------------------------------------------------------
# Adapter-based fetch functions (mirror yfinance_loader interface)
# ---------------------------------------------------------------------------

def fetch_index_history(
    adapter,
    ticker: str,
    start: str,
    end: Optional[str] = None,
    interval: str = "1d",
) -> pd.DataFrame:
    """Fetch OHLCV history for one index or stock via IbkrAdapter.

    Args:
        adapter:  IbkrAdapter instance (must already be connected)
        ticker:   Yahoo-Finance-style ticker, e.g. "^STOXX50E" or "ADS.DE"
        start:    ISO date "YYYY-MM-DD"
        end:      ISO date, defaults to today
        interval: "1d", "1h", or "5m"

    Returns:
        DataFrame with UTC-aware DatetimeIndex and columns
        [Open, High, Low, Close, Adj Close, Volume].
        Empty DataFrame on any error (never raises).
    """
    duration = _dates_to_duration(start, end)
    bar_size = _INTERVAL_TO_BAR_SIZE.get(interval, "1 day")

    if ticker in IBKR_INDEX_MAP:
        symbol, sec_type, exchange, currency = IBKR_INDEX_MAP[ticker]
    else:
        symbol   = _yf_ticker_to_ibkr_symbol(ticker)
        sec_type = "STK"
        exchange = "SMART"
        currency = "EUR"

    log.info(
        "ibkr_loader.fetch_index symbol=%s sec_type=%s duration=%s bar_size=%s",
        symbol, sec_type, duration, bar_size,
    )
    try:
        bars = adapter.get_historical_bars(
            symbol=symbol,
            sec_type=sec_type,
            exchange=exchange,
            currency=currency,
            duration=duration,
            bar_size=bar_size,
        )
        df = _bars_to_df(bars)
        if df.empty:
            log.warning("ibkr_loader.fetch_index empty result ticker=%s", ticker)
        else:
            log.info("ibkr_loader.fetch_index ok ticker=%s rows=%d", ticker, len(df))
        return df
    except Exception as exc:
        log.error("ibkr_loader.fetch_index failed ticker=%s: %s", ticker, exc)
        return pd.DataFrame()


def fetch_constituents_history(
    adapter,
    tickers: list[str],
    start: str,
    end: Optional[str] = None,
    interval: str = "1d",
) -> pd.DataFrame:
    """Fetch OHLCV history for multiple tickers one-by-one with pacing delay.

    IBKR has no batch-download endpoint; each ticker is requested separately
    with a 0.5 s gap to respect pacing limits. Tickers that fail are skipped.

    Returns:
        DataFrame with MultiIndex columns (field, ticker), e.g. df["Close"]["ADS.DE"].
        Matches the yfinance group_by="column" layout.
        Empty DataFrame if every ticker fails.
    """
    duration = _dates_to_duration(start, end)
    bar_size = _INTERVAL_TO_BAR_SIZE.get(interval, "1 day")

    log.info(
        "ibkr_loader.fetch_constituents n=%d duration=%s bar_size=%s",
        len(tickers), duration, bar_size,
    )

    ticker_dfs: dict[str, pd.DataFrame] = {}
    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(0.5)  # IBKR pacing: 0.5 s between requests

        symbol = _yf_ticker_to_ibkr_symbol(ticker)
        try:
            bars = adapter.get_historical_bars(
                symbol=symbol,
                sec_type="STK",
                exchange="SMART",
                currency="EUR",
                duration=duration,
                bar_size=bar_size,
            )
            df = _bars_to_df(bars)
            if not df.empty:
                ticker_dfs[ticker] = df
                log.debug("ibkr_loader.constituent ok ticker=%s rows=%d", ticker, len(df))
            else:
                log.warning("ibkr_loader.constituent empty ticker=%s", ticker)
        except Exception as exc:
            log.warning("ibkr_loader.constituent failed ticker=%s: %s", ticker, exc)

    if not ticker_dfs:
        log.error("ibkr_loader.fetch_constituents: all tickers failed")
        return pd.DataFrame()

    # Build MultiIndex DataFrame: (field, ticker)
    fields = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    frames: dict[str, pd.DataFrame] = {}
    for field in fields:
        series_map = {
            t: df[field]
            for t, df in ticker_dfs.items()
            if field in df.columns
        }
        if series_map:
            frames[field] = pd.DataFrame(series_map)

    if not frames:
        return pd.DataFrame()

    result = pd.concat(frames, axis=1)
    log.info(
        "ibkr_loader.fetch_constituents ok tickers=%d rows=%d",
        len(ticker_dfs), len(result),
    )
    return result


def fetch_euro_stoxx_50(
    adapter,
    start: str,
    end: Optional[str] = None,
) -> dict[str, pd.DataFrame]:
    """Convenience wrapper: fetch Euro Stoxx 50 index + all 50 constituents.

    Args:
        adapter: IbkrAdapter instance (connected)
        start:   ISO date "YYYY-MM-DD"
        end:     ISO date, defaults to today

    Returns:
        {"index": DataFrame, "constituents": DataFrame}
    """
    log.info("ibkr_loader.fetch_euro_stoxx_50 start=%s end=%s", start, end)
    index_df = fetch_index_history(adapter, "^STOXX50E", start=start, end=end)
    constituents_df = fetch_constituents_history(
        adapter, EURO_STOXX_50_TICKERS, start=start, end=end
    )
    return {"index": index_df, "constituents": constituents_df}


# ---------------------------------------------------------------------------
# Conversion helpers (same interface as yfinance_loader)
# ---------------------------------------------------------------------------

def to_historical_bars(df: pd.DataFrame, ticker: str) -> list[HistoricalBar]:
    """Convert an ibkr_loader DataFrame to HistoricalBar objects.

    Identical to yfinance_loader.to_historical_bars() except source="ibkr".
    """
    bars: list[HistoricalBar] = []
    for ts, row in df.iterrows():
        bars.append(HistoricalBar(
            ticker=ticker,
            date=ts.date() if hasattr(ts, "date") else ts,
            open=float(row.get("Open",      float("nan"))),
            high=float(row.get("High",      float("nan"))),
            low=float(row.get("Low",        float("nan"))),
            close=float(row.get("Close",    float("nan"))),
            adj_close=float(row.get("Adj Close", float("nan"))),
            volume=float(row.get("Volume",  0.0)),
            source="ibkr",
        ))
    return bars


# ---------------------------------------------------------------------------
# Legacy: direct ib_insync connection (preserved for backwards-compat)
# ---------------------------------------------------------------------------

def fetch_ibkr_history(
    ticker: str,
    start: str,
    end: Optional[str] = None,
    bar_size: str = "1 day",
    what_to_show: Optional[str] = None,
    use_rth: bool = True,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 10,
    ibkr_symbol: Optional[str] = None,
    ibkr_sec_type: Optional[str] = None,
    ibkr_exchange: Optional[str] = None,
    ibkr_currency: Optional[str] = None,
) -> list[HistoricalBar]:
    """Fetch historical bars by connecting directly via ib_insync.

    Prefer the adapter-based fetch_index_history() for new code; this
    function is kept for scripts that manage their own connection lifecycle.
    """
    try:
        from ib_insync import IB, Contract  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "ib_insync is not installed. Run: pip install ib_insync"
        ) from exc

    end_str  = end or date.today().isoformat()
    start_dt = date.fromisoformat(start)
    end_dt   = date.fromisoformat(end_str)

    params = IBKR_INDEX_MAP.get(ticker)
    if ibkr_symbol:
        sym, sec_type, exchange, currency = (
            ibkr_symbol,
            ibkr_sec_type or "STK",
            ibkr_exchange or "SMART",
            ibkr_currency or "USD",
        )
    elif params:
        sym, sec_type, exchange, currency = params
    else:
        sym, sec_type, exchange, currency = ticker, "STK", "SMART", "USD"

    days = (end_dt - start_dt).days + 1
    duration = f"{days} D" if days <= 365 else f"{max(1, (days + 364) // 365)} Y"
    end_ibkr = end_dt.strftime("%Y%m%d 23:59:59")
    show = what_to_show or "TRADES"

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=10)
        ib.qualifyContracts(Contract(symbol=sym, secType=sec_type,
                                     exchange=exchange, currency=currency))
        raw_bars = ib.reqHistoricalData(
            Contract(symbol=sym, secType=sec_type, exchange=exchange, currency=currency),
            endDateTime=end_ibkr,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=show,
            useRTH=use_rth,
            formatDate=1,
        )
    finally:
        ib.disconnect()

    if not raw_bars:
        return []

    result: list[HistoricalBar] = []
    for bar in raw_bars:
        bar_date = bar.date if isinstance(bar.date, date) else bar.date.date()
        if bar_date < start_dt or bar_date > end_dt:
            continue
        result.append(HistoricalBar(
            ticker=ticker,
            date=bar_date,
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            adj_close=float(bar.close),
            volume=float(bar.volume),
            source="ibkr",
        ))
    return result


def fetch_with_fallback(
    ticker: str,
    start: str,
    end: Optional[str] = None,
    bar_size: str = "1 day",
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 10,
) -> tuple[list[HistoricalBar], str]:
    """Fetch via IBKR first; fall back to Yahoo Finance if unreachable.

    Returns:
        (bars, source) where source is "ibkr" or "yfinance".
    """
    try:
        bars = fetch_ibkr_history(
            ticker, start=start, end=end,
            bar_size=bar_size, host=host, port=port, client_id=client_id,
        )
        if bars:
            return bars, "ibkr"
        log.warning("ibkr returned 0 bars for %s — falling back to yfinance", ticker)
    except Exception as exc:
        log.warning("ibkr failed for %s (%s) — falling back to yfinance", ticker, exc)

    df = _yf_fetch(ticker, start=start, end=end)
    return _yf_to_bars(df, ticker), "yfinance"


def bars_to_dataframe(bars: list[HistoricalBar]) -> pd.DataFrame:
    """Convert HistoricalBar list → DataFrame matching yfinance layout."""
    if not bars:
        return pd.DataFrame()
    rows = [
        {
            "Date":      pd.Timestamp(b.date, tz="UTC"),
            "Open":      b.open,
            "High":      b.high,
            "Low":       b.low,
            "Close":     b.close,
            "Adj Close": b.adj_close,
            "Volume":    b.volume,
        }
        for b in bars
    ]
    return pd.DataFrame(rows).set_index("Date").sort_index()


__all__ = [
    "IBKR_INDEX_MAP",
    "HistoricalBar",
    # Adapter-based (new)
    "fetch_index_history",
    "fetch_constituents_history",
    "fetch_euro_stoxx_50",
    "to_historical_bars",
    "validate_history",
    "_dates_to_duration",
    "_yf_ticker_to_ibkr_symbol",
    # Legacy direct connection
    "fetch_ibkr_history",
    "fetch_with_fallback",
    "bars_to_dataframe",
]
