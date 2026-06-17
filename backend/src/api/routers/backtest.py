"""
Backtest router — Page 4: Backtesting.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from src.backtest.engine import run_backtest
from src.backtest.monte_carlo import run_monte_carlo

router = APIRouter()
log = logging.getLogger(__name__)

_AVAILABLE_STRATEGIES = ["VOL_CARRY_01", "SX5E_STRADDLE", "DISPERSION_Q3"]


class BacktestRequest(BaseModel):
    strategy_id: str = "VOL_CARRY_01"
    start_date: str = "2005-01-01"
    end_date: str = "2026-06-14"
    rebalance_frequency: str = "weekly"
    shock_preset: Optional[str] = None


class ShockPresetRequest(BaseModel):
    preset: str


@router.get("/strategies")
def strategies() -> list[str]:
    return _AVAILABLE_STRATEGIES


@router.post("/run")
def run(body: BacktestRequest) -> dict:
    return run_backtest(
        strategy_id=body.strategy_id,
        start_date=body.start_date,
        end_date=body.end_date,
        rebalance_frequency=body.rebalance_frequency,
        shock_preset=body.shock_preset,
    )


@router.post("/shock-preset")
def shock_preset(body: ShockPresetRequest) -> dict:
    """Run the default strategy filtered to the named shock window."""
    return run_backtest(
        strategy_id="VOL_CARRY_01",
        start_date="2005-01-01",
        end_date="2026-06-14",
        shock_preset=body.preset,
    )


@router.get("/monte-carlo")
def monte_carlo(n_paths: int = 500, strategy_id: str = "VOL_CARRY_01") -> dict:
    return run_monte_carlo(n_paths=n_paths, strategy_id=strategy_id)
