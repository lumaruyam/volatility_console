"""Typed configuration loading and validation.

Configuration is sourced from YAML files in ``configs/`` and overridden by
environment variables using the convention ``VOL_INFRA_<SECTION>__<KEY>``.
Every loaded configuration carries a deterministic hash that downstream
analytics jobs record alongside their outputs, so any derived artifact can
be traced back to the exact configuration that produced it.

Per the roadmap Part IV.J: configuration is an economic input. It is
versioned independently of code and its hash is recorded in every derived
table.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class RuntimeConfig(BaseModel):
    """Runtime environment settings: paths, log behavior, clock tolerance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str = Field(default="development", pattern=r"^(development|staging|production)$")
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")
    log_format: str = Field(default="console", pattern=r"^(console|json)$")
    artifacts_dir: Path = Field(default=Path("./artifacts"))
    logs_dir: Path = Field(default=Path("./logs"))
    clock_drift_tolerance_ms: int = Field(default=1000, ge=0)


class BootstrapConfig(BaseModel):
    """Defaults used by the Step 1 bootstrap smoke test only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_symbol: str = "SPY"
    test_exchange: str = "SMART"
    test_currency: str = "USD"
    quote_timeout_s: float = Field(default=10.0, gt=0)
    use_delayed_data: bool = True


class ReconnectConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    initial_delay_s: float = Field(default=1.0, gt=0)
    max_delay_s: float = Field(default=60.0, gt=0)
    backoff_multiplier: float = Field(default=2.0, gt=1.0)
    jitter_pct: float = Field(default=0.25, ge=0, le=1.0)
    max_attempts: int = Field(default=0, ge=0)  # 0 = unlimited


class PacingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_messages_per_second: int = Field(default=40, gt=0)


class IbkrConfig(BaseModel):
    """IBKR-specific connection settings. Sensitive values come from env vars."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=4002, gt=0, le=65535)
    client_id: int = Field(default=1, ge=1, le=999)
    account: str = ""
    connect_timeout_s: float = Field(default=15.0, gt=0)
    read_only: bool = True
    heartbeat_interval_s: float = Field(default=5.0, gt=0)
    heartbeat_max_age_s: float = Field(default=30.0, gt=0)
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)
    pacing: PacingConfig = Field(default_factory=PacingConfig)


class AppConfig(BaseModel):
    """Root configuration object assembled from YAML + env overrides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime: RuntimeConfig
    bootstrap: BootstrapConfig
    ibkr: IbkrConfig
    client_id_reservations: dict[int, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Environment-variable overlay
# ---------------------------------------------------------------------------


class _EnvOverlay(BaseSettings):
    """Captures every VOL_INFRA_* env var, including nested via __ separator.

    pydantic-settings reads these eagerly. We then merge them on top of the
    YAML baseline. Frozen models above prevent silent in-place mutation.
    """

    model_config = SettingsConfigDict(
        env_prefix="VOL_INFRA_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="allow",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    runtime: dict[str, Any] = Field(default_factory=dict)
    bootstrap: dict[str, Any] = Field(default_factory=dict)
    ibkr: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loading and hashing
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge: overlay wins on scalar conflict."""
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration file {path} did not parse to a mapping")
    return data


def load_config(
    config_dir: Path | str = Path("configs"),
    environment_file: str = "environment.yaml",
    broker_file: str = "broker.yaml",
) -> AppConfig:
    """Load and validate the application configuration.

    Order of precedence (highest wins):
      1. Environment variables (VOL_INFRA_*).
      2. broker.yaml.
      3. environment.yaml.

    Returns a frozen ``AppConfig``. Use :func:`config_hash` to obtain a
    deterministic identifier for lineage tracking.
    """
    config_dir = Path(config_dir)

    environment_yaml = _load_yaml(config_dir / environment_file)
    broker_yaml = _load_yaml(config_dir / broker_file)
    yaml_merged = _deep_merge(environment_yaml, broker_yaml)

    env_overlay = _EnvOverlay().model_dump(exclude_unset=False, exclude_none=True)
    # Strip empty dicts that pydantic-settings may emit for absent prefixes.
    env_overlay = {k: v for k, v in env_overlay.items() if v}
    merged = _deep_merge(yaml_merged, env_overlay)

    return AppConfig.model_validate(merged)


def config_hash(config: AppConfig, prefix: str = "cfg") -> str:
    """Compute a deterministic short hash of the configuration.

    Used for lineage logging. Two configurations that produce different
    economics must produce different hashes. The hash is derived from the
    JSON-serialized model dump with sorted keys.
    """
    payload = config.model_dump_json(indent=None)
    canonical = json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:12]}"
