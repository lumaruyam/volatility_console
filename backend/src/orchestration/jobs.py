"""
Orchestration job entry points.

Jobs are thin wrappers that orchestrate library calls, write manifests, emit metrics.

Rules:
- Dependency ordering: downstream jobs don't run on incomplete upstream data.
- One manifest per job with parameters, versions, outputs.
- Reruns are idempotent: same (run_id, job_name) pair never writes twice.
- All jobs support dry-run mode — no writes, returns {"status": "dry_run"}.
- Structured metrics emitted at job boundaries via MetricsCatalog.
- Correlation chain (session_id → job_id) propagated through all log records.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JobRunContext
# ---------------------------------------------------------------------------

@dataclass
class JobRunContext:
    """Metadata for one job execution, including correlation chain."""
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()).replace("-", "")[:16])
    trade_date: str = ""
    code_version: str = "0.1.0"
    config: dict = field(default_factory=dict)
    dry_run: bool = False
    started_at: float = field(default_factory=time.time)

    @property
    def config_hashes(self) -> dict:
        hashes = {}
        for key, value in self.config.items():
            content = json.dumps(value, sort_keys=True).encode()
            hashes[key] = hashlib.sha256(content).hexdigest()[:8]
        return hashes

    def idempotency_key(self, job_name: str) -> str:
        """Stable key for (run_id, job_name) — used to detect duplicate runs."""
        raw = f"{self.run_id}:{job_name}:{self.trade_date}:{self.code_version}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_manifest_base(self) -> dict:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "trade_date": self.trade_date,
            "code_version": self.code_version,
            "config_hashes": self.config_hashes,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
        }

    @classmethod
    def from_args(cls, args, config: dict) -> "JobRunContext":
        return cls(
            trade_date=getattr(args, "trade_date", ""),
            code_version=getattr(args, "code_version", "0.1.0"),
            config=config,
            dry_run=getattr(args, "dry_run", False),
        )


# ---------------------------------------------------------------------------
# Metrics catalog
# ---------------------------------------------------------------------------

@dataclass
class MetricsCatalog:
    """
    Collects structured metrics emitted by each job step.
    Records are kept in memory per run; the caller writes them to storage.

    Tracked metrics (by category):
      event_rate       raw events per second during collection
      stale_ratio      fraction of stale quotes in a snapshot
      solver_failures  count of IV solver non-convergence events
      scenario_runtime elapsed seconds per scenario run
    """
    records: list[dict] = field(default_factory=list)

    def record(self, metric: str, value: float, labels: dict | None = None,
               timestamp: float | None = None) -> None:
        self.records.append({
            "metric": metric,
            "value": value,
            "labels": labels or {},
            "timestamp": timestamp or time.time(),
        })

    def record_event_rate(self, n_events: int, elapsed_seconds: float,
                          labels: dict | None = None) -> None:
        rate = n_events / elapsed_seconds if elapsed_seconds > 0 else 0.0
        self.record("event_rate", rate, labels)

    def record_stale_ratio(self, n_stale: int, n_total: int,
                           labels: dict | None = None) -> None:
        ratio = n_stale / n_total if n_total > 0 else 0.0
        self.record("stale_ratio", ratio, labels)

    def record_solver_failures(self, n_failures: int,
                                labels: dict | None = None) -> None:
        self.record("solver_failures", float(n_failures), labels)

    def record_scenario_runtime(self, elapsed_seconds: float,
                                 labels: dict | None = None) -> None:
        self.record("scenario_runtime", elapsed_seconds, labels)

    def summary(self) -> dict:
        """Latest value per metric name (for manifest embedding)."""
        seen: dict[str, float] = {}
        for r in self.records:
            seen[r["metric"]] = r["value"]
        return seen


# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------

def check_idempotency(writer, idempotency_key: str) -> bool:
    """
    Return True if this (run_id, job_name) combination was already executed.
    Uses writer.partition_exists(key) when available; False if not supported.
    """
    if hasattr(writer, "partition_exists"):
        return writer.partition_exists(idempotency_key)
    return False


# ---------------------------------------------------------------------------
# Job entry points
# ---------------------------------------------------------------------------

def job_universe_refresh(run: JobRunContext, session, storage_writer,
                          metrics: MetricsCatalog | None = None) -> dict:
    """
    Step 2: Refresh instrument master for the session date.
    Discovers all underlyings and their option chains.
    Writes instrument_master partition.
    """
    logger.info("[universe_refresh] run_id=%s session=%s date=%s",
                run.run_id, run.session_id[:8], run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


def job_live_collect(run: JobRunContext, session, writer, config: dict,
                      metrics: MetricsCatalog | None = None) -> dict:
    """
    Step 3: Start live collection — runs until market close or manual stop.
    Writes raw_market_events partition continuously.
    """
    logger.info("[live_collect] run_id=%s session=%s date=%s",
                run.run_id, run.session_id[:8], run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


def job_incremental_analytics(run: JobRunContext, reader, writer,
                               metrics: MetricsCatalog | None = None) -> dict:
    """
    Run analytics on all raw events received since the last successful run.
    Reads: raw_market_events (since last_run_ts)
    Writes: incremental analytics partition (versioned)

    Idempotent: same run_id + trade_date → same output partition, no double-write.
    """
    logger.info("[incremental_analytics] run_id=%s session=%s date=%s",
                run.run_id, run.session_id[:8], run.trade_date)

    if run.dry_run:
        return {"status": "dry_run"}

    ikey = run.idempotency_key("incremental_analytics")
    if check_idempotency(writer, ikey):
        logger.info("[incremental_analytics] already done — idempotency_key=%s", ikey)
        return {"status": "skipped", "reason": "ALREADY_COMPLETE",
                "idempotency_key": ikey}

    # Real implementation calls downstream steps incrementally.
    raise NotImplementedError


def job_eod_reconciliation(run: JobRunContext, reader, writer,
                            metrics: MetricsCatalog | None = None) -> dict:
    """
    Compare expected analytics outputs vs what was actually written.
    Detects missing partitions, unexpected nulls, row-count anomalies.
    Reads: all analytics partitions for the day
    Writes: reconciliation_report partition
    """
    logger.info("[eod_reconciliation] run_id=%s session=%s date=%s",
                run.run_id, run.session_id[:8], run.trade_date)

    if run.dry_run:
        return {"status": "dry_run"}

    ikey = run.idempotency_key("eod_reconciliation")
    if check_idempotency(writer, ikey):
        logger.info("[eod_reconciliation] already done — idempotency_key=%s", ikey)
        return {"status": "skipped", "reason": "ALREADY_COMPLETE",
                "idempotency_key": ikey}

    raise NotImplementedError


def job_replay(run: JobRunContext, reader, writer,
               expected_symbols: list | None = None,
               metrics: MetricsCatalog | None = None) -> dict:
    """
    Replay full analytics pipeline for run.trade_date from stored raw data.
    Calls replay_day — same library function as used by live processing.
    """
    from src.orchestration.replay import replay_day

    logger.info("[replay] run_id=%s session=%s date=%s",
                run.run_id, run.session_id[:8], run.trade_date)

    if run.dry_run:
        return {"status": "dry_run"}

    ikey = run.idempotency_key("replay")
    if check_idempotency(writer, ikey):
        logger.info("[replay] already done — idempotency_key=%s", ikey)
        return {"status": "skipped", "reason": "ALREADY_COMPLETE",
                "idempotency_key": ikey}

    t0 = time.time()
    result = replay_day(
        trade_date=run.trade_date,
        code_version=run.code_version,
        config=run.config,
        reader=reader,
        writer=writer,
        expected_symbols=expected_symbols,
    )
    elapsed = time.time() - t0
    if metrics:
        metrics.record("replay_runtime", elapsed, {"date": run.trade_date})

    return result


def job_qc_run(run: JobRunContext, reader, writer, underlyings: list[str],
               expected_scenarios: list[str] | None = None,
               metrics: MetricsCatalog | None = None) -> dict:
    """
    Run full daily QC suite for all underlyings.
    Reads: all analytics partitions for run.trade_date
    Writes: qc_report partition
    """
    from src.qc.validation import build_triage_table, run_daily_qc

    logger.info("[qc_run] run_id=%s session=%s date=%s n_underlyings=%d",
                run.run_id, run.session_id[:8], run.trade_date, len(underlyings))

    if run.dry_run:
        return {"status": "dry_run"}

    ikey = run.idempotency_key("qc_run")
    if check_idempotency(writer, ikey):
        logger.info("[qc_run] already done — idempotency_key=%s", ikey)
        return {"status": "skipped", "reason": "ALREADY_COMPLETE",
                "idempotency_key": ikey}

    config = run.config.get("qc", {})
    reports = []
    for und in underlyings:
        all_data = reader.read_analytics_all(run.trade_date, und)
        report = run_daily_qc(
            trade_date=run.trade_date,
            underlying=und,
            run_id=run.run_id,
            all_data=all_data,
            config=config,
            expected_scenarios=expected_scenarios,
        )
        reports.append(report)

    triage = build_triage_table(reports)
    n_fail = sum(1 for t in triage if t["status"] == "fail")
    n_warn = sum(1 for t in triage if t["status"] == "warn")

    if metrics:
        metrics.record("qc_failures", float(n_fail), {"date": run.trade_date})
        metrics.record("qc_warnings", float(n_warn), {"date": run.trade_date})

    result = {
        "status": "ok",
        "n_underlyings": len(underlyings),
        "n_failures": n_fail,
        "n_warnings": n_warn,
        "triage_rows": len(triage),
    }
    writer.write_manifest(result, f"qc_{run.run_id}")
    return result


# ---------------------------------------------------------------------------
# Existing step jobs (unchanged logic, now accept metrics kwarg)
# ---------------------------------------------------------------------------

def job_build_snapshots(run: JobRunContext, reader, writer,
                         metrics: MetricsCatalog | None = None) -> dict:
    logger.info("[build_snapshots] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


def job_build_forwards(run: JobRunContext, reader, writer,
                        metrics: MetricsCatalog | None = None) -> dict:
    logger.info("[build_forwards] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


def job_solve_iv(run: JobRunContext, reader, writer,
                  metrics: MetricsCatalog | None = None) -> dict:
    logger.info("[solve_iv] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


def job_fit_surfaces(run: JobRunContext, reader, writer,
                      metrics: MetricsCatalog | None = None) -> dict:
    logger.info("[fit_surfaces] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


def job_compute_greeks(run: JobRunContext, reader, writer,
                        metrics: MetricsCatalog | None = None) -> dict:
    logger.info("[compute_greeks] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


def job_risk_aggregation(run: JobRunContext, reader, writer,
                          metrics: MetricsCatalog | None = None) -> dict:
    logger.info("[risk_aggregation] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


def job_run_scenarios(run: JobRunContext, reader, writer,
                       metrics: MetricsCatalog | None = None) -> dict:
    logger.info("[run_scenarios] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


def job_run_qc(run: JobRunContext, reader, writer,
                metrics: MetricsCatalog | None = None) -> dict:
    logger.info("[run_qc] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    raise NotImplementedError


# ---------------------------------------------------------------------------
# EOD pipeline
# ---------------------------------------------------------------------------

def job_eod_pipeline(run: JobRunContext, reader, writer,
                      metrics: MetricsCatalog | None = None) -> dict:
    """
    Full end-of-day pipeline in dependency order (Steps 5-14).
    Calls jobs sequentially; stops on critical failure.
    Writes one master manifest.
    """
    logger.info("[eod_pipeline] run_id=%s session=%s date=%s",
                run.run_id, run.session_id[:8], run.trade_date)

    steps = [
        ("build_snapshots",  job_build_snapshots),
        ("build_forwards",   job_build_forwards),
        ("solve_iv",         job_solve_iv),
        ("fit_surfaces",     job_fit_surfaces),
        ("compute_greeks",   job_compute_greeks),
        ("risk_aggregation", job_risk_aggregation),
        ("run_scenarios",    job_run_scenarios),
        ("run_qc",           job_run_qc),
    ]

    results: dict[str, dict] = {}
    for step_name, step_fn in steps:
        t0 = time.time()
        try:
            result = step_fn(run, reader, writer, metrics)
            elapsed = time.time() - t0
            results[step_name] = {**result, "status": "ok", "elapsed": elapsed}
            if metrics:
                metrics.record(f"step_{step_name}_elapsed", elapsed,
                               {"date": run.trade_date})
        except Exception as exc:
            elapsed = time.time() - t0
            logger.error("[eod_pipeline] step=%s FAILED elapsed=%.2fs error=%s",
                         step_name, elapsed, exc)
            results[step_name] = {"status": "failed", "elapsed": elapsed,
                                   "error": str(exc)}
            break

    manifest = run.to_manifest_base()
    manifest["steps"] = results
    manifest["status"] = (
        "success"
        if all(v.get("status") == "ok" for v in results.values())
        else "failed"
    )
    if metrics:
        manifest["metrics_summary"] = metrics.summary()

    writer.write_manifest(manifest, run.run_id)
    return manifest
