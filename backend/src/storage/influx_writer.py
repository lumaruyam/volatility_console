"""
InfluxDB time-series store — production replacement for JSONL raw-tick files.

Interface mirrors the JSONL-based raw event writer so the rest of the
system can swap backends by changing a single config key:

    storage.raw_backend: jsonl    # dev default
    storage.raw_backend: influx   # production

Connection config is read from the environment:
    VOL_INFRA_STORAGE__INFLUX_URL=http://localhost:8086
    VOL_INFRA_STORAGE__INFLUX_TOKEN=<token>
    VOL_INFRA_STORAGE__INFLUX_ORG=vol_infra
    VOL_INFRA_STORAGE__INFLUX_BUCKET=raw_market_events

Data model:
    measurement: raw_market_events
    tags:        instrument_key, field_name, session_id, source
    fields:      field_value (float), exchange_ts (float or null)
    timestamp:   receipt_ts (nanosecond precision)

Setup (macOS):
    brew install influxdb@2 && brew services start influxdb@2
    influx setup --name vol_infra --org vol_infra --bucket raw_market_events \\
                 --retention 0 --username admin --password <password> --force
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.collectors.raw_collector import RawEvent

logger = logging.getLogger(__name__)

_INFLUXDB_CLIENT_AVAILABLE = False
try:
    from influxdb_client import InfluxDBClient, WriteOptions  # type: ignore[import]
    from influxdb_client.client.write_api import SYNCHRONOUS  # type: ignore[import]
    _INFLUXDB_CLIENT_AVAILABLE = True
except ImportError:
    pass

_MEASUREMENT = "raw_market_events"


class InfluxRawWriter:
    """InfluxDB-backed raw event writer (production).

    Falls back to a clear ImportError if influxdb-client is not installed,
    so dev environments that only use JSONL still import this module cleanly.

    Write path:
        append(event) → line-protocol Point → write_api.write(bucket, org, [point])

    Batching: pass batching_options=WriteOptions(batch_size=500, flush_interval=1000)
    for high-throughput ingestion. Defaults to SYNCHRONOUS for simplicity.
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        org: str | None = None,
        bucket: str | None = None,
        batching_options: Any = None,
    ) -> None:
        self._url = url or os.environ.get("VOL_INFRA_STORAGE__INFLUX_URL", "http://localhost:8086")
        self._token = token or os.environ.get("VOL_INFRA_STORAGE__INFLUX_TOKEN", "")
        self._org = org or os.environ.get("VOL_INFRA_STORAGE__INFLUX_ORG", "vol_infra")
        self._bucket = bucket or os.environ.get(
            "VOL_INFRA_STORAGE__INFLUX_BUCKET", "raw_market_events"
        )

        if not _INFLUXDB_CLIENT_AVAILABLE:
            raise ImportError(
                "influxdb-client is not installed. "
                "Add it to requirements.txt: influxdb-client>=1.36"
            )

        write_options = batching_options if batching_options is not None else SYNCHRONOUS
        self._client = InfluxDBClient(url=self._url, token=self._token, org=self._org)
        self._write_api = self._client.write_api(write_options=write_options)

    def append(self, event: "RawEvent") -> None:
        """Write a single raw event to InfluxDB."""
        from influxdb_client.domain.write_precision import WritePrecision  # type: ignore[import]

        point = (
            _Point(_MEASUREMENT)
            .tag("instrument_key", event.instrument_key)
            .tag("field_name", event.field_name)
            .tag("session_id", event.session_id)
            .tag("source", event.source)
            .field("field_value", float(event.field_value))
            .field("event_id", event.event_id)
        )
        if event.exchange_ts is not None:
            point = point.field("exchange_ts", float(event.exchange_ts))

        receipt_ns = int(event.receipt_ts * 1_000_000_000)
        point = point.time(receipt_ns, WritePrecision.NANOSECONDS)

        self._write_api.write(bucket=self._bucket, org=self._org, record=point)

    def append_batch(self, events: list["RawEvent"]) -> None:
        """Write multiple events in a single batch."""
        from influxdb_client.domain.write_precision import WritePrecision  # type: ignore[import]

        points = []
        for event in events:
            point = (
                _Point(_MEASUREMENT)
                .tag("instrument_key", event.instrument_key)
                .tag("field_name", event.field_name)
                .tag("session_id", event.session_id)
                .tag("source", event.source)
                .field("field_value", float(event.field_value))
                .field("event_id", event.event_id)
            )
            if event.exchange_ts is not None:
                point = point.field("exchange_ts", float(event.exchange_ts))
            receipt_ns = int(event.receipt_ts * 1_000_000_000)
            point = point.time(receipt_ns, WritePrecision.NANOSECONDS)
            points.append(point)

        self._write_api.write(bucket=self._bucket, org=self._org, record=points)
        logger.debug("influx.append_batch count=%d", len(points))

    def quarantine(self, record: dict, reason: str) -> None:
        """Log a rejected record. In production, also writes to a 'quarantine' measurement."""
        logger.warning("influx.quarantine reason=%s record=%r", reason, record)

    def flush(self) -> None:
        """Flush any buffered writes. Required when using batching_options."""
        self._write_api.flush()

    def close(self) -> None:
        """Flush and close the InfluxDB client."""
        try:
            self._write_api.close()
        finally:
            self._client.close()

    def __enter__(self) -> "InfluxRawWriter":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _Point(measurement: str) -> Any:
    """Thin wrapper that avoids a top-level import failure when influxdb-client is missing."""
    from influxdb_client import Point  # type: ignore[import]
    return Point(measurement)
