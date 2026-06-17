"""
Order manager for paper-trading via IBKR.

Wraps IBKR session calls for order placement, cancellation, and status queries.
All state is held by the broker; this module never stores orders locally.

Rules:
  - read_only=True: raises ReadOnlyModeError on any placement attempt.
  - paper_trading=True (config): logs a WARNING on every real-looking order
    as a reminder that this is not production.
  - Never place orders inside market-data callbacks.
  - OrderResult always returned — never raise on broker rejection; set status="rejected".

Acceptance criterion: straddle opens/rolls without errors in paper account.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.strategy.straddle import StraddleLeg, StraddlePosition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ReadOnlyModeError(Exception):
    """Raised when order placement is attempted in read-only mode."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class OrderRequest:
    """
    A single order to be placed on one contract.
    action: "BUY" | "SELL"
    order_type: "MKT" | "LMT"
    """
    contract_key: str
    action: str              # "BUY" | "SELL"
    quantity: float          # Positive absolute quantity
    order_type: str = "LMT"
    limit_price: Optional[float] = None
    time_in_force: str = "DAY"
    account: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if self.action not in ("BUY", "SELL"):
            raise ValueError(f"action must be 'BUY' or 'SELL', got {self.action!r}")
        if self.order_type not in ("MKT", "LMT"):
            raise ValueError(f"order_type must be 'MKT' or 'LMT', got {self.order_type!r}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.order_type == "LMT" and self.limit_price is None:
            raise ValueError("LMT order requires a limit_price")


@dataclass
class OrderResult:
    """
    Result of one order placement attempt.
    status: "submitted" | "filled" | "cancelled" | "rejected" | "dry_run"
    """
    order_id: str
    contract_key: str
    action: str
    quantity: float
    status: str
    filled_price: Optional[float] = None
    filled_quantity: float = 0.0
    message: str = ""

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"

    @property
    def is_submitted(self) -> bool:
        return self.status == "submitted"


# ---------------------------------------------------------------------------
# Order manager
# ---------------------------------------------------------------------------

class OrderManager:
    """
    Thin wrapper around the IBKR session for order operations.

    Args:
        session:      Live IBKR session object (or mock).
        read_only:    If True, raise ReadOnlyModeError on any placement.
        paper_trading: If True, emit WARNING on every order as a safety reminder.
    """

    def __init__(self, session, read_only: bool = False,
                 paper_trading: bool = True):
        self.session = session
        self.read_only = read_only
        self.paper_trading = paper_trading

    def place_order(self, request: OrderRequest) -> OrderResult:
        """
        Submit one order to the broker.

        Raises ReadOnlyModeError if read_only=True.
        Returns OrderResult with status="rejected" on broker failure (never raises).
        """
        if self.read_only:
            raise ReadOnlyModeError(
                f"Cannot place {request.action} order for {request.contract_key} "
                "— OrderManager is in read-only mode."
            )

        if self.paper_trading:
            logger.warning(
                "PAPER TRADING: placing %s %g × %s @ %s",
                request.action, request.quantity, request.contract_key,
                request.limit_price or "MKT",
            )

        logger.info(
            "order.place contract=%s action=%s qty=%g type=%s price=%s",
            request.contract_key, request.action, request.quantity,
            request.order_type, request.limit_price,
        )

        try:
            broker_result = self.session.place_order(
                contract_key=request.contract_key,
                action=request.action,
                quantity=request.quantity,
                order_type=request.order_type,
                limit_price=request.limit_price,
                time_in_force=request.time_in_force,
                account=request.account,
            )
            order_id = broker_result.get("order_id", str(uuid.uuid4())[:8])
            status = broker_result.get("status", "submitted")
            filled_price = broker_result.get("filled_price")
            filled_qty = float(broker_result.get("filled_quantity", 0.0))

            logger.info(
                "order.placed order_id=%s status=%s", order_id, status,
            )
            return OrderResult(
                order_id=order_id,
                contract_key=request.contract_key,
                action=request.action,
                quantity=request.quantity,
                status=status,
                filled_price=filled_price,
                filled_quantity=filled_qty,
            )
        except Exception as exc:
            logger.error(
                "order.rejected contract=%s error=%s", request.contract_key, exc,
            )
            return OrderResult(
                order_id="",
                contract_key=request.contract_key,
                action=request.action,
                quantity=request.quantity,
                status="rejected",
                message=str(exc),
            )

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a previously submitted order.
        Returns True on success, False if the broker rejects the cancellation.
        """
        if self.read_only:
            raise ReadOnlyModeError("Cannot cancel orders in read-only mode.")

        logger.info("order.cancel order_id=%s", order_id)
        try:
            result = self.session.cancel_order(order_id=order_id)
            success = bool(result.get("success", True))
            logger.info("order.cancelled order_id=%s success=%s", order_id, success)
            return success
        except Exception as exc:
            logger.error("order.cancel_failed order_id=%s error=%s", order_id, exc)
            return False

    def get_order_status(self, order_id: str) -> OrderResult:
        """
        Query the current status of a previously placed order.
        Returns OrderResult; status="rejected" if query fails.
        """
        logger.info("order.status_query order_id=%s", order_id)
        try:
            broker_result = self.session.get_order_status(order_id=order_id)
            return OrderResult(
                order_id=order_id,
                contract_key=broker_result.get("contract_key", ""),
                action=broker_result.get("action", ""),
                quantity=float(broker_result.get("quantity", 0.0)),
                status=broker_result.get("status", "unknown"),
                filled_price=broker_result.get("filled_price"),
                filled_quantity=float(broker_result.get("filled_quantity", 0.0)),
            )
        except Exception as exc:
            logger.error("order.status_failed order_id=%s error=%s", order_id, exc)
            return OrderResult(
                order_id=order_id, contract_key="", action="",
                quantity=0.0, status="rejected", message=str(exc),
            )

    # -------------------------------------------------------------------------
    # Straddle helpers
    # -------------------------------------------------------------------------

    def open_straddle_orders(
        self,
        position: StraddlePosition,
        config: dict,
    ) -> list[OrderResult]:
        """
        Place BUY orders for both legs of a new straddle.
        Returns list of two OrderResults [call_result, put_result].
        """
        order_type = config.get("order_type", "LMT")
        tif = config.get("time_in_force", "DAY")
        results = []
        for leg in position.legs:
            limit_price = leg.open_price if order_type == "LMT" else None
            req = OrderRequest(
                contract_key=leg.contract_key,
                action="BUY",
                quantity=leg.quantity,
                order_type=order_type,
                limit_price=limit_price,
                time_in_force=tif,
            )
            results.append(self.place_order(req))
        return results

    def close_straddle_orders(
        self,
        position: StraddlePosition,
        close_prices: dict[str, float],
        config: dict,
    ) -> list[OrderResult]:
        """
        Place SELL orders for both legs of an existing straddle.
        close_prices: {contract_key: current_mid_price}
        Returns list of two OrderResults [call_result, put_result].
        """
        order_type = config.get("order_type", "LMT")
        tif = config.get("time_in_force", "DAY")
        results = []
        for leg in position.legs:
            limit_price = close_prices.get(leg.contract_key, leg.open_price) \
                if order_type == "LMT" else None
            req = OrderRequest(
                contract_key=leg.contract_key,
                action="SELL",
                quantity=leg.quantity,
                order_type=order_type,
                limit_price=limit_price,
                time_in_force=tif,
            )
            results.append(self.place_order(req))
        return results
