"""Structured logging with correlation IDs and per-run context.

Per the roadmap Part III Step 15: every log line carries a correlation ID
linking collector sessions to analytics jobs, plus enough structure for an
operator to identify the failing component within minutes.

This module configures ``structlog`` once at process startup. All modules
acquire loggers via :func:`get_logger`. Context is propagated automatically
through ``bind_contextvars`` so nested function calls share run identifiers.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.processors import CallsiteParameter

_CONFIGURED = False


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_logging(
    log_level: str = "INFO",
    log_format: str = "console",
    logs_dir: Path | str = Path("./logs"),
    run_id: str | None = None,
) -> str:
    """Configure structlog and stdlib logging.

    Parameters
    ----------
    log_level
        Minimum level to emit. DEBUG | INFO | WARNING | ERROR.
    log_format
        ``console`` for human-readable colored output, ``json`` for one
        JSON object per line (production default).
    logs_dir
        Directory where rotating log files are written.
    run_id
        Optional correlation ID. If not provided, one is generated.

    Returns
    -------
    str
        The run_id used. Bind this into every job manifest and pass it to
        downstream jobs via env var or argument so log lines link together.
    """
    global _CONFIGURED

    logs_dir_p = _ensure_dir(Path(logs_dir))
    run_id = run_id or _new_run_id()

    clear_contextvars()
    bind_contextvars(run_id=run_id)

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    log_file = logs_dir_p / f"vol_infra_{run_id}.jsonl"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(log_file, mode="a", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=numeric_level,
        format="%(message)s",
        handlers=handlers,
        force=True,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.CallsiteParameterAdder(
            parameters=[
                CallsiteParameter.MODULE,
                CallsiteParameter.FUNC_NAME,
                CallsiteParameter.LINENO,
            ],
        ),
    ]

    renderer: Any
    if log_format == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True

    log = get_logger(__name__)
    log.info(
        "logging.configured",
        log_level=log_level,
        log_format=log_format,
        logs_dir=str(logs_dir_p),
        log_file=str(log_file),
    )
    return run_id


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Acquire a structured logger.

    Safe to call before :func:`configure_logging`, but emitted records will
    use defaults until configuration runs.
    """
    return structlog.get_logger(name)  # type: ignore[return-value]


def _new_run_id() -> str:
    """Generate a sortable, unique run identifier.

    Format: YYYYMMDDTHHMMSSZ_<8-char-uuid> in UTC.
    """
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{ts}_{uuid.uuid4().hex[:8]}"


__all__ = ["configure_logging", "get_logger", "bind_contextvars", "clear_contextvars"]
