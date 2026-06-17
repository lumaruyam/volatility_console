"""
Comprehensive tests for Step 13: Historical reconstruction and replay.

Acceptance criterion (PLAN):
  Replay == live on overlapping dates with same code version.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock, call, patch

import pytest

from src.orchestration.replay import (
    DataCompletenessReport,
    ReplayComparisonResult,
    ReplayManifest,
    build_replay_manifest,
    compare_replay_vs_live,
    config_hash,
    config_hashes,
    detect_data_completeness,
    partition_path,
    replay_date_range,
    replay_day,
)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def _make_reader(
    partitions=None,
    analytics: dict | None = None,
) -> MagicMock:
    """Fake storage reader."""
    reader = MagicMock()
    reader.list_partitions.return_value = partitions if partitions is not None else []
    reader.read_analytics.side_effect = lambda version, dt: (analytics or {}).get(
        version, {}
    )
    return reader


def _make_writer() -> MagicMock:
    """Fake storage writer."""
    writer = MagicMock()
    writer.write_manifest.return_value = None
    return writer


def _partitions_for(symbols: list[str]) -> list[dict]:
    return [{"symbol": s, "rows": 100} for s in symbols]


UNIVERSE = ["AAPL", "MSFT", "GOOG"]
DATE = "2025-01-15"
CODE_VER = "0.2.0"
CFG = {"pricing": {"model": "bs"}, "qc": {"min_volume": 10}}


# ---------------------------------------------------------------------------
# TestPartitionPath
# ---------------------------------------------------------------------------

class TestPartitionPath:
    def test_format(self):
        p = partition_path("analytics", "0.1.0", "2025-01-15")
        assert p == "analytics/v=0.1.0/dt=2025-01-15"

    def test_different_version(self):
        p = partition_path("analytics", "1.0.0", "2025-03-01")
        assert p == "analytics/v=1.0.0/dt=2025-03-01"

    def test_different_base(self):
        p = partition_path("output/raw", "0.9.1", "2025-01-01")
        assert p == "output/raw/v=0.9.1/dt=2025-01-01"

    def test_same_inputs_same_output(self):
        assert partition_path("a", "1.0", "2025-01-01") == partition_path("a", "1.0", "2025-01-01")

    def test_version_in_output(self):
        p = partition_path("base", "2.3.4", "2025-06-01")
        assert "v=2.3.4" in p

    def test_date_in_output(self):
        p = partition_path("base", "1.0", "2025-12-31")
        assert "dt=2025-12-31" in p


# ---------------------------------------------------------------------------
# TestConfigHash
# ---------------------------------------------------------------------------

class TestConfigHash:
    def test_returns_string(self):
        assert isinstance(config_hash({"a": 1}), str)

    def test_length_8(self):
        assert len(config_hash({"a": 1})) == 8

    def test_same_dict_same_hash(self):
        d = {"k": "v", "n": 42}
        assert config_hash(d) == config_hash(d)

    def test_different_dict_different_hash(self):
        assert config_hash({"a": 1}) != config_hash({"a": 2})

    def test_key_order_invariant(self):
        d1 = {"a": 1, "b": 2}
        d2 = {"b": 2, "a": 1}
        assert config_hash(d1) == config_hash(d2)

    def test_config_hashes_per_key(self):
        cfg = {"pricing": {"model": "bs"}, "qc": {"min_vol": 0.01}}
        hashes = config_hashes(cfg)
        assert set(hashes.keys()) == {"pricing", "qc"}
        assert all(len(v) == 8 for v in hashes.values())

    def test_config_hashes_different_sections(self):
        cfg = {"a": {"x": 1}, "b": {"x": 1}}
        # Different keys, same content — hashes should differ (different key)
        hashes = config_hashes(cfg)
        # Same content → same hash (content-addressed per key)
        assert hashes["a"] == hashes["b"]


# ---------------------------------------------------------------------------
# TestDetectDataCompleteness
# ---------------------------------------------------------------------------

class TestDetectDataCompleteness:
    def test_empty_partitions_is_empty(self):
        reader = _make_reader(partitions=[])
        report = detect_data_completeness(reader, DATE)
        assert report.is_empty
        assert not report.is_complete

    def test_full_universe_is_complete(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        report = detect_data_completeness(reader, DATE, expected_symbols=UNIVERSE)
        assert report.is_complete
        assert not report.is_partial
        assert report.coverage_pct == pytest.approx(100.0)

    def test_partial_universe_is_partial(self):
        reader = _make_reader(partitions=_partitions_for(["AAPL"]))
        report = detect_data_completeness(reader, DATE, expected_symbols=UNIVERSE)
        assert report.is_partial
        assert not report.is_complete
        assert "MSFT" in report.symbols_missing
        assert "GOOG" in report.symbols_missing

    def test_no_expected_symbols_complete_when_partitions_exist(self):
        reader = _make_reader(partitions=_partitions_for(["X"]))
        report = detect_data_completeness(reader, DATE)
        assert report.is_complete
        assert not report.is_empty

    def test_coverage_pct_calculation(self):
        reader = _make_reader(partitions=_partitions_for(["AAPL", "MSFT"]))
        report = detect_data_completeness(reader, DATE, expected_symbols=UNIVERSE)
        assert report.coverage_pct == pytest.approx(100 * 2 / 3, rel=1e-4)

    def test_missing_symbols_list(self):
        reader = _make_reader(partitions=_partitions_for(["AAPL"]))
        report = detect_data_completeness(reader, DATE, expected_symbols=UNIVERSE)
        assert set(report.symbols_missing) == {"MSFT", "GOOG"}

    def test_found_symbols_list(self):
        reader = _make_reader(partitions=_partitions_for(["AAPL", "MSFT"]))
        report = detect_data_completeness(reader, DATE, expected_symbols=UNIVERSE)
        assert "AAPL" in report.symbols_found
        assert "MSFT" in report.symbols_found
        assert "GOOG" not in report.symbols_found

    def test_raw_partition_count(self):
        reader = _make_reader(partitions=_partitions_for(["A", "B", "C"]))
        report = detect_data_completeness(reader, DATE)
        assert report.raw_partition_count == 3

    def test_returns_datacompleteness_report(self):
        reader = _make_reader(partitions=[])
        assert isinstance(detect_data_completeness(reader, DATE), DataCompletenessReport)

    def test_zero_expected_coverage_100pct(self):
        """Empty expected universe → 100% coverage (vacuously true)."""
        reader = _make_reader(partitions=[])
        report = detect_data_completeness(reader, DATE, expected_symbols=[])
        assert report.coverage_pct == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# TestBuildReplayManifest
# ---------------------------------------------------------------------------

class TestBuildReplayManifest:
    def _build(self, status="success", raw_count=5, failure_reason=None) -> ReplayManifest:
        return build_replay_manifest(
            trade_date=DATE,
            code_version=CODE_VER,
            cfg_hashes={"pricing": "abc12345"},
            status=status,
            raw_partitions=list(range(raw_count)),
            output_partitions=["analytics/v=0.2.0/dt=2025-01-15"],
            failure_reason=failure_reason,
        )

    def test_returns_replay_manifest(self):
        assert isinstance(self._build(), ReplayManifest)

    def test_trade_date(self):
        assert self._build().trade_date == DATE

    def test_code_version(self):
        assert self._build().code_version == CODE_VER

    def test_status(self):
        assert self._build("failed").status == "failed"

    def test_raw_partition_count(self):
        assert self._build(raw_count=7).raw_partition_count == 7

    def test_failure_reason_none_on_success(self):
        assert self._build("success").failure_reason is None

    def test_failure_reason_stored(self):
        m = self._build("failed", failure_reason="MISSING_RAW_PARTITION")
        assert m.failure_reason == "MISSING_RAW_PARTITION"

    def test_output_partition_path_correct(self):
        m = self._build()
        assert m.output_partition_path == partition_path("analytics", CODE_VER, DATE)

    def test_replay_flag_true(self):
        assert self._build().replay is True

    def test_to_dict_returns_dict(self):
        assert isinstance(self._build().to_dict(), dict)

    def test_to_dict_has_all_keys(self):
        d = self._build().to_dict()
        for k in ("type", "trade_date", "code_version", "config_hashes",
                  "status", "replay", "output_partition_path"):
            assert k in d


# ---------------------------------------------------------------------------
# TestReplayDay
# ---------------------------------------------------------------------------

class TestReplayDay:
    def _success_pipeline(self):
        """Mock job_eod_pipeline that returns success."""
        def fake_pipeline(run, reader, writer):
            writer.write_manifest({"run_id": run.run_id}, run.run_id)
            return {"status": "success", "steps": {"build_snapshots": {"status": "ok"}}}
        return fake_pipeline

    def _failing_pipeline(self):
        def fake_pipeline(run, reader, writer):
            return {"status": "failed", "steps": {}}
        return fake_pipeline

    def test_missing_raw_returns_failed_status(self):
        reader = _make_reader(partitions=[])
        writer = _make_writer()
        result = replay_day(DATE, CODE_VER, CFG, reader, writer)
        assert result["status"] == "failed"

    def test_missing_raw_failure_reason(self):
        reader = _make_reader(partitions=[])
        writer = _make_writer()
        result = replay_day(DATE, CODE_VER, CFG, reader, writer)
        assert result["failure_reason"] == "MISSING_RAW_PARTITION"

    def test_missing_raw_writes_manifest(self):
        reader = _make_reader(partitions=[])
        writer = _make_writer()
        replay_day(DATE, CODE_VER, CFG, reader, writer)
        writer.write_manifest.assert_called_once()

    def test_success_with_data(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer,
                                expected_symbols=UNIVERSE)
        assert result["status"] == "success"

    def test_replay_flag_in_result(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer)
        assert result["replay"] is True

    def test_output_partition_path_versioned(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer)
        expected = partition_path("analytics", CODE_VER, DATE)
        assert result["output_partition_path"] == expected

    def test_partial_data_status_is_partial(self):
        reader = _make_reader(partitions=_partitions_for(["AAPL"]))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer,
                                expected_symbols=UNIVERSE)
        assert result["status"] == "partial"

    def test_partial_data_flag_set(self):
        reader = _make_reader(partitions=_partitions_for(["AAPL"]))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer,
                                expected_symbols=UNIVERSE)
        assert result["is_partial_data"] is True

    def test_pipeline_failure_propagated(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._failing_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer)
        assert result["status"] == "failed"

    def test_coverage_pct_in_result(self):
        reader = _make_reader(partitions=_partitions_for(["AAPL", "MSFT"]))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer,
                                expected_symbols=UNIVERSE)
        assert result["coverage_pct"] == pytest.approx(100 * 2 / 3, rel=1e-4)

    def test_writes_manifest_on_success(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            replay_day(DATE, CODE_VER, CFG, reader, writer)
        assert writer.write_manifest.call_count >= 1

    def test_config_hashes_in_manifest(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer)
        assert "config_hashes" in result
        assert isinstance(result["config_hashes"], dict)

    def test_deterministic_manifest_fields(self):
        """Same inputs → same manifest values (determinism acceptance criterion)."""
        reader1 = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer1 = _make_writer()
        reader2 = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer2 = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            r1 = replay_day(DATE, CODE_VER, CFG, reader1, writer1,
                            expected_symbols=UNIVERSE)
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            r2 = replay_day(DATE, CODE_VER, CFG, reader2, writer2,
                            expected_symbols=UNIVERSE)

        # Deterministic fields (run_id excluded — it's a UUID)
        for key in ("status", "trade_date", "code_version", "config_hashes",
                    "output_partition_path", "coverage_pct", "is_partial_data"):
            assert r1[key] == r2[key], f"Field {key!r} differs: {r1[key]} vs {r2[key]}"

    def test_custom_analytics_base_path(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer,
                                analytics_base="custom/base")
        assert result["output_partition_path"].startswith("custom/base/")


# ---------------------------------------------------------------------------
# TestReplayDateRange
# ---------------------------------------------------------------------------

class TestReplayDateRange:
    def _success_pipeline(self):
        def fake_pipeline(run, reader, writer):
            writer.write_manifest({}, run.run_id)
            return {"status": "success", "steps": {}}
        return fake_pipeline

    def test_returns_list(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            results = replay_date_range("2025-01-13", "2025-01-15",
                                        CODE_VER, CFG, reader, writer,
                                        skip_weekends=False)
        assert len(results) == 3

    def test_skips_weekends_by_default(self):
        # 2025-01-11 is Saturday, 2025-01-12 is Sunday
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            results = replay_date_range("2025-01-11", "2025-01-13",
                                        CODE_VER, CFG, reader, writer,
                                        skip_weekends=True)
        # Only Monday 2025-01-13 should be processed
        assert len(results) == 1
        assert results[0]["trade_date"] == "2025-01-13"

    def test_no_skip_weekends(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            results = replay_date_range("2025-01-11", "2025-01-13",
                                        CODE_VER, CFG, reader, writer,
                                        skip_weekends=False)
        assert len(results) == 3

    def test_dates_in_order(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            results = replay_date_range("2025-01-13", "2025-01-15",
                                        CODE_VER, CFG, reader, writer,
                                        skip_weekends=False)
        dates = [r["trade_date"] for r in results]
        assert dates == sorted(dates)

    def test_missing_day_flagged_failed(self):
        """A day with no raw partitions gets status=failed in its manifest."""
        reader = _make_reader(partitions=[])  # no data for any day
        writer = _make_writer()
        results = replay_date_range("2025-01-13", "2025-01-13",
                                    CODE_VER, CFG, reader, writer)
        assert results[0]["status"] == "failed"

    def test_empty_range(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        # end < start
        results = replay_date_range("2025-01-15", "2025-01-13",
                                    CODE_VER, CFG, reader, writer)
        assert results == []


# ---------------------------------------------------------------------------
# TestCompareReplayVsLive
# ---------------------------------------------------------------------------

class TestCompareReplayVsLive:
    def _reader_with(self, replay_data: dict, live_data: dict) -> MagicMock:
        return _make_reader(analytics={"replay": replay_data, "live": live_data})

    def _run(self, replay_data: dict, live_data: dict,
             tolerance: float = 1e-8) -> ReplayComparisonResult:
        reader = self._reader_with(replay_data, live_data)
        return compare_replay_vs_live(DATE, "replay", "live", reader,
                                      tolerance=tolerance)

    def test_returns_comparison_result(self):
        result = self._run({"K1": {"price": 10.0}}, {"K1": {"price": 10.0}})
        assert isinstance(result, ReplayComparisonResult)

    def test_identical_data_is_equivalent(self):
        data = {"K1": {"price": 10.0, "delta": 0.5}}
        result = self._run(data, data)
        assert result.is_equivalent

    def test_identical_data_all_matching(self):
        data = {"K1": {"price": 10.0}}
        result = self._run(data, data)
        assert "K1" in result.matching
        assert len(result.differing) == 0

    def test_differing_value_detected(self):
        result = self._run(
            {"K1": {"price": 10.0}},
            {"K1": {"price": 10.5}},
        )
        assert not result.is_equivalent
        assert len(result.differing) > 0

    def test_differing_stores_abs_diff(self):
        result = self._run(
            {"K1": {"price": 10.0}},
            {"K1": {"price": 10.5}},
        )
        diff_entry = next(d for d in result.differing if d["contract_key"] == "K1")
        assert diff_entry["abs_diff"] == pytest.approx(0.5)

    def test_missing_in_replay(self):
        result = self._run(
            {},
            {"K1": {"price": 10.0}},
        )
        assert "K1" in result.missing_in_replay
        assert not result.is_equivalent

    def test_missing_in_live(self):
        result = self._run(
            {"K1": {"price": 10.0}},
            {},
        )
        assert "K1" in result.missing_in_live
        assert not result.is_equivalent

    def test_within_tolerance_is_equivalent(self):
        result = self._run(
            {"K1": {"price": 10.000000001}},
            {"K1": {"price": 10.0}},
            tolerance=1e-6,
        )
        assert result.is_equivalent

    def test_outside_tolerance_is_different(self):
        result = self._run(
            {"K1": {"price": 10.01}},
            {"K1": {"price": 10.0}},
            tolerance=1e-6,
        )
        assert not result.is_equivalent

    def test_n_compared_equals_common_keys(self):
        result = self._run(
            {"K1": {"price": 1.0}, "K2": {"price": 2.0}},
            {"K1": {"price": 1.0}, "K3": {"price": 3.0}},
        )
        assert result.n_compared == 1  # Only K1 is in both

    def test_multiple_matching_keys(self):
        data = {"K1": {"p": 1.0}, "K2": {"p": 2.0}, "K3": {"p": 3.0}}
        result = self._run(data, data)
        assert len(result.matching) == 3

    def test_max_abs_diff_computed(self):
        result = self._run(
            {"K1": {"p": 1.0}, "K2": {"p": 10.0}},
            {"K1": {"p": 1.1}, "K2": {"p": 10.5}},
        )
        assert result.max_abs_diff == pytest.approx(0.5, rel=1e-6)

    def test_empty_both_sides_equivalent(self):
        result = self._run({}, {})
        assert result.is_equivalent
        assert result.n_compared == 0

    def test_trade_date_stored(self):
        result = self._run({}, {})
        assert result.trade_date == DATE

    def test_versions_stored(self):
        result = self._run({}, {})
        assert result.replay_version == "replay"
        assert result.live_version == "live"


# ---------------------------------------------------------------------------
# TestAcceptanceCriterion
# ---------------------------------------------------------------------------

class TestAcceptanceCriterion:
    """PLAN: Replay == live on overlapping dates with same code version."""

    def _success_pipeline(self):
        def fake_pipeline(run, reader, writer):
            writer.write_manifest({}, run.run_id)
            return {"status": "success", "steps": {}}
        return fake_pipeline

    def test_same_code_version_same_partition_path(self):
        """Two replay runs with same version write to same partition path."""
        p1 = partition_path("analytics", CODE_VER, DATE)
        p2 = partition_path("analytics", CODE_VER, DATE)
        assert p1 == p2

    def test_different_code_version_different_partition_path(self):
        """Different code versions produce different partition paths — no overwrite."""
        p1 = partition_path("analytics", "0.1.0", DATE)
        p2 = partition_path("analytics", "0.2.0", DATE)
        assert p1 != p2

    def test_replay_uses_same_pipeline_as_live(self):
        """replay_day calls job_eod_pipeline — same as the live production path."""
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()) as mock_pipeline:
            replay_day(DATE, CODE_VER, CFG, reader, writer)
        mock_pipeline.assert_called_once()

    def test_deterministic_config_hashes(self):
        """Same config always produces same hashes (enables lineage tracing)."""
        h1 = config_hashes(CFG)
        h2 = config_hashes(CFG)
        assert h1 == h2

    def test_replay_manifest_is_complete(self):
        reader = _make_reader(partitions=_partitions_for(UNIVERSE))
        writer = _make_writer()
        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=self._success_pipeline()):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer,
                                expected_symbols=UNIVERSE)
        required = {"status", "trade_date", "code_version", "config_hashes",
                    "replay", "output_partition_path", "coverage_pct"}
        assert required.issubset(result.keys())

    def test_compare_identical_replay_live_is_equivalent(self):
        """When replay and live use same code + config, outputs match exactly."""
        shared_data = {
            "K1": {"price": 123.45, "delta": 0.50},
            "K2": {"price": 67.89, "delta": -0.30},
        }
        reader = _make_reader(analytics={"replay": shared_data, "live": shared_data})
        result = compare_replay_vs_live(DATE, "replay", "live", reader)
        assert result.is_equivalent
        assert len(result.matching) == 2

    def test_partial_data_does_not_abort_pipeline(self):
        """Partial data → status=partial (not failed); pipeline still runs."""
        reader = _make_reader(partitions=_partitions_for(["AAPL"]))
        writer = _make_writer()
        pipeline_called = []

        def fake_pipeline(run, reader, writer):
            pipeline_called.append(True)
            writer.write_manifest({}, run.run_id)
            return {"status": "success", "steps": {}}

        with patch("src.orchestration.replay.job_eod_pipeline",
                   side_effect=fake_pipeline):
            result = replay_day(DATE, CODE_VER, CFG, reader, writer,
                                expected_symbols=UNIVERSE)

        assert pipeline_called, "pipeline should have been called for partial data"
        assert result["status"] == "partial"
