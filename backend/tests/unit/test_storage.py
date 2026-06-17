"""
Unit tests for Step 4: Persistent storage and data model.

Acceptance criterion: Replay and live writes use identical schemas.

Coverage:
  - Write-ahead validation quarantines malformed records (never silently drops)
  - Valid records round-trip through Parquet correctly
  - JSONL raw-event layer is append-only and readable
  - lineage_query resolves raw sessions from snapshot context
  - Replay (source="replay") and live (source="live") produce identical schemas
  - Manifest write/read round-trip
  - list_partitions returns correct date list
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pandas as pd
import pytest

from src.storage.schemas import (
    ForwardCurveRow,
    IVPointRow,
    MarketStateSnapshotRow,
    PricingResultRow,
    QCResultRow,
    RawMarketEventRow,
    SurfaceGridRow,
    SurfaceParametersRow,
)
from src.storage.writer import StorageWriter, _validate_raw_event, _validate_snapshot
from src.storage.reader import StorageReader

TRADE_DATE = "2026-06-07"
UNDERLYING = "SPY"
SESSION_ID = "sess_test_001"
SNAP_TS = 1_749_290_400.0  # 2026-06-07 some UTC epoch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def writer(storage_root: Path) -> StorageWriter:
    return StorageWriter(str(storage_root), {})


@pytest.fixture
def reader(storage_root: Path) -> StorageReader:
    return StorageReader(str(storage_root), {})


def _raw_event(source: str = "live", **overrides) -> dict:
    base = dict(
        session_id=SESSION_ID,
        event_id=uuid.uuid4().hex,
        instrument_key=f"{UNDERLYING}|STK|SMART|USD",
        field_name="bid",
        field_value=450.10,
        exchange_ts=None,
        receipt_ts=1_700_000_000.0,
        source=source,
    )
    base.update(overrides)
    return base


def _snapshot_row(**overrides) -> dict:
    base = dict(
        snapshot_ts=SNAP_TS,
        instrument_key=f"{UNDERLYING}|STK|SMART|USD",
        underlying_symbol=UNDERLYING,
        bid=450.10,
        ask=450.12,
        last=450.11,
        mid=450.11,
        volume=1_000_000.0,
        open_interest=None,
        spread_pct=0.0004,
        reference_spot=450.11,
        reference_type="mid",
        quote_age_seconds=1.5,
        is_stale=False,
        is_market_open=True,
        maturity_years=None,
        session_id=SESSION_ID,
        snapshot_version="1.0",
    )
    base.update(overrides)
    return base


def _forward_row(**overrides) -> dict:
    base = dict(
        snapshot_ts=SNAP_TS,
        underlying=UNDERLYING,
        expiry_str="2026-12-19",
        maturity_years=0.53,
        chosen_forward=451.0,
        weighted_mean_forward=451.0,
        median_forward=450.9,
        confidence_score=0.95,
        candidates_count=12,
        fallback_used="none",
        implied_carry=0.005,
        diagnostics_version="1.0",
    )
    base.update(overrides)
    return base


def _iv_row(**overrides) -> dict:
    base = dict(
        snapshot_ts=SNAP_TS,
        contract_key=f"{UNDERLYING}|OPT|SMART|USD|20261219|450.0|C",
        underlying=UNDERLYING,
        expiry_str="2026-12-19",
        maturity_years=0.53,
        strike=450.0,
        option_right="C",
        forward=451.0,
        log_moneyness=-0.002,
        market_price=15.0,
        implied_vol=0.20,
        total_variance=0.021,
        converged=True,
        solver_residual=1e-8,
        iterations=7,
        failure_reason=None,
        model_name="brentq",
        solver_version="1.0",
    )
    base.update(overrides)
    return base


def _qc_row(**overrides) -> dict:
    base = dict(
        run_id="run_abc123",
        check_name="check_spread_pct",
        target_key=f"{UNDERLYING}|STK|SMART|USD",
        qc_status="pass",
        reason_code="ok",
        measured_value=0.0004,
        threshold=0.05,
        severity="info",
        run_ts=SNAP_TS,
        threshold_version="1.0",
        context_json="{}",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Validation unit tests
# ---------------------------------------------------------------------------


class TestValidation:

    def test_raw_event_valid(self):
        assert _validate_raw_event(_raw_event()) is None

    def test_raw_event_missing_session_id(self):
        assert _validate_raw_event(_raw_event(session_id="")) == "missing_session_id"

    def test_raw_event_missing_instrument_key(self):
        assert _validate_raw_event(_raw_event(instrument_key="")) == "missing_instrument_key"

    def test_raw_event_non_finite_value(self):
        import math
        assert _validate_raw_event(_raw_event(field_value=math.nan)) == "non_finite_field_value"

    def test_raw_event_invalid_source(self):
        result = _validate_raw_event(_raw_event(source="unknown"))
        assert result and result.startswith("invalid_source")

    def test_snapshot_valid(self):
        assert _validate_snapshot(_snapshot_row()) is None

    def test_snapshot_crossed_market(self):
        assert _validate_snapshot(_snapshot_row(bid=450.20, ask=450.10)) == "crossed_market"

    def test_snapshot_missing_instrument_key(self):
        assert _validate_snapshot(_snapshot_row(instrument_key="")) == "missing_instrument_key"


# ---------------------------------------------------------------------------
# Raw event write / read round-trip
# ---------------------------------------------------------------------------


class TestRawEventRoundTrip:

    def test_write_then_read(self, writer: StorageWriter, reader: StorageReader):
        ev = _raw_event()
        count = writer.write_raw_events([ev], TRADE_DATE, SESSION_ID)
        assert count == 1

        events = reader.read_raw_events(TRADE_DATE, session_id=SESSION_ID)
        assert len(events) == 1
        assert events[0]["field_value"] == ev["field_value"]
        assert events[0]["source"] == "live"

    def test_append_only_does_not_lose_prior_events(self, writer: StorageWriter,
                                                     reader: StorageReader):
        writer.write_raw_events([_raw_event()], TRADE_DATE, SESSION_ID)
        writer.write_raw_events([_raw_event()], TRADE_DATE, SESSION_ID)
        events = reader.read_raw_events(TRADE_DATE, session_id=SESSION_ID)
        assert len(events) == 2

    def test_malformed_event_quarantined(self, writer: StorageWriter, reader: StorageReader):
        good = _raw_event()
        bad = _raw_event(field_value=float("nan"))
        count = writer.write_raw_events([good, bad], TRADE_DATE, SESSION_ID)
        assert count == 1  # only good one written

        rejected = writer.read_rejected_records("raw_market_events")
        assert len(rejected) == 1
        assert rejected[0]["reason_code"] == "non_finite_field_value"

    def test_malformed_event_never_in_file(self, writer: StorageWriter, reader: StorageReader):
        bad = _raw_event(session_id="")
        writer.write_raw_events([bad], TRADE_DATE, SESSION_ID)
        events = reader.read_raw_events(TRADE_DATE, session_id=SESSION_ID)
        assert len(events) == 0

    def test_filter_by_underlying(self, writer: StorageWriter, reader: StorageReader):
        ev_spy = _raw_event(instrument_key="SPY|STK|SMART|USD")
        ev_qqq = _raw_event(instrument_key="QQQ|STK|SMART|USD")
        writer.write_raw_events([ev_spy, ev_qqq], TRADE_DATE, SESSION_ID)
        result = reader.read_raw_events(TRADE_DATE, underlying="QQQ")
        assert len(result) == 1
        assert result[0]["instrument_key"].startswith("QQQ")


# ---------------------------------------------------------------------------
# Identical schemas: live vs replay  (acceptance criterion)
# ---------------------------------------------------------------------------


class TestLiveVsReplaySchema:
    """Replay and live writes must produce identical column schemas."""

    def test_raw_events_live_vs_replay_identical_schema(self, writer: StorageWriter,
                                                         reader: StorageReader, tmp_path: Path):
        live_ev = _raw_event(source="live")
        replay_ev = _raw_event(source="replay")

        writer.write_raw_events([live_ev], TRADE_DATE, "sess_live")
        writer.write_raw_events([replay_ev], TRADE_DATE, "sess_replay")

        live_rows = reader.read_raw_events(TRADE_DATE, session_id="sess_live")
        replay_rows = reader.read_raw_events(TRADE_DATE, session_id="sess_replay")

        assert live_rows and replay_rows
        assert set(live_rows[0].keys()) == set(replay_rows[0].keys()), (
            "Live and replay raw events must have identical field sets"
        )

    def test_snapshots_live_vs_replay_identical_schema(self, writer: StorageWriter,
                                                        reader: StorageReader):
        snap = _snapshot_row()
        writer.write_snapshots([snap], TRADE_DATE, UNDERLYING)

        snap_replay = _snapshot_row()
        snap_replay["session_id"] = "sess_replay_002"
        writer.write_snapshots([snap_replay], TRADE_DATE, UNDERLYING)

        rows = reader.read_snapshots(TRADE_DATE, UNDERLYING)
        assert len(rows) == 2
        assert set(rows[0].keys()) == set(rows[1].keys())


# ---------------------------------------------------------------------------
# Snapshot write / read
# ---------------------------------------------------------------------------


class TestSnapshots:

    def test_write_then_read(self, writer: StorageWriter, reader: StorageReader):
        snap = _snapshot_row()
        count = writer.write_snapshots([snap], TRADE_DATE, UNDERLYING)
        assert count == 1

        rows = reader.read_snapshots(TRADE_DATE, UNDERLYING)
        assert len(rows) == 1
        assert rows[0]["instrument_key"] == snap["instrument_key"]

    def test_crossed_market_quarantined(self, writer: StorageWriter, reader: StorageReader):
        bad = _snapshot_row(bid=460.0, ask=450.0)
        writer.write_snapshots([bad], TRADE_DATE, UNDERLYING)

        rows = reader.read_snapshots(TRADE_DATE, UNDERLYING)
        assert len(rows) == 0

        rejected = writer.read_rejected_records("market_state_snapshots")
        assert any(r["reason_code"] == "crossed_market" for r in rejected)

    def test_snapshot_ts_range_filter(self, writer: StorageWriter, reader: StorageReader):
        s1 = _snapshot_row(snapshot_ts=1_000.0)
        s2 = _snapshot_row(snapshot_ts=2_000.0)
        s3 = _snapshot_row(snapshot_ts=3_000.0)
        writer.write_snapshots([s1, s2, s3], TRADE_DATE, UNDERLYING)

        rows = reader.read_snapshots(TRADE_DATE, UNDERLYING, snapshot_ts_range=(1_500.0, 2_500.0))
        assert len(rows) == 1
        assert rows[0]["snapshot_ts"] == 2_000.0


# ---------------------------------------------------------------------------
# Forward curve write / read
# ---------------------------------------------------------------------------


class TestForwardCurve:

    def test_write_then_read(self, writer: StorageWriter, reader: StorageReader):
        row = _forward_row()
        count = writer.write_forward_curve([row], TRADE_DATE, UNDERLYING, "v1.0")
        assert count == 1

        rows = reader.read_forward_curve(TRADE_DATE, UNDERLYING, analytics_version="v1.0")
        assert len(rows) == 1
        assert abs(rows[0]["chosen_forward"] - row["chosen_forward"]) < 1e-6

    def test_invalid_forward_quarantined(self, writer: StorageWriter, reader: StorageReader):
        bad = _forward_row(chosen_forward=-1.0)
        writer.write_forward_curve([bad], TRADE_DATE, UNDERLYING, "v1.0")
        rows = reader.read_forward_curve(TRADE_DATE, UNDERLYING, analytics_version="v1.0")
        assert len(rows) == 0
        rejected = writer.read_rejected_records("forward_curve")
        assert any(r["reason_code"] == "invalid_forward" for r in rejected)

    def test_invalid_maturity_quarantined(self, writer: StorageWriter, reader: StorageReader):
        bad = _forward_row(maturity_years=0.0)
        writer.write_forward_curve([bad], TRADE_DATE, UNDERLYING, "v1.0")
        rejected = writer.read_rejected_records("forward_curve")
        assert any(r["reason_code"] == "invalid_maturity" for r in rejected)


# ---------------------------------------------------------------------------
# IV points write / read
# ---------------------------------------------------------------------------


class TestIVPoints:

    def test_write_then_read(self, writer: StorageWriter, reader: StorageReader):
        row = _iv_row()
        count = writer.write_iv_points([row], TRADE_DATE, UNDERLYING, "v1.0")
        assert count == 1
        rows = reader.read_iv_points(TRADE_DATE, UNDERLYING, solver_version="v1.0")
        assert len(rows) == 1
        assert abs(rows[0]["implied_vol"] - row["implied_vol"]) < 1e-8

    def test_invalid_option_right_quarantined(self, writer: StorageWriter, reader: StorageReader):
        bad = _iv_row(option_right="X")
        writer.write_iv_points([bad], TRADE_DATE, UNDERLYING, "v1.0")
        rows = reader.read_iv_points(TRADE_DATE, UNDERLYING, solver_version="v1.0")
        assert len(rows) == 0
        rejected = writer.read_rejected_records("iv_points")
        assert any("invalid_option_right" in r["reason_code"] for r in rejected)

    def test_negative_iv_quarantined(self, writer: StorageWriter, reader: StorageReader):
        bad = _iv_row(implied_vol=-0.01)
        writer.write_iv_points([bad], TRADE_DATE, UNDERLYING, "v1.0")
        rejected = writer.read_rejected_records("iv_points")
        assert any(r["reason_code"] == "invalid_implied_vol" for r in rejected)


# ---------------------------------------------------------------------------
# QC results write / read
# ---------------------------------------------------------------------------


class TestQCResults:

    def test_write_then_read(self, writer: StorageWriter, reader: StorageReader):
        row = _qc_row()
        count = writer.write_qc_results([row], "run_abc123", TRADE_DATE)
        assert count == 1
        rows = reader.read_qc_results("run_abc123")
        assert len(rows) == 1
        assert rows[0]["qc_status"] == "pass"

    def test_invalid_status_quarantined(self, writer: StorageWriter, reader: StorageReader):
        bad = _qc_row(qc_status="unknown")
        writer.write_qc_results([bad], "run_bad", TRADE_DATE)
        rows = reader.read_qc_results("run_bad")
        assert len(rows) == 0
        rejected = writer.read_rejected_records("qc_results")
        assert any("invalid_qc_status" in r["reason_code"] for r in rejected)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:

    def test_write_then_read(self, writer: StorageWriter, reader: StorageReader):
        manifest = {
            "run_id": "run_001",
            "code_version": "0.1.0",
            "trade_date": TRADE_DATE,
            "status": "complete",
        }
        writer.write_manifest(manifest, "run_001")
        loaded = reader.read_manifest("run_001")
        assert loaded == manifest

    def test_missing_manifest_returns_none(self, reader: StorageReader):
        assert reader.read_manifest("nonexistent_run") is None


# ---------------------------------------------------------------------------
# Lineage query
# ---------------------------------------------------------------------------


class TestLineageQuery:

    def test_no_metadata_db_returns_error(self, reader: StorageReader):
        result = reader.lineage_query(SNAP_TS, UNDERLYING)
        assert "error" in result

    def test_lineage_links_session_to_snapshot(self, writer: StorageWriter,
                                                reader: StorageReader):
        # Write raw events
        writer.write_raw_events([_raw_event()], TRADE_DATE, SESSION_ID)
        writer.write_snapshots([_snapshot_row()], TRADE_DATE, UNDERLYING)
        writer.write_lineage(SNAP_TS, UNDERLYING, [SESSION_ID], TRADE_DATE)

        result = reader.lineage_query(SNAP_TS, UNDERLYING)
        assert result["underlying"] == UNDERLYING
        assert SESSION_ID in result["source_raw_sessions"]
        assert len(result["raw_market_events"]) >= 1
        assert len(result["market_state_snapshots"]) >= 1

    def test_lineage_query_multiple_sessions(self, writer: StorageWriter,
                                              reader: StorageReader):
        sid2 = "sess_test_002"
        writer.write_raw_events([_raw_event()], TRADE_DATE, SESSION_ID)
        writer.write_raw_events([_raw_event()], TRADE_DATE, sid2)
        writer.write_lineage(SNAP_TS, UNDERLYING, [SESSION_ID, sid2], TRADE_DATE)

        result = reader.lineage_query(SNAP_TS, UNDERLYING)
        assert SESSION_ID in result["source_raw_sessions"]
        assert sid2 in result["source_raw_sessions"]


# ---------------------------------------------------------------------------
# list_partitions
# ---------------------------------------------------------------------------


class TestListPartitions:

    def test_empty_returns_empty(self, reader: StorageReader):
        assert reader.list_partitions("analytics", "market_state_snapshots") == []

    def test_returns_written_dates(self, writer: StorageWriter, reader: StorageReader):
        writer.write_snapshots([_snapshot_row()], "2026-06-05", UNDERLYING)
        writer.write_snapshots([_snapshot_row()], "2026-06-06", UNDERLYING)
        writer.write_snapshots([_snapshot_row()], "2026-06-07", UNDERLYING)

        dates = reader.list_partitions("analytics", "market_state_snapshots")
        assert "2026-06-05" in dates
        assert "2026-06-07" in dates
        assert dates == sorted(dates)

    def test_date_range_filter(self, writer: StorageWriter, reader: StorageReader):
        for d in ["2026-06-01", "2026-06-05", "2026-06-10"]:
            writer.write_snapshots([_snapshot_row()], d, UNDERLYING)
        dates = reader.list_partitions("analytics", "market_state_snapshots",
                                       date_range=("2026-06-03", "2026-06-07"))
        assert "2026-06-01" not in dates
        assert "2026-06-05" in dates
        assert "2026-06-10" not in dates


# ---------------------------------------------------------------------------
# Version partitioning (no overwrites)
# ---------------------------------------------------------------------------


class TestVersionedPartitions:

    def test_two_versions_coexist(self, writer: StorageWriter, reader: StorageReader):
        row_v1 = _forward_row(chosen_forward=451.0)
        row_v2 = _forward_row(chosen_forward=452.0)

        writer.write_forward_curve([row_v1], TRADE_DATE, UNDERLYING, "v1.0")
        writer.write_forward_curve([row_v2], TRADE_DATE, UNDERLYING, "v2.0")

        rows_v1 = reader.read_forward_curve(TRADE_DATE, UNDERLYING, analytics_version="v1.0")
        rows_v2 = reader.read_forward_curve(TRADE_DATE, UNDERLYING, analytics_version="v2.0")

        assert len(rows_v1) == 1
        assert len(rows_v2) == 1
        assert rows_v1[0]["chosen_forward"] != rows_v2[0]["chosen_forward"]
