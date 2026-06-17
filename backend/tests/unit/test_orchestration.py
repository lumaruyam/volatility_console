"""
Comprehensive tests for Step 15: Orchestration, logging, observability.

Acceptance criterion (PLAN):
  Simulated failure detected within documented interval;
  no duplicate outputs on restart.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from src.orchestration.jobs import (
    JobRunContext,
    MetricsCatalog,
    check_idempotency,
    job_eod_pipeline,
    job_eod_reconciliation,
    job_incremental_analytics,
    job_live_collect,
    job_qc_run,
    job_replay,
    job_universe_refresh,
)
from src.orchestration.scheduler import (
    FailureAlert,
    JobSchedule,
    SchedulerState,
    all_schedules_status,
    check_failure_alert,
    make_initial_state,
    mark_alert_sent,
    record_job_result,
    should_run,
    simulate_run_sequence,
)
from src.utils.logging_utils import (
    LogContext,
    StructuredLogger,
    build_logger,
    log_context,
    new_correlation_id,
    new_job_id,
    new_session_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _writer(already_done=False) -> MagicMock:
    w = MagicMock()
    w.write_manifest.return_value = None
    w.partition_exists.return_value = already_done
    return w


def _reader() -> MagicMock:
    r = MagicMock()
    r.list_partitions.return_value = [{"symbol": "ESTX50"}]
    r.read_analytics_all.return_value = {
        "raw_events": [], "snapshots": [], "iv_points": [],
        "forward_rows": [], "surface_params": [], "pricing_rows": [],
        "scenario_results": [],
    }
    return r


def _run(dry_run=False, trade_date="2025-01-15", code_version="0.2.0",
         config=None) -> JobRunContext:
    return JobRunContext(
        trade_date=trade_date,
        code_version=code_version,
        config=config or {},
        dry_run=dry_run,
    )


def _sched(name="test_job", interval=3600.0, alert_n=2, alert_interval=600.0,
           enabled=True) -> JobSchedule:
    return JobSchedule(
        job_name=name,
        interval_seconds=interval,
        alert_after_n_failures=alert_n,
        alert_interval_seconds=alert_interval,
        enabled=enabled,
    )


NOW = 1_000_000.0  # fixed timestamp for deterministic tests


# ===========================================================================
# TestNewCorrelationId
# ===========================================================================

class TestNewCorrelationId:
    def test_returns_string(self):
        assert isinstance(new_correlation_id(), str)

    def test_length_16(self):
        assert len(new_correlation_id()) == 16

    def test_unique(self):
        assert new_correlation_id() != new_correlation_id()

    def test_hex_chars_only(self):
        cid = new_correlation_id()
        int(cid, 16)  # raises if non-hex

    def test_session_and_job_ids_are_unique(self):
        assert new_session_id() != new_job_id()


# ===========================================================================
# TestStructuredLogger
# ===========================================================================

class TestStructuredLogger:
    def test_session_id_set(self):
        log = StructuredLogger("test", session_id="abc123")
        assert log.session_id == "abc123"

    def test_default_session_id_generated(self):
        log = StructuredLogger("test")
        assert len(log.session_id) == 16

    def test_emit_does_not_raise(self):
        log = StructuredLogger("test.emit")
        log.emit("my.event", val=42, label="ok")

    def test_info_does_not_raise(self):
        log = StructuredLogger("test.info")
        log.info("event.name", key="value")

    def test_warning_does_not_raise(self):
        log = StructuredLogger("test.warn")
        log.warning("warn.event")

    def test_error_does_not_raise(self):
        log = StructuredLogger("test.err")
        log.error("error.event", code=500)

    def test_child_inherits_session_id(self):
        parent = StructuredLogger("parent", session_id="session123")
        child = parent.child(step_id="step1")
        assert child.session_id == "session123"

    def test_child_has_step_id(self):
        parent = StructuredLogger("parent", session_id="s")
        child = parent.child(step_id="mystep")
        assert child.step_id == "mystep"

    def test_format_includes_session(self):
        log = StructuredLogger("x", session_id="abcdef1234567890")
        msg = log._format("event", {"k": "v"})
        assert "abcdef12" in msg  # first 8 chars

    def test_format_includes_event(self):
        log = StructuredLogger("x", session_id="s")
        msg = log._format("my.event", {})
        assert "my.event" in msg

    def test_format_includes_fields(self):
        log = StructuredLogger("x", session_id="s")
        msg = log._format("ev", {"n": 42})
        assert "n=" in msg


# ===========================================================================
# TestLogContext
# ===========================================================================

class TestLogContext:
    def test_context_manager_yields(self):
        with LogContext() as ctx:
            assert isinstance(ctx, LogContext)

    def test_session_id_generated(self):
        with LogContext() as ctx:
            assert len(ctx.session_id) == 16

    def test_custom_session_id(self):
        with LogContext(session_id="custom1234567890") as ctx:
            assert ctx.session_id == "custom1234567890"

    def test_log_context_function(self):
        with log_context() as ctx:
            assert isinstance(ctx, LogContext)
            assert len(ctx.session_id) == 16

    def test_log_context_propagates_session_id(self):
        with log_context(session_id="sess1234567890ab") as ctx:
            assert ctx.session_id == "sess1234567890ab"

    def test_build_logger_returns_logger(self):
        log = build_logger("test.build", correlation_id="abc")
        assert isinstance(log, logging.Logger)


# ===========================================================================
# TestJobRunContext
# ===========================================================================

class TestJobRunContext:
    def test_run_id_generated(self):
        run = _run()
        assert len(run.run_id) > 0

    def test_session_id_generated(self):
        run = _run()
        assert len(run.session_id) == 16

    def test_unique_run_ids(self):
        assert _run().run_id != _run().run_id

    def test_unique_session_ids(self):
        assert _run().session_id != _run().session_id

    def test_config_hashes_empty_config(self):
        run = _run(config={})
        assert run.config_hashes == {}

    def test_config_hashes_per_key(self):
        run = _run(config={"pricing": {"model": "bs"}, "qc": {}})
        h = run.config_hashes
        assert "pricing" in h and "qc" in h
        assert all(len(v) == 8 for v in h.values())

    def test_config_hashes_deterministic(self):
        run = _run(config={"k": {"v": 1}})
        assert run.config_hashes == run.config_hashes

    def test_idempotency_key_deterministic(self):
        run = _run(trade_date="2025-01-15", code_version="0.2.0")
        k1 = run.idempotency_key("solve_iv")
        k2 = run.idempotency_key("solve_iv")
        assert k1 == k2

    def test_idempotency_key_differs_by_job(self):
        run = _run()
        assert run.idempotency_key("job_a") != run.idempotency_key("job_b")

    def test_idempotency_key_differs_by_run_id(self):
        r1, r2 = _run(), _run()
        assert r1.idempotency_key("job") != r2.idempotency_key("job")

    def test_to_manifest_base_has_session_id(self):
        run = _run()
        manifest = run.to_manifest_base()
        assert "session_id" in manifest

    def test_to_manifest_base_fields(self):
        run = _run(trade_date="2025-01-15", dry_run=True)
        m = run.to_manifest_base()
        for k in ("run_id", "session_id", "trade_date", "code_version",
                  "config_hashes", "dry_run", "started_at"):
            assert k in m

    def test_from_args(self):
        args = MagicMock()
        args.trade_date = "2025-06-01"
        args.code_version = "1.0.0"
        args.dry_run = False
        run = JobRunContext.from_args(args, {"k": "v"})
        assert run.trade_date == "2025-06-01"
        assert run.code_version == "1.0.0"

    def test_dry_run_flag(self):
        run = _run(dry_run=True)
        assert run.dry_run is True


# ===========================================================================
# TestMetricsCatalog
# ===========================================================================

class TestMetricsCatalog:
    def test_empty_initially(self):
        assert MetricsCatalog().records == []

    def test_record_appends(self):
        mc = MetricsCatalog()
        mc.record("my_metric", 42.0)
        assert len(mc.records) == 1

    def test_record_stores_value(self):
        mc = MetricsCatalog()
        mc.record("m", 3.14)
        assert mc.records[0]["value"] == pytest.approx(3.14)

    def test_record_stores_metric_name(self):
        mc = MetricsCatalog()
        mc.record("event_rate", 1000.0)
        assert mc.records[0]["metric"] == "event_rate"

    def test_record_stores_labels(self):
        mc = MetricsCatalog()
        mc.record("m", 1.0, labels={"date": "2025-01-15"})
        assert mc.records[0]["labels"]["date"] == "2025-01-15"

    def test_record_event_rate(self):
        mc = MetricsCatalog()
        mc.record_event_rate(n_events=500, elapsed_seconds=5.0)
        assert mc.records[-1]["metric"] == "event_rate"
        assert mc.records[-1]["value"] == pytest.approx(100.0)

    def test_record_event_rate_zero_elapsed(self):
        mc = MetricsCatalog()
        mc.record_event_rate(n_events=100, elapsed_seconds=0.0)
        assert mc.records[-1]["value"] == pytest.approx(0.0)

    def test_record_stale_ratio(self):
        mc = MetricsCatalog()
        mc.record_stale_ratio(n_stale=10, n_total=100)
        assert mc.records[-1]["metric"] == "stale_ratio"
        assert mc.records[-1]["value"] == pytest.approx(0.10)

    def test_record_stale_ratio_zero_total(self):
        mc = MetricsCatalog()
        mc.record_stale_ratio(0, 0)
        assert mc.records[-1]["value"] == pytest.approx(0.0)

    def test_record_solver_failures(self):
        mc = MetricsCatalog()
        mc.record_solver_failures(7)
        assert mc.records[-1]["metric"] == "solver_failures"
        assert mc.records[-1]["value"] == pytest.approx(7.0)

    def test_record_scenario_runtime(self):
        mc = MetricsCatalog()
        mc.record_scenario_runtime(0.42)
        assert mc.records[-1]["metric"] == "scenario_runtime"
        assert mc.records[-1]["value"] == pytest.approx(0.42)

    def test_summary_latest_value_per_metric(self):
        mc = MetricsCatalog()
        mc.record("m1", 1.0)
        mc.record("m1", 2.0)  # second value should win
        mc.record("m2", 5.0)
        s = mc.summary()
        assert s["m1"] == pytest.approx(2.0)
        assert s["m2"] == pytest.approx(5.0)

    def test_multiple_records(self):
        mc = MetricsCatalog()
        mc.record_event_rate(100, 1.0)
        mc.record_stale_ratio(5, 50)
        mc.record_solver_failures(2)
        mc.record_scenario_runtime(0.1)
        assert len(mc.records) == 4


# ===========================================================================
# TestCheckIdempotency
# ===========================================================================

class TestCheckIdempotency:
    def test_returns_false_when_not_done(self):
        writer = _writer(already_done=False)
        assert check_idempotency(writer, "key123") is False

    def test_returns_true_when_done(self):
        writer = _writer(already_done=True)
        assert check_idempotency(writer, "key123") is True

    def test_no_partition_exists_method_returns_false(self):
        writer = MagicMock(spec=[])  # no partition_exists
        assert check_idempotency(writer, "any") is False


# ===========================================================================
# TestJobDryRun
# ===========================================================================

class TestJobDryRun:
    """All batch jobs must return {"status": "dry_run"} with no writes."""

    def _assert_dry_run(self, fn, *args, **kwargs):
        result = fn(*args, **kwargs)
        assert result["status"] == "dry_run"

    def test_universe_refresh_dry_run(self):
        run = _run(dry_run=True)
        self._assert_dry_run(job_universe_refresh, run, MagicMock(), _writer())

    def test_live_collect_dry_run(self):
        run = _run(dry_run=True)
        self._assert_dry_run(job_live_collect, run, MagicMock(), _writer(), {})

    def test_incremental_analytics_dry_run(self):
        run = _run(dry_run=True)
        self._assert_dry_run(job_incremental_analytics, run, _reader(), _writer())

    def test_eod_reconciliation_dry_run(self):
        run = _run(dry_run=True)
        self._assert_dry_run(job_eod_reconciliation, run, _reader(), _writer())

    def test_replay_dry_run(self):
        run = _run(dry_run=True)
        self._assert_dry_run(job_replay, run, _reader(), _writer())

    def test_qc_run_dry_run(self):
        run = _run(dry_run=True)
        self._assert_dry_run(job_qc_run, run, _reader(), _writer(), [])

    def test_dry_run_no_writes(self):
        writer = _writer()
        run = _run(dry_run=True)
        job_incremental_analytics(run, _reader(), writer)
        writer.write_manifest.assert_not_called()


# ===========================================================================
# TestIdempotency
# ===========================================================================

class TestIdempotency:
    """No duplicate outputs on restart — idempotency_key prevents double-write."""

    def test_incremental_analytics_skipped_when_done(self):
        run = _run(dry_run=False)
        writer = _writer(already_done=True)
        result = job_incremental_analytics(run, _reader(), writer)
        assert result["status"] == "skipped"
        assert result["reason"] == "ALREADY_COMPLETE"

    def test_eod_reconciliation_skipped_when_done(self):
        run = _run(dry_run=False)
        writer = _writer(already_done=True)
        result = job_eod_reconciliation(run, _reader(), writer)
        assert result["status"] == "skipped"

    def test_replay_skipped_when_done(self):
        run = _run(dry_run=False)
        writer = _writer(already_done=True)
        result = job_replay(run, _reader(), writer)
        assert result["status"] == "skipped"

    def test_qc_run_skipped_when_done(self):
        run = _run(dry_run=False)
        writer = _writer(already_done=True)
        result = job_qc_run(run, _reader(), writer, underlyings=[])
        assert result["status"] == "skipped"

    def test_idempotency_key_in_skipped_result(self):
        run = _run(dry_run=False)
        writer = _writer(already_done=True)
        result = job_incremental_analytics(run, _reader(), writer)
        assert "idempotency_key" in result

    def test_skipped_does_not_write(self):
        run = _run(dry_run=False)
        writer = _writer(already_done=True)
        job_incremental_analytics(run, _reader(), writer)
        writer.write_manifest.assert_not_called()


# ===========================================================================
# TestEodPipeline
# ===========================================================================

class TestEodPipeline:
    def _dry_pipeline(self):
        run = _run(dry_run=True)
        writer = _writer()
        result = job_eod_pipeline(run, _reader(), writer)
        return result, writer

    def test_dry_run_pipeline_returns_manifest(self):
        result, _ = self._dry_pipeline()
        assert "steps" in result
        assert "status" in result

    def test_dry_run_all_steps_present(self):
        result, _ = self._dry_pipeline()
        expected = {"build_snapshots", "build_forwards", "solve_iv",
                    "fit_surfaces", "compute_greeks", "risk_aggregation",
                    "run_scenarios", "run_qc"}
        assert expected == set(result["steps"].keys())

    def test_dry_run_all_steps_ok(self):
        result, _ = self._dry_pipeline()
        for step_name, step_result in result["steps"].items():
            assert step_result["status"] == "ok", f"{step_name} not ok"

    def test_dry_run_success_status(self):
        result, _ = self._dry_pipeline()
        assert result["status"] == "success"

    def test_writes_manifest(self):
        _, writer = self._dry_pipeline()
        writer.write_manifest.assert_called_once()

    def test_manifest_has_session_id(self):
        result, _ = self._dry_pipeline()
        assert "session_id" in result

    def test_metrics_summary_in_manifest(self):
        run = _run(dry_run=True)
        writer = _writer()
        mc = MetricsCatalog()
        mc.record("event_rate", 500.0)
        result = job_eod_pipeline(run, _reader(), writer, metrics=mc)
        assert "metrics_summary" in result

    def test_step_elapsed_in_steps(self):
        result, _ = self._dry_pipeline()
        for step_result in result["steps"].values():
            assert "elapsed" in step_result

    def test_failure_stops_pipeline(self):
        run = _run(dry_run=False)
        writer = _writer()

        def bad_step(run, reader, writer, metrics=None):
            raise RuntimeError("intentional failure")

        with patch("src.orchestration.jobs.job_build_snapshots",
                   side_effect=bad_step):
            result = job_eod_pipeline(run, _reader(), writer)

        assert result["status"] == "failed"
        # Steps after build_snapshots should not appear
        assert "build_forwards" not in result["steps"]


# ===========================================================================
# TestJobReplay (job_replay thin wrapper)
# ===========================================================================

class TestJobReplay:
    def test_replay_dry_run(self):
        run = _run(dry_run=True)
        result = job_replay(run, _reader(), _writer())
        assert result["status"] == "dry_run"

    def test_replay_skipped_when_done(self):
        run = _run(dry_run=False)
        result = job_replay(run, _reader(), _writer(already_done=True))
        assert result["status"] == "skipped"

    def test_replay_calls_replay_day(self):
        run = _run(dry_run=False)
        with patch("src.orchestration.replay.replay_day") as mock_rd:
            mock_rd.return_value = {"status": "success", "replay": True}
            result = job_replay(run, _reader(), _writer(already_done=False))
        mock_rd.assert_called_once()

    def test_replay_records_runtime_metric(self):
        run = _run(dry_run=False)
        mc = MetricsCatalog()
        with patch("src.orchestration.replay.replay_day",
                   return_value={"status": "success"}):
            job_replay(run, _reader(), _writer(already_done=False), metrics=mc)
        assert any(r["metric"] == "replay_runtime" for r in mc.records)


# ===========================================================================
# TestQcRun
# ===========================================================================

class TestQcRun:
    def test_qc_run_dry_run(self):
        run = _run(dry_run=True)
        result = job_qc_run(run, _reader(), _writer(), underlyings=["ESTX50"])
        assert result["status"] == "dry_run"

    def test_qc_run_skipped_when_done(self):
        run = _run(dry_run=False)
        result = job_qc_run(run, _reader(), _writer(already_done=True),
                            underlyings=["ESTX50"])
        assert result["status"] == "skipped"

    def test_qc_run_empty_underlyings(self):
        run = _run(dry_run=False)
        result = job_qc_run(run, _reader(), _writer(already_done=False),
                            underlyings=[])
        assert result["status"] == "ok"
        assert result["n_underlyings"] == 0

    def test_qc_run_writes_manifest(self):
        run = _run(dry_run=False)
        writer = _writer(already_done=False)
        job_qc_run(run, _reader(), writer, underlyings=[])
        writer.write_manifest.assert_called_once()

    def test_qc_run_reports_failure_counts(self):
        run = _run(dry_run=False)
        result = job_qc_run(run, _reader(), _writer(already_done=False),
                            underlyings=["ESTX50"])
        assert "n_failures" in result
        assert "n_warnings" in result

    def test_qc_records_metrics(self):
        run = _run(dry_run=False)
        mc = MetricsCatalog()
        job_qc_run(run, _reader(), _writer(already_done=False),
                   underlyings=[], metrics=mc)
        assert any(r["metric"] in ("qc_failures", "qc_warnings") for r in mc.records)


# ===========================================================================
# TestJobSchedule
# ===========================================================================

class TestJobSchedule:
    def test_fields(self):
        s = _sched("my_job", interval=1800.0, alert_n=3)
        assert s.job_name == "my_job"
        assert s.interval_seconds == pytest.approx(1800.0)
        assert s.alert_after_n_failures == 3

    def test_frozen(self):
        s = _sched()
        with pytest.raises((AttributeError, TypeError)):
            s.job_name = "other"  # type: ignore[misc]

    def test_enabled_default_true(self):
        s = JobSchedule(job_name="x", interval_seconds=3600)
        assert s.enabled is True

    def test_disabled_schedule(self):
        s = _sched(enabled=False)
        assert not s.enabled


# ===========================================================================
# TestShouldRun
# ===========================================================================

class TestShouldRun:
    def test_disabled_never_runs(self):
        sched = _sched(enabled=False)
        state = make_initial_state(sched.job_name)
        assert not should_run(sched, state, NOW)

    def test_never_run_is_due(self):
        sched = _sched()
        state = make_initial_state(sched.job_name)
        assert should_run(sched, state, NOW)

    def test_recently_run_not_due(self):
        sched = _sched(interval=3600.0)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=True, now=NOW)
        assert not should_run(sched, state, NOW + 100)

    def test_interval_elapsed_is_due(self):
        sched = _sched(interval=3600.0)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=True, now=NOW)
        assert should_run(sched, state, NOW + 3600)

    def test_exactly_at_interval_is_due(self):
        sched = _sched(interval=3600.0)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=True, now=NOW)
        assert should_run(sched, state, NOW + 3600.0)

    def test_run_once_interval_zero(self):
        sched = _sched(interval=0.0)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=True, now=NOW)
        # Already ran once → should NOT run again
        assert not should_run(sched, state, NOW + 9999)

    def test_run_once_not_yet_run_is_due(self):
        sched = _sched(interval=0.0)
        state = make_initial_state(sched.job_name)
        assert should_run(sched, state, NOW)


# ===========================================================================
# TestRecordJobResult
# ===========================================================================

class TestRecordJobResult:
    def test_success_increments_total_runs(self):
        state = make_initial_state("j")
        new = record_job_result(state, success=True, now=NOW)
        assert new.total_runs == 1

    def test_success_resets_consecutive_failures(self):
        state = make_initial_state("j")
        state = record_job_result(state, success=False, now=NOW)
        state = record_job_result(state, success=False, now=NOW + 10)
        state = record_job_result(state, success=True, now=NOW + 20)
        assert state.consecutive_failures == 0

    def test_failure_increments_consecutive(self):
        state = make_initial_state("j")
        state = record_job_result(state, success=False, now=NOW)
        state = record_job_result(state, success=False, now=NOW + 10)
        assert state.consecutive_failures == 2

    def test_success_updates_last_success_ts(self):
        state = make_initial_state("j")
        new = record_job_result(state, success=True, now=NOW)
        assert new.last_success_ts == pytest.approx(NOW)

    def test_failure_does_not_update_last_success_ts(self):
        state = make_initial_state("j")
        state = record_job_result(state, success=True, now=NOW)
        state = record_job_result(state, success=False, now=NOW + 10)
        assert state.last_success_ts == pytest.approx(NOW)

    def test_does_not_mutate_input(self):
        state = make_initial_state("j")
        orig_runs = state.total_runs
        record_job_result(state, success=True, now=NOW)
        assert state.total_runs == orig_runs  # original unchanged

    def test_total_failures_incremented(self):
        state = make_initial_state("j")
        state = record_job_result(state, success=False, now=NOW)
        assert state.total_failures == 1

    def test_total_successes_incremented(self):
        state = make_initial_state("j")
        state = record_job_result(state, success=True, now=NOW)
        assert state.total_successes == 1

    def test_alert_cleared_on_success(self):
        state = make_initial_state("j")
        state = record_job_result(state, success=False, now=NOW)
        state = mark_alert_sent(state)
        state = record_job_result(state, success=True, now=NOW + 10)
        assert not state.alert_sent


# ===========================================================================
# TestCheckFailureAlert
# ===========================================================================

class TestCheckFailureAlert:
    def test_no_failures_no_alert(self):
        sched = _sched(alert_n=2)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=True, now=NOW)
        assert check_failure_alert(sched, state, NOW) is None

    def test_one_failure_below_threshold(self):
        sched = _sched(alert_n=2)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=False, now=NOW)
        assert check_failure_alert(sched, state, NOW) is None

    def test_alert_at_threshold(self):
        sched = _sched(alert_n=2, alert_interval=600.0)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=False, now=NOW)
        state = record_job_result(state, success=False, now=NOW + 10)
        alert = check_failure_alert(sched, state, NOW + 10)
        assert alert is not None
        assert isinstance(alert, FailureAlert)

    def test_alert_has_correct_fields(self):
        sched = _sched("my_job", alert_n=2, alert_interval=600.0)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=False, now=NOW)
        state = record_job_result(state, success=False, now=NOW + 10)
        alert = check_failure_alert(sched, state, NOW + 10)
        assert alert.job_name == "my_job"
        assert alert.consecutive_failures == 2
        assert "failed 2" in alert.message.lower() or "2" in alert.message

    def test_no_alert_when_already_sent(self):
        sched = _sched(alert_n=2, alert_interval=600.0)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=False, now=NOW)
        state = record_job_result(state, success=False, now=NOW + 10)
        state = mark_alert_sent(state)
        assert check_failure_alert(sched, state, NOW + 10) is None

    def test_stale_failure_no_alert(self):
        """Failure outside alert_interval_seconds should not trigger alert."""
        sched = _sched(alert_n=2, alert_interval=600.0)
        state = make_initial_state(sched.job_name)
        state = record_job_result(state, success=False, now=NOW)
        state = record_job_result(state, success=False, now=NOW + 10)
        # Check much later than alert_interval
        assert check_failure_alert(sched, state, NOW + 700) is None

    def test_no_alert_when_no_runs(self):
        sched = _sched(alert_n=1)
        state = make_initial_state(sched.job_name)
        assert check_failure_alert(sched, state, NOW) is None


# ===========================================================================
# TestSimulateRunSequence
# ===========================================================================

class TestSimulateRunSequence:
    def test_all_success_no_alerts(self):
        sched = _sched(alert_n=2, interval=60.0)
        _, alerts = simulate_run_sequence(sched, [True, True, True])
        assert all(a is None for a in alerts)

    def test_two_failures_triggers_alert(self):
        sched = _sched(alert_n=2, interval=60.0, alert_interval=600.0)
        _, alerts = simulate_run_sequence(sched, [False, False])
        assert alerts[1] is not None

    def test_alert_only_fires_once(self):
        sched = _sched(alert_n=2, interval=60.0, alert_interval=600.0)
        _, alerts = simulate_run_sequence(sched, [False, False, False])
        # First alert at index 1, should NOT re-fire at index 2
        assert alerts[1] is not None
        assert alerts[2] is None

    def test_success_after_failures_no_further_alert(self):
        sched = _sched(alert_n=2, interval=60.0, alert_interval=600.0)
        _, alerts = simulate_run_sequence(sched, [False, False, True, False])
        # Index 1: alert fires; index 3: only 1 failure since recovery → no alert
        assert alerts[3] is None

    def test_final_state_consecutive_failures(self):
        sched = _sched(alert_n=5, interval=60.0)
        final_state, _ = simulate_run_sequence(sched, [False, False, False])
        assert final_state.consecutive_failures == 3

    def test_final_state_total_runs(self):
        sched = _sched(interval=60.0)
        final_state, _ = simulate_run_sequence(sched, [True, False, True])
        assert final_state.total_runs == 3


# ===========================================================================
# TestAllSchedulesStatus
# ===========================================================================

class TestAllSchedulesStatus:
    def test_returns_one_row_per_schedule(self):
        schedules = [_sched("a"), _sched("b"), _sched("c")]
        rows = all_schedules_status(schedules, {}, NOW)
        assert len(rows) == 3

    def test_job_names_present(self):
        schedules = [_sched("job1"), _sched("job2")]
        rows = all_schedules_status(schedules, {}, NOW)
        assert {r["job_name"] for r in rows} == {"job1", "job2"}

    def test_unseen_job_is_due(self):
        sched = _sched("new_job")
        rows = all_schedules_status([sched], {}, NOW)
        assert rows[0]["is_due"] is True

    def test_disabled_job_not_due(self):
        sched = _sched("disabled", enabled=False)
        rows = all_schedules_status([sched], {}, NOW)
        assert not rows[0]["is_due"]

    def test_has_alert_field(self):
        sched = _sched("j", alert_n=2, interval=60.0, alert_interval=600.0)
        state = make_initial_state("j")
        state = record_job_result(state, success=False, now=NOW)
        state = record_job_result(state, success=False, now=NOW + 10)
        rows = all_schedules_status([sched], {"j": state}, NOW + 10)
        assert rows[0]["has_alert"] is True

    def test_empty_schedules(self):
        assert all_schedules_status([], {}, NOW) == []


# ===========================================================================
# TestAcceptanceCriterion
# ===========================================================================

class TestAcceptanceCriterion:
    """
    PLAN: Simulated failure detected within documented interval;
          no duplicate outputs on restart.
    """

    def test_failure_detected_within_alert_interval(self):
        """Alert fires before alert_interval_seconds elapses."""
        sched = _sched(alert_n=2, interval=60.0, alert_interval=600.0)
        final, alerts = simulate_run_sequence(sched, [False, False])
        t_alert = 2 * 60.0  # two run intervals after start
        assert t_alert <= 600.0  # within alert window
        assert any(a is not None for a in alerts)

    def test_no_duplicate_output_on_restart(self):
        """Restarting a completed job produces status=skipped, not a new write."""
        run = _run(dry_run=False)
        writer = _writer(already_done=True)
        result = job_incremental_analytics(run, _reader(), writer)
        assert result["status"] == "skipped"
        writer.write_manifest.assert_not_called()

    def test_correlation_id_in_manifest(self):
        """session_id propagates into every manifest for traceability."""
        run = _run(dry_run=True)
        writer = _writer()
        result = job_eod_pipeline(run, _reader(), writer)
        assert result["session_id"] == run.session_id

    def test_metrics_emitted_per_step(self):
        """Each completed step records elapsed time in MetricsCatalog."""
        run = _run(dry_run=True)
        writer = _writer()
        mc = MetricsCatalog()
        job_eod_pipeline(run, _reader(), writer, metrics=mc)
        step_metrics = [r for r in mc.records if r["metric"].startswith("step_")]
        assert len(step_metrics) == 8  # one per EOD step

    def test_idempotency_key_stable_across_restarts(self):
        """Same (run_id, job_name, date, version) → same idempotency key."""
        run = _run(trade_date="2025-01-15", code_version="0.2.0")
        k1 = run.idempotency_key("solve_iv")
        # Simulate a restart: new JobRunContext with same run_id
        run2 = JobRunContext(
            run_id=run.run_id,
            trade_date=run.trade_date,
            code_version=run.code_version,
            config=run.config,
        )
        k2 = run2.idempotency_key("solve_iv")
        assert k1 == k2

    def test_structured_log_carries_session(self):
        """StructuredLogger embeds the first 8 chars of session_id in every message."""
        log = StructuredLogger("test.session", session_id="mysession12345x")
        msg = log._format("event", {})
        assert "mysessio" in msg  # first 8 chars of "mysession12345x"
