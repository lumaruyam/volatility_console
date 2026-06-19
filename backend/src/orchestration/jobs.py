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
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime as _dt
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
# Internal snapshot serialization helpers
# ---------------------------------------------------------------------------

def _snapshot_to_rows(snapshot, session_id: str) -> list[dict]:
    """Flatten a MarketStateSnapshot into MarketStateSnapshotRow dicts."""
    rows = []
    u = snapshot.underlying_state
    rows.append({
        "snapshot_ts": snapshot.snapshot_ts,
        "instrument_key": u.instrument_key,
        "underlying_symbol": u.instrument_key.split("|")[0],
        "bid": u.bid,
        "ask": u.ask,
        "last": u.last,
        "mid": None,
        "volume": u.volume,
        "open_interest": None,
        "spread_pct": u.spread_pct,
        "reference_spot": u.reference_spot,
        "reference_type": u.reference_type,
        "quote_age_seconds": u.quote_age_seconds,
        "is_stale": u.is_stale,
        "is_market_open": u.is_market_open,
        "maturity_years": None,
        "session_id": session_id,
        "snapshot_version": snapshot.snapshot_version,
    })
    for opt in snapshot.option_rows:
        rows.append({
            "snapshot_ts": snapshot.snapshot_ts,
            "instrument_key": opt.instrument_key,
            "underlying_symbol": opt.underlying_symbol,
            "bid": opt.bid,
            "ask": opt.ask,
            "last": opt.last,
            "mid": opt.mid,
            "volume": opt.volume,
            "open_interest": opt.open_interest,
            "spread_pct": opt.spread_pct,
            "reference_spot": None,
            "reference_type": None,
            "quote_age_seconds": opt.quote_age_seconds,
            "is_stale": opt.is_stale,
            "is_market_open": True,
            "maturity_years": opt.maturity_years,
            "session_id": session_id,
            "snapshot_version": "1.0",
        })
    return rows


def _rows_to_snapshot(snap_rows: list[dict]):
    """Reconstruct a MarketStateSnapshot from stored MarketStateSnapshotRow dicts."""
    from src.snapshots.models import MarketStateSnapshot, UnderlyingState, OptionRow

    if not snap_rows:
        return None

    snapshot_ts = snap_rows[0]["snapshot_ts"]
    underlying_row = None
    option_rows: list[OptionRow] = []

    for row in snap_rows:
        key = row.get("instrument_key", "")
        parts = key.split("|")
        is_option = (len(parts) == 8 and parts[1] in ("OPT", "FUT") and parts[4])
        if is_option:
            try:
                expiry_date = _dt.strptime(parts[4], "%Y%m%d").date()
                expiry_str = expiry_date.isoformat()
                strike = float(parts[5]) if parts[5] else None
                if strike is None:
                    continue
                option_right = parts[6]
                multiplier = float(parts[7]) if parts[7] else 100.0
                option_rows.append(OptionRow(
                    instrument_key=key,
                    snapshot_ts=snapshot_ts,
                    underlying_symbol=row.get("underlying_symbol", ""),
                    expiry_str=expiry_str,
                    strike=strike,
                    option_right=option_right,
                    multiplier=multiplier,
                    bid=row.get("bid"),
                    ask=row.get("ask"),
                    last=row.get("last"),
                    mid=row.get("mid"),
                    volume=row.get("volume"),
                    open_interest=row.get("open_interest"),
                    spread_pct=row.get("spread_pct"),
                    quote_age_seconds=row.get("quote_age_seconds"),
                    is_stale=bool(row.get("is_stale", True)),
                    maturity_years=row.get("maturity_years"),
                ))
            except Exception as exc:
                logger.debug("_rows_to_snapshot: skip row %s: %s", key, exc)
        elif row.get("reference_spot") is not None and underlying_row is None:
            underlying_row = row

    if underlying_row is None:
        return None

    underlying = UnderlyingState(
        instrument_key=underlying_row["instrument_key"],
        snapshot_ts=snapshot_ts,
        bid=underlying_row.get("bid"),
        ask=underlying_row.get("ask"),
        last=underlying_row.get("last"),
        volume=underlying_row.get("volume"),
        reference_spot=underlying_row["reference_spot"],
        reference_type=underlying_row.get("reference_type", "unknown"),
        spread_pct=underlying_row.get("spread_pct"),
        is_market_open=bool(underlying_row.get("is_market_open", True)),
        is_stale=bool(underlying_row.get("is_stale", False)),
        quote_age_seconds=underlying_row.get("quote_age_seconds"),
    )

    return MarketStateSnapshot(
        snapshot_ts=snapshot_ts,
        underlying_state=underlying,
        option_rows=option_rows,
    )


