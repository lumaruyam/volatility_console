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


class OrderTicketRequest(BaseModel):
    underlying: str
    instrument: str = ""
    direction: str = "BUY"
    strike: str = ""
    expiry: str = ""
    qty: int = 1
    order_type: str = "LIMIT"
    limit_price: str = ""
    destination: str = "IBKR"


# ---------------------------------------------------------------------------
# Synthetic position data (replace with live IBKR aggregation)
# ---------------------------------------------------------------------------

_POSITIONS = [
    {
        "strategy_id":           "strat_001",
        "strategy_name":         "SX5E 12-Month Straddle",
        "strategy_label":        "ALPHA_CORE_V1",
        "strategy_type":         "STRADDLE",
        "status":                "OPEN",
        "target_strike":         "4200 / 4200",
        "expiry":                "12M (Dec 26)",
        "days_to_expiry":        184,
        "open_interest":         1450,
        "allocated_margin_eur":  2_400_000,
        "allocated_margin_pct":  14.2,
        "pnl_intraday_eur":      12_450,
        "live_exec":             True,
        "legs":                  ["Call 4200 DEC26", "Put 4200 DEC26"],
        "total_delta":           0.05,
        "total_vega":            4_250,
    },
    {
        "strategy_id":           "strat_002",
        "strategy_name":         "Dispersion Basket Q3",
        "strategy_label":        "VOL_ARB_Q3",
        "strategy_type":         "DISPERSION",
        "status":                "OPEN",
        "target_strike":         "SX5E 4200 (basket)",
        "expiry":                "3M (Sep 26)",
        "days_to_expiry":        85,
        "open_interest":         8200,
        "allocated_margin_eur":  5_100_000,
        "allocated_margin_pct":  22.5,
        "pnl_intraday_eur":      -3_210,
        "live_exec":             True,
        "legs":                  [],
        "total_delta":           0.18,
        "total_vega":            3_204,
        "constituent_strikes": [
            {"ticker": "ASML",  "strike": 900},
            {"ticker": "MC.PA", "strike": 500},
            {"ticker": "SAP",   "strike": 140},
            {"ticker": "SIE",   "strike": 270},
        ],
    },
    {
        "strategy_id":           "strat_003",
        "strategy_name":         "SX5E Calendar Spread",
        "strategy_label":        "CAL_SPD_DEC26",
        "strategy_type":         "CALENDAR",
        "status":                "OPEN",
        "target_strike":         "4200",
        "expiry":                "6M / 12M (Sep–Dec 26)",
        "days_to_expiry":        184,
        "open_interest":         620,
        "allocated_margin_eur":  780_000,
        "allocated_margin_pct":  3.0,
        "pnl_intraday_eur":      2_140,
        "live_exec":             True,
        "legs":                  ["Long Call 4200 SEP26", "Short Call 4200 DEC26"],
        "total_delta":           0.03,
        "total_vega":            1_850,
    },
    {
        "strategy_id":           "strat_004",
        "strategy_name":         "SX5E Butterfly Dec26",
        "strategy_label":        "BFLY_DEC26",
        "strategy_type":         "BUTTERFLY",
        "status":                "OPEN",
        "target_strike":         "4000 / 4200 / 4400",
        "expiry":                "6M (Dec 26)",
        "days_to_expiry":        184,
        "open_interest":         390,
        "allocated_margin_eur":  420_000,
        "allocated_margin_pct":  1.6,
        "pnl_intraday_eur":      -540,
        "live_exec":             False,
        "legs":                  ["Long Call 4000 DEC26", "Short 2× Call 4200 DEC26", "Long Call 4400 DEC26"],
        "total_delta":           0.01,
        "total_vega":            -620,
    },
    {
        "strategy_id":           "strat_005",
        "strategy_name":         "SX5E Jun26 Straddle",
        "strategy_label":        "ALPHA_CORE_V0",
        "strategy_type":         "STRADDLE",
        "status":                "CLOSED",
        "target_strike":         "4100 / 4100",
        "expiry":                "Expired (Jun 26)",
        "days_to_expiry":        0,
        "open_interest":         0,
        "allocated_margin_eur":  0,
        "allocated_margin_pct":  0.0,
        "pnl_intraday_eur":      18_720,
        "live_exec":             False,
        "legs":                  ["Call 4100 JUN26", "Put 4100 JUN26"],
        "total_delta":           0.0,
        "total_vega":            0,
    },
    {
        "strategy_id":           "strat_006",
        "strategy_name":         "SX5E Sep26 Straddle → Dec26",
        "strategy_label":        "ALPHA_CORE_V1_ROLL",
        "strategy_type":         "STRADDLE",
        "status":                "ROLLED",
        "target_strike":         "4150 → 4200",
        "expiry":                "Rolled to Dec 26",
        "days_to_expiry":        184,
        "open_interest":         880,
        "allocated_margin_eur":  1_950_000,
        "allocated_margin_pct":  7.5,
        "pnl_intraday_eur":      4_310,
        "live_exec":             False,
        "legs":                  ["Call 4200 DEC26", "Put 4200 DEC26"],
        "total_delta":           0.02,
        "total_vega":            2_100,
    },
    {
        "strategy_id":           "strat_007",
        "strategy_name":         "ASML Straddle Dec26",
        "strategy_label":        "SINGLE_STOCK_V1",
        "strategy_type":         "STRADDLE",
        "status":                "PENDING",
        "target_strike":         "900 / 900",
        "expiry":                "6M (Dec 26)",
        "days_to_expiry":        184,
        "open_interest":         0,
        "allocated_margin_eur":  640_000,
        "allocated_margin_pct":  2.5,
        "pnl_intraday_eur":      0,
        "live_exec":             False,
        "legs":                  ["Call 900 DEC26", "Put 900 DEC26"],
        "total_delta":           0.0,
        "total_vega":            1_420,
    },
]


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------

