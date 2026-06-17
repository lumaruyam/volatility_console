"""
Storage write adapters — partitioned file store for raw and derived data.

Partitioning scheme:
  Raw events:  data/raw/dt=YYYY-MM-DD/session=SESSION_ID/events.jsonl   (append-only)
  Analytics:   data/analytics/{table}/dt=YYYY-MM-DD/[underlying=X/][v=VER/]data.parquet

SQLite at storage_root/metadata.db tracks write_log, lineage, and rejected_records.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from src.storage.schemas import (
    ForwardCurveRow,
    IVPointRow,
    MarketStateSnapshotRow,
    QCResultRow,
    RawMarketEventRow,
)

logger = logging.getLogger(__name__)

_VALID_SOURCES = {"live", "replay"}
_VALID_OPTION_RIGHTS = {"C", "P"}
_VALID_QC_STATUS = {"pass", "warn", "fail"}
_VALID_SEVERITY = {"info", "warn", "critical"}


def _is_finite_or_none(v: Any) -> bool:
    if v is None:
        return True
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _validate_raw_event(d: dict) -> Optional[str]:
    if not d.get("session_id"):
        return "missing_session_id"
    if not d.get("event_id"):
        return "missing_event_id"
    if not d.get("instrument_key"):
        return "missing_instrument_key"
    if not d.get("field_name"):
        return "missing_field_name"
    if not _is_finite_or_none(d.get("field_value")) or d.get("field_value") is None:
        return "non_finite_field_value"
    if not _is_finite_or_none(d.get("receipt_ts")) or d.get("receipt_ts") is None:
        return "non_finite_receipt_ts"
    if d.get("source") not in _VALID_SOURCES:
        return f"invalid_source:{d.get('source')}"
    return None


def _validate_snapshot(d: dict) -> Optional[str]:
    if not d.get("instrument_key"):
        return "missing_instrument_key"
    if not _is_finite_or_none(d.get("snapshot_ts")) or d.get("snapshot_ts") is None:
        return "non_finite_snapshot_ts"
    bid, ask = d.get("bid"), d.get("ask")
    if bid is not None and ask is not None and bid > ask:
        return "crossed_market"
    mid = d.get("mid")
    if mid is not None and not _is_finite_or_none(mid):
        return "non_finite_mid"
    return None


def _validate_forward_curve(d: dict) -> Optional[str]:
    if not d.get("underlying"):
        return "missing_underlying"
    fwd = d.get("chosen_forward")
    if fwd is None or not math.isfinite(float(fwd)) or float(fwd) <= 0:
        return "invalid_forward"
    mat = d.get("maturity_years")
    if mat is None or not math.isfinite(float(mat)) or float(mat) <= 0:
        return "invalid_maturity"
    return None


def _validate_iv_point(d: dict) -> Optional[str]:
    if not d.get("contract_key"):
        return "missing_contract_key"
    iv = d.get("implied_vol")
    if iv is None or not math.isfinite(float(iv)) or float(iv) < 0:
        return "invalid_implied_vol"
    if d.get("option_right") not in _VALID_OPTION_RIGHTS:
        return f"invalid_option_right:{d.get('option_right')}"
    return None


def _validate_qc_result(d: dict) -> Optional[str]:
    if not d.get("run_id"):
        return "missing_run_id"
    if d.get("qc_status") not in _VALID_QC_STATUS:
        return f"invalid_qc_status:{d.get('qc_status')}"
    if d.get("severity") not in _VALID_SEVERITY:
        return f"invalid_severity:{d.get('severity')}"
    return None


_VALIDATORS = {
    "raw_market_events": _validate_raw_event,
    "market_state_snapshots": _validate_snapshot,
    "forward_curve": _validate_forward_curve,
    "iv_points": _validate_iv_point,
    "qc_results": _validate_qc_result,
}


class StorageWriter:
    def __init__(self, storage_root: str, config: dict):
        self.storage_root = Path(storage_root)
        self.config = config
        self._db_path = self.storage_root / "metadata.db"
        self._init_sqlite()

    def _init_sqlite(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self._db_path))
        con.executescript("""
            CREATE TABLE IF NOT EXISTS write_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL,
                table_name TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                underlying TEXT,
                version TEXT,
                record_count INTEGER,
                partition_path TEXT,
                write_ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lineage (
                snapshot_ts REAL NOT NULL,
                underlying TEXT NOT NULL,
                source_layer TEXT NOT NULL,
                source_session_id TEXT,
                trade_date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rejected_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                record_json TEXT NOT NULL,
                reject_ts REAL NOT NULL
            );
        """)
        con.commit()
        con.close()

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    def _log_write(self, layer: str, table: str, trade_date: str,
                   underlying: str, version: str, count: int, path: Path) -> None:
        con = self._db()
        con.execute(
            "INSERT INTO write_log "
            "(layer, table_name, trade_date, underlying, version, record_count, partition_path, write_ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (layer, table, trade_date, underlying, version, count,
             str(path), datetime.now(timezone.utc).timestamp()),
        )
        con.commit()
        con.close()

    def _quarantine(self, table: str, reason: str, record: dict) -> None:
        con = self._db()
        con.execute(
            "INSERT INTO rejected_records (table_name, reason_code, record_json, reject_ts) "
            "VALUES (?,?,?,?)",
            (table, reason, json.dumps(record, default=str),
             datetime.now(timezone.utc).timestamp()),
        )
        con.commit()
        con.close()
        logger.warning("Quarantined record from %s: %s", table, reason)

    def _to_dict(self, row: Any) -> dict:
        return row if isinstance(row, dict) else asdict(row)

    def _filter_valid(self, rows: list, table: str) -> list[dict]:
        """Run write-ahead validation; quarantine invalid rows; return valid dicts."""
        validator = _VALIDATORS.get(table)
        valid = []
        for row in rows:
            d = self._to_dict(row)
            if validator:
                reason = validator(d)
                if reason:
                    self._quarantine(table, reason, d)
                    continue
            valid.append(d)
        return valid

    def _write_parquet(self, rows: list[dict], path: Path) -> int:
        if not rows:
            return 0
        df = pd.DataFrame(rows)
        if path.exists():
            existing = pd.read_parquet(path)
            df = pd.concat([existing, df], ignore_index=True)
        df.to_parquet(path, index=False)
        return len(rows)

    def write_raw_events(self, events: list, trade_date: str, session_id: str) -> int:
        """
        Append raw market events to immutable raw layer as JSONL.
        Partition: data/raw/dt=TRADE_DATE/session=SESSION_ID/events.jsonl
        """
        partition = self.storage_root / "raw" / f"dt={trade_date}" / f"session={session_id}"
        partition.mkdir(parents=True, exist_ok=True)
        out_path = partition / "events.jsonl"

        valid = self._filter_valid(events, "raw_market_events")
        with open(out_path, "a") as fh:
            for d in valid:
                fh.write(json.dumps(d) + "\n")

        self._log_write("raw", "raw_market_events", trade_date, "", session_id, len(valid), out_path)
        return len(valid)

    def write_instrument_master(self, rows: list, as_of_date: str,
                                  universe_version: str) -> int:
        path = self._get_partition_path(
            "analytics", "instrument_master", as_of_date, version=universe_version
        ) / "data.parquet"
        valid = []
        for row in rows:
            d = self._to_dict(row)
            if not d.get("instrument_key") or not d.get("as_of_date"):
                self._quarantine("instrument_master", "missing_pk", d)
            else:
                valid.append(d)
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "instrument_master", as_of_date, "", universe_version, count, path)
        return count

    def write_snapshots(self, rows: list, trade_date: str, underlying: str) -> int:
        path = self._get_partition_path(
            "analytics", "market_state_snapshots", trade_date, underlying=underlying
        ) / "data.parquet"
        valid = self._filter_valid(rows, "market_state_snapshots")
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "market_state_snapshots", trade_date, underlying, "", count, path)
        return count

    def write_forward_curve(self, rows: list, trade_date: str, underlying: str,
                              analytics_version: str) -> int:
        path = self._get_partition_path(
            "analytics", "forward_curve", trade_date,
            underlying=underlying, version=analytics_version
        ) / "data.parquet"
        valid = self._filter_valid(rows, "forward_curve")
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "forward_curve", trade_date, underlying, analytics_version, count, path)
        return count

    def write_iv_points(self, rows: list, trade_date: str, underlying: str,
                         solver_version: str) -> int:
        path = self._get_partition_path(
            "analytics", "iv_points", trade_date,
            underlying=underlying, version=solver_version
        ) / "data.parquet"
        valid = self._filter_valid(rows, "iv_points")
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "iv_points", trade_date, underlying, solver_version, count, path)
        return count

    def write_surface_parameters(self, rows: list, trade_date: str,
                                   underlying: str, model_version: str) -> int:
        path = self._get_partition_path(
            "analytics", "surface_parameters", trade_date,
            underlying=underlying, version=model_version
        ) / "data.parquet"
        valid = []
        for row in rows:
            d = self._to_dict(row)
            if not d.get("underlying"):
                self._quarantine("surface_parameters", "missing_underlying", d)
            else:
                valid.append(d)
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "surface_parameters", trade_date, underlying, model_version, count, path)
        return count

    def write_surface_grid(self, rows: list, trade_date: str, underlying: str,
                             model_version: str) -> int:
        path = self._get_partition_path(
            "analytics", "surface_grid", trade_date,
            underlying=underlying, version=model_version
        ) / "data.parquet"
        valid = []
        for row in rows:
            d = self._to_dict(row)
            if not d.get("underlying"):
                self._quarantine("surface_grid", "missing_underlying", d)
            else:
                valid.append(d)
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "surface_grid", trade_date, underlying, model_version, count, path)
        return count

    def write_pricing_results(self, rows: list, trade_date: str,
                                pricer_version: str) -> int:
        path = self._get_partition_path(
            "analytics", "pricing_results", trade_date, version=pricer_version
        ) / "data.parquet"
        valid = []
        for row in rows:
            d = self._to_dict(row)
            if not d.get("contract_key"):
                self._quarantine("pricing_results", "missing_contract_key", d)
            else:
                valid.append(d)
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "pricing_results", trade_date, "", pricer_version, count, path)
        return count

    def write_risk_aggregates(self, rows: list, trade_date: str,
                               analytics_version: str) -> int:
        path = self._get_partition_path(
            "analytics", "risk_aggregates", trade_date, version=analytics_version
        ) / "data.parquet"
        valid = []
        for row in rows:
            d = self._to_dict(row)
            if not d.get("portfolio_id"):
                self._quarantine("risk_aggregates", "missing_portfolio_id", d)
            else:
                valid.append(d)
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "risk_aggregates", trade_date, "", analytics_version, count, path)
        return count

    def write_scenario_results(self, rows: list, trade_date: str,
                                 scenario_version: str) -> int:
        path = self._get_partition_path(
            "analytics", "scenario_results", trade_date, version=scenario_version
        ) / "data.parquet"
        valid = []
        for row in rows:
            d = self._to_dict(row)
            if not d.get("scenario_id"):
                self._quarantine("scenario_results", "missing_scenario_id", d)
            else:
                valid.append(d)
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "scenario_results", trade_date, "", scenario_version, count, path)
        return count

    def write_qc_results(self, rows: list, run_id: str, trade_date: str) -> int:
        path = self._get_partition_path(
            "analytics", "qc_results", trade_date, version=run_id
        ) / "data.parquet"
        valid = self._filter_valid(rows, "qc_results")
        count = self._write_parquet(valid, path)
        self._log_write("analytics", "qc_results", trade_date, "", run_id, count, path)
        return count

    def write_manifest(self, manifest: dict, run_id: str) -> None:
        manifest_path = self.storage_root / "manifests" / f"{run_id}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "w") as fh:
            json.dump(manifest, fh, indent=2)
        logger.info("Manifest written: %s", manifest_path)

    def write_lineage(self, snapshot_ts: float, underlying: str,
                      source_session_ids: list[str], trade_date: str) -> None:
        """Register which raw sessions contributed to a snapshot."""
        con = self._db()
        for sid in source_session_ids:
            con.execute(
                "INSERT INTO lineage (snapshot_ts, underlying, source_layer, source_session_id, trade_date) "
                "VALUES (?,?,?,?,?)",
                (snapshot_ts, underlying, "raw", sid, trade_date),
            )
        con.commit()
        con.close()

    def read_rejected_records(self, table_name: Optional[str] = None) -> list[dict]:
        """Return quarantined records, optionally filtered by table."""
        con = self._db()
        if table_name:
            rows = con.execute(
                "SELECT table_name, reason_code, record_json, reject_ts "
                "FROM rejected_records WHERE table_name=?",
                (table_name,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT table_name, reason_code, record_json, reject_ts FROM rejected_records"
            ).fetchall()
        con.close()
        return [
            {"table": r[0], "reason_code": r[1],
             "record": json.loads(r[2]), "reject_ts": r[3]}
            for r in rows
        ]

    def _get_partition_path(self, layer: str, table: str, trade_date: str,
                              underlying: str = "", version: str = "") -> Path:
        parts: list[Any] = [self.storage_root, layer, table, f"dt={trade_date}"]
        if underlying:
            parts.append(f"underlying={underlying}")
        if version:
            parts.append(f"v={version}")
        path = Path(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path
