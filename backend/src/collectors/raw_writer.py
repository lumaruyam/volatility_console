"""Append-only raw event writer and replay reader.

Design principles:
  - Append-only: never modify or delete existing records.
  - Validate before write: malformed events never enter the raw store.
  - Quarantine, never drop: every rejected event is stored with a reason code.
  - Kill-safe: JSONL lines are newline-terminated; a killed process at most
    leaves a partial last line, which the replay reader skips gracefully.
  - Session isolation: each session writes to its own subdirectory so a
    restart cannot overwrite or corrupt prior session data.

Storage layout (under ``data_root``):
  raw/
    dt=YYYY-MM-DD/
      session=SESSION_ID/
        raw_market_events.jsonl     ← one RawEvent per line
  quarantine/
    dt=YYYY-MM-DD/
      session=SESSION_ID/
        quarantine.jsonl             ← one quarantine record per line
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from src.collectors.raw_collector import KNOWN_FIELDS, RawEvent

log = logging.getLogger(__name__)

_RAW_TABLE = "raw_market_events"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_raw_event(event: RawEvent) -> None:
    """Raise ``ValueError`` describing the first schema violation found.

    Called by :meth:`RawWriter.append` before every write.
    """
    if not event.session_id:
        raise ValueError("session_id is required")
    if not event.event_id:
        raise ValueError("event_id is required")
    if not event.instrument_key:
        raise ValueError("instrument_key is required")
    if not event.field_name:
        raise ValueError("field_name is required")
    if event.field_name not in KNOWN_FIELDS:
        raise ValueError(f"Unknown field_name: {event.field_name!r}. Known: {sorted(KNOWN_FIELDS)}")
    if not math.isfinite(event.field_value):
        raise ValueError(f"field_value must be finite, got {event.field_value}")
    if event.receipt_ts <= 0:
        raise ValueError(f"receipt_ts must be a positive epoch timestamp, got {event.receipt_ts}")
    if event.exchange_ts is not None and event.exchange_ts <= 0:
        raise ValueError(f"exchange_ts must be a positive epoch timestamp, got {event.exchange_ts}")
    if event.source not in ("live", "replay"):
        raise ValueError(f"source must be 'live' or 'replay', got {event.source!r}")


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class RawWriter:
    """Append-only writer that persists :class:`RawEvent` objects to JSONL files.

    Each :class:`RawWriter` instance is bound to one session. Create a new
    instance for each session (program start / reconnect that creates a new
    session ID). Never share a writer across sessions.

    File handles are opened lazily on the first write and kept open for the
    lifetime of the writer. Call :meth:`close` at session end (or use as a
    context manager).
    """

    def __init__(self, data_root: Path | str, session_id: str, session_date: date) -> None:
        self._raw_dir = (
            Path(data_root) / "raw"
            / f"dt={session_date.isoformat()}"
            / f"session={session_id}"
        )
        self._quarantine_dir = (
            Path(data_root) / "quarantine"
            / f"dt={session_date.isoformat()}"
            / f"session={session_id}"
        )
        self._session_id = session_id
        self._session_date = session_date
        self._handles: dict[str, Any] = {}          # table → open file handle
        self._write_counter: dict[str, int] = {}    # table → events written

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, event: RawEvent) -> None:
        """Validate and append one event to the raw store.

        Raises ``ValueError`` if the event fails schema validation so the caller
        can quarantine it instead of silently dropping it.
        """
        validate_raw_event(event)
        fh = self._get_handle(_RAW_TABLE)
        fh.write(json.dumps(event.to_dict()) + "\n")
        fh.flush()  # flush after every event so a kill doesn't lose the line
        self._write_counter[_RAW_TABLE] = self._write_counter.get(_RAW_TABLE, 0) + 1

    def quarantine(self, raw_payload: Any, reason: str) -> None:
        """Store a malformed event in the quarantine partition with its reason code.

        Never silently drop events: operators need quarantine records to tune
        filters and track data quality.
        """
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "quarantine_id": uuid.uuid4().hex,
            "session_id": self._session_id,
            "reason_code": reason,
            "payload": raw_payload if isinstance(raw_payload, (dict, list)) else str(raw_payload),
            "quarantined_at": time.time(),
        }
        path = self._quarantine_dir / "quarantine.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        log.warning("writer.quarantine reason=%s", reason)

    def flush(self) -> None:
        """Flush all open file handles."""
        for fh in self._handles.values():
            fh.flush()

    def close(self) -> None:
        """Flush and close all open file handles."""
        for fh in self._handles.values():
            try:
                fh.flush()
                fh.close()
            except OSError:
                pass
        self._handles.clear()

    def get_partition_counts(self) -> dict[str, int]:
        """Return per-table write counters for operational reporting."""
        return dict(self._write_counter)

    @property
    def raw_dir(self) -> Path:
        return self._raw_dir

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "RawWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_handle(self, table: str) -> Any:
        if table not in self._handles:
            self._raw_dir.mkdir(parents=True, exist_ok=True)
            path = self._raw_dir / f"{table}.jsonl"
            self._handles[table] = path.open("a", encoding="utf-8")
        return self._handles[table]


# ---------------------------------------------------------------------------
# Replay reader
# ---------------------------------------------------------------------------


def replay_session(
    data_root: Path | str,
    session_date: date,
    session_id: str | None = None,
) -> Iterator[RawEvent]:
    """Yield all raw events for a given date, sorted by receipt_ts.

    Parameters
    ----------
    data_root
        Root of the data directory (same value used when constructing RawWriter).
    session_date
        The trading date to replay.
    session_id
        If provided, only replay events from that specific session.
        If None, replay all sessions for the date.

    Handles partial last lines (from a killed process) gracefully by catching
    ``json.JSONDecodeError`` on each line independently.

    This function uses the same :class:`RawEvent` model as the live collector
    so the snapshot builder sees no difference between live and replayed data.
    """
    raw_dir = Path(data_root) / "raw" / f"dt={session_date.isoformat()}"
    if not raw_dir.exists():
        log.info("replay.no_data date=%s", session_date)
        return

    session_dirs = sorted(raw_dir.iterdir())
    events: list[RawEvent] = []
    files_read = 0
    lines_skipped = 0

    for session_dir in session_dirs:
        if not session_dir.name.startswith("session="):
            continue
        sid = session_dir.name[len("session="):]
        if session_id is not None and sid != session_id:
            continue

        for jsonl_path in sorted(session_dir.glob("*.jsonl")):
            with jsonl_path.open("r", encoding="utf-8") as fh:
                for line_num, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        events.append(RawEvent.from_dict(raw).as_replay())
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                        lines_skipped += 1
                        log.warning(
                            "replay.malformed_line path=%s line=%d error=%s",
                            jsonl_path, line_num, exc,
                        )
            files_read += 1

    events.sort(key=lambda e: e.receipt_ts)
    log.info(
        "replay.loaded date=%s session=%s files=%d events=%d skipped=%d",
        session_date, session_id or "all", files_read, len(events), lines_skipped,
    )
    yield from events


def load_quarantine(
    data_root: Path | str,
    session_date: date,
    session_id: str | None = None,
) -> list[dict]:
    """Load all quarantine records for a given date."""
    q_dir = Path(data_root) / "quarantine" / f"dt={session_date.isoformat()}"
    if not q_dir.exists():
        return []
    records: list[dict] = []
    for session_dir in sorted(q_dir.iterdir()):
        if not session_dir.name.startswith("session="):
            continue
        sid = session_dir.name[len("session="):]
        if session_id is not None and sid != session_id:
            continue
        path = session_dir / "quarantine.jsonl"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return records
