"""
Strategy router — Page 3: StrategyExecution.
Positions, L2 order book, hedge suggestions, and execution actions.
"""

from __future__ import annotations

import logging
import math

from fastapi import APIRouter
from pydantic import BaseModel

from src.connectivity.market_depth import fetch_order_book
from src.risk.hedge_suggest import compute_hedge_suggestions

router = APIRouter()
log = logging.getLogger(__name__)


def _nan(x: object) -> bool:
    """True when x is NaN, None, or non-numeric."""
    try:
        return math.isnan(float(x))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# Request bodies for POST endpoints
# ---------------------------------------------------------------------------

class HedgeRequest(BaseModel):
    action: str
    strategy_id: str


class RollRequest(BaseModel):
    strategy_id: str


class HedgeOrderRequest(BaseModel):
    strategy_id: str
    target_delta: float = 0.0


class LiquidateRequest(BaseModel):
    strategy_id: str


# ---------------------------------------------------------------------------
# Synthetic position data (replace with live IBKR aggregation)
# ---------------------------------------------------------------------------

_POSITIONS = [
    {
        "strategy_id":           "strat_001",
        "strategy_name":         "SX5E 12-Month Straddle",
        "strategy_label":        "ALPHA_CORE_V1",
        "target_strike":         "4200 / 4200",
        "expiry":                "12M (Dec 26)",
        "open_interest":         1450,
        "allocated_margin_eur":  2_400_000,
        "allocated_margin_pct":  14.2,
        "pnl_intraday_eur":      12_450,
        "live_exec":             True,
        "legs":                  ["Call 4200 DEC26", "Put 4200 DEC26"],
    },
    {
        "strategy_id":           "strat_002",
        "strategy_name":         "Dispersion Basket",
        "strategy_label":        "VOL_ARB_Q3",
        "target_strike":         "N/A",
        "expiry":                "3M (Sep 26)",
        "open_interest":         8200,
        "allocated_margin_eur":  5_100_000,
        "allocated_margin_pct":  22.5,
        "pnl_intraday_eur":      -3_210,
        "live_exec":             True,
        "legs":                  [],
    },
]


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------

@router.get("/positions")
def positions() -> list[dict]:
    """
    Strategy positions.

    When IBKR is connected: reads account portfolio via ib_insync, groups
    option legs into straddles (same underlying + expiry + strike), and maps
    each group to the strategy format expected by StrategyExecution.tsx.

    Falls back to synthetic `_POSITIONS` when the adapter is offline.
    """
    from src.connectivity.adapter_registry import get_adapter

    adapter = get_adapter()
    if adapter is not None and adapter.is_healthy():
        try:
            return _positions_from_ibkr(adapter)
        except Exception as exc:
            log.warning("positions: IBKR fetch failed (%s) — using synthetic fallback", exc)

    return _POSITIONS


