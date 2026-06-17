"""
PostgreSQL metadata store — production replacement for SQLite.

Interface mirrors StorageWriter exactly so the rest of the system
can swap backends by changing a single config key:

    storage.backend: sqlite   # dev default
    storage.backend: postgres # production

Connection DSN is read from the environment:
    VOL_INFRA_STORAGE__POSTGRES_DSN=postgresql://localhost/vol_infra

Schema is identical to the SQLite baseline (write_log, lineage,
rejected_records). The Parquet analytics layer is shared between both
backends — only the metadata tables differ.

Setup (macOS):
    brew install postgresql@15 && brew services start postgresql@15
    createdb vol_infra
    python scripts/init_postgres.py --config configs/environment.yaml
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PSYCOPG2_AVAILABLE = False
try:
    import psycopg2  # type: ignore[import]
    import psycopg2.extras  # type: ignore[import]
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    pass

_DDL = """
CREATE TABLE IF NOT EXISTS write_log (
    id SERIAL PRIMARY KEY,
    layer TEXT NOT NULL,
    table_name TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    underlying TEXT,
    version TEXT,
    record_count INTEGER,
    partition_path TEXT,
    write_ts DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS lineage (
    snapshot_ts DOUBLE PRECISION NOT NULL,
    underlying TEXT NOT NULL,
    source_layer TEXT NOT NULL,
    source_session_id TEXT,
    trade_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_records (
    id SERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    record_json TEXT NOT NULL,
    reject_ts DOUBLE PRECISION NOT NULL
);
"""


class PostgresMetadataStore:
    """PostgreSQL-backed metadata store (production).

    Falls back to a clear error if psycopg2 is not installed or the DSN
    is not configured — so dev environments with only SQLite still work.
    """

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn or os.environ.get("VOL_INFRA_STORAGE__POSTGRES_DSN")
        if not self._dsn:
            raise RuntimeError(
                "PostgreSQL DSN not configured. "
                "Set VOL_INFRA_STORAGE__POSTGRES_DSN or pass dsn= to constructor."
            )
        if not _PSYCOPG2_AVAILABLE:
            raise ImportError(
                "psycopg2 is not installed. "
                "Add it to requirements.txt: psycopg2-binary>=2.9"
            )

    def _conn(self):  # type: ignore[return]
        return psycopg2.connect(self._dsn)

    def init_schema(self) -> None:
        """Create metadata tables if they don't exist."""
        with self._conn() as con:
            with con.cursor() as cur:
                cur.execute(_DDL)

    def log_write(self, layer: str, table: str, trade_date: str, underlying: str,
                  version: str, count: int, path: str) -> None:
        with self._conn() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO write_log "
                    "(layer, table_name, trade_date, underlying, version, "
                    "record_count, partition_path, write_ts) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (layer, table, trade_date, underlying, version, count,
                     path, datetime.now(timezone.utc).timestamp()),
                )

    def write_lineage(self, snapshot_ts: float, underlying: str,
                      source_session_ids: list[str], trade_date: str) -> None:
        with self._conn() as con:
            with con.cursor() as cur:
                for sid in source_session_ids:
                    cur.execute(
                        "INSERT INTO lineage "
                        "(snapshot_ts, underlying, source_layer, source_session_id, trade_date) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (snapshot_ts, underlying, "raw", sid, trade_date),
                    )

    def quarantine(self, table: str, reason: str, record: dict) -> None:
        with self._conn() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO rejected_records (table_name, reason_code, record_json, reject_ts) "
                    "VALUES (%s,%s,%s,%s)",
                    (table, reason, json.dumps(record, default=str),
                     datetime.now(timezone.utc).timestamp()),
                )
        logger.warning("postgres.quarantine table=%s reason=%s", table, reason)

    def lineage_query(self, snapshot_ts: float, underlying: str) -> dict[str, Any]:
        """Resolve which raw sessions contributed to (snapshot_ts, underlying)."""
        with self._conn() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT trade_date FROM lineage "
                    "WHERE underlying=%s AND ABS(snapshot_ts - %s) < 0.001 LIMIT 1",
                    (underlying, snapshot_ts),
                )
                row = cur.fetchone()
                trade_date = row[0] if row else datetime.fromtimestamp(
                    snapshot_ts, tz=timezone.utc
                ).strftime("%Y-%m-%d")

                cur.execute(
                    "SELECT DISTINCT source_session_id FROM lineage "
                    "WHERE underlying=%s AND ABS(snapshot_ts - %s) < 0.001",
                    (underlying, snapshot_ts),
                )
                sessions = [r[0] for r in cur.fetchall() if r[0]]

                cur.execute(
                    "SELECT layer, table_name, version, record_count, partition_path, write_ts "
                    "FROM write_log "
                    "WHERE trade_date=%s AND (underlying=%s OR underlying='')",
                    (trade_date, underlying),
                )
                tables_map: dict[str, list] = {}
                for layer, table, version, count, path, ts in cur.fetchall():
                    tables_map.setdefault(table, []).append(
                        {"layer": layer, "version": version,
                         "record_count": count, "path": path, "write_ts": ts}
                    )

        return {
            "snapshot_ts": snapshot_ts,
            "underlying": underlying,
            "trade_date": trade_date,
            "source_raw_sessions": sessions,
            **{k: tables_map.get(k, []) for k in [
                "raw_market_events", "market_state_snapshots",
                "forward_curve", "iv_points", "surface_parameters", "surface_grid",
            ]},
        }
