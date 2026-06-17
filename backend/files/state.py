"""Session state model and broker-agnostic interfaces.

The roadmap Part IV.B is explicit: the rest of the codebase should never
import broker callback enums directly. It should consume a broker-agnostic
event stream. This module defines that stream.

The state machine has five states:

    DISCONNECTED -> CONNECTING -> CONNECTED -> DEGRADED -> RECONNECTING

with transitions enforced so accidental short-circuits are impossible.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


class SessionState(str, Enum):
    """Lifecycle of a broker session."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"


# Allowed forward transitions. The state machine refuses anything else.
_ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.DISCONNECTED: frozenset(
        {SessionState.CONNECTING, SessionState.DISCONNECTED}
    ),
    SessionState.CONNECTING: frozenset(
        {SessionState.CONNECTED, SessionState.DISCONNECTED, SessionState.RECONNECTING}
    ),
    SessionState.CONNECTED: frozenset(
        {SessionState.DEGRADED, SessionState.DISCONNECTED, SessionState.RECONNECTING}
    ),
    SessionState.DEGRADED: frozenset(
        {SessionState.CONNECTED, SessionState.DISCONNECTED, SessionState.RECONNECTING}
    ),
    SessionState.RECONNECTING: frozenset(
        {SessionState.CONNECTING, SessionState.DISCONNECTED}
    ),
}


def assert_transition(current: SessionState, target: SessionState) -> None:
    """Raise ``ValueError`` if the transition is not legal."""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(
            f"Illegal session transition: {current.value} -> {target.value}. "
            f"Allowed from {current.value}: "
            f"{sorted(s.value for s in _ALLOWED_TRANSITIONS[current])}"
        )


# ---------------------------------------------------------------------------
# Normalized contract and event records (broker-agnostic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CanonicalContract:
    """Canonical instrument representation used across the platform.

    Per roadmap Part I "Core naming conventions". The fields here are the
    stable composite key. Broker-specific identifiers go in ``broker_id``
    and ``broker_payload`` for audit.
    """

    underlying_symbol: str
    sec_type: str                    # STK | OPT | FUT | IND etc.
    exchange: str
    currency: str
    expiry: str | None = None        # YYYYMMDD for options, None for underlyings
    strike: float | None = None
    right: str | None = None         # C | P, None for non-options
    multiplier: int | None = None
    broker_id: int | None = None     # IBKR conId or equivalent
    broker_payload: dict[str, Any] | None = None  # raw broker response for audit

    @property
    def instrument_key(self) -> str:
        """Stable string key independent of broker session."""
        parts = [
            self.underlying_symbol,
            self.sec_type,
            self.exchange,
            self.currency,
        ]
        if self.sec_type == "OPT":
            parts.extend(
                [
                    self.expiry or "",
                    f"{self.strike:g}" if self.strike is not None else "",
                    self.right or "",
                    str(self.multiplier or ""),
                ]
            )
        return "|".join(parts)


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    """Single-point market-data observation, normalized."""

    instrument_key: str
    receipt_ts: datetime             # when collector received the data (UTC)
    exchange_ts: datetime | None     # exchange-supplied timestamp if available
    bid: float | None
    ask: float | None
    last: float | None
    bid_size: float | None = None
    ask_size: float | None = None
    last_size: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    is_delayed: bool = False
    source_flags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """State-machine transition event for downstream observers."""

    ts: datetime
    previous: SessionState
    current: SessionState
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------


class EventSink(Protocol):
    """Anything that consumes normalized events.

    Used so the session and adapter can publish without coupling to a
    specific storage or in-memory consumer.
    """

    def on_session_event(self, event: SessionEvent) -> None: ...
    def on_quote(self, quote: QuoteSnapshot) -> None: ...


class BrokerAdapter(ABC):
    """Abstract broker interface.

    Implementations:

    - :class:`vol_infra.connectivity.ibkr_adapter.IbkrAdapter`: live IBKR.
    - :class:`vol_infra.connectivity.mock_adapter.MockAdapter`: deterministic
      fixture-driven adapter for tests and replay.

    The abstraction lets the rest of the system stay broker-agnostic per the
    roadmap Part IV.B.
    """

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_healthy(self) -> bool: ...

    @abstractmethod
    def resolve_contract(
        self,
        underlying_symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> CanonicalContract: ...

    @abstractmethod
    def request_snapshot(
        self,
        contract: CanonicalContract,
        timeout_s: float = 10.0,
        delayed: bool = False,
    ) -> QuoteSnapshot: ...

    @abstractmethod
    def heartbeat_age_s(self) -> float | None: ...


__all__ = [
    "SessionState",
    "assert_transition",
    "CanonicalContract",
    "QuoteSnapshot",
    "SessionEvent",
    "EventSink",
    "BrokerAdapter",
]