def _positions_from_ibkr(adapter) -> list[dict]:
    """Convert ib_insync PortfolioItem list to the strategy position format."""
    from datetime import datetime
    from collections import defaultdict

    _NAV_TOTAL = 26_000_000

    ib = adapter._ib
    items = ib.portfolio()
    if not items:
        return _POSITIONS

    # Group option legs by (underlying_symbol, expiry_YYYYMM, strike)
    # Non-options go into a separate "spot/fut" bucket
    straddle_buckets: dict[tuple, list] = defaultdict(list)
    other: list = []

    for item in items:
        c = item.contract
        if c.secType == "OPT":
            expiry_mo = (c.lastTradeDateOrContractMonth or "")[:6]  # "202612"
            key = (c.symbol, expiry_mo, float(c.strike or 0))
            straddle_buckets[key].append(item)
        else:
            other.append(item)

    result: list[dict] = []
    counter = 1

    for (symbol, expiry_mo, strike), legs in straddle_buckets.items():
        calls = [i for i in legs if i.contract.right == "C"]
        puts  = [i for i in legs if i.contract.right == "P"]

        # Parse expiry label e.g. "202612" → "DEC 26"
        try:
            exp_date = datetime.strptime(expiry_mo, "%Y%m")
            expiry_label = exp_date.strftime("%b %y").upper()
        except Exception:
            expiry_label = expiry_mo

        unrealized = sum(
            float(i.unrealizedPNL) for i in legs if not _nan(i.unrealizedPNL)
        )
        mkt_value = sum(
            abs(float(i.marketValue)) for i in legs if not _nan(i.marketValue)
        )

        leg_labels = []
        for i in legs:
            right_word = "Call" if i.contract.right == "C" else "Put"
            leg_labels.append(f"{right_word} {int(strike)} {expiry_label}")

        strategy_name = (
            f"{symbol} {expiry_label} Straddle" if calls and puts
            else f"{symbol} {expiry_label} {'Call' if calls else 'Put'}"
        )
        target_strike = (
            f"{int(strike)} / {int(strike)}" if calls and puts else str(int(strike))
        )

        result.append({
            "strategy_id":          f"strat_{counter:03d}",
            "strategy_name":        strategy_name,
            "strategy_label":       "LIVE_IBKR",
            "target_strike":        target_strike,
            "expiry":               f"({expiry_label})",
            "open_interest":        int(sum(abs(float(i.position)) for i in legs)),
            "allocated_margin_eur": round(mkt_value),
            "allocated_margin_pct": round(mkt_value / _NAV_TOTAL * 100, 1),
            "pnl_intraday_eur":     round(unrealized),
            "live_exec":            True,
            "legs":                 leg_labels,
        })
        counter += 1

    for item in other:
        c = item.contract
        unrealized = float(item.unrealizedPNL) if not _nan(item.unrealizedPNL) else 0.0
        mkt_value  = abs(float(item.marketValue)) if not _nan(item.marketValue) else 0.0
        result.append({
            "strategy_id":          f"strat_{counter:03d}",
            "strategy_name":        f"{c.symbol} {c.secType}",
            "strategy_label":       "LIVE_IBKR",
            "target_strike":        "N/A",
            "expiry":               c.lastTradeDateOrContractMonth or "N/A",
            "open_interest":        int(abs(float(item.position))),
            "allocated_margin_eur": round(mkt_value),
            "allocated_margin_pct": round(mkt_value / _NAV_TOTAL * 100, 1),
            "pnl_intraday_eur":     round(unrealized),
            "live_exec":            True,
            "legs":                 [f"{c.symbol} {c.secType}"],
        })
        counter += 1

    return result if result else _POSITIONS


@router.get("/orderbook")
def orderbook(ticker: str = "SX5E") -> list[dict]:
    """Simulated L2 order book snapshot (top-of-book). Refreshes with slight jitter each call."""
    return fetch_order_book(ticker)


@router.get("/hedge-suggestions")
def hedge_suggestions() -> list[dict]:
    """Active hedge suggestions from the delta/vega engine.

    Passes live portfolio delta from portfolio_state (single source of truth)
    so the threshold alert reflects the same Greeks as all other endpoints.
    """
    from src.risk.portfolio_state import get_portfolio_greeks
    greeks = get_portfolio_greeks()
    return compute_hedge_suggestions(portfolio_delta=greeks["portfolio_delta"])


# ---------------------------------------------------------------------------
# POST endpoints (paper-trading, return acknowledgement only)
# ---------------------------------------------------------------------------

@router.post("/execute-hedge")
def execute_hedge(body: HedgeRequest) -> dict:
    log.info("execute-hedge strategy=%s action=%s", body.strategy_id, body.action)
    return {"status": "ok", "action": body.action, "strategy_id": body.strategy_id}


@router.post("/roll")
def roll(body: RollRequest) -> dict:
    log.info("roll strategy=%s", body.strategy_id)
    return {"status": "ok", "strategy_id": body.strategy_id, "message": "Roll order submitted"}


@router.post("/hedge")
def hedge(body: HedgeOrderRequest) -> dict:
    log.info("hedge strategy=%s target_delta=%s", body.strategy_id, body.target_delta)
    return {"status": "ok", "strategy_id": body.strategy_id, "message": "Delta hedge submitted"}


@router.post("/liquidate")
def liquidate(body: LiquidateRequest) -> dict:
    log.info("liquidate strategy=%s", body.strategy_id)
    return {"status": "ok", "strategy_id": body.strategy_id, "message": "Liquidation order submitted"}
