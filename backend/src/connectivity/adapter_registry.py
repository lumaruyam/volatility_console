"""
Global IBKR adapter registry.

Holds the single shared IbkrAdapter instance for the process lifetime.
The FastAPI lifespan in src/api/main.py sets the adapter once on startup;
all data-fetching modules call get_adapter() to retrieve it.

Returns None when IBKR is not connected — callers fall back to yfinance.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# Module-level singleton — set once by main.py lifespan
_adapter = None


def get_adapter():
    """Return the active IbkrAdapter, or None if not connected."""
    return _adapter


def set_adapter(adapter) -> None:
    """Called by main.py lifespan. Pass None when connection failed."""
    global _adapter
    _adapter = adapter
    if adapter is not None and adapter.is_healthy():
        log.info("adapter_registry: IBKR adapter registered — live data active")
    else:
        log.warning("adapter_registry: no healthy IBKR adapter — yfinance fallback active")


def build_adapter_from_env() -> Optional[object]:
    """
    Instantiate IbkrAdapter from environment variables.
    Returns None if ib_insync is not installed or IBKR is explicitly disabled.

    Environment variables:
        IBKR_HOST              default 127.0.0.1
        IBKR_PORT              default 7497  (paper: 7497, live: 7496)
        IBKR_CLIENT_ID         default 1
        IBKR_ENABLED           set to 0/false/no to skip IBKR entirely
                               (school use: avoids 15-second port-blocked hang)
        IBKR_CONNECT_TIMEOUT   connect timeout in seconds, default 5
    """
    if os.getenv("IBKR_ENABLED", "1").lower() in ("0", "false", "no"):
        log.info("adapter_registry: IBKR disabled via IBKR_ENABLED — yfinance fallback active")
        return None

    try:
        from src.connectivity.ibkr_adapter import IbkrAdapter
    except ImportError:
        log.warning("adapter_registry: ib_insync not installed — IBKR disabled")
        return None

    host      = os.getenv("IBKR_HOST",      "127.0.0.1")
    port      = int(os.getenv("IBKR_PORT",  "7497"))
    client_id = int(os.getenv("IBKR_CLIENT_ID", "1"))
    timeout   = float(os.getenv("IBKR_CONNECT_TIMEOUT", "5"))

    log.info(
        "adapter_registry: building IbkrAdapter host=%s port=%s client_id=%s timeout=%.1fs",
        host, port, client_id, timeout,
    )
    return IbkrAdapter(
        host=host,
        port=port,
        client_id=client_id,
        read_only=False,    # need write for paper-trade order submission
        delayed_data=True,  # paper accounts use delayed data by default
        connect_timeout_s=timeout,
    )


__all__ = ["get_adapter", "set_adapter", "build_adapter_from_env"]