def _iv_rows_to_iv_points(iv_rows: list[dict]):
    """Reconstruct IVPoint objects from stored IVPointRow dicts."""
    from src.surfaces.models import IVPoint

    points = []
    for row in iv_rows:
        try:
            points.append(IVPoint(
                contract_key=row["contract_key"],
                snapshot_ts=row["snapshot_ts"],
                expiry_str=row["expiry_str"],
                maturity_years=float(row["maturity_years"]),
                strike=float(row["strike"]),
                forward=float(row["forward"]),
                log_moneyness=float(row["log_moneyness"]),
                implied_vol=float(row["implied_vol"]),
                total_variance=float(row["total_variance"]),
                weight=float(row.get("weight", 1.0)),
                qc_status=row.get("qc_status", "usable"),
            ))
        except Exception as exc:
            logger.debug("_iv_rows_to_iv_points: skip %s: %s",
                         row.get("contract_key", "?"), exc)
    return points


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

    from src.universe.discovery import (
        load_universe_config, UniverseConfig, UniverseSpec,
        UniverseStore, refresh_universe,
    )

    ucfg = run.config.get("universe", {})
    config_dir = ucfg.get("config_dir", "configs")
    store_root = run.config.get("storage_root", "data")

    try:
        u_config = load_universe_config(config_dir)
    except FileNotFoundError:
        specs = tuple(
            UniverseSpec(
                symbol=u["symbol"],
                sec_type=u.get("sec_type", "STK"),
                exchange=u.get("exchange", "SMART"),
                currency=u.get("currency", "USD"),
            )
            for u in ucfg.get("underlyings", [])
        )
        u_config = UniverseConfig(
            version=ucfg.get("version", "1.0"),
            underlyings=specs,
        )

    from datetime import date as _date
    store = UniverseStore(root=store_root)
    session_date = _date.fromisoformat(run.trade_date)

    summary = refresh_universe(
        session_date=session_date,
        config=u_config,
        adapter=session,
        store=store,
    )

    if metrics:
        metrics.record("universe_option_count", float(summary.get("option_count", 0)),
                       {"date": run.trade_date})
        metrics.record("universe_error_count", float(summary.get("error_count", 0)),
                       {"date": run.trade_date})

    result = {"status": "ok", **summary}
    storage_writer.write_manifest(result, f"universe_{run.run_id}")
    return result


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

    from datetime import date as _date
    from src.collectors.raw_collector import RawCollector
    from src.collectors.raw_writer import RawWriter
    from src.connectivity.state import CanonicalContract
    from src.universe.discovery import (
        UniverseStore, UniverseConfig, UniverseSpec,
        load_active_universe, load_universe_config,
    )

    collect_cfg = {**run.config.get("live_collect", {}), **config}
    duration = float(collect_cfg.get("duration_seconds", 27000))
    flush_interval = float(collect_cfg.get("flush_interval_seconds", 60))
    store_root = run.config.get("storage_root", "data")
    session_date = _date.fromisoformat(run.trade_date)

    raw_writer = RawWriter(
        data_root=store_root,
        session_id=run.session_id,
        session_date=session_date,
    )
    collector = RawCollector(adapter=session, writer=raw_writer)

    # Load contracts to subscribe
    u_store = UniverseStore(root=store_root)
    ucfg = run.config.get("universe", {})
    config_dir = ucfg.get("config_dir", "configs")
    try:
        u_config = load_universe_config(config_dir)
    except Exception:
        specs = tuple(
            UniverseSpec(symbol=u["symbol"])
            for u in ucfg.get("underlyings", [])
        )
        u_config = UniverseConfig(version="1.0", underlyings=specs)

    opt_contracts = load_active_universe(session_date, u_config, u_store)
    canonical = [
        CanonicalContract(
            underlying_symbol=c.underlying_symbol,
            sec_type=c.sec_type,
            exchange=c.exchange,
            currency=c.currency,
            expiry=c.expiry.strftime("%Y%m%d") if c.expiry else None,
            strike=c.strike,
            right=c.right,
            multiplier=c.multiplier,
            broker_id=c.broker_id,
            broker_payload=c.broker_payload,
        )
        for c in opt_contracts
    ]

    t0 = time.time()
    collector.subscribe(canonical)

    try:
        while time.time() - t0 < duration:
            time.sleep(min(flush_interval, duration - (time.time() - t0)))
            raw_writer.flush()
    finally:
        collector.cancel()
        raw_writer.close()

    elapsed = time.time() - t0
    summary = collector.get_session_summary()

    if metrics:
        metrics.record_event_rate(summary.get("event_count", 0), elapsed,
                                  {"date": run.trade_date})

    result = {
        "status": "ok",
        "elapsed_seconds": round(elapsed, 1),
        **summary,
    }
    writer.write_manifest(result, f"live_collect_{run.run_id}")
    return result


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

    from src.collectors.raw_collector import RawEvent
    from src.snapshots.builder import build_snapshot
    from src.forwards.engine import estimate_forward_curve
    from src.iv.solver import solve_iv_batch, log_moneyness, total_variance

    underlyings = run.config.get("underlyings", [])
    snap_config_base = run.config.get("snapshot", {})
    fwd_config = run.config.get("forwards", {})
    iv_config = run.config.get("iv_solver", {})
    solver_version = run.config.get("solver_version", run.code_version)
    last_run_ts = float(run.config.get("last_run_ts", 0.0))

    total_iv_solved = 0
    total_iv_failed = 0

    for u_config in underlyings:
        symbol = u_config["symbol"]
        underlying_key = u_config["underlying_key"]
        rate = float(u_config.get("rate", 0.05))

        raw_dicts = reader.read_raw_events(run.trade_date, underlying=symbol)
        events = [
            RawEvent.from_dict(d) for d in raw_dicts
            if float(d.get("receipt_ts", 0)) > last_run_ts
        ]
        if not events:
            continue

        snapshot_ts = max(e.receipt_ts for e in events)
        contracts = reader.read_instrument_master(run.trade_date, underlying=symbol)
        option_keys = [r["instrument_key"] for r in contracts if r.get("sec_type") == "OPT"]

        snap_cfg = {
            **snap_config_base,
            "underlying_key": underlying_key,
            "underlying_symbol": symbol,
            "option_contracts": option_keys,
            "snapshot_date": run.trade_date,
        }
        snapshot = build_snapshot(events, snapshot_ts, snap_cfg)
        snap_rows = _snapshot_to_rows(snapshot, run.session_id)
        writer.write_snapshots(snap_rows, run.trade_date, symbol)

        maturities = {
            opt.expiry_str: opt.maturity_years
            for opt in snapshot.option_rows
            if opt.maturity_years is not None
        }
        if not maturities:
            continue

        fwd_results = estimate_forward_curve(
            snapshot, list(maturities.items()), rate, fwd_config
        )
        forwards = {f.expiry_str: f.chosen_forward for f in fwd_results}
        carries = {f.expiry_str: 0.0 for f in fwd_results}

        spot = snapshot.underlying_state.reference_spot
        records = []
        for opt in snapshot.option_rows:
            if opt.mid is None or opt.mid <= 0 or opt.is_stale:
                continue
            if opt.maturity_years is None or opt.maturity_years <= 0:
                continue
            fwd = forwards.get(opt.expiry_str, spot)
            records.append({
                "market_price": opt.mid,
                "S": spot, "K": opt.strike, "T": opt.maturity_years,
                "r": rate, "q": carries.get(opt.expiry_str, 0.0),
                "option_type": opt.option_right,
                "contract_key": opt.instrument_key,
                "snapshot_ts": snapshot_ts,
                "forward": fwd,
                "expiry_str": opt.expiry_str,
                "underlying": symbol,
            })

        if records:
            solve_results = solve_iv_batch(records, iv_config)
            iv_rows = []
            for rec, res in zip(records, solve_results):
                if not res.converged or res.implied_vol is None:
                    total_iv_failed += 1
                    continue
                total_iv_solved += 1
                fwd = rec["forward"]
                iv_rows.append({
                    "snapshot_ts": snapshot_ts,
                    "contract_key": rec["contract_key"],
                    "underlying": symbol,
                    "expiry_str": rec["expiry_str"],
                    "maturity_years": rec["T"],
                    "strike": rec["K"],
                    "option_right": rec["option_type"],
                    "forward": fwd,
                    "log_moneyness": log_moneyness(rec["K"], fwd),
                    "market_price": rec["market_price"],
                    "implied_vol": res.implied_vol,
                    "total_variance": total_variance(res.implied_vol, rec["T"]),
                    "converged": True,
                    "solver_residual": res.residual,
                    "iterations": res.iterations,
                    "failure_reason": None,
                    "model_name": res.model_name,
                    "solver_version": solver_version,
                    "qc_status": "usable",
                    "weight": 1.0,
                })
            if iv_rows:
                writer.write_iv_points(iv_rows, run.trade_date, symbol, solver_version)

    if metrics:
        metrics.record_solver_failures(total_iv_failed, {"date": run.trade_date})

    result = {
        "status": "ok",
        "total_iv_solved": total_iv_solved,
        "total_iv_failed": total_iv_failed,
        "idempotency_key": ikey,
    }
    writer.write_manifest(result, f"incremental_{run.run_id}")
    return result


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

    underlyings = [u["symbol"] for u in run.config.get("underlyings", [])]
    recon_cfg = run.config.get("reconciliation", {})
    min_iv_rows = int(recon_cfg.get("min_iv_rows_per_underlying", 1))
    min_fwd_rows = int(recon_cfg.get("min_forward_rows_per_underlying", 1))

    issues = []
    table_counts: dict[str, int] = {}

    for symbol in underlyings:
        snap_rows = reader.read_snapshots(run.trade_date, symbol)
        fwd_rows = reader.read_forward_curve(run.trade_date, symbol)
        iv_rows = reader.read_iv_points(run.trade_date, symbol)
        surf_rows = reader.read_surface_parameters(run.trade_date, symbol)

        table_counts[f"snapshots:{symbol}"] = len(snap_rows)
        table_counts[f"forward_curve:{symbol}"] = len(fwd_rows)
        table_counts[f"iv_points:{symbol}"] = len(iv_rows)
        table_counts[f"surface_params:{symbol}"] = len(surf_rows)

        if not snap_rows:
            issues.append({"type": "MISSING_PARTITION", "table": "snapshots", "underlying": symbol})
        if len(fwd_rows) < min_fwd_rows:
            issues.append({"type": "LOW_ROW_COUNT", "table": "forward_curve",
                           "underlying": symbol, "count": len(fwd_rows)})
        if len(iv_rows) < min_iv_rows:
            issues.append({"type": "LOW_ROW_COUNT", "table": "iv_points",
                           "underlying": symbol, "count": len(iv_rows)})

        null_iv = sum(1 for r in iv_rows if r.get("implied_vol") is None)
        if null_iv:
            issues.append({"type": "NULL_VALUES", "table": "iv_points",
                           "underlying": symbol, "null_count": null_iv})

    n_issues = len(issues)
    if metrics:
        metrics.record("reconciliation_issues", float(n_issues), {"date": run.trade_date})

    result = {
        "status": "ok" if n_issues == 0 else "issues_found",
        "n_issues": n_issues,
        "issues": issues,
        "table_counts": table_counts,
        "idempotency_key": ikey,
    }
    writer.write_manifest(result, f"reconciliation_{run.run_id}")
    return result


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
        all_data = {
            "raw_events": reader.read_raw_events(run.trade_date, underlying=und),
            "snapshots": reader.read_snapshots(run.trade_date, und),
            "iv_points": reader.read_iv_points(run.trade_date, und),
            "forward_rows": reader.read_forward_curve(run.trade_date, und),
            "surface_params": reader.read_surface_parameters(run.trade_date, und),
            "pricing_rows": reader.read_pricing_results(run.trade_date, underlying=und),
            "scenario_results": reader.read_scenario_results(run.trade_date, underlying=und),
        }
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
# EOD pipeline step jobs
# ---------------------------------------------------------------------------