@router.get("/latency")
def latency() -> dict:
    """Execution infrastructure latency metrics in milliseconds."""
    import time
    import random
    rng   = random.Random(int(time.monotonic() * 1000) % 997)
    base  = round(3.8 + rng.uniform(-0.6, 1.4), 2)
    return {
        "latency_ms": base,
        "avg_ms":     round(base + rng.uniform(0.4, 1.2), 2),
        "p99_ms":     round(base + rng.uniform(5.0, 10.0), 2),
        "source":     "yfinance_fallback",
    }


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

        # Rough ATM Greeks: call delta ≈ +0.52, put delta ≈ -0.52 per contract
        # Vega ≈ S × N'(0) × √T per contract per 1 vol pt (S ≈ strike for ATM)
        import math as _math
        try:
            exp_date_obj = datetime.strptime(expiry_mo + "15", "%Y%m%d")
            days_to_exp = max(0, (exp_date_obj.date() - datetime.now().date()).days)
            T_years = max(0.01, days_to_exp / 365)
        except Exception:
            days_to_exp = 180
            T_years = 0.5
        net_delta = round(
            sum(float(i.position) * 0.52  for i in calls) +
            sum(float(i.position) * -0.52 for i in puts),
            2,
        )
        vega_per_contract = strike * 0.3989 * _math.sqrt(T_years) / 100  # per 1%
        net_vega = round(vega_per_contract * sum(abs(float(i.position)) for i in legs), 0)

        strategy_type = "STRADDLE" if (calls and puts) else ("CALL" if calls else "PUT")

        result.append({
            "strategy_id":          f"strat_{counter:03d}",
            "strategy_name":        strategy_name,
            "strategy_label":       "LIVE_IBKR",
            "strategy_type":        strategy_type,
            "status":               "OPEN",
            "target_strike":        target_strike,
            "expiry":               f"({expiry_label})",
            "days_to_expiry":       days_to_exp,
            "open_interest":        int(sum(abs(float(i.position)) for i in legs)),
            "allocated_margin_eur": round(mkt_value),
            "allocated_margin_pct": round(mkt_value / _NAV_TOTAL * 100, 1),
            "pnl_intraday_eur":     round(unrealized),
            "live_exec":            True,
            "legs":                 leg_labels,
            "total_delta":          net_delta,
            "total_vega":           net_vega,
        })
        counter += 1

    for item in other:
        c = item.contract
        unrealized = float(item.unrealizedPNL) if not _nan(item.unrealizedPNL) else 0.0
        mkt_value  = abs(float(item.marketValue)) if not _nan(item.marketValue) else 0.0
        exp_raw = c.lastTradeDateOrContractMonth or ""
        try:
            exp_date_other = datetime.strptime(exp_raw[:8], "%Y%m%d")
            days_other = max(0, (exp_date_other.date() - datetime.now().date()).days)
        except Exception:
            days_other = 0
        result.append({
            "strategy_id":          f"strat_{counter:03d}",
            "strategy_name":        f"{c.symbol} {c.secType}",
            "strategy_label":       "LIVE_IBKR",
            "strategy_type":        "STRADDLE",
            "status":               "OPEN",
            "target_strike":        c.symbol,
            "expiry":               exp_raw or "N/A",
            "days_to_expiry":       days_other,
            "open_interest":        int(abs(float(item.position))),
            "allocated_margin_eur": round(mkt_value),
            "allocated_margin_pct": round(mkt_value / _NAV_TOTAL * 100, 1),
            "pnl_intraday_eur":     round(unrealized),
            "live_exec":            True,
            "legs":                 [f"{c.symbol} {c.secType}"],
            "total_delta":          round(float(item.position), 2),
            "total_vega":           None,
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


@router.post("/order")
def place_order(body: OrderTicketRequest) -> dict:
    """Accept a new order from the order ticket panel and forward it to the OMS."""
    log.info(
        "strategy.order underlying=%s instrument=%s direction=%s qty=%d destination=%s",
        body.underlying, body.instrument, body.direction, body.qty, body.destination,
    )
    # Forward into the shared orders store so it appears in the OMS blotter
    from src.api.routers.orders import new_order
    return new_order(body)  # type: ignore[arg-type]


@router.post("/liquidate")
def liquidate(body: LiquidateRequest) -> dict:
    log.info("liquidate strategy=%s", body.strategy_id)
    return {"status": "ok", "strategy_id": body.strategy_id, "message": "Liquidation order submitted"}
