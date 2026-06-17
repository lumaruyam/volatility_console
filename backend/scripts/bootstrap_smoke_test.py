"""Bootstrap smoke test for Step 1 of the roadmap.

Per roadmap Step 1, this script proves end-to-end connectivity without
placing orders. It prints, then logs, then writes to a manifest:

    1. Session state machine transitions.
    2. Current UTC time and local clock-drift check.
    3. Contract resolution for one configured underlying.
    4. One market-data retrieval (snapshot).

Exit codes
----------
0   all checks passed
2   environment / config error (no IBKR contact made)
3   connectivity error (could not reach IBKR)
4   data retrieval error (connected but snapshot failed)
5   health-check failure (clock drift, heartbeat, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure the project root (parent of this script's directory) is on sys.path
# so that `import src` works regardless of the cwd the caller uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src
from src.connectivity.ibkr_adapter import IbkrAdapter
from src.connectivity.mock_adapter import MockAdapter
from src.connectivity.session import Session
from src.connectivity.state import BrokerAdapter, SessionEvent
from src.utils.config import AppConfig, config_hash, load_config
from src.utils.logging import configure_logging, get_logger
from src.utils.time_utils import (
    measure_clock_drift_ms,
    now_utc_iso,
)

log = get_logger("bootstrap")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Step 1 bootstrap smoke test")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the in-memory MockAdapter instead of IBKR. Useful for environment validation without a broker.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("configs"),
        help="Directory containing environment.yaml and broker.yaml",
    )
    parser.add_argument(
        "--skip-clock-check",
        action="store_true",
        help="Skip the NTP clock-drift verification.",
    )
    return parser.parse_args()


def _build_adapter(config: AppConfig, mock: bool) -> BrokerAdapter:
    if mock:
        log.info("bootstrap.adapter kind=mock")
        return MockAdapter()
    log.info(
        "bootstrap.adapter kind=ibkr host=%s port=%s client_id=%s",
        config.ibkr.host, config.ibkr.port, config.ibkr.client_id,
    )
    return IbkrAdapter(
        host=config.ibkr.host,
        port=config.ibkr.port,
        client_id=config.ibkr.client_id,
        account=config.ibkr.account,
        connect_timeout_s=config.ibkr.connect_timeout_s,
        read_only=config.ibkr.read_only,
        delayed_data=config.bootstrap.use_delayed_data,
    )


def _check_clock(config: AppConfig, skip: bool) -> dict[str, Any]:
    if skip:
        return {"status": "skipped"}
    try:
        drift_ms = measure_clock_drift_ms()
        within_tolerance = drift_ms <= config.runtime.clock_drift_tolerance_ms
        log.info(
            "bootstrap.clock_check drift_ms=%d tolerance_ms=%d within_tolerance=%s",
            drift_ms, config.runtime.clock_drift_tolerance_ms, within_tolerance,
        )
        return {
            "status": "pass" if within_tolerance else "warn",
            "drift_ms": drift_ms,
            "tolerance_ms": config.runtime.clock_drift_tolerance_ms,
        }
    except Exception as exc:
        log.warning("bootstrap.clock_check.failed error=%s", exc)
        return {"status": "skipped", "error": str(exc)}


def main() -> int:
    args = _parse_args()

    # ------------------------------------------------------------------
    # Stage 0: configuration
    # ------------------------------------------------------------------
    try:
        config = load_config(config_dir=args.config_dir)
    except Exception as exc:
        print(f"FATAL: failed to load config: {exc}", file=sys.stderr)
        return 2

    run_id = configure_logging(
        log_level=config.runtime.log_level,
        log_format=config.runtime.log_format,
        logs_dir=config.runtime.logs_dir,
    )
    cfg_hash = config_hash(config)
    log.info(
        "bootstrap.start code_version=%s config_hash=%s environment=%s mock=%s",
        src.__version__, cfg_hash, config.runtime.environment, args.mock,
    )

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "code_version": src.__version__,
        "config_hash": cfg_hash,
        "environment": config.runtime.environment,
        "started_at": now_utc_iso(),
        "mock": args.mock,
        "stages": {},
    }

    # ------------------------------------------------------------------
    # Stage 1: clock-drift check
    # ------------------------------------------------------------------
    manifest["stages"]["clock_check"] = _check_clock(config, args.skip_clock_check)

    # ------------------------------------------------------------------
    # Stage 2: connectivity
    # ------------------------------------------------------------------
    adapter = _build_adapter(config, mock=args.mock)
    session_events: list[dict[str, Any]] = []

    def collect_event(event: SessionEvent) -> None:
        session_events.append(
            {
                "ts": event.ts.isoformat().replace("+00:00", "Z"),
                "previous": event.previous.value,
                "current": event.current.value,
                "reason": event.reason,
                "detail": event.detail,
            }
        )

    session = Session(
        adapter,
        initial_delay_s=config.ibkr.reconnect.initial_delay_s,
        max_delay_s=config.ibkr.reconnect.max_delay_s,
        backoff_multiplier=config.ibkr.reconnect.backoff_multiplier,
        jitter_pct=config.ibkr.reconnect.jitter_pct,
        max_attempts=3 if not args.mock else 1,  # fail fast in smoke test
        heartbeat_max_age_s=config.ibkr.heartbeat_max_age_s,
        on_event=collect_event,
    )

    exit_code = 0
    try:
        session.connect()
        manifest["stages"]["connectivity"] = {
            "status": "pass",
            "final_state": session.state.value,
            "reconnect_attempts": session.reconnect_attempts,
        }
    except Exception as exc:
        log.error("bootstrap.connect.failed error=%s", exc)
        manifest["stages"]["connectivity"] = {
            "status": "fail",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        manifest["session_events"] = session_events
        manifest["status"] = "fail"
        manifest["finished_at"] = now_utc_iso()
        _write_manifest(manifest, config)
        return 3

    # ------------------------------------------------------------------
    # Stage 3: contract resolution
    # ------------------------------------------------------------------
    try:
        contract = adapter.resolve_contract(
            underlying_symbol=config.bootstrap.test_symbol,
            sec_type="STK",
            exchange=config.bootstrap.test_exchange,
            currency=config.bootstrap.test_currency,
        )
        manifest["stages"]["contract_resolution"] = {
            "status": "pass",
            "instrument_key": contract.instrument_key,
            "broker_id": contract.broker_id,
            "broker_payload": contract.broker_payload,
        }
    except Exception as exc:
        log.error("bootstrap.resolve.failed error=%s", exc)
        manifest["stages"]["contract_resolution"] = {
            "status": "fail",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        exit_code = 4
        manifest["session_events"] = session_events
        manifest["status"] = "fail"
        manifest["finished_at"] = now_utc_iso()
        session.disconnect()
        _write_manifest(manifest, config)
        return exit_code

    # ------------------------------------------------------------------
    # Stage 4: snapshot retrieval
    # ------------------------------------------------------------------
    try:
        quote = adapter.request_snapshot(
            contract,
            timeout_s=config.bootstrap.quote_timeout_s,
            delayed=config.bootstrap.use_delayed_data,
        )
        manifest["stages"]["snapshot"] = {
            "status": "pass" if (quote.bid or quote.ask or quote.last) else "warn",
            "instrument_key": quote.instrument_key,
            "receipt_ts": quote.receipt_ts.isoformat().replace("+00:00", "Z"),
            "bid": quote.bid,
            "ask": quote.ask,
            "last": quote.last,
            "is_delayed": quote.is_delayed,
            "source_flags": quote.source_flags,
        }
    except Exception as exc:
        log.error("bootstrap.snapshot.failed error=%s", exc)
        manifest["stages"]["snapshot"] = {
            "status": "fail",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        exit_code = 4

    # ------------------------------------------------------------------
    # Stage 5: health check
    # ------------------------------------------------------------------
    is_healthy = session.check_health()
    heartbeat_age = adapter.heartbeat_age_s()
    manifest["stages"]["health"] = {
        "status": "pass" if is_healthy else "warn",
        "session_state": session.state.value,
        "heartbeat_age_s": round(heartbeat_age, 3) if heartbeat_age is not None else None,
        "heartbeat_max_age_s": config.ibkr.heartbeat_max_age_s,
    }
    if not is_healthy and exit_code == 0:
        exit_code = 5

    # ------------------------------------------------------------------
    # Tear down
    # ------------------------------------------------------------------
    session.disconnect()
    manifest["session_events"] = session_events
    manifest["finished_at"] = now_utc_iso()
    manifest["status"] = (
        "pass"
        if exit_code == 0 and all(
            s.get("status") in ("pass", "skipped") for s in manifest["stages"].values()
        )
        else ("warn" if exit_code == 0 else "fail")
    )

    _write_manifest(manifest, config)
    _print_summary(manifest)
    return exit_code


def _write_manifest(manifest: dict[str, Any], config: AppConfig) -> None:
    artifacts_dir = Path(config.runtime.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / f"bootstrap_{manifest['run_id']}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, default=str)
    log.info("bootstrap.manifest.written path=%s", path)


def _print_summary(manifest: dict[str, Any]) -> None:
    print()
    print("=" * 72)
    print(f"  vol-infra bootstrap  |  run_id={manifest['run_id']}")
    print(f"  code_version={manifest['code_version']}  config_hash={manifest['config_hash']}")
    print("=" * 72)
    for name, stage in manifest["stages"].items():
        status = stage.get("status", "?")
        marker = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]", "skipped": "[SKIP]"}.get(
            status, "[ ?  ]"
        )
        print(f"  {marker}  {name}")
    print("-" * 72)
    print(f"  overall: {manifest['status'].upper()}")
    print("=" * 72)
    print()


if __name__ == "__main__":
    sys.exit(main())
