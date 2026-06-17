"""Deterministic in-memory adapter used for tests and the bootstrap ``--mock`` mode.

Per the roadmap Part IV.B: "the replay source can emit the same internal
event objects as the live adapter." That principle starts here. The mock
adapter implements the same :class:`BrokerAdapter` interface so the rest of
the system can be exercised end-to-end without an IBKR session.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from src.connectivity.state import (
    BrokerAdapter,
    CanonicalContract,
    QuoteSnapshot,
)
from src.utils.logging import get_logger

log = get_logger(__name__)


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

    def __init__(
        self,
        fixture_quotes: dict[str, tuple[float, float, float]] | None = None,
        fail_on_connect: bool = False,
    ) -> None:
        self.fixture_quotes = fixture_quotes or {
            "SPY": (450.10, 450.12, 450.11),
            "AAPL": (195.20, 195.22, 195.21),
        }
        self.fail_on_connect = fail_on_connect
        self._connected = False
        self._last_heartbeat: float | None = None

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

    def is_healthy(self) -> bool:
        return self._connected

    def heartbeat_age_s(self) -> float | None:
        if self._last_heartbeat is None:
            return None
        return time.monotonic() - self._last_heartbeat

    def resolve_contract(
        self,
        underlying_symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> CanonicalContract:
        if not self._connected:
            raise RuntimeError("MockAdapter is not connected")
        log.info("mock.resolve_contract", symbol=underlying_symbol)
        # Deterministic conId derived from symbol so tests are stable.
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


__all__ = ["MockAdapter"]
