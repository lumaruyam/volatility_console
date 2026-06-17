"""Raw market-data collector service.

CRITICAL RULE: Do NOT compute analytics inside broker callbacks.
Callbacks must only: normalize → stamp → persist.
Heavy logic inside callbacks causes dropped events, backpressure, and
undebuggable data loss. Analytics (IV, Greeks, surfaces) belong in
downstream pipeline stages that read from the raw store.

Architecture:
  BrokerAdapter  →  [QuoteSnapshot callback]  →  RawCollector._on_quote
  RawCollector   →  _normalize_quote_snapshot  →  RawWriter.append / quarantine
  RawWriter      →  JSONL files on disk (partitioned by date + session)
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from src.connectivity.state import BrokerAdapter, CanonicalContract, QuoteSnapshot

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pacing limiter
# ---------------------------------------------------------------------------


class PacingLimiter:
    """Token-bucket rate limiter for IBKR API calls.

    IBKR enforces ~50 messages/second on most API endpoints. This limiter
    keeps outbound message rate at or below max_per_second (default 40,
    per broker.yaml) to avoid IBKR rate-limit bans.

    Usage::

        limiter = PacingLimiter(max_per_second=40)
        limiter.throttle()   # blocks until a token is available
        adapter.send(...)
    """

    def __init__(self, max_per_second: float = 40.0) -> None:
        if max_per_second <= 0:
            raise ValueError(f"max_per_second must be positive, got {max_per_second}")
        self.max_per_second = max_per_second
        self._tokens: float = max_per_second
        self._last_refill: float = time.monotonic()

    def throttle(self) -> None:
        """Block (sleep in 1 ms increments) until a token is available."""
        while True:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self.max_per_second,
                self._tokens + elapsed * self.max_per_second,
            )
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            time.sleep(0.001)

    def available_tokens(self) -> float:
        """Current token count (for monitoring/testing, not thread-safe)."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        return min(self.max_per_second, self._tokens + elapsed * self.max_per_second)


# Recognized field names produced by the collector.
KNOWN_FIELDS = frozenset(
    {"bid", "ask", "last", "bid_size", "ask_size", "last_size", "volume", "open_interest"}
)


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawEvent:
    """Normalized single market-data field observation.

    One ``RawEvent`` per field per tick. A single ``QuoteSnapshot`` with bid,
    ask, and last populated yields three ``RawEvent`` objects.

    All timestamps are UTC epoch seconds (float). Using floats rather than
    ``datetime`` keeps the hot path allocation-free and makes JSONL lines
    directly numeric.
    """

    session_id: str
    event_id: str              # uuid4 hex, unique within the platform
    instrument_key: str
    field_name: str            # one of KNOWN_FIELDS
    field_value: float
    exchange_ts: float | None  # from exchange; None if not supplied
    receipt_ts: float          # when collector received the tick (UTC epoch)
    source: str = "live"       # "live" | "replay"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "event_id": self.event_id,
            "instrument_key": self.instrument_key,
            "field_name": self.field_name,
            "field_value": self.field_value,
            "exchange_ts": self.exchange_ts,
            "receipt_ts": self.receipt_ts,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RawEvent":
        return cls(
            session_id=d["session_id"],
            event_id=d["event_id"],
            instrument_key=d["instrument_key"],
            field_name=d["field_name"],
            field_value=float(d["field_value"]),
            exchange_ts=float(d["exchange_ts"]) if d.get("exchange_ts") is not None else None,
            receipt_ts=float(d["receipt_ts"]),
            source=d.get("source", "live"),
        )

    def as_replay(self) -> "RawEvent":
        """Return a copy of this event marked source='replay'."""
        return dataclasses.replace(self, source="replay")


# ---------------------------------------------------------------------------
# Session tracking
# ---------------------------------------------------------------------------


