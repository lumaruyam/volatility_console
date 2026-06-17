"""
ATM implied volatility estimator.

Primary: reads stored surface parameters from data/storage/.
Fallback: 30-day historical realized volatility from yfinance as ATM vol proxy.
"""

from __future__ import annotations

import math
import logging
import time
from datetime import date, timedelta
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Display ticker → Yahoo Finance ticker
TICKER_YFINANCE_MAP: dict[str, str] = {
    # Indices (used by risk/backtest pages)
    "SX5E":  "^STOXX50E", "SPX": "^GSPC", "NDX": "^NDX",
    "DAX":   "^GDAXI",    "NKY": "^N225", "CAC40": "^FCHI",
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

_SPOT_DEFAULTS: dict[str, float] = {
    # Indices
    "SX5E": 4952.8, "SPX": 5123.4, "NDX": 18042.0, "DAX": 17850.0, "NKY": 39210.0,
    # Euro Stoxx 50 constituents
    "ASML": 889.30,  "MC.PA": 512.60,  "SAP": 143.02,   "SIE": 274.17,  "OR.PA": 385.85,
    "TTE":   78.00,  "SU.PA": 276.95,  "AIR": 183.68,   "ALV": 397.10,  "SAN.MC":  11.63,
    "BNP":   98.65,  "AI.PA": 165.86,  "DTE":  28.01, "IBE.MC":  20.49,  "SASY":   76.30,
    "ITX.MC": 56.64, "UCG.MI": 77.64, "INGA":  26.33,   "BAS":  49.30,   "BMW":    67.14,
    "BAYN":  36.08, "BBVA.MC": 21.10, "EL.PA": 184.05, "RMS.PA": 1712.00, "ISP.MI":  6.05,
    "DHL":   52.82, "ENEL.MI":  9.93, "ENI.MI": 22.01, "ABI.BR":  70.76,  "AD.AS":  36.01,
    "ADYEN": 858.90,  "ADS": 174.55,  "SGEF": 123.35,  "SAF.PA": 324.00, "RACE.MI": 310.30,
    "MUV2": 461.50,   "CRH":  74.20,  "FLTR": 185.40,   "BN.PA":  66.46,   "DB1":  248.60,
    "DBK":   30.30,   "IFX":  79.98, "PRX.AS":  39.21,  "CS.PA":  41.88, "KER.PA": 259.20,
    "STLAM":  5.97,  "HEIA":  92.45,  "VOW3":  85.42,   "ENGI":  27.01,  "NOKIA":   12.10,
}

_VOL_DEFAULTS: dict[str, float] = {
    # Indices
    "SX5E": 0.142, "SPX": 0.128, "NDX": 0.165, "DAX": 0.151, "NKY": 0.182,
    # Euro Stoxx 50 constituents
    "ASML": 0.242, "MC.PA": 0.198,  "SAP": 0.215,   "SIE": 0.221, "OR.PA": 0.174,
    "TTE":  0.236, "SU.PA": 0.208,  "AIR": 0.251,   "ALV": 0.162, "SAN.MC": 0.284,
    "BNP":  0.267, "AI.PA": 0.159,  "DTE": 0.148, "IBE.MC": 0.171,  "SASY": 0.168,
    "ITX.MC": 0.192, "UCG.MI": 0.315, "INGA": 0.259, "BAS": 0.210,  "BMW": 0.234,
    "BAYN": 0.342, "BBVA.MC": 0.291, "EL.PA": 0.185, "RMS.PA": 0.227, "ISP.MI": 0.246,
    "DHL":  0.203, "ENEL.MI": 0.190, "ENI.MI": 0.225, "ABI.BR": 0.189, "AD.AS": 0.153,
    "ADYEN": 0.416, "ADS": 0.278, "SGEF": 0.181, "SAF.PA": 0.212, "RACE.MI": 0.250,
    "MUV2": 0.175,  "CRH": 0.239, "FLTR": 0.280,  "BN.PA": 0.157,  "DB1": 0.164,
    "DBK":  0.295,  "IFX": 0.331, "PRX.AS": 0.263, "CS.PA": 0.179, "KER.PA": 0.285,
    "STLAM": 0.320, "HEIA": 0.183, "VOW3": 0.261,  "ENGI": 0.214, "NOKIA": 0.248,
}

# Simple TTL cache: ticker → (value, expiry_epoch)
_spot_cache: dict[str, tuple[float, float]] = {}
_vol_cache: dict[str, tuple[float, float]] = {}
_CACHE_TTL = 120.0  # seconds


def get_spot(ticker: str) -> float:
    """Most recent close price for ticker, cached for 2 minutes."""
    now = time.monotonic()
    cached = _spot_cache.get(ticker)
    if cached and cached[1] > now:
        return cached[0]

    value = _fetch_spot(ticker)
    _spot_cache[ticker] = (value, now + _CACHE_TTL)
    return value


def get_atm_vol(ticker: str, expiry: Optional[str] = None) -> float:
    """ATM vol estimate for ticker, cached for 2 minutes."""
    now = time.monotonic()
    cached = _vol_cache.get(ticker)
    if cached and cached[1] > now:
        return cached[0]

    value = _realized_vol(ticker)
    _vol_cache[ticker] = (value, now + _CACHE_TTL)
    return value


def _fetch_spot(ticker: str) -> float:
    try:
        from src.historical.data_fetcher import fetch_spot
        spot = fetch_spot(ticker)
        if spot is not None:
            return spot
    except Exception as exc:
        log.warning("get_spot fallback for %s: %s", ticker, exc)
    return _SPOT_DEFAULTS.get(ticker, 100.0)


def _realized_vol(ticker: str, window_days: int = 30) -> float:
    """Annualised realized vol from rolling window as ATM vol proxy."""
    try:
        from src.historical.data_fetcher import fetch_history
        yf_ticker = TICKER_YFINANCE_MAP.get(ticker, ticker)
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=window_days * 3)).isoformat()
        df = fetch_history(yf_ticker, start=start, end=end)
        if df.empty or "Close" not in df.columns:
            return _VOL_DEFAULTS.get(ticker, 0.20)
        closes = df["Close"].dropna()
        if len(closes) < 5:
            return _VOL_DEFAULTS.get(ticker, 0.20)
        log_rets = np.log(closes / closes.shift(1)).dropna()
        vol = float(log_rets.tail(window_days).std() * math.sqrt(252))
        return round(max(0.05, min(vol, 1.0)), 4)
    except Exception as exc:
        log.warning("realized_vol fallback for %s: %s", ticker, exc)
        return _VOL_DEFAULTS.get(ticker, 0.20)
