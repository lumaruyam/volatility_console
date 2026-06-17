"""IBKR-specific implementation of :class:`BrokerAdapter`.

This module is the only place where ``ib_insync`` types are imported. All
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

import logging
import math
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.connectivity.state import (
    BrokerAdapter,
    CanonicalContract,
    OptionChainParams,
    QuoteSnapshot,
)

if TYPE_CHECKING:  # pragma: no cover
    from ib_insync import IB, Contract  # type: ignore[import-not-found]

log = logging.getLogger(__name__)


def _is_nan(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _coalesce_nan(x: float | None) -> float | None:
    """Convert NaN sentinels (which IBKR uses freely) to ``None``."""
    return None if _is_nan(x) else x


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


class IbkrAdapter(BrokerAdapter):
    """IBKR adapter using ``ib_insync``.

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
        self._ticker_map: dict[int, tuple[object, Callable]] = {}  # req_id → (contract, update_fn)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the IBKR session and verify a round-trip succeeds."""
        try:
            from ib_insync import IB  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ib_insync is not installed. Run `pip install ib_insync` to fix."
            ) from exc

        ib = IB()
        log.info(
            "ibkr.connect.attempt host=%s port=%s client_id=%s read_only=%s",
            self.host, self.port, self.client_id, self.read_only,
        )
        ib.connect(
            host=self.host,
            port=self.port,
            clientId=self.client_id,
            timeout=self.connect_timeout_s,
            readonly=self.read_only,
            account=self.account or "",
        )

        if self.delayed_data:
            ib.reqMarketDataType(3)
            log.info("ibkr.market_data_type mode=3 (delayed)")
        else:
            ib.reqMarketDataType(1)
            log.info("ibkr.market_data_type mode=1 (live)")

        self._ib = ib
        self._mark_heartbeat()
        log.info(
            "ibkr.connect.success server_version=%s tws_time=%s",
            ib.client.serverVersion(), str(ib.reqCurrentTime()),
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
        from ib_insync import Contract  # type: ignore[import-not-found]

        ib = self._require_connected()
        contract = Contract(symbol=underlying_symbol, secType=sec_type, exchange=exchange, currency=currency)

        log.info(
            "ibkr.resolve_contract.request symbol=%s sec_type=%s exchange=%s currency=%s",
            underlying_symbol, sec_type, exchange, currency,
        )
        details = ib.reqContractDetails(contract)
        self._mark_heartbeat()

        if not details:
            raise LookupError(
                f"Contract not found: {underlying_symbol} {sec_type} {exchange} {currency}"
            )
        if len(details) > 1:
            log.warning(
                "ibkr.resolve_contract.ambiguous count=%d first_conid=%d",
                len(details), details[0].contract.conId,
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
            "ibkr.resolve_contract.success instrument_key=%s broker_id=%s",
            canonical.instrument_key, canonical.broker_id,
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
        """Request a single market-data snapshot and return a normalized record."""
        from ib_insync import Contract  # type: ignore[import-not-found]

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
            "ibkr.snapshot.request instrument_key=%s delayed=%s timeout_s=%s",
            contract.instrument_key, delayed or self.delayed_data, timeout_s,
        )

        ticker = ib.reqMktData(broker_contract, genericTickList="", snapshot=False, regulatorySnapshot=False)
        try:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                ib.sleep(0.2)
                if not _is_nan(ticker.bid) or not _is_nan(ticker.ask) or not _is_nan(ticker.last):
                    break
            self._mark_heartbeat()

            received_at = _now_utc()
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
                "ibkr.snapshot.success instrument_key=%s bid=%s ask=%s last=%s is_delayed=%s",
                contract.instrument_key, quote.bid, quote.ask, quote.last, quote.is_delayed,
            )
            return quote
        finally:
            ib.cancelMktData(broker_contract)


    # ------------------------------------------------------------------
    # Streaming subscriptions (Step 3)
    # ------------------------------------------------------------------

    def subscribe_quotes(
        self,
        contracts: list[CanonicalContract],
        callback: Callable[[QuoteSnapshot], None],
    ) -> list[int]:
        """Subscribe to streaming market data via reqMktData.

        Returns one req_id per contract. The callback fires on every ticker
        update event; it receives a normalized QuoteSnapshot.
        """
        from ib_insync import Contract  # type: ignore[import-not-found]

        ib = self._require_connected()
        req_ids: list[int] = []

        for canonical in contracts:
            broker_contract = self._make_broker_contract(canonical, Contract)
            ticker = ib.reqMktData(
                broker_contract, genericTickList="", snapshot=False, regulatorySnapshot=False
            )

            def _on_update(t: object, c: CanonicalContract = canonical, cb: Callable = callback) -> None:
                quote = QuoteSnapshot(
                    instrument_key=c.instrument_key,
                    receipt_ts=_now_utc(),
                    exchange_ts=None,
                    bid=_coalesce_nan(getattr(t, "bid", None)),
                    ask=_coalesce_nan(getattr(t, "ask", None)),
                    last=_coalesce_nan(getattr(t, "last", None)),
                    bid_size=_coalesce_nan(getattr(t, "bidSize", None)),
                    ask_size=_coalesce_nan(getattr(t, "askSize", None)),
                    last_size=_coalesce_nan(getattr(t, "lastSize", None)),
                    volume=_coalesce_nan(getattr(t, "volume", None)),
                    open_interest=None,
                    is_delayed=self.delayed_data,
                )
                cb(quote)

            ticker.updateEvent += _on_update
            self._ticker_map[ticker.reqId] = (broker_contract, _on_update)
            req_ids.append(ticker.reqId)
            self._mark_heartbeat()

        log.info("ibkr.subscribe_quotes.done count=%d", len(req_ids))
        return req_ids

    def cancel_quotes(self, req_ids: list[int]) -> None:
        ib = self._require_connected()
        for rid in req_ids:
            if rid in self._ticker_map:
                contract, _ = self._ticker_map.pop(rid)
                try:
                    ib.cancelMktData(contract)
                except Exception as exc:
                    log.warning("ibkr.cancel_quotes.error req_id=%d error=%s", rid, exc)
        log.info("ibkr.cancel_quotes.done count=%d", len(req_ids))

    def _make_broker_contract(self, canonical: CanonicalContract, Contract: type) -> object:  # type: ignore[return]
        c = Contract(
            conId=canonical.broker_id or 0,
            symbol=canonical.underlying_symbol,
            secType=canonical.sec_type,
            exchange=canonical.exchange,
            currency=canonical.currency,
        )
        if canonical.expiry:
            c.lastTradeDateOrContractMonth = canonical.expiry
        if canonical.strike:
            c.strike = canonical.strike
        if canonical.right:
            c.right = canonical.right
        if canonical.multiplier:
            c.multiplier = str(canonical.multiplier)
        return c

    def request_option_chain_params(
        self,
        underlying_symbol: str,
        sec_type: str = "STK",
        underlying_con_id: int | None = None,
    ) -> list[OptionChainParams]:
        """Fetch option chain parameters via ``reqSecDefOptParams``.

        One broker call returns all listed (exchange, trading_class) pairs with
        their full expiration and strike sets. Caller does not need to make
        per-contract detail requests to build the chain.
        """
        ib = self._require_connected()
        log.info(
            "ibkr.option_chain_params.request symbol=%s sec_type=%s con_id=%s",
            underlying_symbol, sec_type, underlying_con_id,
        )
        raw = ib.reqSecDefOptParams(
            underlyingSymbol=underlying_symbol,
            futFopExchange="",
            underlyingSecType=sec_type,
            underlyingConId=underlying_con_id or 0,
        )
        self._mark_heartbeat()
        result: list[OptionChainParams] = []
        for p in raw:
            result.append(OptionChainParams(
                exchange=str(p.exchange),
                trading_class=str(p.tradingClass),
                multiplier=int(p.multiplier) if p.multiplier else 100,
                expirations=tuple(sorted(p.expirations)),
                strikes=tuple(sorted(p.strikes)),
            ))
        log.info(
            "ibkr.option_chain_params.success symbol=%s chain_count=%d",
            underlying_symbol, len(result),
        )
        return result

    def get_historical_bars(
        self,
        symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
        duration: str = "3 Y",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[dict]:
        """Fetch historical OHLCV bars via ``reqHistoricalData``.

        Parameters
        ----------
        symbol:       IBKR symbol e.g. ``"ESTX50"``, ``"SPY"``
        sec_type:     ``"STK"``, ``"IND"``, ``"FUT"``
        exchange:     e.g. ``"EUREX"``, ``"SMART"``
        currency:     e.g. ``"EUR"``, ``"USD"``
        duration:     IBKR string — ``"3 Y"``, ``"1 Y"``, ``"6 M"``, ``"30 D"``
        bar_size:     ``"1 day"``, ``"1 hour"``, ``"5 mins"``
        what_to_show: ``"TRADES"``, ``"MIDPOINT"``, ``"BID"``, ``"ASK"``
        use_rth:      True = regular trading hours only

        Returns
        -------
        List of dicts: ``date, open, high, low, close, volume, bar_count, average``
        """
        ib = self._require_connected()

        from ib_async import Contract
        contract = Contract(
            symbol=symbol,
            secType=sec_type,
            exchange=exchange,
            currency=currency,
        )

        log.info(
            "ibkr.historical_bars.request symbol=%s sec_type=%s duration=%s bar_size=%s",
            symbol, sec_type, duration, bar_size,
        )

        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
        )
        self._mark_heartbeat()

        result = []
        for bar in bars:
            result.append({
                "date": str(bar.date),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume) if hasattr(bar, "volume") else 0,
                "bar_count": int(bar.barCount) if hasattr(bar, "barCount") else 0,
                "average": float(bar.average) if hasattr(bar, "average") else float(bar.close),
            })

        log.info("ibkr.historical_bars.success symbol=%s bars=%d", symbol, len(result))
        return result


__all__ = ["IbkrAdapter"]
