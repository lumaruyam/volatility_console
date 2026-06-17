"""Deterministic in-memory adapter used for tests and the bootstrap ``--mock`` mode.

Per the roadmap Part IV.B: "the replay source can emit the same internal
event objects as the live adapter." That principle starts here. The mock
adapter implements the same :class:`BrokerAdapter` interface so the rest of
the system can be exercised end-to-end without an IBKR session.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from src.connectivity.state import (
    BrokerAdapter,
    CanonicalContract,
    OptionChainParams,
    QuoteSnapshot,
)

log = logging.getLogger(__name__)


class MockAdapter(BrokerAdapter):
    """Deterministic mock. Resolves any symbol; returns fixed quote book.

    Parameters
    ----------
    fixture_quotes
        Mapping from underlying symbol to (bid, ask, last) triple. Symbols
        not in the map get a default tight market around 100.0.
    fail_on_connect
        If True, ``connect`` raises. Useful for testing error paths.
    """

    _DEFAULT_QUOTE = (99.95, 100.05, 100.00)

    # Fixed expirations (YYYYMMDD). Chosen so they are 40 / 103 / 166 DTE from
    # 2026-06-07 (the project's reference session date) and all fall within the
    # default maturity window min_dte=1 / max_dte=180.
    _CHAIN_PARAMS: dict[str, OptionChainParams] = {
        "SPY": OptionChainParams(
            exchange="SMART",
            trading_class="SPY",
            multiplier=100,
            expirations=("20260717", "20260918", "20261120"),
            strikes=(430.0, 440.0, 450.0, 460.0, 470.0),
        ),
        "QQQ": OptionChainParams(
            exchange="SMART",
            trading_class="QQQ",
            multiplier=100,
            expirations=("20260717", "20260918", "20261120"),
            strikes=(460.0, 470.0, 480.0, 490.0, 500.0),
        ),
        # Euro Stoxx 50 index options (EUREX). Multiplier=10, currency=EUR.
        # Strikes in 50-point increments centred on ~5000.
        "ESTX50": OptionChainParams(
            exchange="EUREX",
            trading_class="OESX",
            multiplier=10,
            expirations=("20260717", "20260918", "20261219"),
            strikes=(4850.0, 4900.0, 4950.0, 5000.0, 5050.0, 5100.0, 5150.0),
        ),
    }
    _DEFAULT_CHAIN = OptionChainParams(
        exchange="SMART",
        trading_class="MOCK",
        multiplier=100,
        expirations=("20260717",),
        strikes=(95.0, 100.0, 105.0),
    )

    def __init__(
        self,
        fixture_quotes: dict[str, tuple[float, float, float]] | None = None,
        fail_on_connect: bool = False,
    ) -> None:
        self.fixture_quotes = fixture_quotes or {
            "SPY": (450.10, 450.12, 450.11),
            "AAPL": (195.20, 195.22, 195.21),
            "ESTX50": (4998.0, 5002.0, 5000.0),
        }
        self.fail_on_connect = fail_on_connect
        self._connected = False
        self._last_heartbeat: float | None = None
        # Streaming subscription registry: req_id → (contract, callback)
        self._subscriptions: dict[int, tuple[CanonicalContract, Callable[[QuoteSnapshot], None]]] = {}
        self._next_req_id: int = 1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if self.fail_on_connect:
            raise RuntimeError("MockAdapter: simulated connect failure")
        log.info("mock.connect")
        self._connected = True
        self._last_heartbeat = time.monotonic()

    def disconnect(self) -> None:
        log.info("mock.disconnect")
        self._connected = False
        self._last_heartbeat = None
        self._subscriptions.clear()

    def is_healthy(self) -> bool:
        return self._connected

    def heartbeat_age_s(self) -> float | None:
        if self._last_heartbeat is None:
            return None
        return time.monotonic() - self._last_heartbeat

    # ------------------------------------------------------------------
    # One-shot operations
    # ------------------------------------------------------------------

    def resolve_contract(
        self,
        underlying_symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> CanonicalContract:
        if not self._connected:
            raise RuntimeError("MockAdapter is not connected")
        log.info("mock.resolve_contract symbol=%s", underlying_symbol)
        broker_id = sum(ord(c) for c in underlying_symbol) * 10
        return CanonicalContract(
            underlying_symbol=underlying_symbol,
            sec_type=sec_type,
            exchange=exchange,
            currency=currency,
            broker_id=broker_id,
            broker_payload={"source": "mock"},
        )

    def request_snapshot(
        self,
        contract: CanonicalContract,
        timeout_s: float = 10.0,
        delayed: bool = False,
    ) -> QuoteSnapshot:
        if not self._connected:
            raise RuntimeError("MockAdapter is not connected")
        bid, ask, last = self.fixture_quotes.get(
            contract.underlying_symbol, self._DEFAULT_QUOTE
        )
        self._last_heartbeat = time.monotonic()
        return QuoteSnapshot(
            instrument_key=contract.instrument_key,
            receipt_ts=datetime.now(tz=timezone.utc),
            exchange_ts=None,
            bid=bid,
            ask=ask,
            last=last,
            bid_size=100,
            ask_size=100,
            last_size=10,
            volume=10_000,
            open_interest=None,
            is_delayed=delayed,
            source_flags={"source": "mock", "market_data_type": 3 if delayed else 1},
        )

    def request_option_chain_params(
        self,
        underlying_symbol: str,
        sec_type: str = "STK",
        underlying_con_id: int | None = None,
    ) -> list[OptionChainParams]:
        if not self._connected:
            raise RuntimeError("MockAdapter is not connected")
        log.info("mock.request_option_chain_params symbol=%s", underlying_symbol)
        params = self._CHAIN_PARAMS.get(underlying_symbol, self._DEFAULT_CHAIN)
        return [params]

    # ------------------------------------------------------------------
    # Streaming subscriptions
    # ------------------------------------------------------------------

    def subscribe_quotes(
        self,
        contracts: list[CanonicalContract],
        callback: Callable[[QuoteSnapshot], None],
    ) -> list[int]:
        """Register streaming subscriptions. Returns one req_id per contract."""
        if not self._connected:
            raise RuntimeError("MockAdapter is not connected")
        req_ids: list[int] = []
        for contract in contracts:
            rid = self._next_req_id
            self._next_req_id += 1
            self._subscriptions[rid] = (contract, callback)
            req_ids.append(rid)
        log.info("mock.subscribe_quotes count=%d", len(contracts))
        return req_ids

    def cancel_quotes(self, req_ids: list[int]) -> None:
        for rid in req_ids:
            self._subscriptions.pop(rid, None)
        log.info("mock.cancel_quotes count=%d", len(req_ids))

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def emit_tick(
        self,
        req_id: int,
        bid: float | None = None,
        ask: float | None = None,
        last: float | None = None,
        bid_size: float | None = None,
        ask_size: float | None = None,
        volume: float | None = None,
        open_interest: float | None = None,
    ) -> None:
        """Emit a synthetic quote for a subscribed contract.

        Triggers the registered callback synchronously. Only available in tests.
        """
        if req_id not in self._subscriptions:
            raise KeyError(f"req_id {req_id} is not subscribed")
        contract, callback = self._subscriptions[req_id]
        self._last_heartbeat = time.monotonic()
        quote = QuoteSnapshot(
            instrument_key=contract.instrument_key,
            receipt_ts=datetime.now(tz=timezone.utc),
            exchange_ts=None,
            bid=bid,
            ask=ask,
            last=last,
            bid_size=bid_size,
            ask_size=ask_size,
            open_interest=open_interest,
            volume=volume,
        )
        callback(quote)

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)


__all__ = ["MockAdapter"]
