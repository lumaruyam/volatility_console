"""Instrument master — universe discovery and persistence.

Discovers option chains via the broker adapter and materializes the canonical
universe for a session date. Raw broker payloads are stored alongside the
normalized records for audit and lineage.

Storage layout (under ``data/instrument_master/``):

    dt=YYYY-MM-DD/
        underlying=SYMBOL/
            underlying.json     — UnderlyingContract record
            chain_params.json   — raw OptionChainParams from the broker
            chain.jsonl         — normalized OptionContract records, one per line

Critical rules applied here:
  - Never overwrite historical partitions (write to dt= partition = session date).
  - Store broker payloads alongside normalized records.
  - Replay uses the same code path as live (no separate historical logic).
  - Duplicates removed before any write; instrument_key is the dedup key.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from src.connectivity.state import BrokerAdapter, OptionChainParams
from src.universe.contracts import (
    OptionContract,
    UnderlyingContract,
    deduplicate_contracts,
    filter_by_delta_approx,
    filter_by_dte,
    filter_by_maturity_ladder,
    filter_by_strike_range,
    validate_option_contract,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniverseSpec:
    """One configured underlying from universe.yaml."""

    symbol: str
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    description: str | None = None


@dataclass(frozen=True)
class UniverseConfig:
    """Typed view of universe.yaml."""

    version: str
    underlyings: tuple[UniverseSpec, ...]
    min_dte: int = 1
    max_dte: int = 180
    strike_selection_mode: str = "all"  # all | range_pct | delta_based
    range_pct: float = 0.30
    day_count_convention: str = "act/365"
    # Maturity ladder (per professor): keep expiries close to these DTE targets.
    # Empty tuple = disabled (fall back to min_dte/max_dte window).
    maturity_ladder_days: tuple[int, ...] = ()
    maturity_ladder_tolerance_days: int = 5
    # Delta-based strike selection
    delta_range: tuple[float, float] = (-0.30, 0.30)
    delta_steps: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.30)
    approx_vol: float = 0.20  # flat-vol proxy for pre-surface delta approximation


def load_universe_config(config_dir: Path | str = Path("configs")) -> UniverseConfig:
    """Load and validate universe.yaml."""
    path = Path(config_dir) / "universe.yaml"
    if not path.exists():
        raise FileNotFoundError(f"universe.yaml not found at {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    underlyings = tuple(
        UniverseSpec(
            symbol=u["symbol"],
            sec_type=u.get("sec_type", "STK"),
            exchange=u.get("exchange", "SMART"),
            currency=u.get("currency", "USD"),
            description=u.get("description"),
        )
        for u in raw.get("underlyings", [])
    )
    opts = raw.get("options", {})
    maturity = opts.get("maturity_window", {})
    strike_sel = opts.get("strike_selection", {})

    ladder_raw = opts.get("maturity_ladder_days", [])
    maturity_ladder_days = tuple(int(d) for d in ladder_raw) if ladder_raw else ()

    delta_range_raw = strike_sel.get("delta_range", [-0.30, 0.30])
    delta_range = (float(delta_range_raw[0]), float(delta_range_raw[1]))

    delta_steps_raw = strike_sel.get("delta_steps", [0.10, 0.15, 0.20, 0.25, 0.30])
    delta_steps = tuple(float(d) for d in delta_steps_raw)

    return UniverseConfig(
        version=str(raw.get("version", "1.0")),
        underlyings=underlyings,
        min_dte=int(maturity.get("min_dte", 1)),
        max_dte=int(maturity.get("max_dte", 180)),
        strike_selection_mode=strike_sel.get("mode", "all"),
        range_pct=float(strike_sel.get("range_pct", 0.30)),
        day_count_convention=opts.get("day_count_convention", "act/365"),
        maturity_ladder_days=maturity_ladder_days,
        maturity_ladder_tolerance_days=int(opts.get("maturity_ladder_tolerance_days", 5)),
        delta_range=delta_range,
        delta_steps=delta_steps,
        approx_vol=float(strike_sel.get("approx_vol", 0.20)),
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class UniverseStore:
    """File-based instrument master storage.

    Partitioned by session date and underlying symbol. Each write is atomic at
    the file level (full overwrite). Historical partitions are never overwritten
    because each session date writes to its own ``dt=`` directory.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root) / "instrument_master"

    def _partition_dir(self, symbol: str, as_of_date: date) -> Path:
        return self.root / f"dt={as_of_date.isoformat()}" / f"underlying={symbol}"

    # ------------------------------------------------------------------
    # Underlying
    # ------------------------------------------------------------------

    def save_underlying(self, u: UnderlyingContract) -> None:
        if u.as_of_date is None:
            raise ValueError("UnderlyingContract.as_of_date is required for storage")
        d = self._partition_dir(u.symbol, u.as_of_date)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "underlying.json").open("w", encoding="utf-8") as fh:
            json.dump(u.to_dict(), fh, indent=2, default=str)
        log.debug("universe.store.underlying symbol=%s date=%s", u.symbol, u.as_of_date)

    def load_underlying(self, symbol: str, as_of_date: date) -> UnderlyingContract | None:
        path = self._partition_dir(symbol, as_of_date) / "underlying.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return UnderlyingContract.from_dict(json.load(fh))

    # ------------------------------------------------------------------
    # Option chain
    # ------------------------------------------------------------------

    def save_chain_params(
        self, symbol: str, as_of_date: date, params: list[OptionChainParams]
    ) -> None:
        """Persist the raw broker chain params for audit."""
        d = self._partition_dir(symbol, as_of_date)
        d.mkdir(parents=True, exist_ok=True)
        raw = [
            {
                "exchange": p.exchange,
                "trading_class": p.trading_class,
                "multiplier": p.multiplier,
                "expirations": list(p.expirations),
                "strikes": list(p.strikes),
            }
            for p in params
        ]
        with (d / "chain_params.json").open("w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2)

    def save_option_chain(
        self, symbol: str, as_of_date: date, contracts: list[OptionContract]
    ) -> None:
        d = self._partition_dir(symbol, as_of_date)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "chain.jsonl").open("w", encoding="utf-8") as fh:
            for c in contracts:
                fh.write(json.dumps(c.to_dict(), default=str) + "\n")
        log.debug(
            "universe.store.chain symbol=%s date=%s count=%d",
            symbol, as_of_date, len(contracts),
        )

    def load_option_chain(self, symbol: str, as_of_date: date) -> list[OptionContract]:
        path = self._partition_dir(symbol, as_of_date) / "chain.jsonl"
        if not path.exists():
            return []
        contracts: list[OptionContract] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    contracts.append(OptionContract.from_dict(json.loads(line)))
        return contracts

    def resolve_by_key(self, instrument_key: str, as_of_date: date) -> OptionContract | None:
        """Look up a stored contract by its instrument_key."""
        parts = instrument_key.split("|")
        if len(parts) < 1:
            return None
        symbol = parts[0]
        for c in self.load_option_chain(symbol, as_of_date):
            if c.instrument_key == instrument_key:
                return c
        return None

    def list_available_dates(self, symbol: str) -> list[date]:
        """Return all session dates for which data exists, sorted ascending."""
        dates: list[date] = []
        prefix = f"underlying={symbol}"
        if not self.root.exists():
            return dates
        for dt_dir in sorted(self.root.iterdir()):
            if not dt_dir.name.startswith("dt="):
                continue
            if (dt_dir / prefix / "chain.jsonl").exists():
                try:
                    dates.append(date.fromisoformat(dt_dir.name[3:]))
                except ValueError:
                    pass
        return dates


