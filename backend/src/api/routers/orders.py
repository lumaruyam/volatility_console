"""
Orders router — Page 6: OMS blotter.

GET  /blotter              → current order list (synthetic seed, IBKR when connected)
POST /{order_id}/cancel    → cancel a staged/submitted order
POST /new                  → acknowledge a new order from the order ticket
"""

from __future__ import annotations

import logging
from copy import deepcopy

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
log = logging.getLogger(__name__)

# In-memory order store — seeded with realistic Euro Stoxx 50 universe orders.
# In production this would query IBKR ib.openOrders() / ib.trades().
_ORDERS: list[dict] = [
    {"order_id": "ord_a1b2c3d4e5f60001", "status": "filled",    "side": "BUY",  "qty": 50,  "underlying": "SX5E",  "expiry": "20260717", "strike": 4200, "right": "C", "order_type": "LMT", "limit_price": 82.40, "filled_qty": 50,  "fill_price": 82.35},
    {"order_id": "ord_a1b2c3d4e5f60002", "status": "submitted", "side": "SELL", "qty": 25,  "underlying": "SX5E",  "expiry": "20260717", "strike": 4400, "right": "C", "order_type": "LMT", "limit_price": 41.10, "filled_qty": 0},
    {"order_id": "ord_a1b2c3d4e5f60003", "status": "partial",   "side": "BUY",  "qty": 100, "underlying": "ASML",  "expiry": "20261215", "strike": 900,  "right": "P", "order_type": "LMT", "limit_price": 54.25, "filled_qty": 35,  "fill_price": 54.20},
    {"order_id": "ord_a1b2c3d4e5f60004", "status": "staged",    "side": "SELL", "qty": 75,  "underlying": "MC.PA", "expiry": "20260919", "strike": 510,  "right": "P", "order_type": "LMT", "limit_price": 9.85,  "filled_qty": 0},
    {"order_id": "ord_a1b2c3d4e5f60005", "status": "rejected",  "side": "BUY",  "qty": 40,  "underlying": "ASML",  "expiry": "20260821", "strike": 1000, "right": "C", "order_type": "MKT", "filled_qty": 0, "reason": "Insufficient buying power — margin check failed"},
    {"order_id": "ord_a1b2c3d4e5f60006", "status": "filled",    "side": "SELL", "qty": 30,  "underlying": "SX5E",  "expiry": "20260619", "strike": 4000, "right": "P", "order_type": "LMT", "limit_price": 12.80, "filled_qty": 30,  "fill_price": 12.92},
    {"order_id": "ord_a1b2c3d4e5f60007", "status": "cancelled", "side": "BUY",  "qty": 60,  "underlying": "SAP",   "expiry": "20261016", "strike": 140,  "right": "C", "order_type": "LMT", "limit_price": 7.55,  "filled_qty": 0, "reason": "User cancelled before fill"},
    {"order_id": "ord_a1b2c3d4e5f60008", "status": "submitted", "side": "BUY",  "qty": 200, "underlying": "SX5E",  "expiry": "20260918", "strike": 4100, "right": "C", "order_type": "LMT", "limit_price": 14.20, "filled_qty": 0},
    {"order_id": "ord_a1b2c3d4e5f60009", "status": "staged",    "side": "SELL", "qty": 15,  "underlying": "TTE",   "expiry": "20261218", "strike": 60,   "right": "P", "order_type": "LMT", "limit_price": 11.05, "filled_qty": 0},
    {"order_id": "ord_a1b2c3d4e5f60010", "status": "rejected",  "side": "SELL", "qty": 80,  "underlying": "SX5E",  "expiry": "20260717", "strike": 4600, "right": "C", "order_type": "LMT", "limit_price": 28.60, "filled_qty": 0, "reason": "Price outside NBBO tolerance band"},
    {"order_id": "ord_a1b2c3d4e5f60011", "status": "filled",    "side": "BUY",  "qty": 45,  "underlying": "SIE",   "expiry": "20260918", "strike": 270,  "right": "C", "order_type": "MKT", "filled_qty": 45, "fill_price": 6.18},
    {"order_id": "ord_a1b2c3d4e5f60012", "status": "partial",   "side": "SELL", "qty": 120, "underlying": "OR.PA", "expiry": "20261120", "strike": 220,  "right": "P", "order_type": "LMT", "limit_price": 18.40, "filled_qty": 70, "fill_price": 18.38},
]

_ORDER_COUNTER = [13]  # mutable counter for new order IDs


class NewOrderRequest(BaseModel):
    underlying: str
    instrument: str = ""
    direction: str = "BUY"
    strike: str = ""
    expiry: str = ""
    qty: int = 1
    order_type: str = "LMT"
    limit_price: str = ""
    destination: str = "IBKR"


@router.get("/blotter")
def blotter() -> list[dict]:
    """Return current OMS order blotter. Newest first."""
    return deepcopy(list(reversed(_ORDERS)))


@router.post("/{order_id}/cancel")
def cancel(order_id: str) -> dict:
    """Transition a staged/submitted/partial order to cancelled."""
    for order in _ORDERS:
        if order["order_id"] == order_id:
            if order["status"] in ("staged", "submitted", "partial"):
                order["status"] = "cancelled"
                log.info("orders.cancel order_id=%s", order_id)
                return {"status": "ok", "order_id": order_id}
            raise HTTPException(
                status_code=409,
                detail=f"Order {order_id} has status '{order['status']}' and cannot be cancelled",
            )
    raise HTTPException(status_code=404, detail=f"Order {order_id} not found")


@router.post("/new")
def new_order(body: NewOrderRequest) -> dict:
    """Accept a new order from the order ticket and add it to the blotter."""
    try:
        strike_val = float(body.strike) if body.strike else 0.0
        lp_val     = float(body.limit_price) if body.limit_price else None
    except ValueError:
        strike_val = 0.0
        lp_val     = None

    oid = f"ord_a1b2c3d4e5f6{_ORDER_COUNTER[0]:04d}"
    _ORDER_COUNTER[0] += 1

    order: dict = {
        "order_id":   oid,
        "status":     "staged",
        "side":       body.direction,
        "qty":        body.qty,
        "underlying": body.underlying,
        "expiry":     body.expiry.replace("-", "") if body.expiry else "",
        "strike":     strike_val,
        "right":      "C" if "call" in body.instrument.lower() else "P",
        "order_type": "LMT" if body.order_type == "LIMIT" else "MKT",
        "filled_qty": 0,
    }
    if lp_val is not None:
        order["limit_price"] = lp_val

    _ORDERS.append(order)
    log.info("orders.new order_id=%s underlying=%s side=%s qty=%d", oid, body.underlying, body.direction, body.qty)
    return {"status": "ok", "order_id": oid, "message": f"Order staged for routing to {body.destination}"}
