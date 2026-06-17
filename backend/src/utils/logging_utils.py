"""
Structured logging with correlation IDs.

Correlation chain: session_id → job_id → step_id
Every log record emitted through StructuredLogger carries all three
so that a single grep on session_id recovers the full execution trace.

Usage:
    log = StructuredLogger("my.module", session_id="abc", job_id="def")
    log.emit("iv_solver.done", n_solved=1000, elapsed=0.4)

    with LogContext(session_id="xyz") as ctx:
        log2 = StructuredLogger("other", session_id=ctx.session_id)
        ...
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator, Optional


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def new_correlation_id() -> str:
    """16-character hex correlation ID."""
    return str(uuid.uuid4()).replace("-", "")[:16]


def new_session_id() -> str:
    return new_correlation_id()


def new_job_id() -> str:
    return new_correlation_id()


# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------

class StructuredLogger:
    """
    Thin wrapper around the stdlib logger that:
      - attaches correlation chain (session_id, job_id, step_id) to every record
      - emits key=value structured fields alongside the event name
      - exposes .emit(event, **fields) for metric-style records
    """

    def __init__(
        self,
        name: str,
        session_id: str = "",
        job_id: str = "",
        step_id: str = "",
        level: str = "INFO",
    ):
        self.session_id = session_id or new_session_id()
        self.job_id = job_id
        self.step_id = step_id
        self._logger = logging.getLogger(name)
        self._logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        self._ensure_handler()

    def _ensure_handler(self) -> None:
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self._logger.addHandler(handler)

    def _prefix(self) -> str:
        parts = []
        if self.session_id:
            parts.append(f"session={self.session_id[:8]}")
        if self.job_id:
            parts.append(f"job={self.job_id[:8]}")
        if self.step_id:
            parts.append(f"step={self.step_id[:8]}")
        return " ".join(parts)

    def _format(self, event: str, fields: dict) -> str:
        prefix = self._prefix()
        kv = " ".join(f"{k}={v!r}" for k, v in fields.items())
        parts = [p for p in (prefix, event, kv) if p]
        return " ".join(parts)

    def emit(self, event: str, level: str = "INFO", **fields: Any) -> None:
        """Emit a structured log record with event name and key=value fields."""
        msg = self._format(event, fields)
        log_level = getattr(logging, level.upper(), logging.INFO)
        self._logger.log(log_level, msg)

    def info(self, event: str, **fields: Any) -> None:
        self.emit(event, level="INFO", **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.emit(event, level="WARNING", **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.emit(event, level="ERROR", **fields)

    def child(self, step_id: str = "", job_id: str = "") -> "StructuredLogger":
        """Return a new StructuredLogger that inherits the session_id."""
        return StructuredLogger(
            name=self._logger.name,
            session_id=self.session_id,
            job_id=job_id or self.job_id,
            step_id=step_id,
        )


# ---------------------------------------------------------------------------
# LogContext — context manager for scoped correlation IDs
# ---------------------------------------------------------------------------

@dataclass
class LogContext:
    """
    Context manager that generates a fresh session_id for a logical block.
    Useful at session boundaries (market open, EOD pipeline, replay run).

    Usage:
        with LogContext() as ctx:
            log = StructuredLogger("mod", session_id=ctx.session_id)
    """
    session_id: str = field(default_factory=new_session_id)
    started_at: float = field(default_factory=time.time)

    def __enter__(self) -> "LogContext":
        return self

    def __exit__(self, *_) -> None:
        pass


@contextmanager
def log_context(session_id: str = "") -> Generator[LogContext, None, None]:
    """Functional version of LogContext."""
    ctx = LogContext(session_id=session_id or new_session_id())
    yield ctx


# ---------------------------------------------------------------------------
# Legacy helper (backward compat)
# ---------------------------------------------------------------------------

def build_logger(
    name: str,
    level: str = "INFO",
    correlation_id: str = "",
) -> logging.Logger:
    """Build a stdlib logger with structured formatting and optional correlation ID."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = "%(asctime)s [%(levelname)s] %(name)s"
        if correlation_id:
            fmt += f" [{correlation_id[:8]}]"
        fmt += " %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)

    return logger
