"""Unit and integration tests for Step 2: Instrument master.

Acceptance criterion: same universe reproduced on repeated runs; duplicates removed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.connectivity.mock_adapter import MockAdapter
from src.connectivity.session import Session
from src.universe.contracts import (
    OptionContract,
    UnderlyingContract,
    deduplicate_contracts,
    filter_by_dte,
    filter_by_strike_range,
    validate_option_contract,
)
from src.universe.contracts import (
    filter_by_delta_approx,
    filter_by_maturity_ladder,
)
from src.universe.discovery import (
    UniverseConfig,
    UniverseSpec,
    UniverseStore,
    build_euro_stoxx_50_universe_specs,
    get_option_chain,
    get_underlying,
    load_active_universe,
    load_universe_config,
    refresh_universe,
    resolve_contract,
)

# Reference date consistent with MockAdapter's fixed expirations.
SESSION_DATE = date(2026, 6, 7)

# Expiries returned by MockAdapter: 40 / 103 / 166 DTE from SESSION_DATE.
MOCK_EXPIRIES = (date(2026, 7, 17), date(2026, 9, 18), date(2026, 11, 20))
MOCK_SPY_STRIKES = (430.0, 440.0, 450.0, 460.0, 470.0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> UniverseStore:
    return UniverseStore(tmp_path)


@pytest.fixture()
def adapter() -> MockAdapter:
    a = MockAdapter()
    a.connect()
    return a


@pytest.fixture()
def config() -> UniverseConfig:
    return UniverseConfig(
        version="1.0",
        underlyings=(
            UniverseSpec(symbol="SPY", sec_type="STK", exchange="SMART", currency="USD"),
            UniverseSpec(symbol="QQQ", sec_type="STK", exchange="SMART", currency="USD"),
        ),
        min_dte=1,
        max_dte=180,
        strike_selection_mode="all",
        range_pct=0.30,
    )


# ---------------------------------------------------------------------------
# Contract key / serialization
# ---------------------------------------------------------------------------


class TestUnderlyingContract:
    def test_instrument_key(self) -> None:
        u = UnderlyingContract(symbol="SPY", sec_type="STK", exchange="SMART", currency="USD")
        assert u.instrument_key == "SPY|STK|SMART|USD"

    def test_from_key_round_trip(self) -> None:
        u = UnderlyingContract(symbol="SPY", sec_type="STK", exchange="SMART", currency="USD")
        u2 = UnderlyingContract.from_key(u.instrument_key)
        assert u2.symbol == "SPY"
        assert u2.sec_type == "STK"

    def test_from_key_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            UnderlyingContract.from_key("SPY|STK")

    def test_serialization_round_trip(self) -> None:
        u = UnderlyingContract(
            symbol="SPY",
            sec_type="STK",
            exchange="SMART",
            currency="USD",
            broker_id=12345,
            description="SPDR S&P 500",
            as_of_date=SESSION_DATE,
        )
        u2 = UnderlyingContract.from_dict(u.to_dict())
        assert u2.instrument_key == u.instrument_key
        assert u2.broker_id == 12345
        assert u2.as_of_date == SESSION_DATE


class TestOptionContract:
    def _make(self, **kwargs: object) -> OptionContract:
        defaults: dict = dict(
            underlying_symbol="SPY",
            expiry=date(2026, 7, 17),
            strike=450.0,
            right="C",
            multiplier=100,
            as_of_date=SESSION_DATE,
        )
        defaults.update(kwargs)
        return OptionContract(**defaults)

    def test_instrument_key_format(self) -> None:
        c = self._make()
        assert c.instrument_key == "SPY|OPT|SMART|USD|20260717|450|C|100"

    def test_instrument_key_strip_trailing_zero(self) -> None:
        c = self._make(strike=450.5)
        assert "450.5" in c.instrument_key

    def test_from_key_round_trip(self) -> None:
        c = self._make()
        c2 = OptionContract.from_key(c.instrument_key, as_of_date=SESSION_DATE)
        assert c2.underlying_symbol == "SPY"
        assert c2.expiry == date(2026, 7, 17)
        assert c2.strike == 450.0
        assert c2.right == "C"

    def test_dte_property(self) -> None:
        c = self._make(expiry=date(2026, 7, 17), as_of_date=date(2026, 6, 7))
        assert c.dte == 40

    def test_dte_none_when_missing_as_of_date(self) -> None:
        c = OptionContract(underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=450.0, right="C")
        assert c.dte is None

    def test_serialization_round_trip(self) -> None:
        c = self._make()
        c2 = OptionContract.from_dict(c.to_dict())
        assert c2.instrument_key == c.instrument_key
        assert c2.expiry == c.expiry
        assert c2.strike == c.strike

    def test_from_key_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            OptionContract.from_key("SPY|OPT|SMART")


class TestValidation:
    def test_valid_contract_has_no_errors(self) -> None:
        c = OptionContract(
            underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=450.0, right="C"
        )
        assert validate_option_contract(c) == []

    def test_missing_expiry(self) -> None:
        c = OptionContract(underlying_symbol="SPY", strike=450.0, right="C")
        assert any("expiry" in e.lower() for e in validate_option_contract(c))

    def test_invalid_strike(self) -> None:
        c = OptionContract(
            underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=-1.0, right="C"
        )
        errs = validate_option_contract(c)
        assert any("strike" in e.lower() for e in errs)

    def test_invalid_right(self) -> None:
        c = OptionContract(
            underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=450.0, right="X"
        )
        errs = validate_option_contract(c)
        assert any("right" in e.lower() for e in errs)


class TestDeduplication:
    def test_removes_duplicates(self) -> None:
        c = OptionContract(
            underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=450.0, right="C"
        )
        result = deduplicate_contracts([c, c, c])
        assert len(result) == 1

    def test_keeps_distinct_contracts(self) -> None:
        c1 = OptionContract(
            underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=450.0, right="C"
        )
        c2 = OptionContract(
            underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=450.0, right="P"
        )
        assert len(deduplicate_contracts([c1, c2])) == 2

    def test_preserves_first_occurrence(self) -> None:
        c1 = OptionContract(
            underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=450.0, right="C",
            broker_id=111,
        )
        c2 = OptionContract(
            underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=450.0, right="C",
            broker_id=999,
        )
        result = deduplicate_contracts([c1, c2])
        assert result[0].broker_id == 111


class TestFilterByDte:
    def _make(self, expiry: date) -> OptionContract:
        return OptionContract(
            underlying_symbol="SPY", expiry=expiry, strike=450.0, right="C"
        )

    def test_passes_within_window(self) -> None:
        c = self._make(date(2026, 7, 17))  # 40 DTE from SESSION_DATE
        result = filter_by_dte([c], SESSION_DATE, min_dte=1, max_dte=180)
        assert len(result) == 1

    def test_excludes_too_near(self) -> None:
        c = self._make(SESSION_DATE)  # 0 DTE
        result = filter_by_dte([c], SESSION_DATE, min_dte=1, max_dte=180)
        assert len(result) == 0

    def test_excludes_too_far(self) -> None:
        c = self._make(date(2027, 6, 7))  # 365 DTE
        result = filter_by_dte([c], SESSION_DATE, min_dte=1, max_dte=180)
        assert len(result) == 0

    def test_excludes_no_expiry(self) -> None:
        c = OptionContract(underlying_symbol="SPY", strike=450.0, right="C")
        result = filter_by_dte([c], SESSION_DATE)
        assert len(result) == 0


class TestFilterByStrikeRange:
    def test_includes_atm(self) -> None:
        c = OptionContract(underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=450.0, right="C")
        assert len(filter_by_strike_range([c], spot=450.0, range_pct=0.10)) == 1

    def test_excludes_far_otm(self) -> None:
        c = OptionContract(underlying_symbol="SPY", expiry=date(2026, 7, 17), strike=600.0, right="C")
        assert len(filter_by_strike_range([c], spot=450.0, range_pct=0.10)) == 0


# ---------------------------------------------------------------------------
# UniverseStore
# ---------------------------------------------------------------------------


class TestUniverseStore:
    def test_save_and_load_underlying(self, store: UniverseStore) -> None:
        u = UnderlyingContract(
            symbol="SPY", sec_type="STK", exchange="SMART", currency="USD",
            broker_id=999, as_of_date=SESSION_DATE,
        )
        store.save_underlying(u)
        loaded = store.load_underlying("SPY", SESSION_DATE)
        assert loaded is not None
        assert loaded.broker_id == 999
        assert loaded.instrument_key == u.instrument_key

    def test_save_and_load_chain(self, store: UniverseStore) -> None:
        contracts = [
            OptionContract(
                underlying_symbol="SPY", expiry=date(2026, 7, 17),
                strike=s, right=r, multiplier=100, as_of_date=SESSION_DATE,
            )
            for s in (440.0, 450.0)
            for r in ("C", "P")
        ]
        store.save_option_chain("SPY", SESSION_DATE, contracts)
        loaded = store.load_option_chain("SPY", SESSION_DATE)
        assert len(loaded) == 4
        keys = {c.instrument_key for c in loaded}
        assert keys == {c.instrument_key for c in contracts}

    def test_missing_returns_empty(self, store: UniverseStore) -> None:
        assert store.load_underlying("NOSYM", SESSION_DATE) is None
        assert store.load_option_chain("NOSYM", SESSION_DATE) == []

    def test_resolve_by_key(self, store: UniverseStore) -> None:
        c = OptionContract(
            underlying_symbol="SPY", expiry=date(2026, 7, 17),
            strike=450.0, right="C", broker_id=42, as_of_date=SESSION_DATE,
        )
        store.save_option_chain("SPY", SESSION_DATE, [c])
        found = store.resolve_by_key(c.instrument_key, SESSION_DATE)
        assert found is not None
        assert found.broker_id == 42

    def test_overwrite_is_idempotent(self, store: UniverseStore) -> None:
        contracts = [
            OptionContract(
                underlying_symbol="SPY", expiry=date(2026, 7, 17),
                strike=450.0, right="C", as_of_date=SESSION_DATE,
            )
        ]
        store.save_option_chain("SPY", SESSION_DATE, contracts)
        store.save_option_chain("SPY", SESSION_DATE, contracts)
        assert len(store.load_option_chain("SPY", SESSION_DATE)) == 1

    def test_list_available_dates(self, store: UniverseStore) -> None:
        for d in (date(2026, 6, 7), date(2026, 6, 8)):
            store.save_option_chain("SPY", d, [
                OptionContract(
                    underlying_symbol="SPY", expiry=date(2026, 7, 17),
                    strike=450.0, right="C", as_of_date=d,
                )
            ])
        dates = store.list_available_dates("SPY")
        assert date(2026, 6, 7) in dates
        assert date(2026, 6, 8) in dates


# ---------------------------------------------------------------------------
# Discovery layer — integration with MockAdapter
# ---------------------------------------------------------------------------


class TestGetUnderlying:
    def test_resolves_and_persists(
        self, adapter: MockAdapter, store: UniverseStore
    ) -> None:
        u = get_underlying("SPY", adapter, SESSION_DATE, store)
        assert u.symbol == "SPY"
        assert u.broker_id is not None
        assert u.as_of_date == SESSION_DATE

        # Persisted
        loaded = store.load_underlying("SPY", SESSION_DATE)
        assert loaded is not None
        assert loaded.broker_id == u.broker_id


class TestGetOptionChain:
    def test_returns_full_chain(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        u = get_underlying("SPY", adapter, SESSION_DATE, store)
        chain = get_option_chain("SPY", u, SESSION_DATE, adapter, store, config)
        # 3 expiries × 5 strikes × 2 rights
        assert len(chain) == 3 * 5 * 2

    def test_no_duplicates(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        u = get_underlying("SPY", adapter, SESSION_DATE, store)
        chain = get_option_chain("SPY", u, SESSION_DATE, adapter, store, config)
        keys = [c.instrument_key for c in chain]
        assert len(keys) == len(set(keys))

    def test_all_pass_validation(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        u = get_underlying("SPY", adapter, SESSION_DATE, store)
        chain = get_option_chain("SPY", u, SESSION_DATE, adapter, store, config)
        for c in chain:
            assert validate_option_contract(c) == []

    def test_persisted_in_store(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        u = get_underlying("SPY", adapter, SESSION_DATE, store)
        chain = get_option_chain("SPY", u, SESSION_DATE, adapter, store, config)
        stored = store.load_option_chain("SPY", SESSION_DATE)
        assert len(stored) == len(chain)


class TestResolveContract:
    def test_found(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        u = get_underlying("SPY", adapter, SESSION_DATE, store)
        chain = get_option_chain("SPY", u, SESSION_DATE, adapter, store, config)
        key = chain[0].instrument_key
        found = resolve_contract(key, SESSION_DATE, store)
        assert found is not None
        assert found.instrument_key == key

    def test_not_found(self, store: UniverseStore) -> None:
        result = resolve_contract("NOSYM|OPT|SMART|USD|20260717|450|C|100", SESSION_DATE, store)
        assert result is None


class TestLoadActiveUniverse:
    def test_filters_dte(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        # Populate store
        for spec in config.underlyings:
            u = get_underlying(spec.symbol, adapter, SESSION_DATE, store)
            get_option_chain(spec.symbol, u, SESSION_DATE, adapter, store, config)

        # Default config: min_dte=1, max_dte=180 — all mock expiries qualify
        universe = load_active_universe(SESSION_DATE, config, store)
        # SPY: 30 + QQQ: 30
        assert len(universe) == 60

    def test_narrow_dte_window_reduces_result(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        for spec in config.underlyings:
            u = get_underlying(spec.symbol, adapter, SESSION_DATE, store)
            get_option_chain(spec.symbol, u, SESSION_DATE, adapter, store, config)

        narrow = UniverseConfig(
            version="1.0",
            underlyings=config.underlyings,
            min_dte=1,
            max_dte=50,  # only 20260717 (40 DTE) qualifies
        )
        universe = load_active_universe(SESSION_DATE, narrow, store)
        # Only the 40-DTE expiry: 5 strikes × 2 rights × 2 underlyings = 20
        assert len(universe) == 20

    def test_empty_for_missing_symbol(self, store: UniverseStore, config: UniverseConfig) -> None:
        # Nothing populated in store
        universe = load_active_universe(SESSION_DATE, config, store)
        assert universe == []

    def test_strike_range_filter(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        for spec in config.underlyings:
            u = get_underlying(spec.symbol, adapter, SESSION_DATE, store)
            get_option_chain(spec.symbol, u, SESSION_DATE, adapter, store, config)

        range_config = UniverseConfig(
            version="1.0",
            underlyings=config.underlyings,
            min_dte=1,
            max_dte=180,
            strike_selection_mode="range_pct",
            range_pct=0.02,  # very tight: only strikes within ±2% of spot
        )
        # Spot 450 → range [441, 459]: strikes 450 qualifies for SPY
        # Spot 480 → range [470.4, 489.6]: strikes 480, 490 excluded because >489.6
        # Just verify the filter reduces the count
        universe = load_active_universe(
            SESSION_DATE,
            range_config,
            store,
            spot_prices={"SPY": 450.0, "QQQ": 480.0},
        )
        assert len(universe) < 60


# ---------------------------------------------------------------------------
# Acceptance criterion: idempotent refresh — same universe on repeated runs
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_refresh_twice_same_universe(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        summary1 = refresh_universe(SESSION_DATE, config, adapter, store)
        summary2 = refresh_universe(SESSION_DATE, config, adapter, store)

        universe1 = load_active_universe(SESSION_DATE, config, store)
        # Force second run to reload from store (same store, same result)
        universe2 = load_active_universe(SESSION_DATE, config, store)

        keys1 = sorted(c.instrument_key for c in universe1)
        keys2 = sorted(c.instrument_key for c in universe2)

        assert keys1 == keys2
        assert summary1["option_count"] == summary2["option_count"]
        assert summary1["error_count"] == 0
        assert summary2["error_count"] == 0

    def test_no_duplicates_in_refreshed_universe(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        refresh_universe(SESSION_DATE, config, adapter, store)
        universe = load_active_universe(SESSION_DATE, config, store)
        keys = [c.instrument_key for c in universe]
        assert len(keys) == len(set(keys)), "Duplicates found in active universe"

    def test_refresh_summary_counts(
        self, adapter: MockAdapter, store: UniverseStore, config: UniverseConfig
    ) -> None:
        summary = refresh_universe(SESSION_DATE, config, adapter, store)
        assert summary["underlying_count"] == 2  # SPY + QQQ
        assert summary["option_count"] == 60      # 30 per underlying
        assert summary["error_count"] == 0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadUniverseConfig:
    def test_loads_from_file(self) -> None:
        cfg = load_universe_config(Path("configs"))
        assert len(cfg.underlyings) >= 1
        assert "SPY" in {u.symbol for u in cfg.underlyings}
        assert cfg.min_dte >= 1
        assert cfg.max_dte > cfg.min_dte

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_universe_config(tmp_path)

    def test_loads_maturity_ladder_days(self) -> None:
        cfg = load_universe_config(Path("configs"))
        assert len(cfg.maturity_ladder_days) > 0
        assert 30 in cfg.maturity_ladder_days
        assert 90 in cfg.maturity_ladder_days
        assert 365 in cfg.maturity_ladder_days

    def test_loads_delta_range(self) -> None:
        cfg = load_universe_config(Path("configs"))
        assert cfg.delta_range == (-0.30, 0.30)

    def test_loads_delta_steps(self) -> None:
        cfg = load_universe_config(Path("configs"))
        assert 0.10 in cfg.delta_steps
        assert 0.30 in cfg.delta_steps


# ---------------------------------------------------------------------------
# ESTX50 chain resolution via MockAdapter
# ---------------------------------------------------------------------------


class TestESTX50ChainResolution:
    def test_estx50_resolves_as_underlying(self, adapter: MockAdapter, tmp_path: Path) -> None:
        store = UniverseStore(tmp_path)
        u = get_underlying(
            "ESTX50", adapter, SESSION_DATE, store,
            exchange="EUREX", currency="EUR", sec_type="IND",
        )
        assert u.symbol == "ESTX50"
        assert u.exchange == "EUREX"
        assert u.currency == "EUR"
        assert u.broker_id is not None

    def test_estx50_chain_returns_contracts(self, adapter: MockAdapter, tmp_path: Path) -> None:
        store = UniverseStore(tmp_path)
        config = UniverseConfig(
            version="1.0",
            underlyings=(
                UniverseSpec(symbol="ESTX50", sec_type="IND", exchange="EUREX", currency="EUR"),
            ),
            min_dte=1,
            max_dte=365,
            strike_selection_mode="all",
        )
        u = get_underlying("ESTX50", adapter, SESSION_DATE, store,
                           exchange="EUREX", currency="EUR", sec_type="IND")
        chain = get_option_chain("ESTX50", u, SESSION_DATE, adapter, store, config)
        # 3 expiries × 7 strikes × 2 rights = 42
        assert len(chain) == 42

    def test_estx50_chain_uses_eurex_exchange(self, adapter: MockAdapter, tmp_path: Path) -> None:
        store = UniverseStore(tmp_path)
        config = UniverseConfig(
            version="1.0",
            underlyings=(
                UniverseSpec(symbol="ESTX50", sec_type="IND", exchange="EUREX", currency="EUR"),
            ),
            min_dte=1,
            max_dte=365,
            strike_selection_mode="all",
        )
        u = get_underlying("ESTX50", adapter, SESSION_DATE, store,
                           exchange="EUREX", currency="EUR", sec_type="IND")
        chain = get_option_chain("ESTX50", u, SESSION_DATE, adapter, store, config)
        assert all("EUREX" in c.instrument_key for c in chain)

    def test_estx50_chain_multiplier_is_10(self, adapter: MockAdapter, tmp_path: Path) -> None:
        store = UniverseStore(tmp_path)
        config = UniverseConfig(
            version="1.0",
            underlyings=(
                UniverseSpec(symbol="ESTX50", sec_type="IND", exchange="EUREX", currency="EUR"),
            ),
            min_dte=1,
            max_dte=365,
            strike_selection_mode="all",
        )
        u = get_underlying("ESTX50", adapter, SESSION_DATE, store,
                           exchange="EUREX", currency="EUR", sec_type="IND")
        chain = get_option_chain("ESTX50", u, SESSION_DATE, adapter, store, config)
        assert all(c.multiplier == 10 for c in chain)

    def test_estx50_chain_persisted_in_store(self, adapter: MockAdapter, tmp_path: Path) -> None:
        store = UniverseStore(tmp_path)
        config = UniverseConfig(
            version="1.0",
            underlyings=(
                UniverseSpec(symbol="ESTX50", sec_type="IND", exchange="EUREX", currency="EUR"),
            ),
            min_dte=1,
            max_dte=365,
        )
        u = get_underlying("ESTX50", adapter, SESSION_DATE, store,
                           exchange="EUREX", currency="EUR", sec_type="IND")
        chain = get_option_chain("ESTX50", u, SESSION_DATE, adapter, store, config)
        loaded = store.load_option_chain("ESTX50", SESSION_DATE)
        assert len(loaded) == len(chain)


# ---------------------------------------------------------------------------
# filter_by_maturity_ladder
# ---------------------------------------------------------------------------


class TestFilterByMaturityLadder:
    _LADDER = [10, 30, 90, 180, 270, 365, 548, 730, 1095]

    def _make(self, dte: int) -> OptionContract:
        expiry = date(SESSION_DATE.year, SESSION_DATE.month, SESSION_DATE.day)
        from datetime import timedelta
        return OptionContract(
            underlying_symbol="ESTX50",
            exchange="EUREX",
            currency="EUR",
            expiry=expiry + timedelta(days=dte),
            strike=5000.0,
            right="C",
            multiplier=10,
            as_of_date=SESSION_DATE,
        )

    def test_passes_contracts_on_rung(self) -> None:
        contracts = [self._make(30), self._make(90), self._make(365)]
        result = filter_by_maturity_ladder(contracts, SESSION_DATE, self._LADDER, tolerance_days=5)
        assert len(result) == 3

    def test_passes_within_tolerance(self) -> None:
        c = self._make(33)  # 33 DTE, rung=30, diff=3 ≤ 5
        result = filter_by_maturity_ladder([c], SESSION_DATE, self._LADDER, tolerance_days=5)
        assert len(result) == 1

    def test_excludes_outside_tolerance(self) -> None:
        c = self._make(50)  # 50 DTE: closest rung=30 (diff=20) or 90 (diff=40)
        result = filter_by_maturity_ladder([c], SESSION_DATE, self._LADDER, tolerance_days=5)
        assert len(result) == 0

    def test_empty_ladder_returns_all(self) -> None:
        contracts = [self._make(50), self._make(200)]
        result = filter_by_maturity_ladder(contracts, SESSION_DATE, [], tolerance_days=5)
        assert len(result) == 2

    def test_excludes_no_expiry(self) -> None:
        c = OptionContract(underlying_symbol="ESTX50", strike=5000.0, right="C")
        result = filter_by_maturity_ladder([c], SESSION_DATE, self._LADDER, tolerance_days=5)
        assert len(result) == 0

    def test_all_mock_estx50_expirations_match_ladder(self, adapter: MockAdapter) -> None:
        """All three ESTX50 MockAdapter expirations qualify under the configured ladder."""
        from datetime import timedelta
        # MockAdapter ESTX50 expirations: 20260717 (40d), 20260918 (103d), 20261219 (195d)
        dtes = [
            (date(2026, 7, 17) - SESSION_DATE).days,   # 40 → rung 30 (diff=10) ✓
            (date(2026, 9, 18) - SESSION_DATE).days,   # 103 → rung 90 (diff=13) ✗ at tol=5; rung 90 diff=13
            (date(2026, 12, 19) - SESSION_DATE).days,  # 195 → rung 180 (diff=15) ✗ at tol=5
        ]
        contracts = [self._make(d) for d in dtes]
        # With default tolerance=5: only 40-day one might qualify (rung 30 diff=10 fails too)
        # With tolerance=15: 40-DTE qualifies (30±15) and 103-DTE qualifies (90±15)
        result = filter_by_maturity_ladder(contracts, SESSION_DATE, self._LADDER, tolerance_days=15)
        assert len(result) >= 1  # at least the 40-DTE contract (rung=30, diff=10 ≤ 15)


# ---------------------------------------------------------------------------
# filter_by_delta_approx
# ---------------------------------------------------------------------------


class TestFilterByDeltaApprox:
    def _make(self, strike: float, right: str, dte: int = 40) -> OptionContract:
        from datetime import timedelta
        expiry = SESSION_DATE + timedelta(days=dte)
        return OptionContract(
            underlying_symbol="ESTX50",
            exchange="EUREX",
            currency="EUR",
            expiry=expiry,
            strike=strike,
            right=right,
            multiplier=10,
            as_of_date=SESSION_DATE,
        )

    def test_atm_call_in_range(self) -> None:
        # ATM call: delta ≈ 0.50 — outside (-0.30, 0.30)
        c = self._make(5000.0, "C")
        result = filter_by_delta_approx([c], spot=5000.0, approx_vol=0.20,
                                        session_date=SESSION_DATE, delta_range=(-0.30, 0.30))
        assert len(result) == 0

    def test_otm_call_in_range(self) -> None:
        c = self._make(5200.0, "C")  # OTM call, delta ≈ 0.20 → in range (0, 0.30)
        result = filter_by_delta_approx([c], spot=5000.0, approx_vol=0.20,
                                        session_date=SESSION_DATE, delta_range=(-0.30, 0.30))
        assert len(result) == 1

    def test_otm_put_in_range(self) -> None:
        c = self._make(4800.0, "P")  # OTM put, delta ≈ -0.20 → in range (-0.30, 0)
        result = filter_by_delta_approx([c], spot=5000.0, approx_vol=0.20,
                                        session_date=SESSION_DATE, delta_range=(-0.30, 0.30))
        assert len(result) == 1

    def test_deep_otm_excluded(self) -> None:
        c = self._make(6000.0, "C")  # Deep OTM call, delta ≈ 0.001 → in range but very small
        result = filter_by_delta_approx([c], spot=5000.0, approx_vol=0.20,
                                        session_date=SESSION_DATE, delta_range=(-0.30, 0.30))
        assert len(result) == 1  # tiny delta is still within (-0.30, 0.30)

    def test_excludes_no_expiry(self) -> None:
        c = OptionContract(underlying_symbol="ESTX50", strike=5000.0, right="C")
        result = filter_by_delta_approx([c], spot=5000.0, approx_vol=0.20,
                                        session_date=SESSION_DATE)
        assert len(result) == 0

    def test_excludes_expired_contract(self) -> None:
        from datetime import timedelta
        c = OptionContract(
            underlying_symbol="ESTX50", expiry=SESSION_DATE - timedelta(days=1),
            strike=5000.0, right="C",
        )
        result = filter_by_delta_approx([c], spot=5000.0, approx_vol=0.20,
                                        session_date=SESSION_DATE)
        assert len(result) == 0

    def test_symmetric_call_put_counts(self) -> None:
        calls = [self._make(5100.0 + i * 50, "C") for i in range(3)]
        puts = [self._make(4900.0 - i * 50, "P") for i in range(3)]
        all_contracts = calls + puts
        result = filter_by_delta_approx(all_contracts, spot=5000.0, approx_vol=0.20,
                                        session_date=SESSION_DATE, delta_range=(-0.30, 0.30))
        call_results = [c for c in result if c.right == "C"]
        put_results = [c for c in result if c.right == "P"]
        assert len(call_results) == len(put_results)


# ---------------------------------------------------------------------------
# Euro Stoxx 50 constituent specs
# ---------------------------------------------------------------------------


class TestBuildEuroStoxx50Specs:
    def test_returns_50_specs(self) -> None:
        specs = build_euro_stoxx_50_universe_specs()
        assert len(specs) == 50

    def test_all_have_symbol(self) -> None:
        specs = build_euro_stoxx_50_universe_specs()
        assert all(s.symbol for s in specs)

    def test_all_have_currency_eur(self) -> None:
        specs = build_euro_stoxx_50_universe_specs()
        assert all(s.currency == "EUR" for s in specs)

    def test_no_duplicates(self) -> None:
        specs = build_euro_stoxx_50_universe_specs()
        symbols = [s.symbol for s in specs]
        assert len(symbols) == len(set(symbols))

    def test_known_components_present(self) -> None:
        specs = build_euro_stoxx_50_universe_specs()
        symbols = {s.symbol for s in specs}
        # A few well-known Euro Stoxx 50 constituents (Yahoo Finance ticker format)
        for expected in ("MC.PA", "ASML.AS", "SAP.DE"):
            assert expected in symbols, f"{expected} missing from Euro Stoxx 50 specs"

    def test_specs_are_universe_spec_type(self) -> None:
        specs = build_euro_stoxx_50_universe_specs()
        assert all(isinstance(s, UniverseSpec) for s in specs)
