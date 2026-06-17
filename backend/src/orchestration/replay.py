"""
Historical reconstruction and replay.

CRITICAL RULE: Replay must use the SAME code path as live processing.
Do not create a separate 'historical only' implementation.
Dual code paths always drift and become inconsistent.

Replay writes to versioned partitions — never overwrites prior analytics.
Partition scheme: analytics/v=CODE_VERSION/dt=YYYY-MM-DD/

Acceptance criterion: replay == live on overlapping dates with same code version.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from src.orchestration.jobs import JobRunContext, job_eod_pipeline

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Partition path helpers
# ---------------------------------------------------------------------------

def partition_path(base: str, code_version: str, trade_date: str) -> str:
    """
    Return the canonical versioned partition path for analytics output.
    Scheme: {base}/v={code_version}/dt={trade_date}

    This is the ONLY place where the path schema is defined — all writers
    must call this function rather than building paths ad-hoc.
    """
    return f"{base}/v={code_version}/dt={trade_date}"


def config_hash(config: dict) -> str:
    """SHA-256 (first 8 chars) of a JSON-serialised config dict."""
    content = json.dumps(config, sort_keys=True).encode()
    return hashlib.sha256(content).hexdigest()[:8]


def config_hashes(config: dict) -> dict:
    """Return per-key hashes for a nested config dict."""
    return {k: config_hash(v if isinstance(v, dict) else {"_": v})
            for k, v in config.items()}


# ---------------------------------------------------------------------------
# Data-completeness detection
# ---------------------------------------------------------------------------

@dataclass
class DataCompletenessReport:
    trade_date: str
    expected_symbols: list[str]
    symbols_found: list[str]
    symbols_missing: list[str]
    coverage_pct: float          # 0–100
    is_partial: bool             # True when 0 < found < expected
    is_empty: bool               # True when found == 0
    is_complete: bool            # True when found == expected (or no expectation)
    raw_partition_count: int


def detect_data_completeness(
    reader,
    trade_date: str,
    expected_symbols: Optional[list[str]] = None,
) -> DataCompletenessReport:
    """
    Check how complete the raw data is for a given trade date.

    Args:
        reader:           Storage reader with list_partitions interface.
        trade_date:       ISO date string.
        expected_symbols: Universe of expected symbols. When None the check
                          only looks at whether any raw partitions exist.

    Returns:
        DataCompletenessReport — always returned, even for missing data.
    """
    raw_partitions = reader.list_partitions(
        "raw", "raw_market_events", date_range=(trade_date, trade_date)
    )
    partition_count = len(raw_partitions)

    if expected_symbols is None:
        # No expected universe — report based on partition existence only
        found = list({p.get("symbol", "") for p in raw_partitions if p.get("symbol")})
        missing: list[str] = []
        coverage = 100.0 if partition_count > 0 else 0.0
    else:
        found_set = {p.get("symbol", "") for p in raw_partitions if p.get("symbol")}
        found = [s for s in expected_symbols if s in found_set]
        missing = [s for s in expected_symbols if s not in found_set]
        n_exp = len(expected_symbols)
        coverage = 100.0 * len(found) / n_exp if n_exp > 0 else 100.0

    n_found = len(found)
    n_expected = len(expected_symbols) if expected_symbols is not None else n_found

    return DataCompletenessReport(
        trade_date=trade_date,
        expected_symbols=expected_symbols or [],
        symbols_found=found,
        symbols_missing=missing,
        coverage_pct=coverage,
        is_partial=(0 < n_found < n_expected) if expected_symbols else False,
        is_empty=partition_count == 0,
        is_complete=(n_found == n_expected) if expected_symbols else (partition_count > 0),
        raw_partition_count=partition_count,
    )


# ---------------------------------------------------------------------------
# Replay manifest
# ---------------------------------------------------------------------------

@dataclass
class ReplayManifest:
    """
    Structured replay audit record.
    Written to storage after every replay attempt — success or failure.
    """
    type: str = "replay"
    trade_date: str = ""
    code_version: str = ""
    config_hashes: dict = field(default_factory=dict)
    status: str = "unknown"           # "success" | "failed" | "partial" | "skipped"
    failure_reason: Optional[str] = None
    raw_partition_count: int = 0
    coverage_pct: float = 100.0
    is_partial_data: bool = False
    output_partition_path: str = ""
    pipeline_steps: dict = field(default_factory=dict)
    analytics_version: str = "1.0"
    replay: bool = True

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "trade_date": self.trade_date,
            "code_version": self.code_version,
            "config_hashes": self.config_hashes,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "raw_partition_count": self.raw_partition_count,
            "coverage_pct": self.coverage_pct,
            "is_partial_data": self.is_partial_data,
            "output_partition_path": self.output_partition_path,
            "pipeline_steps": self.pipeline_steps,
            "analytics_version": self.analytics_version,
            "replay": self.replay,
        }


def build_replay_manifest(
    trade_date: str,
    code_version: str,
    cfg_hashes: dict,
    status: str,
    raw_partitions: list,
    output_partitions: list,
    failure_reason: Optional[str] = None,
    coverage_pct: float = 100.0,
    is_partial_data: bool = False,
    pipeline_steps: Optional[dict] = None,
) -> ReplayManifest:
    """Build a structured replay manifest for archival."""
    return ReplayManifest(
        trade_date=trade_date,
        code_version=code_version,
        config_hashes=cfg_hashes,
        status=status,
        failure_reason=failure_reason,
        raw_partition_count=len(raw_partitions),
        coverage_pct=coverage_pct,
        is_partial_data=is_partial_data,
        output_partition_path=output_partitions[0] if output_partitions else "",
        pipeline_steps=pipeline_steps or {},
        replay=True,
    )


# ---------------------------------------------------------------------------
# Core replay functions
# ---------------------------------------------------------------------------

def replay_day(
    trade_date: str,
    code_version: str,
    config: dict,
    reader,
    writer,
    expected_symbols: Optional[list[str]] = None,
    analytics_base: str = "analytics",
) -> dict:
    """
    Reconstruct analytics for one historical day from stored raw data.

    Uses identical pipeline to live processing (calls job_eod_pipeline).
    Writes to versioned partition: analytics/v=CODE_VERSION/dt=DATE/

    Args:
        trade_date:       ISO date string "YYYY-MM-DD"
        code_version:     Pinned code version for this replay run
        config:           Configuration dict (pinned version)
        reader:           Storage reader
        writer:           Storage writer
        expected_symbols: Universe for completeness check; None = skip check
        analytics_base:   Base path for output partitions (default "analytics")

    Returns:
        Manifest dict with status, coverage, pipeline step results.
    """
    completeness = detect_data_completeness(reader, trade_date, expected_symbols)

    if completeness.is_empty:
        logger.error("replay_day: no raw partitions for %s — aborting", trade_date)
        manifest = build_replay_manifest(
            trade_date=trade_date,
            code_version=code_version,
            cfg_hashes=config_hashes(config),
            status="failed",
            raw_partitions=[],
            output_partitions=[],
            failure_reason="MISSING_RAW_PARTITION",
        )
        writer.write_manifest(manifest.to_dict(), f"replay_{trade_date}")
        return manifest.to_dict()

    if completeness.is_partial:
        logger.warning(
            "replay_day: partial data for %s — %.1f%% coverage, missing: %s",
            trade_date, completeness.coverage_pct, completeness.symbols_missing,
        )

    out_path = partition_path(analytics_base, code_version, trade_date)

    run = JobRunContext(
        trade_date=trade_date,
        code_version=code_version,
        config=config,
    )

    logger.info(
        "replay_day: date=%s code_version=%s out_path=%s coverage=%.1f%%",
        trade_date, code_version, out_path, completeness.coverage_pct,
    )

    pipeline_result = job_eod_pipeline(run, reader, writer)

    final_status = pipeline_result.get("status", "failed")
    if completeness.is_partial and final_status == "success":
        final_status = "partial"

    manifest = build_replay_manifest(
        trade_date=trade_date,
        code_version=code_version,
        cfg_hashes=run.config_hashes,
        status=final_status,
        raw_partitions=list(range(completeness.raw_partition_count)),
        output_partitions=[out_path],
        coverage_pct=completeness.coverage_pct,
        is_partial_data=completeness.is_partial,
        pipeline_steps=pipeline_result.get("steps", {}),
    )
    writer.write_manifest(manifest.to_dict(), f"replay_{trade_date}")

    result = manifest.to_dict()
    result["run_id"] = run.run_id
    return result


def replay_date_range(
    start_date: str,
    end_date: str,
    code_version: str,
    config: dict,
    reader,
    writer,
    expected_symbols: Optional[list[str]] = None,
    skip_weekends: bool = True,
) -> list[dict]:
    """
    Replay analytics over a date range.
    Detects missing raw partitions and creates partial-data flags.
    Returns list of per-day manifests in date order.
    """
    results = []
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    while current <= end:
        if skip_weekends and current.weekday() >= 5:  # Sat=5, Sun=6
            current += timedelta(days=1)
            continue

        date_str = current.isoformat()
        result = replay_day(date_str, code_version, config, reader, writer,
                            expected_symbols=expected_symbols)
        results.append(result)
        logger.info(
            "replay_date_range: %s → status=%s coverage=%.1f%%",
            date_str, result.get("status"), result.get("coverage_pct", 100.0),
        )
        current += timedelta(days=1)

    return results


# ---------------------------------------------------------------------------
# Replay vs live comparison
# ---------------------------------------------------------------------------

@dataclass
class ReplayComparisonResult:
    trade_date: str
    replay_version: str
    live_version: str
    matching: list[str]         # contract_keys with identical values
    differing: list[dict]       # {contract_key, metric, replay_val, live_val, abs_diff}
    missing_in_replay: list[str]
    missing_in_live: list[str]
    n_compared: int
    max_abs_diff: float
    is_equivalent: bool         # True when differing is empty


def compare_replay_vs_live(
    trade_date: str,
    replay_version: str,
    live_version: str,
    reader,
    tolerance: float = 1e-8,
) -> ReplayComparisonResult:
    """
    Compare replay outputs with live outputs for an overlapping date.

    Both partitions are read from versioned paths:
      analytics/v=REPLAY_VERSION/dt=DATE/
      analytics/v=LIVE_VERSION/dt=DATE/

    Args:
        trade_date:     ISO date string
        replay_version: Code version tag used during replay
        live_version:   Code version tag from live run
        reader:         Storage reader with read_analytics interface
        tolerance:      Absolute tolerance for numeric equality

    Returns:
        ReplayComparisonResult with matching/differing/missing sets.
    """
    replay_data: dict = reader.read_analytics(replay_version, trade_date)
    live_data: dict = reader.read_analytics(live_version, trade_date)

    replay_keys = set(replay_data.keys())
    live_keys = set(live_data.keys())

    missing_in_replay = sorted(live_keys - replay_keys)
    missing_in_live = sorted(replay_keys - live_keys)
    common_keys = replay_keys & live_keys

    matching: list[str] = []
    differing: list[dict] = []
    max_abs_diff = 0.0

    for key in sorted(common_keys):
        r_val = replay_data[key]
        l_val = live_data[key]

        if isinstance(r_val, dict) and isinstance(l_val, dict):
            key_diffs = []
            for metric in sorted(set(r_val) | set(l_val)):
                rv = r_val.get(metric)
                lv = l_val.get(metric)
                if rv is None or lv is None:
                    key_diffs.append({
                        "contract_key": key,
                        "metric": metric,
                        "replay_val": rv,
                        "live_val": lv,
                        "abs_diff": None,
                    })
                else:
                    try:
                        diff = abs(float(rv) - float(lv))
                        max_abs_diff = max(max_abs_diff, diff)
                        if diff > tolerance:
                            key_diffs.append({
                                "contract_key": key,
                                "metric": metric,
                                "replay_val": rv,
                                "live_val": lv,
                                "abs_diff": diff,
                            })
                    except (TypeError, ValueError):
                        if rv != lv:
                            key_diffs.append({
                                "contract_key": key,
                                "metric": metric,
                                "replay_val": rv,
                                "live_val": lv,
                                "abs_diff": None,
                            })
            if key_diffs:
                differing.extend(key_diffs)
            else:
                matching.append(key)
        else:
            try:
                diff = abs(float(r_val) - float(l_val))
                max_abs_diff = max(max_abs_diff, diff)
                if diff <= tolerance:
                    matching.append(key)
                else:
                    differing.append({
                        "contract_key": key,
                        "metric": "_value",
                        "replay_val": r_val,
                        "live_val": l_val,
                        "abs_diff": diff,
                    })
            except (TypeError, ValueError):
                if r_val == l_val:
                    matching.append(key)
                else:
                    differing.append({
                        "contract_key": key,
                        "metric": "_value",
                        "replay_val": r_val,
                        "live_val": l_val,
                        "abs_diff": None,
                    })

    return ReplayComparisonResult(
        trade_date=trade_date,
        replay_version=replay_version,
        live_version=live_version,
        matching=matching,
        differing=differing,
        missing_in_replay=missing_in_replay,
        missing_in_live=missing_in_live,
        n_compared=len(common_keys),
        max_abs_diff=max_abs_diff,
        is_equivalent=(len(differing) == 0 and
                       len(missing_in_replay) == 0 and
                       len(missing_in_live) == 0),
    )