# ---------------------------------------------------------------------------
# Discovery functions
# ---------------------------------------------------------------------------


def get_underlying(
    symbol: str,
    adapter: BrokerAdapter,
    as_of_date: date,
    store: UniverseStore,
    exchange: str = "SMART",
    currency: str = "USD",
    sec_type: str = "STK",
) -> UnderlyingContract:
    """Resolve a symbol into a canonical UnderlyingContract and persist it.

    Saves the raw broker payload alongside the normalized record.
    """
    canonical = adapter.resolve_contract(
        underlying_symbol=symbol,
        sec_type=sec_type,
        exchange=exchange,
        currency=currency,
    )
    underlying = UnderlyingContract(
        symbol=canonical.underlying_symbol,
        sec_type=canonical.sec_type,
        exchange=canonical.exchange,
        currency=canonical.currency,
        broker_id=canonical.broker_id,
        broker_payload=canonical.broker_payload,
        as_of_date=as_of_date,
    )
    store.save_underlying(underlying)
    log.info(
        "universe.get_underlying symbol=%s broker_id=%s date=%s",
        symbol, underlying.broker_id, as_of_date,
    )
    return underlying


def get_option_chain(
    symbol: str,
    underlying: UnderlyingContract,
    as_of_date: date,
    adapter: BrokerAdapter,
    store: UniverseStore,
    config: UniverseConfig,
) -> list[OptionContract]:
    """Discover all listed option contracts for a symbol and persist them.

    Algorithm:
    1. Fetch OptionChainParams from broker (one API call per underlying).
    2. Build cartesian product: expiry × strike × {C, P} for each chain.
    3. Validate each contract; log and skip invalid entries.
    4. Deduplicate by instrument_key.
    5. Store raw params + normalized records.
    6. Return the deduplicated list.
    """
    params = adapter.request_option_chain_params(
        underlying_symbol=symbol,
        sec_type=underlying.sec_type,
        underlying_con_id=underlying.broker_id,
    )
    store.save_chain_params(symbol, as_of_date, params)

    contracts: list[OptionContract] = []
    skip_count = 0

    for chain in params:
        payload: dict[str, Any] = {
            "source": "chain_params",
            "exchange": chain.exchange,
            "trading_class": chain.trading_class,
        }
        for expiry_str in chain.expirations:
            try:
                expiry_date = datetime.strptime(expiry_str, "%Y%m%d").date()
            except ValueError:
                log.warning(
                    "universe.chain.bad_expiry symbol=%s expiry=%s", symbol, expiry_str
                )
                skip_count += 1
                continue

            for strike in chain.strikes:
                for right in ("C", "P"):
                    c = OptionContract(
                        underlying_symbol=symbol,
                        sec_type="OPT",
                        exchange=chain.exchange,
                        currency=underlying.currency,
                        expiry=expiry_date,
                        strike=strike,
                        right=right,
                        multiplier=chain.multiplier,
                        trading_class=chain.trading_class,
                        broker_payload=payload,
                        as_of_date=as_of_date,
                    )
                    errors = validate_option_contract(c)
                    if errors:
                        log.warning(
                            "universe.chain.invalid symbol=%s key=%s errors=%s",
                            symbol, c.instrument_key, errors,
                        )
                        skip_count += 1
                        continue
                    contracts.append(c)

    deduped = deduplicate_contracts(contracts)
    dup_count = len(contracts) - len(deduped)
    if dup_count:
        log.warning("universe.chain.duplicates_removed symbol=%s count=%d", symbol, dup_count)

    store.save_option_chain(symbol, as_of_date, deduped)
    log.info(
        "universe.get_option_chain symbol=%s date=%s total=%d skipped=%d",
        symbol, as_of_date, len(deduped), skip_count,
    )
    return deduped


