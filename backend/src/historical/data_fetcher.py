"""
Unified market data fetcher — IBKR primary, yfinance fallback, disk cache offline safety net.

All modules that need historical OHLCV or spot prices should import from here.

Data source priority:
  1. IBKR (via adapter_registry)  — live spot snapshots, OHLCV up to ~3 years
  2. Yahoo Finance                 — fallback; primary for long backtests (>3Y)
  3. Disk cache (data/cache/ohlcv) — offline fallback when both above are unreachable
                                     (e.g. presenting at school where port 7497 is blocked)

Populate the disk cache at home before any offline presentation:
    cd backend && python scripts/seed_cache.py

Usage:
    from src.historical.data_fetcher import fetch_history, fetch_spot

    df   = fetch_history("^STOXX50E", start="2024-01-01")
    spot = fetch_spot("SX5E")          # display ticker
    spot = fetch_spot("^STOXX50E")     # yfinance ticker — also accepted
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# IBKR paper-account daily-bar limit (requesting > 3Y returns empty or partial data)
_IBKR_MAX_DAYS = 3 * 365

# Display ticker → Yahoo Finance ticker (mirrors TICKER_YFINANCE_MAP in atm_vol.py)
_DISPLAY_TO_YF: dict[str, str] = {
    # Indices
    "SX5E": "^STOXX50E", "SPX": "^GSPC", "NDX": "^NDX",
    "DAX":  "^GDAXI",    "NKY": "^N225", "CAC40": "^FCHI",
    # Euro Stoxx 50 constituents
    "ASML":    "ASML.AS",  "MC.PA":   "MC.PA",   "SAP":     "SAP.DE",
    "SIE":     "SIE.DE",   "OR.PA":   "OR.PA",   "TTE":     "TTE.PA",
    "SU.PA":   "SU.PA",    "AIR":     "AIR.PA",  "ALV":     "ALV.DE",
    "SAN.MC":  "SAN.MC",   "BNP":     "BNP.PA",  "AI.PA":   "AI.PA",
    "DTE":     "DTE.DE",   "IBE.MC":  "IBE.MC",  "SASY":    "SAN.PA",
    "ITX.MC":  "ITX.MC",   "UCG.MI":  "UCG.MI",  "INGA":    "INGA.AS",
    "BAS":     "BAS.DE",   "BMW":     "BMW.DE",   "BAYN":    "BAYN.DE",
    "BBVA.MC": "BBVA.MC",  "EL.PA":   "EL.PA",   "RMS.PA":  "RMS.PA",
    "ISP.MI":  "ISP.MI",   "DHL":     "DHL.DE",  "ENEL.MI": "ENEL.MI",
    "ENI.MI":  "ENI.MI",   "ABI.BR":  "ABI.BR",  "AD.AS":   "AD.AS",
    "ADYEN":   "ADYEN.AS", "ADS":     "ADS.DE",  "SGEF":    "DG.PA",
    "SAF.PA":  "SAF.PA",   "RACE.MI": "RACE.MI", "MUV2":    "MUV2.DE",
    "CRH":     "CRH.L",    "FLTR":    "FLTR.L",  "BN.PA":   "BN.PA",
    "DB1":     "DB1.DE",   "DBK":     "DBK.DE",  "IFX":     "IFX.DE",
    "PRX.AS":  "PRX.AS",   "CS.PA":   "CS.PA",   "KER.PA":  "KER.PA",
    "STLAM":   "STLAM.MI", "HEIA":    "HEIA.AS", "VOW3":    "VOW3.DE",
    "ENGI":    "ENGI.PA",  "NOKIA":   "NOKIA.HE",
}


def _to_yf(ticker: str) -> str:
    """Convert display ticker to yfinance ticker. Pass-through if already yf-style."""
    return _DISPLAY_TO_YF.get(ticker, ticker)


def fetch_history(
    ticker: str,
    start: str,
    end: Optional[str] = None,
    interval: str = "1d",
) -> pd.DataFrame:
    """
    Fetch OHLCV history. IBKR primary, yfinance fallback.

    Args:
        ticker:   Display ticker ("SX5E") or yfinance ticker ("^STOXX50E") — both accepted.
        start:    ISO date "YYYY-MM-DD"
        end:      ISO date, defaults to today
        interval: Bar size — "1d" for daily (only size IBKR paper reliably supports)

    Returns:
        DataFrame with columns [Open, High, Low, Close, Adj Close, Volume].
        Empty DataFrame if both sources fail.
    """
    from src.historical import yfinance_loader
    from src.connectivity.adapter_registry import get_adapter

    end_str  = end or date.today().isoformat()
    yf_ticker = _to_yf(ticker)

    start_dt = date.fromisoformat(start)
    end_dt   = date.fromisoformat(end_str)
    days     = (end_dt - start_dt).days

    adapter = get_adapter()

    from src.historical import disk_cache

    # Tier 1: IBKR — only for windows IBKR can reliably serve on a paper account
    if adapter is not None and adapter.is_healthy() and days <= _IBKR_MAX_DAYS:
        try:
            from src.historical import ibkr_loader
            df = ibkr_loader.fetch_index_history(
                adapter, yf_ticker, start=start, end=end_str, interval=interval,
            )
            if not df.empty and len(df) >= 5:
                log.info("data_fetcher: ibkr source ticker=%s rows=%d", ticker, len(df))
                disk_cache.save(yf_ticker, df)
                return df
            log.warning(
                "data_fetcher: ibkr returned <5 rows for ticker=%s — falling back",
                ticker,
            )
        except Exception as exc:
            log.warning(
                "data_fetcher: ibkr failed ticker=%s (%s) — falling back",
                ticker, exc,
            )
    elif days > _IBKR_MAX_DAYS:
        log.info(
            "data_fetcher: %d-day window exceeds IBKR limit — using yfinance for ticker=%s",
            days, ticker,
        )

    # Tier 2: yfinance live (fallback for recent data, primary for long history)
    try:
        df = yfinance_loader.fetch_index_history(yf_ticker, start=start, end=end_str, interval=interval)
        if not df.empty and len(df) >= 5:
            label = "yfinance-long-history" if days > _IBKR_MAX_DAYS else "yfinance-fallback"
            log.info("data_fetcher: %s ticker=%s rows=%d", label, ticker, len(df))
            disk_cache.save(yf_ticker, df)
            return df
    except Exception as exc:
        log.warning("data_fetcher: yfinance failed ticker=%s (%s) — trying disk cache", ticker, exc)

    # Tier 3: disk cache — offline safety net for presentations without internet/IBKR
    df = disk_cache.load(yf_ticker, start=start, end=end_str)
    if not df.empty:
        log.warning(
            "data_fetcher: serving ticker=%s from disk cache (%d rows) — no live source available",
            ticker, len(df),
        )
        return df

    log.error("data_fetcher: all sources failed for ticker=%s", ticker)
    return pd.DataFrame()


def fetch_spot(ticker: str) -> Optional[float]:
    """
    Fetch current spot price. IBKR snapshot primary, yfinance last close fallback.

    Args:
        ticker: Display ticker ("SX5E") or yfinance ticker ("^STOXX50E") — both accepted.

    Returns:
        Float spot price, or None if both sources fail (caller uses its own default).
    """
    from src.historical import yfinance_loader
    from src.connectivity.adapter_registry import get_adapter

    yf_ticker = _to_yf(ticker)
    adapter   = get_adapter()

    # IBKR snapshot path — live bid/ask midpoint
    if adapter is not None and adapter.is_healthy():
        try:
            from src.historical.ibkr_loader import IBKR_INDEX_MAP
            from src.connectivity.state import CanonicalContract

            params = IBKR_INDEX_MAP.get(yf_ticker)
            if params:
                sym, sec_type, exchange, currency = params
            else:
                # Stock ticker: strip Yahoo suffix and exchange prefix
                sym      = yf_ticker.lstrip("^").split(".")[0]
                sec_type = "STK"
                exchange = "SMART"
                currency = "EUR"

            contract = CanonicalContract(
                underlying_symbol=sym,
                sec_type=sec_type,
                exchange=exchange,
                currency=currency,
            )
            snapshot = adapter.request_snapshot(contract, timeout_s=5.0)

            if snapshot.bid is not None and snapshot.ask is not None:
                spot = (snapshot.bid + snapshot.ask) / 2.0
            elif snapshot.last is not None:
                spot = float(snapshot.last)
            else:
                raise ValueError("snapshot has no bid/ask/last")

            log.info("data_fetcher: ibkr spot ticker=%s spot=%.4f", ticker, spot)
            return spot

        except Exception as exc:
            log.warning(
                "data_fetcher: ibkr spot failed ticker=%s (%s) — falling back to yfinance",
                ticker, exc,
            )

    # Tier 2: yfinance fallback — last available close
    try:
        end_str   = date.today().isoformat()
        start_str = (date.today() - timedelta(days=7)).isoformat()
        df = yfinance_loader.fetch_index_history(yf_ticker, start=start_str, end=end_str)
        if not df.empty:
            spot = float(df["Close"].dropna().iloc[-1])
            log.info("data_fetcher: yfinance spot ticker=%s spot=%.4f", ticker, spot)
            return spot
    except Exception as exc:
        log.warning("data_fetcher: yfinance spot failed ticker=%s (%s) — trying disk cache", ticker, exc)

    # Tier 3: disk cache — use most recent cached close
    from src.historical import disk_cache
    spot = disk_cache.load_latest_close(yf_ticker)
    if spot is not None:
        log.warning(
            "data_fetcher: serving spot ticker=%s=%.4f from disk cache — no live source available",
            ticker, spot,
        )
        return spot

    return None


__all__ = ["fetch_history", "fetch_spot"]