def job_build_snapshots(run: JobRunContext, reader, writer,
                         metrics: MetricsCatalog | None = None) -> dict:
    """
    Step 5: Build end-of-day market state snapshots from raw events.
    Reads: raw_market_events, instrument_master
    Writes: market_state_snapshots
    """
    logger.info("[build_snapshots] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}

    from src.collectors.raw_collector import RawEvent
    from src.snapshots.builder import build_snapshot

    underlyings = run.config.get("underlyings", [])
    snap_config_base = run.config.get("snapshot", {})
    total_rows = 0
    total_stale = 0

    for u_config in underlyings:
        symbol = u_config["symbol"]
        underlying_key = u_config["underlying_key"]

        raw_dicts = reader.read_raw_events(run.trade_date, underlying=symbol)
        if not raw_dicts:
            logger.warning("[build_snapshots] no raw events for %s on %s",
                           symbol, run.trade_date)
            continue

        events = [RawEvent.from_dict(d) for d in raw_dicts]
        snapshot_ts = float(u_config.get("snapshot_ts") or
                            max(e.receipt_ts for e in events))

        contracts = reader.read_instrument_master(run.trade_date, underlying=symbol)
        option_keys = [r["instrument_key"] for r in contracts
                       if r.get("sec_type") == "OPT"]

        snap_cfg = {
            **snap_config_base,
            "underlying_key": underlying_key,
            "underlying_symbol": symbol,
            "option_contracts": option_keys,
            "snapshot_date": run.trade_date,
        }

        snapshot = build_snapshot(events, snapshot_ts, snap_cfg)
        rows = _snapshot_to_rows(snapshot, run.session_id)
        n_stale = sum(1 for r in rows if r.get("is_stale"))
        total_stale += n_stale

        n_written = writer.write_snapshots(rows, run.trade_date, symbol)
        writer.write_lineage(snapshot_ts, symbol, [run.session_id], run.trade_date)
        total_rows += n_written

        if metrics:
            metrics.record_stale_ratio(n_stale, len(rows), {"underlying": symbol})

    if metrics:
        metrics.record("snapshots_total_rows", float(total_rows), {"date": run.trade_date})

    result = {
        "status": "ok",
        "total_rows": total_rows,
        "total_stale": total_stale,
        "n_underlyings": len(underlyings),
    }
    writer.write_manifest(result, f"snapshots_{run.run_id}")
    return result


def job_build_forwards(run: JobRunContext, reader, writer,
                        metrics: MetricsCatalog | None = None) -> dict:
    """
    Step 6: Build forward curve from market state snapshots.
    Reads: market_state_snapshots
    Writes: forward_curve
    """
    logger.info("[build_forwards] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}

    from src.forwards.engine import estimate_forward_curve, compute_carry_diagnostics

    underlyings = run.config.get("underlyings", [])
    fwd_config = run.config.get("forwards", {})
    analytics_version = run.config.get("analytics_version", run.code_version)
    total_rows = 0

    for u_config in underlyings:
        symbol = u_config["symbol"]
        rate = float(u_config.get("rate", 0.05))

        snap_rows = reader.read_snapshots(run.trade_date, symbol)
        snapshot = _rows_to_snapshot(snap_rows)
        if snapshot is None:
            logger.warning("[build_forwards] no usable snapshot for %s", symbol)
            continue

        maturities = {
            opt.expiry_str: opt.maturity_years
            for opt in snapshot.option_rows
            if opt.maturity_years is not None and opt.maturity_years > 0
        }
        if not maturities:
            logger.warning("[build_forwards] no maturities for %s", symbol)
            continue

        fwd_results = estimate_forward_curve(
            snapshot, list(maturities.items()), rate, fwd_config
        )
        spot = snapshot.underlying_state.reference_spot

        rows = []
        for fwd in fwd_results:
            implied_carry = None
            if fwd.maturity_years > 0 and fwd.chosen_forward > 0 and spot > 0:
                try:
                    cd = compute_carry_diagnostics(fwd, spot, rate)
                    implied_carry = cd.implied_carry
                except Exception:
                    pass

            rows.append({
                "snapshot_ts": snapshot.snapshot_ts,
                "underlying": symbol,
                "expiry_str": fwd.expiry_str,
                "maturity_years": fwd.maturity_years,
                "chosen_forward": fwd.chosen_forward,
                "weighted_mean_forward": fwd.weighted_mean_forward,
                "median_forward": fwd.median_forward,
                "confidence_score": fwd.confidence_score,
                "candidates_count": fwd.candidates_after_filter,
                "fallback_used": fwd.fallback_used,
                "implied_carry": implied_carry,
                "diagnostics_version": analytics_version,
            })

        n_written = writer.write_forward_curve(rows, run.trade_date, symbol, analytics_version)
        total_rows += n_written

    if metrics:
        metrics.record("forward_curve_rows", float(total_rows), {"date": run.trade_date})

    result = {
        "status": "ok",
        "total_rows": total_rows,
        "n_underlyings": len(underlyings),
    }
    writer.write_manifest(result, f"forwards_{run.run_id}")
    return result


def job_solve_iv(run: JobRunContext, reader, writer,
                  metrics: MetricsCatalog | None = None) -> dict:
    """
    Step 7: Invert market prices to implied volatilities.
    Reads: market_state_snapshots, forward_curve
    Writes: iv_points
    """
    logger.info("[solve_iv] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}

    from src.iv.solver import solve_iv_batch, log_moneyness, total_variance

    underlyings = run.config.get("underlyings", [])
    iv_config = run.config.get("iv_solver", {})
    solver_version = run.config.get("solver_version", run.code_version)
    total_converged = 0
    total_failed = 0

    for u_config in underlyings:
        symbol = u_config["symbol"]
        rate = float(u_config.get("rate", 0.05))

        snap_rows = reader.read_snapshots(run.trade_date, symbol)
        snapshot = _rows_to_snapshot(snap_rows)
        if snapshot is None:
            logger.warning("[solve_iv] no snapshot for %s", symbol)
            continue

        fwd_rows = reader.read_forward_curve(run.trade_date, symbol)
        forwards = {r["expiry_str"]: float(r["chosen_forward"]) for r in fwd_rows}
        carries = {r["expiry_str"]: float(r.get("implied_carry") or 0.0)
                   for r in fwd_rows}

        spot = snapshot.underlying_state.reference_spot
        max_spread = float(iv_config.get("max_option_spread_pct", 0.30))

        records = []
        for opt in snapshot.option_rows:
            if opt.mid is None or opt.mid <= 0:
                continue
            if opt.is_stale:
                continue
            if opt.maturity_years is None or opt.maturity_years <= 0:
                continue
            if opt.spread_pct is not None and opt.spread_pct > max_spread:
                continue
            fwd = forwards.get(opt.expiry_str, spot)
            records.append({
                "market_price": opt.mid,
                "S": spot,
                "K": opt.strike,
                "T": opt.maturity_years,
                "r": rate,
                "q": carries.get(opt.expiry_str, 0.0),
                "option_type": opt.option_right,
                "contract_key": opt.instrument_key,
                "snapshot_ts": snapshot.snapshot_ts,
                "forward": fwd,
                "expiry_str": opt.expiry_str,
                "underlying": symbol,
                "spread_pct": opt.spread_pct or 0.01,
            })

        if not records:
            logger.warning("[solve_iv] no valid options to solve for %s", symbol)
            continue

        solve_results = solve_iv_batch(records, iv_config)
        iv_rows = []

        for rec, res in zip(records, solve_results):
            if not res.converged or res.implied_vol is None:
                total_failed += 1
                continue
            total_converged += 1
            fwd = rec["forward"]
            k = log_moneyness(rec["K"], fwd)
            w = total_variance(res.implied_vol, rec["T"])
            spread = rec.get("spread_pct", 0.01)
            qc_status = "usable" if spread < 0.10 else "caution"

            iv_rows.append({
                "snapshot_ts": rec["snapshot_ts"],
                "contract_key": rec["contract_key"],
                "underlying": symbol,
                "expiry_str": rec["expiry_str"],
                "maturity_years": rec["T"],
                "strike": rec["K"],
                "option_right": rec["option_type"],
                "forward": fwd,
                "log_moneyness": k,
                "market_price": rec["market_price"],
                "implied_vol": res.implied_vol,
                "total_variance": w,
                "converged": True,
                "solver_residual": res.residual,
                "iterations": res.iterations,
                "failure_reason": None,
                "model_name": res.model_name,
                "solver_version": solver_version,
                "qc_status": qc_status,
                "weight": 1.0 / (spread + 1e-6),
            })

        if iv_rows:
            writer.write_iv_points(iv_rows, run.trade_date, symbol, solver_version)

        if metrics:
            metrics.record_solver_failures(total_failed, {"underlying": symbol})

    result = {
        "status": "ok",
        "total_converged": total_converged,
        "total_failed": total_failed,
    }
    writer.write_manifest(result, f"solve_iv_{run.run_id}")
    return result


def job_fit_surfaces(run: JobRunContext, reader, writer,
                      metrics: MetricsCatalog | None = None) -> dict:
    """
    Step 8: Fit volatility surface (SVI per slice, PCHIP fallback).
    Reads: iv_points
    Writes: surface_parameters, surface_grid
    """
    logger.info("[fit_surfaces] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}

    from src.surfaces.calibration import fit_surface

    underlyings = run.config.get("underlyings", [])
    surf_config = run.config.get("surface", {})
    model_version = run.config.get("model_version", run.code_version)
    total_slices = 0
    total_violations = 0

    for u_config in underlyings:
        symbol = u_config["symbol"]

        iv_rows = reader.read_iv_points(run.trade_date, symbol)
        iv_points = _iv_rows_to_iv_points(iv_rows)
        if not iv_points:
            logger.warning("[fit_surfaces] no IV points for %s", symbol)
            continue

        snapshot_ts = iv_rows[0]["snapshot_ts"] if iv_rows else 0.0
        surface = fit_surface(iv_points, surf_config, underlying=symbol,
                              snapshot_ts=snapshot_ts)

        param_rows = []
        grid_rows = []

        for sl in surface.slices:
            total_slices += 1
            p = sl.params
            param_rows.append({
                "snapshot_ts": snapshot_ts,
                "underlying": symbol,
                "expiry_str": sl.expiry_str,
                "maturity_years": sl.maturity_years,
                "model_name": sl.model,
                "model_version": model_version,
                "svi_a": p.a if p is not None else None,
                "svi_b": p.b if p is not None else None,
                "svi_rho": p.rho if p is not None else None,
                "svi_m": p.m if p is not None else None,
                "svi_sigma": p.sigma if p is not None else None,
                "fit_rmse": sl.rmse if math.isfinite(sl.rmse) else None,
                "fit_max_error": sl.max_error if math.isfinite(sl.max_error) else None,
                "n_accepted_points": sl.n_accepted,
                "quality_flag": sl.quality_flag,
            })

            for k, w in zip(sl.grid_log_moneyness, sl.grid_total_variance):
                iv_val = math.sqrt(w / sl.maturity_years) if sl.maturity_years > 0 and w > 0 else 0.0
                grid_rows.append({
                    "snapshot_ts": snapshot_ts,
                    "underlying": symbol,
                    "expiry_str": sl.expiry_str,
                    "maturity_years": sl.maturity_years,
                    "log_moneyness": k,
                    "total_variance": w,
                    "implied_vol": iv_val,
                    "model_name": sl.model,
                    "model_version": model_version,
                })

        n_violations = len(surface.calendar_violations)
        total_violations += n_violations
        if n_violations:
            logger.warning("[fit_surfaces] %d calendar violations for %s",
                           n_violations, symbol)

        if param_rows:
            writer.write_surface_parameters(param_rows, run.trade_date, symbol, model_version)
        if grid_rows:
            writer.write_surface_grid(grid_rows, run.trade_date, symbol, model_version)

    if metrics:
        metrics.record("surface_slices_fitted", float(total_slices), {"date": run.trade_date})
        metrics.record("calendar_violations", float(total_violations), {"date": run.trade_date})

    result = {
        "status": "ok",
        "total_slices": total_slices,
        "calendar_violations": total_violations,
        "n_underlyings": len(underlyings),
    }
    writer.write_manifest(result, f"fit_surfaces_{run.run_id}")
    return result


def job_compute_greeks(run: JobRunContext, reader, writer,
                        metrics: MetricsCatalog | None = None) -> dict:
    """
    Step 11: Compute per-position Greeks and portfolio aggregates.
    Reads: positions, iv_points, forward_curve, market_state_snapshots
    Writes: pricing_results, risk_aggregates
    """
    logger.info("[compute_greeks] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}

    from src.pricing.european import price_european
    from src.risk.aggregation import compute_position_risk, aggregate_risk
    from src.risk.models import Position

    underlyings = run.config.get("underlyings", [])
    risk_config = run.config.get("risk", {})
    analytics_version = risk_config.get("analytics_version", run.code_version)
    group_keys = risk_config.get("group_keys", ["underlying_symbol", "portfolio_id"])
    portfolio_id = run.config.get("portfolio_id", "default")

    # Build analytics_snapshots: contract_key → analytics dict (S, K, T, r, q, sigma, option_type)
    analytics_snapshots: dict[str, dict] = {}
    spot_by_symbol: dict[str, float] = {}

    for u_config in underlyings:
        symbol = u_config["symbol"]
        rate = float(u_config.get("rate", 0.05))

        snap_rows = reader.read_snapshots(run.trade_date, symbol)
        snapshot = _rows_to_snapshot(snap_rows)
        if snapshot is None:
            continue

        spot = snapshot.underlying_state.reference_spot
        spot_by_symbol[symbol] = spot

        fwd_rows = reader.read_forward_curve(run.trade_date, symbol)
        forwards = {r["expiry_str"]: float(r["chosen_forward"]) for r in fwd_rows}
        carries = {r["expiry_str"]: float(r.get("implied_carry") or 0.0)
                   for r in fwd_rows}

        iv_rows = reader.read_iv_points(run.trade_date, symbol)
        for iv in iv_rows:
            if not iv.get("converged") or iv.get("implied_vol") is None:
                continue
            key = iv["contract_key"]
            analytics_snapshots[key] = {
                "S": spot,
                "K": float(iv["strike"]),
                "T": float(iv["maturity_years"]),
                "r": rate,
                "q": carries.get(iv["expiry_str"], 0.0),
                "sigma": float(iv["implied_vol"]),
                "option_type": iv["option_right"],
                "multiplier": 100.0,
                "forward": forwards.get(iv["expiry_str"], spot),
                "snapshot_ts": float(iv["snapshot_ts"]),
            }

    # Load positions from storage
    position_dicts = reader.read_positions(run.trade_date, portfolio_id=portfolio_id)
    positions = [
        Position(
            portfolio_id=p.get("portfolio_id", portfolio_id),
            contract_key=p["contract_key"],
            underlying_symbol=p.get("underlying_symbol",
                                    p["contract_key"].split("|")[0]),
            quantity=float(p["quantity"]),
            avg_cost=p.get("avg_cost"),
            currency=p.get("currency", "EUR"),
        )
        for p in position_dicts
    ]

    if not positions:
        logger.info("[compute_greeks] no positions found for %s on %s",
                    portfolio_id, run.trade_date)

    position_risks = []
    pricing_rows = []

    for pos in positions:
        snap = analytics_snapshots.get(pos.contract_key)
        if snap is None:
            logger.warning("[compute_greeks] no analytics for %s", pos.contract_key)
            continue
        try:
            pr = compute_position_risk(pos, snap, price_european, risk_config)
            position_risks.append(pr)
            pricing_rows.append({
                "snapshot_ts": snap["snapshot_ts"],
                "contract_key": pos.contract_key,
                "underlying": pos.underlying_symbol,
                "pricer_name": "black_scholes",
                "pricer_version": analytics_version,
                "model_price": pr.model_price,
                "delta": pr.delta,
                "gamma": pr.gamma,
                "vega_per_point": pr.vega_per_point,
                "theta_per_day": pr.theta_per_day,
                "dollar_gamma": pr.dollar_gamma,
                "dollar_vega": pr.dollar_vega,
                "forward_used": snap["forward"],
                "sigma_used": snap["sigma"],
            })
        except Exception as exc:
            logger.error("[compute_greeks] position %s failed: %s",
                         pos.contract_key, exc)

    if pricing_rows:
        writer.write_pricing_results(pricing_rows, run.trade_date, analytics_version)

    aggregates = aggregate_risk(position_risks, group_keys)
    agg_rows = []
    for agg in aggregates:
        snap_ts = position_risks[0].snapshot_ts if position_risks else 0.0
        agg_rows.append({
            "valuation_ts": snap_ts,
            "portfolio_id": agg.portfolio_id,
            "group_key": agg.group_key,
            "net_delta": agg.net_delta,
            "net_gamma": agg.net_gamma,
            "net_vega": agg.net_vega,
            "net_theta": agg.net_theta,
            "net_dollar_delta": agg.net_dollar_delta,
            "net_dollar_gamma": agg.net_dollar_gamma,
            "net_dollar_vega": agg.net_dollar_vega,
            "net_market_value": agg.net_market_value,
            "position_count": agg.position_count,
            "analytics_version": analytics_version,
            "snapshot_ts_used": snap_ts,
        })

    if agg_rows:
        writer.write_risk_aggregates(agg_rows, run.trade_date, analytics_version)

    result = {
        "status": "ok",
        "n_positions": len(positions),
        "n_position_risks": len(position_risks),
        "n_aggregates": len(aggregates),
    }
    writer.write_manifest(result, f"greeks_{run.run_id}")
    return result


def job_risk_aggregation(run: JobRunContext, reader, writer,
                          metrics: MetricsCatalog | None = None) -> dict:
    """Alias: full risk aggregation step (same as compute_greeks + portfolio roll-up)."""
    logger.info("[risk_aggregation] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}
    return job_compute_greeks(run, reader, writer, metrics)


def job_run_scenarios(run: JobRunContext, reader, writer,
                       metrics: MetricsCatalog | None = None) -> dict:
    """
    Step 12: Stress PnL under named scenario grid.
    Reads: positions, iv_points, forward_curve, market_state_snapshots
    Writes: scenario_results
    """
    logger.info("[run_scenarios] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}

    from src.pricing.european import price_european
    from src.risk.scenarios import run_scenario_grid, load_scenarios_from_config
    from src.risk.models import Position

    underlyings = run.config.get("underlyings", [])
    scenario_config = run.config.get("scenarios", {})
    risk_config = run.config.get("risk", {})
    analytics_version = risk_config.get("analytics_version", run.code_version)
    scenario_version = scenario_config.get("version", "1.0")
    method = scenario_config.get("method", "full_reprice")
    portfolio_id = run.config.get("portfolio_id", "default")

    scenarios = load_scenarios_from_config(scenario_config)
    if not scenarios:
        logger.warning("[run_scenarios] no scenarios configured")
        return {"status": "ok", "n_scenarios": 0, "n_positions": 0}

    # Build analytics_snapshots: contract_key → {S, K, T, r, q, sigma, option_type}
    analytics_snapshots: dict[str, dict] = {}

    for u_config in underlyings:
        symbol = u_config["symbol"]
        rate = float(u_config.get("rate", 0.05))

        snap_rows = reader.read_snapshots(run.trade_date, symbol)
        snapshot = _rows_to_snapshot(snap_rows)
        if snapshot is None:
            continue

        spot = snapshot.underlying_state.reference_spot
        fwd_rows = reader.read_forward_curve(run.trade_date, symbol)
        forwards = {r["expiry_str"]: float(r["chosen_forward"]) for r in fwd_rows}
        carries = {r["expiry_str"]: float(r.get("implied_carry") or 0.0)
                   for r in fwd_rows}

        iv_rows = reader.read_iv_points(run.trade_date, symbol)
        for iv in iv_rows:
            if not iv.get("converged") or iv.get("implied_vol") is None:
                continue
            key = iv["contract_key"]
            analytics_snapshots[key] = {
                "S": spot,
                "K": float(iv["strike"]),
                "T": float(iv["maturity_years"]),
                "r": rate,
                "q": carries.get(iv["expiry_str"], 0.0),
                "sigma": float(iv["implied_vol"]),
                "option_type": iv["option_right"],
                "multiplier": 100.0,
                "forward": forwards.get(iv["expiry_str"], spot),
                "snapshot_ts": float(iv["snapshot_ts"]),
            }

    position_dicts = reader.read_positions(run.trade_date, portfolio_id=portfolio_id)
    positions = [
        Position(
            portfolio_id=p.get("portfolio_id", portfolio_id),
            contract_key=p["contract_key"],
            underlying_symbol=p.get("underlying_symbol",
                                    p["contract_key"].split("|")[0]),
            quantity=float(p["quantity"]),
        )
        for p in position_dicts
    ]

    if not positions:
        logger.info("[run_scenarios] no positions for %s on %s", portfolio_id, run.trade_date)
        return {"status": "ok", "n_scenarios": len(scenarios), "n_positions": 0}

    snap_ts = next(iter(analytics_snapshots.values()), {}).get("snapshot_ts", 0.0)
    run_config = {
        **risk_config,
        "valuation_ts": snap_ts,
        "snapshot_ts": snap_ts,
        "analytics_version": analytics_version,
    }

    t0 = time.time()
    scenario_results = run_scenario_grid(
        scenarios=scenarios,
        positions=positions,
        analytics_snapshots=analytics_snapshots,
        pricer=price_european,
        config=run_config,
        method=method,
    )
    elapsed = time.time() - t0

    if metrics:
        metrics.record_scenario_runtime(elapsed, {"date": run.trade_date})

    # Serialize results
    scenario_rows = []
    for sr in scenario_results:
        for line in sr.line_results:
            scenario_rows.append({
                "valuation_ts": sr.valuation_ts,
                "portfolio_id": sr.portfolio_id,
                "scenario_id": sr.scenario_id,
                "scenario_version": sr.scenario_version,
                "contract_key": line["contract_key"],
                "base_price": line.get("base_price", 0.0),
                "stressed_price": line.get("stressed_price") or 0.0,
                "pnl": line["pnl"],
                "method": sr.method,
                "analytics_version": sr.analytics_version,
                "snapshot_ts_used": sr.snapshot_ts,
            })

    if scenario_rows:
        writer.write_scenario_results(scenario_rows, run.trade_date, scenario_version)

    result = {
        "status": "ok",
        "n_scenarios": len(scenario_results),
        "n_positions": len(positions),
        "elapsed_seconds": round(elapsed, 2),
    }
    writer.write_manifest(result, f"scenarios_{run.run_id}")
    return result


def job_run_qc(run: JobRunContext, reader, writer,
                metrics: MetricsCatalog | None = None) -> dict:
    """
    Step 13: Run QC suite across all underlyings.
    Reads: all analytics partitions for run.trade_date
    Writes: qc_results
    """
    logger.info("[run_qc] run_id=%s date=%s", run.run_id, run.trade_date)
    if run.dry_run:
        return {"status": "dry_run"}

    from src.qc.validation import run_daily_qc, build_triage_table
    from src.storage.schemas import QCResultRow
    import json as _json

    underlyings_cfg = run.config.get("underlyings", [])
    qc_config = run.config.get("qc", {})
    expected_scenarios = run.config.get("expected_scenarios")

    reports = []
    for u_config in underlyings_cfg:
        symbol = u_config["symbol"]
        all_data = {
            "raw_events": reader.read_raw_events(run.trade_date, underlying=symbol),
            "snapshots": reader.read_snapshots(run.trade_date, symbol),
            "iv_points": reader.read_iv_points(run.trade_date, symbol),
            "forward_rows": reader.read_forward_curve(run.trade_date, symbol),
            "surface_params": reader.read_surface_parameters(run.trade_date, symbol),
            "pricing_rows": reader.read_pricing_results(run.trade_date, underlying=symbol),
            "scenario_results": reader.read_scenario_results(run.trade_date, underlying=symbol),
        }
        report = run_daily_qc(
            trade_date=run.trade_date,
            underlying=symbol,
            run_id=run.run_id,
            all_data=all_data,
            config=qc_config,
            expected_scenarios=expected_scenarios,
        )
        reports.append(report)

    run_ts = time.time()
    qc_rows = []
    for report in reports:
        for check in report.checks:
            qc_rows.append({
                "run_id": run.run_id,
                "check_name": check.check_name,
                "target_key": check.target_key,
                "qc_status": check.status,
                "reason_code": check.reason_code or "",
                "measured_value": check.measured_value,
                "threshold": check.threshold,
                "severity": check.severity,
                "run_ts": run_ts,
                "threshold_version": check.threshold_version,
                "context_json": _json.dumps(check.context),
            })

    if qc_rows:
        writer.write_qc_results(qc_rows, run.run_id, run.trade_date)

    triage = build_triage_table(reports)
    n_fail = sum(1 for t in triage if t.get("status") == "fail")
    n_warn = sum(1 for t in triage if t.get("status") == "warn")

    if metrics:
        metrics.record("qc_failures", float(n_fail), {"date": run.trade_date})
        metrics.record("qc_warnings", float(n_warn), {"date": run.trade_date})

    result = {
        "status": "ok",
        "n_underlyings": len(underlyings_cfg),
        "n_checks": len(qc_rows),
        "n_failures": n_fail,
        "n_warnings": n_warn,
    }
    writer.write_manifest(result, f"qc_{run.run_id}")
    return result


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