def resolve_contract(
    instrument_key: str, as_of_date: date, store: UniverseStore
) -> OptionContract | None:
    """Look up a canonical contract by its instrument_key.

    Checks local storage only. Returns None if not found.
    The broker_id in the returned record enables the canonical key ↔ broker ID round-trip.
    """
    return store.resolve_by_key(instrument_key, as_of_date)


def load_active_universe(
    session_date: date,
    config: UniverseConfig,
    store: UniverseStore,
    spot_prices: dict[str, float] | None = None,
) -> list[OptionContract]:
    """Load the active option universe for a trading session.

    Reproducible: same (session_date, config) → same universe.
    Sources from stored chain data; applies DTE and optional strike filters.

    Parameters
    ----------
    spot_prices
        Optional {symbol: spot} dict. When provided and strike_selection_mode
        is ``range_pct``, strikes outside ±range_pct of spot are excluded.
    """
    active: list[OptionContract] = []

    for spec in config.underlyings:
        raw = store.load_option_chain(spec.symbol, session_date)
        if not raw:
            log.warning(
                "universe.load_active.missing symbol=%s date=%s",
                spec.symbol, session_date,
            )
            continue

        # Step 1: maturity filter — ladder takes priority over min/max DTE window
        if config.maturity_ladder_days:
            filtered = filter_by_maturity_ladder(
                raw, session_date,
                list(config.maturity_ladder_days),
                config.maturity_ladder_tolerance_days,
            )
        else:
            filtered = filter_by_dte(raw, session_date, config.min_dte, config.max_dte)

        # Step 2: strike filter
        spot = spot_prices.get(spec.symbol) if spot_prices else None
        if config.strike_selection_mode == "range_pct" and spot is not None:
            filtered = filter_by_strike_range(filtered, spot, config.range_pct)
        elif config.strike_selection_mode == "delta_based" and spot is not None:
            filtered = filter_by_delta_approx(
                filtered, spot, config.approx_vol,
                session_date, config.delta_range,
            )

        deduped = deduplicate_contracts(filtered)
        active.extend(deduped)
        log.info(
            "universe.load_active symbol=%s date=%s raw=%d after_filters=%d",
            spec.symbol, session_date, len(raw), len(deduped),
        )

    return active


