"""
Offline disk cache for OHLCV data.

One Parquet file per ticker in data/cache/ohlcv/.
Used as a final fallback when both IBKR and yfinance are unreachable
(e.g. presenting at school where IBKR port 7497 is blocked).

Populate before the presentation with:
    cd backend && python scripts/seed_cache.py
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# Resolve relative to this file's location so it works from any working dir
_CACHE_DIR = Path(__file__).parents[2] / "data" / "cache" / "ohlcv"


def _path(ticker: str) -> Path:
    safe = ticker.replace("^", "").replace(".", "_").replace("/", "_")
    return _CACHE_DIR / f"{safe}.parquet"


def load(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Read cached OHLCV for ticker, filtered to [start, end]. Returns empty DF on miss."""
    p = _path(ticker)
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index, utc=True)
        mask = (df.index >= pd.Timestamp(start, tz="UTC")) & \
               (df.index <= pd.Timestamp(end + "T23:59:59", tz="UTC"))
        sliced = df.loc[mask]
        if not sliced.empty:
            log.info("disk_cache: hit ticker=%s rows=%d", ticker, len(sliced))
        return sliced
    except Exception as exc:
        log.warning("disk_cache: read failed ticker=%s: %s", ticker, exc)
        return pd.DataFrame()


def load_latest_close(ticker: str) -> Optional[float]:
    """Return the most recent cached close price, or None if cache is empty."""
    p = _path(ticker)
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
        closes = df["Close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1])
    except Exception as exc:
        log.warning("disk_cache: load_latest_close failed ticker=%s: %s", ticker, exc)
        return None


def save(ticker: str, df: pd.DataFrame) -> None:
    """Merge df into the existing cache file (deduplicating by date index)."""
    if df is None or df.empty:
        return
    p = _path(ticker)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        incoming = df.copy()
        incoming.index = pd.to_datetime(incoming.index, utc=True)
        if p.exists():
            existing = pd.read_parquet(p)
            existing.index = pd.to_datetime(existing.index, utc=True)
            combined = pd.concat([existing, incoming])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        else:
            combined = incoming.sort_index()
        combined.to_parquet(p)
        log.debug("disk_cache: saved ticker=%s total_rows=%d", ticker, len(combined))
    except Exception as exc:
        log.warning("disk_cache: write failed ticker=%s: %s", ticker, exc)


def cache_info() -> list[dict]:
    """Return a summary of what's in the cache (for diagnostics)."""
    if not _CACHE_DIR.exists():
        return []
    results = []
    for p in sorted(_CACHE_DIR.glob("*.parquet")):
        try:
            df = pd.read_parquet(p)
            df.index = pd.to_datetime(df.index, utc=True)
            results.append({
                "ticker_file": p.stem,
                "rows": len(df),
                "start": df.index.min().date().isoformat() if not df.empty else None,
                "end":   df.index.max().date().isoformat() if not df.empty else None,
                "size_kb": round(p.stat().st_size / 1024, 1),
            })
        except Exception:
            results.append({"ticker_file": p.stem, "rows": 0, "error": True})
    return results
