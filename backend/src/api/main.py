"""
FastAPI application entry point.

IBKR is connected on startup via the lifespan handler and stored in
adapter_registry. All data-fetching modules call adapter_registry.get_adapter()
and fall back to yfinance automatically when IBKR is unavailable.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routers import market, risk, strategy, backtest, shock, orders

# Load .env from backend root (if present). Lets you drop .env.school → .env at school.
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parents[2] / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
        logging.getLogger(__name__).info("startup: loaded env from %s", _env_path)
except ImportError:
    pass  # python-dotenv not installed — rely on shell environment

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ------------------------------------------------------------------ startup
    from src.connectivity.adapter_registry import build_adapter_from_env, set_adapter

    adapter = build_adapter_from_env()
    if adapter is not None:
        try:
            adapter.connect()
            set_adapter(adapter)
            log.info("startup: IBKR connected — primary data source is live")
        except Exception as exc:
            log.warning(
                "startup: IBKR connection failed (%s) — yfinance fallback active", exc
            )
            set_adapter(None)
    else:
        log.warning("startup: ib_insync not installed — yfinance fallback active")
        set_adapter(None)

    yield

    # ----------------------------------------------------------------- shutdown
    from src.connectivity.adapter_registry import get_adapter

    active = get_adapter()
    if active is not None:
        try:
            active.disconnect()
            log.info("shutdown: IBKR disconnected cleanly")
        except Exception as exc:
            log.warning("shutdown: error disconnecting IBKR (%s)", exc)


app = FastAPI(title="Vol Infra API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8080"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market.router,   prefix="/api/market")
app.include_router(risk.router,     prefix="/api/risk")
app.include_router(strategy.router, prefix="/api/strategy")
app.include_router(backtest.router, prefix="/api/backtest")
app.include_router(shock.router,    prefix="/api/shock")
app.include_router(orders.router,   prefix="/api/orders")
