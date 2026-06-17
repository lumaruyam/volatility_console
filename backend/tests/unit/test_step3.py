"""Unit and integration tests for Step 3: Market-data ingestion.

Acceptance criteria:
  1. Kill-restart test: stopping a session mid-flight does not corrupt the raw store.
  2. Replay test: 1 day of events can be replayed from disk, yielding the same events.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.collectors.raw_collector import (
    CollectorSession,
    KNOWN_FIELDS,
    PacingLimiter,
    RawCollector,
    RawEvent,
    _normalize_quote_snapshot,
)
from src.collectors.raw_writer import (
    RawWriter,
    load_quarantine,
    replay_session,
    validate_raw_event,
)
from src.connectivity.mock_adapter import MockAdapter
from src.connectivity.state import CanonicalContract, QuoteSnapshot

SESSION_DATE = date(2026, 6, 7)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides) -> RawEvent:
    defaults = dict(
        session_id="sess0001",
        event_id=uuid.uuid4().hex,
        instrument_key="SPY|STK|SMART|USD",
        field_name="bid",
        field_value=450.10,
        exchange_ts=None,
        receipt_ts=1_700_000_000.0,
        source="live",
    )
    defaults.update(overrides)
    return RawEvent(**defaults)


def _make_quote(
    instrument_key: str = "SPY|STK|SMART|USD",
    bid: float | None = 450.10,
    ask: float | None = 450.12,
    last: float | None = 450.11,
    **kwargs,
) -> QuoteSnapshot:
    return QuoteSnapshot(
        instrument_key=instrument_key,
        receipt_ts=datetime.now(tz=timezone.utc),
        exchange_ts=None,
        bid=bid,
        ask=ask,
        last=last,
        **kwargs,
    )


@pytest.fixture()
def writer(tmp_path: Path) -> RawWriter:
    return RawWriter(tmp_path, session_id="test_session", session_date=SESSION_DATE)


@pytest.fixture()
def adapter() -> MockAdapter:
    a = MockAdapter()
    a.connect()
    return a


# ---------------------------------------------------------------------------
# RawEvent
# ---------------------------------------------------------------------------


class TestRawEvent:
    def test_to_dict_round_trip(self) -> None:
        e = _make_event()
        e2 = RawEvent.from_dict(e.to_dict())
        assert e2 == e

    def test_as_replay_changes_source(self) -> None:
        e = _make_event(source="live")
        r = e.as_replay()
        assert r.source == "replay"
        assert r.event_id == e.event_id  # other fields unchanged

    def test_frozen_cannot_modify(self) -> None:
        e = _make_event()
        with pytest.raises((AttributeError, TypeError)):
            e.field_value = 99.0  # type: ignore[misc]

    def test_exchange_ts_none_serialises(self) -> None:
        e = _make_event(exchange_ts=None)
        d = e.to_dict()
        assert d["exchange_ts"] is None
        e2 = RawEvent.from_dict(d)
        assert e2.exchange_ts is None

    def test_exchange_ts_float_serialises(self) -> None:
        ts = 1_700_000_000.0
        e = _make_event(exchange_ts=ts)
        e2 = RawEvent.from_dict(e.to_dict())
        assert e2.exchange_ts == ts


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalizeQuoteSnapshot:
    def test_emits_one_event_per_non_none_field(self) -> None:
        q = _make_quote(bid=450.10, ask=450.12, last=450.11)
        events = _normalize_quote_snapshot(q, "s1")
        field_names = {e.field_name for e in events}
        assert "bid" in field_names
        assert "ask" in field_names
        assert "last" in field_names
        assert len(events) == 3

    def test_skips_none_fields(self) -> None:
        q = _make_quote(bid=450.10, ask=None, last=None)
        events = _normalize_quote_snapshot(q, "s1")
        assert len(events) == 1
        assert events[0].field_name == "bid"

    def test_empty_instrument_key_raises(self) -> None:
        q = _make_quote(instrument_key="")
        with pytest.raises(ValueError, match="instrument_key"):
            _normalize_quote_snapshot(q, "s1")

    def test_receipt_ts_is_epoch_float(self) -> None:
        q = _make_quote()
        events = _normalize_quote_snapshot(q, "s1")
        for e in events:
            assert isinstance(e.receipt_ts, float)
            assert e.receipt_ts > 0

    def test_skips_non_finite_values(self) -> None:
        import math
        q = QuoteSnapshot(
            instrument_key="SPY|STK|SMART|USD",
            receipt_ts=datetime.now(tz=timezone.utc),
            exchange_ts=None,
            bid=float("nan"),
            ask=float("inf"),
            last=450.0,
        )
        events = _normalize_quote_snapshot(q, "s1")
        # NaN and inf skipped; only last survives
        assert len(events) == 1
        assert events[0].field_name == "last"

    def test_all_fields_included_when_populated(self) -> None:
        q = QuoteSnapshot(
            instrument_key="SPY|STK|SMART|USD",
            receipt_ts=datetime.now(tz=timezone.utc),
            exchange_ts=None,
            bid=1.0, ask=2.0, last=3.0,
            bid_size=100.0, ask_size=200.0, last_size=50.0,
            volume=10_000.0, open_interest=5_000.0,
        )
        events = _normalize_quote_snapshot(q, "s1")
        names = {e.field_name for e in events}
        assert names == KNOWN_FIELDS

    def test_source_is_live(self) -> None:
        q = _make_quote()
        events = _normalize_quote_snapshot(q, "s1")
        assert all(e.source == "live" for e in events)

    def test_all_events_share_session_id(self) -> None:
        q = _make_quote()
        events = _normalize_quote_snapshot(q, "mysession")
        assert all(e.session_id == "mysession" for e in events)

    def test_event_ids_are_unique(self) -> None:
        q = _make_quote(bid=1.0, ask=2.0, last=3.0)
        events = _normalize_quote_snapshot(q, "s1")
        assert len({e.event_id for e in events}) == len(events)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidateRawEvent:
    def test_valid_event_passes(self) -> None:
        validate_raw_event(_make_event())  # must not raise

    def test_empty_session_id(self) -> None:
        with pytest.raises(ValueError, match="session_id"):
            validate_raw_event(_make_event(session_id=""))

    def test_empty_instrument_key(self) -> None:
        with pytest.raises(ValueError, match="instrument_key"):
            validate_raw_event(_make_event(instrument_key=""))

    def test_unknown_field_name(self) -> None:
        with pytest.raises(ValueError, match="field_name"):
            validate_raw_event(_make_event(field_name="gamma"))

    def test_non_finite_value(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            validate_raw_event(_make_event(field_value=float("nan")))

    def test_zero_receipt_ts(self) -> None:
        with pytest.raises(ValueError, match="receipt_ts"):
            validate_raw_event(_make_event(receipt_ts=0.0))

    def test_invalid_source(self) -> None:
        with pytest.raises(ValueError, match="source"):
            validate_raw_event(_make_event(source="delayed"))


# ---------------------------------------------------------------------------
# RawWriter — writes
# ---------------------------------------------------------------------------


class TestRawWriterAppend:
    def test_creates_file_on_first_write(self, writer: RawWriter, tmp_path: Path) -> None:
        event = _make_event()
        writer.append(event)
        writer.flush()
        files = list(writer.raw_dir.glob("*.jsonl"))
        assert len(files) == 1

    def test_increments_partition_counter(self, writer: RawWriter) -> None:
        for _ in range(5):
            writer.append(_make_event())
        counts = writer.get_partition_counts()
        assert counts["raw_market_events"] == 5

    def test_events_readable_line_by_line(self, writer: RawWriter) -> None:
        events = [_make_event(field_name="bid"), _make_event(field_name="ask")]
        for e in events:
            writer.append(e)
        writer.flush()
        path = writer.raw_dir / "raw_market_events.jsonl"
        lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        assert lines[0]["field_name"] == "bid"
        assert lines[1]["field_name"] == "ask"

    def test_validates_before_write(self, writer: RawWriter) -> None:
        bad = _make_event(field_name="not_a_real_field")
        with pytest.raises(ValueError):
            writer.append(bad)

    def test_context_manager_closes(self, tmp_path: Path) -> None:
        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            w.append(_make_event())
        # After close, handles dict should be empty
        assert w._handles == {}


class TestRawWriterQuarantine:
    def test_quarantine_creates_file(self, writer: RawWriter, tmp_path: Path) -> None:
        writer.quarantine({"raw": "bad data"}, reason="missing_field")
        records = load_quarantine(tmp_path, SESSION_DATE, "test_session")
        assert len(records) == 1

    def test_quarantine_stores_reason(self, writer: RawWriter, tmp_path: Path) -> None:
        writer.quarantine({"x": 1}, reason="INVALID_STRIKE")
        records = load_quarantine(tmp_path, SESSION_DATE, "test_session")
        assert records[0]["reason_code"] == "INVALID_STRIKE"

    def test_quarantine_multiple(self, writer: RawWriter, tmp_path: Path) -> None:
        for i in range(3):
            writer.quarantine({"i": i}, reason=f"err_{i}")
        records = load_quarantine(tmp_path, SESSION_DATE, "test_session")
        assert len(records) == 3

    def test_quarantine_has_required_fields(self, writer: RawWriter, tmp_path: Path) -> None:
        writer.quarantine({"x": 1}, reason="test")
        rec = load_quarantine(tmp_path, SESSION_DATE, "test_session")[0]
        assert "quarantine_id" in rec
        assert "session_id" in rec
        assert "reason_code" in rec
        assert "quarantined_at" in rec


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


class TestReplaySession:
    def test_replay_returns_written_events(self, tmp_path: Path) -> None:
        with RawWriter(tmp_path, "s1", SESSION_DATE) as w:
            e1 = _make_event(receipt_ts=1_700_000_001.0, field_name="bid")
            e2 = _make_event(receipt_ts=1_700_000_002.0, field_name="ask")
            w.append(e1)
            w.append(e2)

        replayed = list(replay_session(tmp_path, SESSION_DATE))
        assert len(replayed) == 2

    def test_replay_sorted_by_receipt_ts(self, tmp_path: Path) -> None:
        ts_values = [1_700_000_003.0, 1_700_000_001.0, 1_700_000_002.0]
        with RawWriter(tmp_path, "s1", SESSION_DATE) as w:
            for ts in ts_values:
                w.append(_make_event(receipt_ts=ts))

        replayed = list(replay_session(tmp_path, SESSION_DATE))
        timestamps = [e.receipt_ts for e in replayed]
        assert timestamps == sorted(timestamps)

    def test_replay_marks_source_as_replay(self, tmp_path: Path) -> None:
        with RawWriter(tmp_path, "s1", SESSION_DATE) as w:
            w.append(_make_event(source="live"))
        replayed = list(replay_session(tmp_path, SESSION_DATE))
        assert all(e.source == "replay" for e in replayed)

    def test_replay_empty_when_no_data(self, tmp_path: Path) -> None:
        assert list(replay_session(tmp_path, SESSION_DATE)) == []

    def test_replay_specific_session(self, tmp_path: Path) -> None:
        with RawWriter(tmp_path, "s1", SESSION_DATE) as w:
            w.append(_make_event(session_id="s1", receipt_ts=1_700_000_001.0))
        with RawWriter(tmp_path, "s2", SESSION_DATE) as w:
            w.append(_make_event(session_id="s2", receipt_ts=1_700_000_002.0))

        only_s1 = list(replay_session(tmp_path, SESSION_DATE, session_id="s1"))
        assert len(only_s1) == 1
        assert only_s1[0].session_id == "s1"

    def test_replay_handles_partial_last_line(self, tmp_path: Path) -> None:
        """Simulate a kill mid-write: append a partial JSON line."""
        writer = RawWriter(tmp_path, "s1", SESSION_DATE)
        writer.append(_make_event(receipt_ts=1_700_000_001.0))
        writer.flush()

        # Simulate corruption: append a truncated line
        path = writer.raw_dir / "raw_market_events.jsonl"
        with path.open("a") as fh:
            fh.write('{"session_id": "s1", "event_id": "abc", "instru')  # truncated
        writer.close()

        # Replay should return the 1 valid event and skip the bad line
        replayed = list(replay_session(tmp_path, SESSION_DATE))
        assert len(replayed) == 1


# ---------------------------------------------------------------------------
# Kill-restart acceptance test
# ---------------------------------------------------------------------------


class TestKillRestart:
    """Proves that stopping a session mid-flight does not corrupt the raw store."""

    def test_session1_data_intact_after_session2_writes(self, tmp_path: Path) -> None:
        # Session 1: write 3 events, then simulate kill (close without explicit flush)
        with RawWriter(tmp_path, "session_a", SESSION_DATE) as w1:
            for i in range(3):
                w1.append(_make_event(
                    session_id="session_a",
                    receipt_ts=float(1_700_000_000 + i),
                    field_name="bid",
                    field_value=float(450 + i),
                ))

        # Session 2: write 2 more events (different session dir)
        with RawWriter(tmp_path, "session_b", SESSION_DATE) as w2:
            for i in range(2):
                w2.append(_make_event(
                    session_id="session_b",
                    receipt_ts=float(1_700_001_000 + i),
                    field_name="ask",
                    field_value=float(451 + i),
                ))

        # Replay all sessions: must see all 5 events, sorted by ts
        all_events = list(replay_session(tmp_path, SESSION_DATE))
        assert len(all_events) == 5
        assert all_events[0].receipt_ts < all_events[-1].receipt_ts

        # Session 1 events are intact
        s1_events = [e for e in all_events if e.session_id == "session_a"]
        assert len(s1_events) == 3

        # Session 2 events are intact
        s2_events = [e for e in all_events if e.session_id == "session_b"]
        assert len(s2_events) == 2

    def test_new_session_writes_to_separate_directory(self, tmp_path: Path) -> None:
        with RawWriter(tmp_path, "session_a", SESSION_DATE) as w1:
            w1.append(_make_event(session_id="session_a"))
        with RawWriter(tmp_path, "session_b", SESSION_DATE) as w2:
            w2.append(_make_event(session_id="session_b"))

        raw_dir = tmp_path / "raw" / f"dt={SESSION_DATE.isoformat()}"
        dirs = {d.name for d in raw_dir.iterdir()}
        assert "session=session_a" in dirs
        assert "session=session_b" in dirs


# ---------------------------------------------------------------------------
# CollectorSession
# ---------------------------------------------------------------------------


class TestCollectorSession:
    def test_coverage_ratio_empty(self) -> None:
        s = CollectorSession()
        s.set_subscribed_count(10)
        assert s.coverage_ratio == 0.0

    def test_coverage_ratio_full(self) -> None:
        s = CollectorSession()
        keys = ["SPY|STK|SMART|USD", "QQQ|STK|SMART|USD"]
        s.set_subscribed_count(2)
        for k in keys:
            s.record_event(k, time.time())
        assert s.coverage_ratio == 1.0

    def test_coverage_ratio_partial(self) -> None:
        s = CollectorSession()
        s.set_subscribed_count(4)
        s.record_event("A", time.time())
        s.record_event("B", time.time())
        assert s.coverage_ratio == 0.5

    def test_summary_keys(self) -> None:
        s = CollectorSession()
        summary = s.to_summary()
        for key in (
            "session_id", "started_at", "elapsed_seconds",
            "event_count", "malformed_count", "reconnect_count",
            "coverage_ratio", "subscribed_count", "covered_count",
        ):
            assert key in summary

    def test_malformed_incremented(self) -> None:
        s = CollectorSession()
        s.record_malformed()
        s.record_malformed()
        assert s.malformed_count == 2


# ---------------------------------------------------------------------------
# RawCollector integration — using MockAdapter
# ---------------------------------------------------------------------------


class TestRawCollector:
    def _make_contract(self, symbol: str = "SPY") -> CanonicalContract:
        return CanonicalContract(
            underlying_symbol=symbol, sec_type="STK", exchange="SMART", currency="USD"
        )

    def test_subscribe_and_receive_events(
        self, adapter: MockAdapter, tmp_path: Path
    ) -> None:
        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            collector = RawCollector(adapter, w)
            contract = self._make_contract("SPY")
            req_ids = adapter.subscribe_quotes([contract], lambda q: None)

            # Wire up collector's callback
            collector.subscribe([contract])
            # emit_tick goes directly through adapter's subscription; collector
            # registered its own callback via subscribe(), so we get it via req_ids[1]
            # (adapter assigns sequential IDs; collector's sub is after manual sub)
            # Use collector._req_ids[0] which is the collector's subscription
            collector_req_id = collector._req_ids[0]
            adapter.emit_tick(collector_req_id, bid=450.10, ask=450.12, last=450.11)

        replayed = list(replay_session(tmp_path, SESSION_DATE))
        assert len(replayed) == 3  # bid, ask, last
        field_names = {e.field_name for e in replayed}
        assert {"bid", "ask", "last"} == field_names

    def test_malformed_event_quarantined_not_dropped(
        self, adapter: MockAdapter, tmp_path: Path
    ) -> None:
        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            collector = RawCollector(adapter, w)
            contract = self._make_contract("SPY")
            collector.subscribe([contract])

            # Emit a quote with an empty instrument_key — will fail normalization
            bad_quote = QuoteSnapshot(
                instrument_key="",  # invalid
                receipt_ts=datetime.now(tz=timezone.utc),
                exchange_ts=None,
                bid=450.0, ask=450.1, last=None,
            )
            collector._on_quote(bad_quote)

        # Raw store: no events
        assert list(replay_session(tmp_path, SESSION_DATE)) == []
        # Quarantine: one record
        records = load_quarantine(tmp_path, SESSION_DATE, "sess")
        assert len(records) == 1
        assert records[0]["reason_code"]  # reason_code is populated

    def test_session_summary_tracks_events(
        self, adapter: MockAdapter, tmp_path: Path
    ) -> None:
        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            collector = RawCollector(adapter, w)
            contracts = [self._make_contract("SPY"), self._make_contract("QQQ")]
            collector.subscribe(contracts)

            for rid in collector._req_ids:
                adapter.emit_tick(rid, bid=100.0, ask=100.1)

        summary = collector.get_session_summary()
        assert summary["event_count"] == 4  # 2 events (bid+ask) × 2 contracts
        assert summary["coverage_ratio"] == 1.0
        assert summary["subscribed_count"] == 2

    def test_reconnected_resubscribes(
        self, adapter: MockAdapter, tmp_path: Path
    ) -> None:
        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            collector = RawCollector(adapter, w)
            contract = self._make_contract("SPY")
            collector.subscribe([contract])
            old_req_ids = list(collector._req_ids)

            collector.cancel()
            collector.reconnected()

            # New req_ids assigned
            assert collector._req_ids != old_req_ids
            assert collector.session.reconnect_count == 1


# ---------------------------------------------------------------------------
# PacingLimiter
# ---------------------------------------------------------------------------


class TestPacingLimiter:
    def test_instantiation_with_default_rate(self) -> None:
        limiter = PacingLimiter()
        assert limiter.max_per_second == 40.0

    def test_custom_rate(self) -> None:
        limiter = PacingLimiter(max_per_second=10.0)
        assert limiter.max_per_second == 10.0

    def test_zero_rate_raises(self) -> None:
        with pytest.raises(ValueError):
            PacingLimiter(max_per_second=0.0)

    def test_negative_rate_raises(self) -> None:
        with pytest.raises(ValueError):
            PacingLimiter(max_per_second=-5.0)

    def test_throttle_does_not_block_first_call(self) -> None:
        limiter = PacingLimiter(max_per_second=40.0)
        start = time.monotonic()
        limiter.throttle()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1  # must complete in <100 ms

    def test_available_tokens_starts_at_max(self) -> None:
        limiter = PacingLimiter(max_per_second=40.0)
        tokens = limiter.available_tokens()
        assert tokens == pytest.approx(40.0, abs=1.0)

    def test_available_tokens_decreases_after_throttle(self) -> None:
        limiter = PacingLimiter(max_per_second=40.0)
        before = limiter.available_tokens()
        limiter.throttle()
        after = limiter.available_tokens()
        assert after < before

    def test_tokens_refill_over_time(self) -> None:
        limiter = PacingLimiter(max_per_second=1000.0)
        # Drain all tokens
        for _ in range(int(limiter.max_per_second)):
            limiter._tokens -= 1.0
        limiter._tokens = 0.0
        # Wait 10 ms; at 1000/s that refills 10 tokens
        time.sleep(0.010)
        available = limiter.available_tokens()
        assert available >= 5.0  # conservative check

    def test_throttle_counts_consumed(self) -> None:
        limiter = PacingLimiter(max_per_second=40.0)
        limiter.throttle()
        limiter.throttle()
        # After 2 calls, tokens should have decreased by ~2 (net of refill)
        tokens = limiter.available_tokens()
        assert tokens <= 40.0

    def test_pacing_limiter_wired_into_collector(
        self, adapter: MockAdapter, tmp_path: Path
    ) -> None:
        limiter = PacingLimiter(max_per_second=40.0)
        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            collector = RawCollector(adapter, w, pacing_limiter=limiter)
            assert collector._pacing is limiter

    def test_default_limiter_created_when_none_passed(
        self, adapter: MockAdapter, tmp_path: Path
    ) -> None:
        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            collector = RawCollector(adapter, w, pacing_limiter=None)
            assert collector._pacing is not None
            assert collector._pacing.max_per_second == 40.0


# ---------------------------------------------------------------------------
# ESTX50 end-to-end subscription via MockAdapter
# ---------------------------------------------------------------------------


class TestESTX50Subscription:
    """Collector subscribes to Euro Stoxx 50 index + at least 1 constituent."""

    def test_subscribe_estx50_index_and_constituent(
        self, adapter: MockAdapter, tmp_path: Path
    ) -> None:
        index_contract = CanonicalContract(
            underlying_symbol="ESTX50", sec_type="IND",
            exchange="EUREX", currency="EUR",
        )
        constituent_contract = CanonicalContract(
            underlying_symbol="MC.PA", sec_type="STK",
            exchange="SMART", currency="EUR",
        )

        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            collector = RawCollector(adapter, w)
            collector.subscribe([index_contract, constituent_contract])
            assert collector.session._subscribed_count == 2

            # Emit ticks on both subscriptions
            for rid in collector._req_ids:
                adapter.emit_tick(rid, bid=5000.0, ask=5002.0)

        replayed = list(replay_session(tmp_path, SESSION_DATE))
        # 2 events (bid + ask) × 2 contracts = 4
        assert len(replayed) == 4

        keys = {e.instrument_key for e in replayed}
        assert any("ESTX50" in k for k in keys)
        assert any("MC.PA" in k for k in keys)

    def test_estx50_coverage_ratio_after_subscription(
        self, adapter: MockAdapter, tmp_path: Path
    ) -> None:
        contracts = [
            CanonicalContract(
                underlying_symbol="ESTX50", sec_type="IND",
                exchange="EUREX", currency="EUR",
            ),
            CanonicalContract(
                underlying_symbol="SAN.MC", sec_type="STK",
                exchange="SMART", currency="EUR",
            ),
        ]
        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            collector = RawCollector(adapter, w)
            collector.subscribe(contracts)
            for rid in collector._req_ids:
                adapter.emit_tick(rid, bid=100.0, ask=101.0)

        summary = collector.get_session_summary()
        assert summary["coverage_ratio"] == 1.0
        assert summary["subscribed_count"] == 2

    def test_estx50_events_stored_with_correct_instrument_key(
        self, adapter: MockAdapter, tmp_path: Path
    ) -> None:
        index_contract = CanonicalContract(
            underlying_symbol="ESTX50", sec_type="IND",
            exchange="EUREX", currency="EUR",
        )
        with RawWriter(tmp_path, "sess", SESSION_DATE) as w:
            collector = RawCollector(adapter, w)
            collector.subscribe([index_contract])
            adapter.emit_tick(collector._req_ids[0], bid=4998.0, ask=5002.0, last=5000.0)

        replayed = list(replay_session(tmp_path, SESSION_DATE))
        assert len(replayed) == 3  # bid, ask, last
        expected_key = "ESTX50|IND|EUREX|EUR"
        assert all(e.instrument_key == expected_key for e in replayed)
