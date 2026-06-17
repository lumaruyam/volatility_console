"""Unit tests for Step 1 components.

Per the roadmap testing strategy:
    - Unit tests for pure logic.
    - Integration tests using the MockAdapter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.connectivity.mock_adapter import MockAdapter
from src.connectivity.session import Session
from src.connectivity.state import (
    CanonicalContract,
    SessionEvent,
    SessionState,
    assert_transition,
)
from src.utils.config import (
    AppConfig,
    BootstrapConfig,
    IbkrConfig,
    RuntimeConfig,
    config_hash,
    load_config,
)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_legal_transition_disconnected_to_connecting(self) -> None:
        assert_transition(SessionState.DISCONNECTED, SessionState.CONNECTING)

    def test_legal_transition_connected_to_degraded(self) -> None:
        assert_transition(SessionState.CONNECTED, SessionState.DEGRADED)

    def test_legal_transition_degraded_to_connected(self) -> None:
        assert_transition(SessionState.DEGRADED, SessionState.CONNECTED)

    def test_illegal_transition_disconnected_to_connected(self) -> None:
        with pytest.raises(ValueError, match="Illegal session transition"):
            assert_transition(SessionState.DISCONNECTED, SessionState.CONNECTED)

    def test_illegal_transition_connected_to_connecting(self) -> None:
        with pytest.raises(ValueError):
            assert_transition(SessionState.CONNECTED, SessionState.CONNECTING)


# ---------------------------------------------------------------------------
# Canonical contract
# ---------------------------------------------------------------------------


class TestCanonicalContract:
    def test_instrument_key_for_stock(self) -> None:
        c = CanonicalContract(
            underlying_symbol="SPY",
            sec_type="STK",
            exchange="SMART",
            currency="USD",
        )
        assert c.instrument_key == "SPY|STK|SMART|USD"

    def test_instrument_key_for_option(self) -> None:
        c = CanonicalContract(
            underlying_symbol="SPY",
            sec_type="OPT",
            exchange="SMART",
            currency="USD",
            expiry="20260619",
            strike=450.0,
            right="C",
            multiplier=100,
        )
        assert c.instrument_key == "SPY|OPT|SMART|USD|20260619|450|C|100"

    def test_instrument_key_is_deterministic(self) -> None:
        c1 = CanonicalContract(
            underlying_symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD"
        )
        c2 = CanonicalContract(
            underlying_symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD"
        )
        assert c1.instrument_key == c2.instrument_key


# ---------------------------------------------------------------------------
# Session lifecycle (using MockAdapter)
# ---------------------------------------------------------------------------


class TestSessionWithMock:
    def test_connect_and_disconnect(self) -> None:
        adapter = MockAdapter()
        session = Session(adapter, max_attempts=1)
        events: list[SessionEvent] = []
        session._on_event = events.append  # type: ignore[method-assign]

        session.connect()
        assert session.state == SessionState.CONNECTED
        assert adapter.is_healthy()

        session.disconnect()
        assert session.state == SessionState.DISCONNECTED
        assert not adapter.is_healthy()

        # Should have emitted at least: CONNECTING, CONNECTED, DISCONNECTED.
        states = [e.current for e in events]
        assert SessionState.CONNECTING in states
        assert SessionState.CONNECTED in states
        assert SessionState.DISCONNECTED in states

    def test_disconnect_is_idempotent(self) -> None:
        adapter = MockAdapter()
        session = Session(adapter, max_attempts=1)
        session.disconnect()  # already disconnected; should not raise
        assert session.state == SessionState.DISCONNECTED

    def test_failed_connect_raises_after_max_attempts(self) -> None:
        adapter = MockAdapter(fail_on_connect=True)
        session = Session(adapter, initial_delay_s=0.01, max_delay_s=0.01, max_attempts=2)
        with pytest.raises(RuntimeError, match="Reconnect exhausted"):
            session.connect()
        assert session.state == SessionState.DISCONNECTED

    def test_context_manager(self) -> None:
        adapter = MockAdapter()
        with Session(adapter, max_attempts=1) as session:
            assert session.state == SessionState.CONNECTED
        assert session.state == SessionState.DISCONNECTED


# ---------------------------------------------------------------------------
# Adapter contract (using mock)
# ---------------------------------------------------------------------------


class TestMockAdapter:
    def test_resolve_contract_returns_deterministic_broker_id(self) -> None:
        adapter = MockAdapter()
        adapter.connect()
        c1 = adapter.resolve_contract("SPY")
        c2 = adapter.resolve_contract("SPY")
        assert c1.broker_id == c2.broker_id
        adapter.disconnect()

    def test_request_snapshot_returns_fixture_data(self) -> None:
        adapter = MockAdapter()
        adapter.connect()
        c = adapter.resolve_contract("SPY")
        q = adapter.request_snapshot(c)
        assert q.bid == 450.10
        assert q.ask == 450.12
        assert q.last == 450.11
        assert q.instrument_key == c.instrument_key
        adapter.disconnect()

    def test_snapshot_when_disconnected_raises(self) -> None:
        adapter = MockAdapter()
        c = CanonicalContract("SPY", "STK", "SMART", "USD")
        with pytest.raises(RuntimeError, match="not connected"):
            adapter.request_snapshot(c)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigLoader:
    def test_load_default_config(self) -> None:
        config = load_config(config_dir=Path("configs"))
        assert isinstance(config, AppConfig)
        assert config.runtime.environment in ("development", "staging", "production")
        assert config.ibkr.port > 0
        assert config.bootstrap.test_symbol  # not empty

    def test_config_hash_is_deterministic(self) -> None:
        config = load_config(config_dir=Path("configs"))
        h1 = config_hash(config)
        h2 = config_hash(config)
        assert h1 == h2
        assert h1.startswith("cfg_")

    def test_config_hash_differs_when_inputs_differ(self) -> None:
        base = load_config(config_dir=Path("configs"))
        modified = AppConfig(
            runtime=base.runtime,
            bootstrap=BootstrapConfig(
                test_symbol="QQQ",  # changed
                test_exchange=base.bootstrap.test_exchange,
                test_currency=base.bootstrap.test_currency,
                quote_timeout_s=base.bootstrap.quote_timeout_s,
                use_delayed_data=base.bootstrap.use_delayed_data,
            ),
            ibkr=base.ibkr,
            client_id_reservations=base.client_id_reservations,
        )
        assert config_hash(base) != config_hash(modified)

    def test_invalid_port_rejected(self) -> None:
        with pytest.raises(Exception):
            IbkrConfig(port=70000)

    def test_invalid_environment_rejected(self) -> None:
        with pytest.raises(Exception):
            RuntimeConfig(environment="prod")  # must be production/staging/development