def build_euro_stoxx_50_universe_specs() -> list[UniverseSpec]:
    """Return a UniverseSpec for each of the 50 Euro Stoxx 50 constituents.

    Symbols use the Yahoo Finance ticker format (e.g. "ADS.DE", "AI.PA").
    In production these map to IBKR STK contracts on their primary exchange.
    The MockAdapter resolves any symbol, so all 50 are testable without IBKR.
    """
    from src.historical.yfinance_loader import EURO_STOXX_50_TICKERS

    return [
        UniverseSpec(
            symbol=ticker,
            sec_type="STK",
            exchange="SMART",
            currency="EUR",
            description=f"Euro Stoxx 50 constituent ({ticker})",
        )
        for ticker in EURO_STOXX_50_TICKERS
    ]


def refresh_universe(
    session_date: date,
    config: UniverseConfig,
    adapter: BrokerAdapter,
    store: UniverseStore,
) -> dict[str, Any]:
    """Full universe refresh: discover all configured underlyings and their chains.

    Idempotent: re-running produces the same stored files (full overwrite per
    partition). Use the returned summary to detect partial failures.

    Returns
    -------
    dict
        ``{underlying_count, option_count, error_count, errors}``
    """
    underlying_count = 0
    option_count = 0
    error_count = 0
    errors: list[str] = []

    for spec in config.underlyings:
        try:
            underlying = get_underlying(
                symbol=spec.symbol,
                adapter=adapter,
                as_of_date=session_date,
                store=store,
                exchange=spec.exchange,
                currency=spec.currency,
                sec_type=spec.sec_type,
            )
            underlying_count += 1
        except Exception as exc:
            msg = f"{spec.symbol}: underlying resolution failed — {exc}"
            log.error("universe.refresh.underlying_error %s", msg)
            errors.append(msg)
            error_count += 1
            continue

        try:
            chain = get_option_chain(
                symbol=spec.symbol,
                underlying=underlying,
                as_of_date=session_date,
                adapter=adapter,
                store=store,
                config=config,
            )
            option_count += len(chain)
        except Exception as exc:
            msg = f"{spec.symbol}: chain discovery failed — {exc}"
            log.error("universe.refresh.chain_error %s", msg)
            errors.append(msg)
            error_count += 1

    summary: dict[str, Any] = {
        "session_date": session_date.isoformat(),
        "underlying_count": underlying_count,
        "option_count": option_count,
        "error_count": error_count,
        "errors": errors,
    }
    log.info(
        "universe.refresh.done date=%s underlyings=%d options=%d errors=%d",
        session_date, underlying_count, option_count, error_count,
    )
    return summary