@dataclass
class CollectorSession:
    """Mutable session state updated on every event.

    ``coverage_ratio`` = instruments_with_at_least_one_event / subscribed_count.
    A low ratio late in the day indicates stale subscriptions or connectivity issues.
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    started_at: float = field(default_factory=time.time)
    event_count: int = 0
    malformed_count: int = 0
    reconnect_count: int = 0
    last_event_ts: float | None = None
    _covered_keys: set[str] = field(default_factory=set, repr=False)
    _subscribed_count: int = field(default=0, repr=False)

    def record_event(self, instrument_key: str, receipt_ts: float) -> None:
        self.event_count += 1
        self.last_event_ts = receipt_ts
        self._covered_keys.add(instrument_key)

    def record_malformed(self) -> None:
        self.malformed_count += 1

    def set_subscribed_count(self, n: int) -> None:
        self._subscribed_count = n

    @property
    def coverage_ratio(self) -> float:
        if self._subscribed_count == 0:
            return 0.0
        return len(self._covered_keys) / self._subscribed_count

    def to_summary(self) -> dict:
        elapsed = time.time() - self.started_at
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "elapsed_seconds": round(elapsed, 3),
            "event_count": self.event_count,
            "malformed_count": self.malformed_count,
            "reconnect_count": self.reconnect_count,
            "last_event_ts": self.last_event_ts,
            "subscribed_count": self._subscribed_count,
            "covered_count": len(self._covered_keys),
            "coverage_ratio": round(self.coverage_ratio, 4),
        }


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class RawCollector:
    """Subscribe to underlying and option market data; persist raw events.

    The collector is intentionally stateless between ticks. Each callback
    invocation runs: normalize → stamp → persist, and returns immediately.
    No analytics, no shared mutable state that could block the event thread.

    Reconnect handling: the caller (orchestrator / heartbeat loop) detects
    session drops via ``Session.check_health()`` and calls
    ``collector.reconnected()`` after the session reconnects. The collector
    re-subscribes using the stored contract list.
    """

    def __init__(
        self,
        adapter: BrokerAdapter,
        writer: "RawWriter",  # type: ignore[name-defined]
        pacing_limiter: PacingLimiter | None = None,
    ) -> None:
        self.adapter = adapter
        self.writer = writer
        self.session = CollectorSession()
        self._contracts: list[CanonicalContract] = []
        self._req_ids: list[int] = []
        # Use provided limiter or a default 40 msg/s limiter (per broker.yaml).
        self._pacing = pacing_limiter or PacingLimiter(max_per_second=40.0)

    def subscribe(self, contracts: list[CanonicalContract]) -> None:
        """Subscribe to streaming market data for all given contracts.

        Each subscribe_quotes call is paced to stay within the broker rate limit.
        """
        self._contracts = list(contracts)
        self.session.set_subscribed_count(len(contracts))
        self._pacing.throttle()
        self._req_ids = self.adapter.subscribe_quotes(contracts, self._on_quote)
        log.info(
            "collector.subscribed session_id=%s count=%d",
            self.session.session_id, len(contracts),
        )

    def cancel(self) -> None:
        """Cancel all active subscriptions (call before session disconnect)."""
        if self._req_ids:
            self.adapter.cancel_quotes(self._req_ids)
            self._req_ids = []
        log.info("collector.cancelled session_id=%s", self.session.session_id)

    def reconnected(self) -> None:
        """Re-subscribe after a session reconnect. Call from the orchestrator."""
        self.session.reconnect_count += 1
        if self._contracts:
            self._req_ids = self.adapter.subscribe_quotes(self._contracts, self._on_quote)
            log.info(
                "collector.resubscribed session_id=%s reconnect_count=%d count=%d",
                self.session.session_id, self.session.reconnect_count, len(self._contracts),
            )

    def _on_quote(self, quote: QuoteSnapshot) -> None:
        """Broker callback. Normalize → stamp → persist. Nothing else."""
        try:
            events = _normalize_quote_snapshot(quote, self.session.session_id)
            for event in events:
                self.writer.append(event)
                self.session.record_event(event.instrument_key, event.receipt_ts)
        except Exception as exc:
            self.session.record_malformed()
            self.writer.quarantine(
                {"instrument_key": quote.instrument_key, "raw": repr(quote)},
                reason=str(exc),
            )
            log.warning(
                "collector.malformed instrument=%s error=%s",
                quote.instrument_key, exc,
            )

    def get_session_summary(self) -> dict:
        return self.session.to_summary()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_quote_snapshot(
    quote: QuoteSnapshot,
    session_id: str,
) -> list[RawEvent]:
    """Convert one QuoteSnapshot into a list of RawEvent (one per non-None field).

    Raises ValueError if required fields are missing or the quote is structurally
    invalid. The caller quarantines the raw payload on any exception.
    """
    if not quote.instrument_key:
        raise ValueError("QuoteSnapshot.instrument_key is empty")

    receipt_ts: float = quote.receipt_ts.timestamp()
    exchange_ts: float | None = (
        quote.exchange_ts.timestamp() if quote.exchange_ts is not None else None
    )

    field_map: dict[str, float | None] = {
        "bid": quote.bid,
        "ask": quote.ask,
        "last": quote.last,
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
        "last_size": quote.last_size,
        "volume": quote.volume,
        "open_interest": quote.open_interest,
    }

    events: list[RawEvent] = []
    for field_name, value in field_map.items():
        if value is None:
            continue
        fv = float(value)
        if not math.isfinite(fv):
            log.warning(
                "collector.normalize.non_finite instrument=%s field=%s value=%s",
                quote.instrument_key, field_name, value,
            )
            continue
        events.append(
            RawEvent(
                session_id=session_id,
                event_id=uuid.uuid4().hex,
                instrument_key=quote.instrument_key,
                field_name=field_name,
                field_value=fv,
                exchange_ts=exchange_ts,
                receipt_ts=receipt_ts,
                source="live",
            )
        )

    return events
