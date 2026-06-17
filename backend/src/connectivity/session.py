"""Session controller wrapping a :class:`BrokerAdapter` in the state machine.

Responsibilities:
    - Enforce legal state transitions (delegated to :func:`assert_transition`).
    - Manage connect / disconnect lifecycle.
    - Run exponential-backoff-with-jitter reconnect on failure.
    - Surface heartbeat age for the health check layer.
    - Emit :class:`SessionEvent` records on every transition for downstream
      observability and audit.

The session does not subscribe to streaming data: that is the collector's
job (Step 3). The session is the transport.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Callable

from src.connectivity.state import (
    BrokerAdapter,
    SessionEvent,
    SessionState,
    assert_transition,
)

log = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


class Session:
    """Lifecycle wrapper around a :class:`BrokerAdapter`.

    Parameters
    ----------
    adapter
        Concrete broker adapter (IBKR, mock, replay).
    initial_delay_s
        First reconnect backoff window.
    max_delay_s
        Cap on backoff window.
    backoff_multiplier
        Multiplicative increase between reconnect attempts.
    jitter_pct
        Fraction of the current delay used as uniform jitter.
    max_attempts
        0 = unlimited reconnect attempts.
    heartbeat_max_age_s
        If heartbeat is older than this when :meth:`check_health` is called,
        the session transitions to DEGRADED.
    on_event
        Optional observer invoked on every transition.
    """

    def __init__(
        self,
        adapter: BrokerAdapter,
        *,
        initial_delay_s: float = 1.0,
        max_delay_s: float = 60.0,
        backoff_multiplier: float = 2.0,
        jitter_pct: float = 0.25,
        max_attempts: int = 0,
        heartbeat_max_age_s: float = 30.0,
        on_event: Callable[[SessionEvent], None] | None = None,
    ) -> None:
        self.adapter = adapter
        self.initial_delay_s = initial_delay_s
        self.max_delay_s = max_delay_s
        self.backoff_multiplier = backoff_multiplier
        self.jitter_pct = jitter_pct
        self.max_attempts = max_attempts
        self.heartbeat_max_age_s = heartbeat_max_age_s
        self._on_event = on_event

        self._state: SessionState = SessionState.DISCONNECTED
        self._reconnect_attempts = 0
        self._connected_at: float | None = None

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts

    def _transition(self, target: SessionState, reason: str, **detail: object) -> None:
        assert_transition(self._state, target)
        event = SessionEvent(
            ts=_now_utc(),
            previous=self._state,
            current=target,
            reason=reason,
            detail=dict(detail),
        )
        log.info(
            "session.transition previous=%s current=%s reason=%s",
            event.previous.value, event.current.value, reason,
        )
        self._state = target
        if self._on_event is not None:
            self._on_event(event)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect synchronously. Raises if all reconnect attempts exhaust."""
        self._transition(SessionState.CONNECTING, reason="connect_requested")
        try:
            self.adapter.connect()
        except Exception as exc:
            log.warning("session.connect.failed error=%s", exc)
            self._transition(SessionState.RECONNECTING, reason="initial_connect_failed", error=str(exc))
            self._reconnect_loop()
            return

        self._transition(SessionState.CONNECTED, reason="connect_success")
        self._connected_at = time.monotonic()
        self._reconnect_attempts = 0

    def disconnect(self) -> None:
        """Clean disconnect. Idempotent: safe to call from any state."""
        if self._state == SessionState.DISCONNECTED:
            return
        try:
            self.adapter.disconnect()
        finally:
            self._transition(SessionState.DISCONNECTED, reason="disconnect_requested")
            self._connected_at = None
            self._reconnect_attempts = 0

    # ------------------------------------------------------------------
    # Reconnect with exponential backoff and jitter
    # ------------------------------------------------------------------

    def _reconnect_loop(self) -> None:
        """Attempt to reconnect until success, manual stop, or attempt cap."""
        delay = self.initial_delay_s
        while True:
            self._reconnect_attempts += 1
            if self.max_attempts and self._reconnect_attempts > self.max_attempts:
                log.error("session.reconnect.exhausted attempts=%d", self._reconnect_attempts)
                self._transition(SessionState.DISCONNECTED, reason="reconnect_exhausted")
                raise RuntimeError(
                    f"Reconnect exhausted after {self._reconnect_attempts} attempts"
                )

            sleep_for = delay + random.uniform(0, delay * self.jitter_pct)
            log.info(
                "session.reconnect.waiting attempt=%d delay_s=%.3f",
                self._reconnect_attempts, sleep_for,
            )
            time.sleep(sleep_for)

            self._transition(SessionState.CONNECTING, reason="reconnect_attempt")
            try:
                self.adapter.connect()
            except Exception as exc:
                log.warning(
                    "session.reconnect.failed attempt=%d error=%s",
                    self._reconnect_attempts, exc,
                )
                self._transition(SessionState.RECONNECTING, reason="reconnect_failed", error=str(exc))
                delay = min(delay * self.backoff_multiplier, self.max_delay_s)
                continue

            self._transition(SessionState.CONNECTED, reason="reconnect_success")
            self._connected_at = time.monotonic()
            self._reconnect_attempts = 0
            return

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def check_health(self) -> bool:
        """Return True iff the session is healthy."""
        if self._state != SessionState.CONNECTED and self._state != SessionState.DEGRADED:
            return False
        if not self.adapter.is_healthy():
            self._transition(SessionState.RECONNECTING, reason="adapter_unhealthy")
            self._reconnect_loop()
            return self._state == SessionState.CONNECTED

        age = self.adapter.heartbeat_age_s()
        if age is not None and age > self.heartbeat_max_age_s:
            if self._state == SessionState.CONNECTED:
                self._transition(
                    SessionState.DEGRADED,
                    reason="heartbeat_stale",
                    heartbeat_age_s=round(age, 3),
                    threshold_s=self.heartbeat_max_age_s,
                )
            return False

        if self._state == SessionState.DEGRADED:
            self._transition(SessionState.CONNECTED, reason="heartbeat_recovered")
        return True

    def __enter__(self) -> "Session":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disconnect()


__all__ = ["Session"]
