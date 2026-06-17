"""IBKR-specific implementation of :class:`BrokerAdapter`.

This module is the only place where ``ib_async`` types are imported. All
downstream consumers see normalized :class:`CanonicalContract` and
:class:`QuoteSnapshot` records, never IBKR ``Contract`` or ``Ticker``.

The adapter is intentionally thin. It does not normalize event streams from
streaming subscriptions in Step 1: that belongs to the collectors layer
(Step 3). Step 1 covers only:
    - connect / disconnect lifecycle
    - synchronous contract resolution
    - synchronous one-shot snapshot retrieval
    - heartbeat tracking
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from src.connectivity.state import (
    BrokerAdapter,
    CanonicalContract,
    QuoteSnapshot,
)
from src.utils.logging import get_logger
from src.utils.time_utils import now_utc

if TYPE_CHECKING:  # pragma: no cover
    from ib_async import IB, Contract  # type: ignore[import-not-found]

log = get_logger(__name__)


def _is_nan(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _coalesce_nan(x: float | None) -> float | None:
    """Convert NaN sentinels (which IBKR uses freely) to ``None``."""
    return None if _is_nan(x) else x


class IbkrAdapter(BrokerAdapter):
    """IBKR adapter using ``ib_async``.

    Parameters
    ----------
    host, port, client_id, account
        Connection parameters. Source from :class:`IbkrConfig`.
    connect_timeout_s
        Hard timeout on the initial connect handshake.
    read_only
        If True, the API session is opened read-only. Step 1 requires this.
    delayed_data
        If True, request delayed (free) market data via
        ``reqMarketDataType(3)``. Paper accounts without OPRA entitlements
        should set this to True.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        account: str = "",
        connect_timeout_s: float = 15.0,
        read_only: bool = True,
        delayed_data: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.account = account
        self.connect_timeout_s = connect_timeout_s
        self.read_only = read_only
        self.delayed_data = delayed_data
        self._ib: IB | None = None
        self._last_heartbeat: float | None = None  # monotonic seconds

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the IBKR session and verify a round-trip succeeds."""
        # Import inside the method so the rest of the codebase can be
        # imported without ib_async being installed (helpful in CI).
        try:
            from ib_async import IB  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ib_async is not installed. Run `uv pip install -e .` to fix."
            ) from exc

        ib = IB()
        log.info(
            "ibkr.connect.attempt",
            host=self.host,
            port=self.port,
            client_id=self.client_id,
            read_only=self.read_only,
        )
        ib.connect(
            host=self.host,
            port=self.port,
            clientId=self.client_id,
            timeout=self.connect_timeout_s,
            readonly=self.read_only,
            account=self.account or "",
        )

        # Request delayed data if entitlements are unavailable. This must be
        # done before any reqMktData call. Mode 3 = delayed frozen.
        if self.delayed_data:
            ib.reqMarketDataType(3)
            log.info("ibkr.market_data_type", mode=3, mode_name="delayed")
        else:
            ib.reqMarketDataType(1)
            log.info("ibkr.market_data_type", mode=1, mode_name="live")

        self._ib = ib
        self._mark_heartbeat()
        log.info(
            "ibkr.connect.success",
            server_version=ib.client.serverVersion(),
            tws_time=str(ib.reqCurrentTime()),
        )

    def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            log.info("ibkr.disconnect")
            self._ib.disconnect()
        self._ib = None
        self._last_heartbeat = None

    def is_healthy(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def heartbeat_age_s(self) -> float | None:
        if self._last_heartbeat is None:
            return None
        return time.monotonic() - self._last_heartbeat

    def _mark_heartbeat(self) -> None:
        self._last_heartbeat = time.monotonic()

    def _require_connected(self) -> "IB":
        if self._ib is None or not self._ib.isConnected():
            raise RuntimeError("IBKR adapter is not connected. Call connect() first.")
        return self._ib

    # ------------------------------------------------------------------
    # Contract resolution
    # ------------------------------------------------------------------

    def resolve_contract(
        self,
        underlying_symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> CanonicalContract:
        """Resolve a human-readable symbol to a canonical contract.

        Persists the raw broker response under ``broker_payload`` per the
        roadmap rule: keep broker payloads as evidence.
        """
        from ib_async import Contract  # type: ignore[import-not-found]

        ib = self._require_connected()
        contract = Contract(symbol=underlying_symbol, secType=sec_type, exchange=exchange, currency=currency)

        log.info(
            "ibkr.resolve_contract.request",
            symbol=underlying_symbol,
            sec_type=sec_type,
            exchange=exchange,
            currency=currency,
        )
        details = ib.reqContractDetails(contract)
        self._mark_heartbeat()

        if not details:
            raise LookupError(
                f"Contract not found: {underlying_symbol} {sec_type} {exchange} {currency}"
            )
        if len(details) > 1:
            log.warning(
                "ibkr.resolve_contract.ambiguous",
                count=len(details),
                first_conid=details[0].contract.conId,
            )

        d = details[0]
        c = d.contract

        canonical = CanonicalContract(
            underlying_symbol=c.symbol,
            sec_type=c.secType,
            exchange=c.exchange or exchange,
            currency=c.currency,
            expiry=c.lastTradeDateOrContractMonth or None,
            strike=c.strike if c.strike else None,
            right=c.right or None,
            multiplier=int(c.multiplier) if c.multiplier else None,
            broker_id=c.conId,
            broker_payload={
                "longName": getattr(d, "longName", None),
                "tradingClass": getattr(c, "tradingClass", None),
                "primaryExchange": getattr(c, "primaryExchange", None),
                "minTick": getattr(d, "minTick", None),
                "marketName": getattr(d, "marketName", None),
            },
        )
        log.info(
            "ibkr.resolve_contract.success",
            instrument_key=canonical.instrument_key,
            broker_id=canonical.broker_id,
        )
        return canonical

    # ------------------------------------------------------------------
    # Snapshot retrieval
    # ------------------------------------------------------------------

    def request_snapshot(
        self,
        contract: CanonicalContract,
        timeout_s: float = 10.0,
        delayed: bool = False,
    ) -> QuoteSnapshot:
        """Request a single market-data snapshot and return a normalized record.

        Polls until at least one of bid, ask, or last is populated, or the
        timeout elapses. Returns the best-available record regardless; the
        ``source_flags`` document what was missing.
        """
        from ib_async import Contract  # type: ignore[import-not-found]

        ib = self._require_connected()
        broker_contract = Contract(
            conId=contract.broker_id or 0,
            symbol=contract.underlying_symbol,
            secType=contract.sec_type,
            exchange=contract.exchange,
            currency=contract.currency,
        )
        if contract.expiry:
            broker_contract.lastTradeDateOrContractMonth = contract.expiry
        if contract.strike:
            broker_contract.strike = contract.strike
        if contract.right:
            broker_contract.right = contract.right
        if contract.multiplier:
            broker_contract.multiplier = str(contract.multiplier)

        log.info(
            "ibkr.snapshot.request",
            instrument_key=contract.instrument_key,
            delayed=delayed or self.delayed_data,
            timeout_s=timeout_s,
        )

        ticker = ib.reqMktData(broker_contract, genericTickList="", snapshot=False, regulatorySnapshot=False)
        try:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                ib.sleep(0.2)
                if not _is_nan(ticker.bid) or not _is_nan(ticker.ask) or not _is_nan(ticker.last):
                    break
            self._mark_heartbeat()

            received_at = now_utc()
            quote = QuoteSnapshot(
                instrument_key=contract.instrument_key,
                receipt_ts=received_at,
                exchange_ts=None,
                bid=_coalesce_nan(ticker.bid),
                ask=_coalesce_nan(ticker.ask),
                last=_coalesce_nan(ticker.last),
                bid_size=_coalesce_nan(ticker.bidSize),
                ask_size=_coalesce_nan(ticker.askSize),
                last_size=_coalesce_nan(ticker.lastSize),
                volume=_coalesce_nan(ticker.volume),
                open_interest=None,
                is_delayed=bool(self.delayed_data or delayed),
                source_flags={
                    "ticker_time": str(ticker.time) if ticker.time else None,
                    "market_data_type": getattr(ticker, "marketDataType", None),
                },
            )
            log.info(
                "ibkr.snapshot.success",
                instrument_key=contract.instrument_key,
                bid=quote.bid,
                ask=quote.ask,
                last=quote.last,
                is_delayed=quote.is_delayed,
            )
            return quote
        finally:
            ib.cancelMktData(broker_contract)


__all__ = ["IbkrAdapter"]
