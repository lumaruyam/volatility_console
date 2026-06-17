"""
Storage read adapters — partitioned file store for raw and derived data.

Reads from the same partitioning scheme written by StorageWriter.
lineage_query() uses the SQLite metadata.db to reconstruct provenance.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class StorageReader:
    def __init__(self, storage_root: str, config: dict):
        self.storage_root = Path(storage_root)
        self.config = config
        self._db_path = self.storage_root / "metadata.db"

    def _db(self) -> Optional[sqlite3.Connection]:
        if not self._db_path.exists():
            return None
        return sqlite3.connect(str(self._db_path))

    def _read_parquet(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            return pd.read_parquet(path).to_dict(orient="records")
        except Exception as exc:
            logger.error("Failed to read parquet %s: %s", path, exc)
            return []

    def _find_partition(self, layer: str, table: str, trade_date: str,
                         underlying: str = "", version: Optional[str] = None) -> Optional[Path]:
        """Return path to data.parquet for the most-recent version if version is None."""
        base = self.storage_root / layer / table / f"dt={trade_date}"
        if underlying:
            base = base / f"underlying={underlying}"

        if version:
            candidate = base / f"v={version}" / "data.parquet"
            return candidate if candidate.exists() else None

        if not base.exists():
            return None

        # No version specified: try direct data.parquet first, then newest v= partition
        direct = base / "data.parquet"
        if direct.exists():
            return direct

        versioned = sorted(
            [p / "data.parquet" for p in base.iterdir()
             if p.is_dir() and p.name.startswith("v=") and (p / "data.parquet").exists()]
        )
        return versioned[-1] if versioned else None

    def read_raw_events(self, trade_date: str,
                         session_id: Optional[str] = None,
                         underlying: Optional[str] = None) -> list[dict]:
        """Load raw events for a trade date from JSONL files."""
        raw_base = self.storage_root / "raw" / f"dt={trade_date}"
        if not raw_base.exists():
            return []

        session_dirs = (
            [raw_base / f"session={session_id}"] if session_id
            else [p for p in raw_base.iterdir() if p.is_dir()]
        )

        results = []
        for sdir in session_dirs:
            events_file = sdir / "events.jsonl"
            if not events_file.exists():
                continue
            with open(events_file) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed line in %s", events_file)
                        continue
                    if underlying and ev.get("instrument_key", "").split("|")[0] != underlying:
                        continue
                    results.append(ev)
        return results

    def read_instrument_master(self, as_of_date: str,
                                 underlying: Optional[str] = None) -> list[dict]:
        base = self.storage_root / "analytics" / "instrument_master" / f"dt={as_of_date}"
        if not base.exists():
            return []

        rows: list[dict] = []
        for version_dir in sorted(base.iterdir()):
            p = version_dir / "data.parquet"
            if p.exists():
                rows.extend(self._read_parquet(p))

        if underlying:
            rows = [r for r in rows if r.get("underlying_symbol") == underlying]
        return rows

    def read_snapshots(self, trade_date: str, underlying: str,
                        snapshot_ts_range: Optional[tuple] = None) -> list[dict]:
        path = self._find_partition("analytics", "market_state_snapshots", trade_date,
                                    underlying=underlying)
        rows = self._read_parquet(path) if path else []
        if snapshot_ts_range:
            lo, hi = snapshot_ts_range
            rows = [r for r in rows if lo <= r.get("snapshot_ts", 0) <= hi]
        return rows

    def read_forward_curve(self, trade_date: str, underlying: str,
                             analytics_version: Optional[str] = None) -> list[dict]:
        path = self._find_partition("analytics", "forward_curve", trade_date,
                                    underlying=underlying, version=analytics_version)
        return self._read_parquet(path) if path else []

    def read_iv_points(self, trade_date: str, underlying: str,
                        solver_version: Optional[str] = None) -> list[dict]:
        path = self._find_partition("analytics", "iv_points", trade_date,
                                    underlying=underlying, version=solver_version)
        return self._read_parquet(path) if path else []

    def read_surface_parameters(self, trade_date: str, underlying: str,
                                  model_version: Optional[str] = None) -> list[dict]:
        path = self._find_partition("analytics", "surface_parameters", trade_date,
                                    underlying=underlying, version=model_version)
        return self._read_parquet(path) if path else []

    def read_surface_grid(self, trade_date: str, underlying: str,
                           model_version: Optional[str] = None) -> list[dict]:
        path = self._find_partition("analytics", "surface_grid", trade_date,
                                    underlying=underlying, version=model_version)
        return self._read_parquet(path) if path else []

    def read_pricing_results(self, trade_date: str,
                              underlying: Optional[str] = None,
                              pricer_version: Optional[str] = None) -> list[dict]:
        """Load pre-computed model prices and Greeks for a trade date."""
        path = self._find_partition("analytics", "pricing_results", trade_date,
                                    version=pricer_version)
        rows = self._read_parquet(path) if path else []
        if underlying:
            rows = [r for r in rows if r.get("underlying") == underlying]
        return rows

    def read_positions(self, trade_date: str,
                        portfolio_id: Optional[str] = None) -> list[dict]:
        """Load signed position quantities for a trade date."""
        path = self._find_partition("analytics", "positions", trade_date)
        rows = self._read_parquet(path) if path else []
        if portfolio_id:
            rows = [r for r in rows if r.get("portfolio_id") == portfolio_id]
        return rows

    def read_scenario_results(self, trade_date: str,
                               underlying: Optional[str] = None,
                               scenario_version: Optional[str] = None) -> list[dict]:
        """Load scenario PnL rows for a trade date."""
        path = self._find_partition("analytics", "scenario_results", trade_date,
                                    version=scenario_version)
        rows = self._read_parquet(path) if path else []
        if underlying:
            rows = [r for r in rows if r.get("underlying") == underlying]
        return rows

    def read_qc_results(self, run_id: str) -> list[dict]:
        base = self.storage_root / "analytics" / "qc_results"
        results = []
        if not base.exists():
            return results
        for dt_dir in base.iterdir():
            p = dt_dir / f"v={run_id}" / "data.parquet"
            if p.exists():
                results.extend(self._read_parquet(p))
        return results

    def read_manifest(self, run_id: str) -> Optional[dict]:
        path = self.storage_root / "manifests" / f"{run_id}.json"
        if not path.exists():
            return None
        with open(path) as fh:
            return json.load(fh)

    def list_partitions(self, layer: str, table: str,
                         date_range: Optional[tuple] = None) -> list[str]:
        """Return sorted list of trade dates available for a table."""
        base = self.storage_root / layer / table
        if not base.exists():
            return []
        dates = [
            p.name.removeprefix("dt=")
            for p in sorted(base.iterdir())
            if p.is_dir() and p.name.startswith("dt=")
        ]
        if date_range:
            lo, hi = date_range
            dates = [d for d in dates if lo <= d <= hi]
        return dates

    def lineage_query(self, snapshot_ts: float, underlying: str) -> dict:
        """
        Return which raw sessions and analytics partitions produced the snapshot
        for (snapshot_ts, underlying).

        Returns dict:
          snapshot_ts, underlying, trade_date,
          source_raw_sessions,
          market_state_snapshots, forward_curve, iv_points,
          surface_parameters, surface_grid
        """
        con = self._db()
        if con is None:
            return {"error": "metadata.db not found", "snapshot_ts": snapshot_ts,
                    "underlying": underlying}

        # Resolve trade_date from stored lineage (most authoritative source),
        # falling back to deriving it from the timestamp epoch.
        trade_date_row = con.execute(
            "SELECT trade_date FROM lineage "
            "WHERE underlying=? AND ABS(snapshot_ts - ?) < 0.001 LIMIT 1",
            (underlying, snapshot_ts),
        ).fetchone()
        trade_date = (
            trade_date_row[0] if trade_date_row
            else datetime.fromtimestamp(snapshot_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        )

        sessions = con.execute(
            "SELECT DISTINCT source_session_id FROM lineage "
            "WHERE underlying=? AND ABS(snapshot_ts - ?) < 0.001 AND source_layer='raw'",
            (underlying, snapshot_ts),
        ).fetchall()
        source_session_ids = [r[0] for r in sessions if r[0]]

        write_rows = con.execute(
            "SELECT layer, table_name, version, record_count, partition_path, write_ts "
            "FROM write_log "
            "WHERE trade_date=? AND (underlying=? OR underlying='')",
            (trade_date, underlying),
        ).fetchall()
        con.close()

        tables_map: dict[str, list[dict]] = {}
        for layer, table, version, count, path, ts in write_rows:
            tables_map.setdefault(table, []).append(
                {"layer": layer, "version": version,
                 "record_count": count, "path": path, "write_ts": ts}
            )

        return {
            "snapshot_ts": snapshot_ts,
            "underlying": underlying,
            "trade_date": trade_date,
            "source_raw_sessions": source_session_ids,
            "raw_market_events": tables_map.get("raw_market_events", []),
            "market_state_snapshots": tables_map.get("market_state_snapshots", []),
            "forward_curve": tables_map.get("forward_curve", []),
            "iv_points": tables_map.get("iv_points", []),
            "surface_parameters": tables_map.get("surface_parameters", []),
            "surface_grid": tables_map.get("surface_grid", []),
        }
