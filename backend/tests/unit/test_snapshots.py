"""Unit tests for snapshot builder.

Acceptance criterion: Same raw events + params → identical snapshots on re-run.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from src.collectors.raw_collector import RawEvent
from src.snapshots.builder import (
    _derive_state_flags,
    _latest_by_field_before,
    build_option_row,
    build_snapshot,
    choose_reference_spot,
    _build_option_rows,
)
from src.snapshots.models import MarketStateSnapshot, OptionRow, UnderlyingState

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SNAP_TS = 1_000.0
CONFIG = {"max_spread_pct_for_mid": 0.05}

OPT_KEY = "SPY|OPT|SMART|USD|20261219|450|C|100"
OPT_KEY_PUT = "SPY|OPT|SMART|USD|20261219|450|P|100"
OPT_KEY_DEC_PUT = "SPY|OPT|SMART|USD|20261219|440|P|100"
UNDERLYING_KEY = "SPY|STK|SMART|USD"

SNAPSHOT_DATE = date(2026, 1, 2)  # 2026-01-02 → maturity in ~11.5 months

BASE_CONFIG = {
    "underlying_symbol": "SPY",
    "underlying_key": UNDERLYING_KEY,
    "max_underlying_age_seconds": 30,
    "max_option_age_seconds": 60,
    "max_spread_pct_for_mid": 0.05,
    "session_open": True,
    "snapshot_date": SNAPSHOT_DATE,
}


def _evt(key: str, field: str, value: float, ts: float,
          source: str = "live") -> RawEvent:
    return RawEvent(
        session_id="sess",
        event_id=uuid.uuid4().hex,
        instrument_key=key,
        field_name=field,
        field_value=value,
        exchange_ts=None,
        receipt_ts=ts,
        source=source,
    )


def _underlying_state(**overrides) -> UnderlyingState:
    base = dict(
        instrument_key=UNDERLYING_KEY,
        snapshot_ts=SNAP_TS,
        bid=449.5,
        ask=450.5,
        last=450.0,
        volume=5_000_000.0,
        reference_spot=450.0,
        reference_type="mid",
        spread_pct=0.002,
        is_market_open=True,
        is_stale=False,
        quote_age_seconds=5.0,
    )
    base.update(overrides)
    return UnderlyingState(**base)


# ---------------------------------------------------------------------------
# choose_reference_spot (already partially tested — kept + extended)
# ---------------------------------------------------------------------------


class TestReferenceSpot:

    def test_tight_spread_uses_mid(self):
        spot, ref_type = choose_reference_spot(bid=99.5, ask=100.5, last=100.0, config=CONFIG)
        assert ref_type == "mid"
        assert abs(spot - 100.0) < 1e-8

    def test_mid_formula_correct(self):
        spot, ref_type = choose_reference_spot(bid=100.0, ask=102.0, last=101.0, config=CONFIG)
        assert ref_type == "mid"
        assert abs(spot - 101.0) < 1e-8

    def test_wide_spread_falls_back_to_last(self):
        config = {"max_spread_pct_for_mid": 0.01}
        spot, ref_type = choose_reference_spot(bid=90.0, ask=110.0, last=100.5, config=config)
        assert ref_type == "last"
        assert spot == 100.5

    def test_no_bid_falls_back_to_last(self):
        spot, ref_type = choose_reference_spot(bid=None, ask=100.5, last=100.0, config=CONFIG)
        assert ref_type == "last"

    def test_no_data_raises_with_no_fallback(self):
        with pytest.raises(ValueError, match="No valid reference spot"):
            choose_reference_spot(bid=None, ask=None, last=None, config=CONFIG)

    def test_close_fallback_used(self):
        config = {**CONFIG, "prior_close": 99.0}
        spot, ref_type = choose_reference_spot(bid=None, ask=None, last=None, config=config)
        assert ref_type == "close"
        assert spot == 99.0

    def test_carry_forward_used_last(self):
        config = {**CONFIG, "carry_forward_spot": 98.5}
        spot, ref_type = choose_reference_spot(bid=None, ask=None, last=None, config=config)
        assert ref_type == "carry_forward"
        assert spot == 98.5

    def test_negative_last_skipped(self):
        config = {**CONFIG, "prior_close": 99.0}
        spot, ref_type = choose_reference_spot(bid=None, ask=None, last=-5.0, config=config)
        assert ref_type == "close"

    def test_crossed_market_skips_mid(self):
        spot, ref_type = choose_reference_spot(bid=101.0, ask=99.0, last=100.0, config=CONFIG)
        assert ref_type == "last"


# ---------------------------------------------------------------------------
# _latest_by_field_before
# ---------------------------------------------------------------------------


class TestLatestByField:

    def test_returns_latest_event_per_field(self):
        events = [
            _evt("SPY", "bid", 100.0, 1.0),
            _evt("SPY", "bid", 101.0, 2.0),
            _evt("SPY", "ask", 101.5, 1.5),
        ]
        result = _latest_by_field_before(events, cutoff_ts=3.0)
        assert result["SPY"]["bid"].field_value == 101.0
        assert result["SPY"]["ask"].field_value == 101.5

    def test_respects_cutoff_timestamp(self):
        events = [
            _evt("SPY", "bid", 100.0, 1.0),
            _evt("SPY", "bid", 105.0, 5.0),
        ]
        result = _latest_by_field_before(events, cutoff_ts=3.0)
        assert result["SPY"]["bid"].field_value == 100.0

    def test_event_exactly_at_cutoff_included(self):
        events = [_evt("SPY", "bid", 100.0, 3.0)]
        result = _latest_by_field_before(events, cutoff_ts=3.0)
        assert result["SPY"]["bid"].field_value == 100.0

    def test_multiple_instruments(self):
        events = [
            _evt("SPY", "bid", 450.0, 1.0),
            _evt("QQQ", "bid", 350.0, 1.0),
        ]
        result = _latest_by_field_before(events, cutoff_ts=2.0)
        assert "SPY" in result
        assert "QQQ" in result

    def test_empty_events_returns_empty(self):
        result = _latest_by_field_before([], cutoff_ts=10.0)
        assert result == {}


# ---------------------------------------------------------------------------
# build_option_row
# ---------------------------------------------------------------------------


class TestBuildOptionRow:

    def _fields(self, bid=14.5, ask=15.5, ts=990.0) -> dict:
        return {
            "bid": _evt(OPT_KEY, "bid", bid, ts),
            "ask": _evt(OPT_KEY, "ask", ask, ts),
        }

    def test_basic_construction(self):
        row = build_option_row(OPT_KEY, self._fields(), SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert row.underlying_symbol == "SPY"
        assert row.expiry_str == "2026-12-19"
        assert row.strike == 450.0
        assert row.option_right == "C"
        assert row.multiplier == 100.0

    def test_mid_computed_from_bid_ask(self):
        row = build_option_row(OPT_KEY, self._fields(bid=14.0, ask=16.0), SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert abs(row.mid - 15.0) < 1e-8

    def test_mid_none_when_crossed(self):
        fields = {
            "bid": _evt(OPT_KEY, "bid", 16.0, 990.0),
            "ask": _evt(OPT_KEY, "ask", 14.0, 990.0),
        }
        row = build_option_row(OPT_KEY, fields, SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert row.mid is None

    def test_mid_none_when_only_bid(self):
        fields = {"bid": _evt(OPT_KEY, "bid", 14.5, 990.0)}
        row = build_option_row(OPT_KEY, fields, SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert row.mid is None

    def test_no_data_row_is_valid(self):
        row = build_option_row(OPT_KEY, {}, SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert row.bid is None
        assert row.ask is None
        assert row.mid is None
        assert row.is_stale is True

    def test_fresh_quote_not_stale(self):
        row = build_option_row(OPT_KEY, self._fields(ts=999.0), SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert row.is_stale is False
        assert abs(row.quote_age_seconds - 1.0) < 1e-6

    def test_old_quote_is_stale(self):
        config = {**BASE_CONFIG, "max_option_age_seconds": 5}
        row = build_option_row(OPT_KEY, self._fields(ts=900.0), SNAP_TS, config)
        assert row is not None
        assert row.is_stale is True

    def test_maturity_years_positive(self):
        row = build_option_row(OPT_KEY, self._fields(), SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert row.maturity_years is not None
        assert row.maturity_years > 0

    def test_maturity_years_approx_correct(self):
        # snapshot_date=2026-01-02, expiry=2026-12-19 ≈ 351 days / 365 ≈ 0.961
        row = build_option_row(OPT_KEY, self._fields(), SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert abs(row.maturity_years - 351 / 365) < 0.005

    def test_expired_option_maturity_is_none(self):
        expired_key = "SPY|OPT|SMART|USD|20250101|450|C|100"
        config = {**BASE_CONFIG, "snapshot_date": date(2026, 1, 2)}
        row = build_option_row(expired_key, {}, SNAP_TS, config)
        assert row is not None
        assert row.maturity_years is None

    def test_invalid_key_returns_none(self):
        row = build_option_row("BADKEY", {}, SNAP_TS, BASE_CONFIG)
        assert row is None

    def test_spread_pct_computed(self):
        row = build_option_row(OPT_KEY, self._fields(bid=14.0, ask=16.0), SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert row.spread_pct is not None
        assert abs(row.spread_pct - 2.0 / 15.0) < 1e-8

    def test_put_key_parsed_correctly(self):
        fields = {"bid": _evt(OPT_KEY_PUT, "bid", 13.0, 990.0)}
        row = build_option_row(OPT_KEY_PUT, fields, SNAP_TS, BASE_CONFIG)
        assert row is not None
        assert row.option_right == "P"
        assert row.strike == 450.0

    def test_source_replay_produces_identical_row(self):
        fields_live = {"bid": _evt(OPT_KEY, "bid", 14.5, 990.0, source="live")}
        fields_replay = {"bid": _evt(OPT_KEY, "bid", 14.5, 990.0, source="replay")}
        row_live = build_option_row(OPT_KEY, fields_live, SNAP_TS, BASE_CONFIG)
        row_replay = build_option_row(OPT_KEY, fields_replay, SNAP_TS, BASE_CONFIG)
        assert row_live is not None and row_replay is not None
        # All analytics fields identical — source label not propagated to OptionRow
        assert row_live.bid == row_replay.bid
        assert row_live.mid == row_replay.mid
        assert row_live.maturity_years == row_replay.maturity_years


# ---------------------------------------------------------------------------
# _build_option_rows
# ---------------------------------------------------------------------------


class TestBuildOptionRows:

    def test_empty_contracts_returns_empty(self):
        config = {**BASE_CONFIG, "option_contracts": []}
        rows = _build_option_rows({}, SNAP_TS, config)
        assert rows == []

    def test_builds_row_per_contract(self):
        config = {**BASE_CONFIG, "option_contracts": [OPT_KEY, OPT_KEY_PUT]}
        state_by_key = {
            OPT_KEY: {"bid": _evt(OPT_KEY, "bid", 14.5, 990.0),
                      "ask": _evt(OPT_KEY, "ask", 15.5, 990.0)},
            OPT_KEY_PUT: {"bid": _evt(OPT_KEY_PUT, "bid", 13.0, 990.0),
                          "ask": _evt(OPT_KEY_PUT, "ask", 14.0, 990.0)},
        }
        rows = _build_option_rows(state_by_key, SNAP_TS, config)
        assert len(rows) == 2

    def test_no_data_row_still_included(self):
        config = {**BASE_CONFIG, "option_contracts": [OPT_KEY]}
        rows = _build_option_rows({}, SNAP_TS, config)
        assert len(rows) == 1
        assert rows[0].bid is None

    def test_invalid_key_excluded(self):
        config = {**BASE_CONFIG, "option_contracts": [OPT_KEY, "BADKEY"]}
        rows = _build_option_rows({}, SNAP_TS, config)
        assert len(rows) == 1  # BADKEY rejected, OPT_KEY included with no data

    def test_accepts_option_contract_objects(self):
        from src.universe.contracts import OptionContract
        from datetime import date as d
        contract = OptionContract(
            underlying_symbol="SPY", expiry=d(2026, 12, 19),
            strike=450.0, right="C", multiplier=100,
        )
        config = {**BASE_CONFIG, "option_contracts": [contract]}
        rows = _build_option_rows({}, SNAP_TS, config)
        assert len(rows) == 1
        assert rows[0].strike == 450.0


# ---------------------------------------------------------------------------
# _derive_state_flags
# ---------------------------------------------------------------------------


class TestDeriveStateFlags:

    def _make_row(self, expiry: str, bid=None, ask=None) -> OptionRow:
        return OptionRow(
            instrument_key=f"SPY|OPT|SMART|USD|{expiry.replace('-','')}|450|C|100",
            snapshot_ts=SNAP_TS,
            underlying_symbol="SPY",
            expiry_str=expiry,
            strike=450.0,
            option_right="C",
            multiplier=100.0,
            bid=bid,
            ask=ask,
            last=None,
            mid=(bid + ask) / 2 if bid and ask else None,
            volume=None,
            open_interest=None,
            spread_pct=None,
            quote_age_seconds=5.0,
            is_stale=False,
            maturity_years=0.5,
        )

    def test_session_open_from_config(self):
        underlying = _underlying_state()
        flags = _derive_state_flags(underlying, [], {**BASE_CONFIG, "session_open": False})
        assert flags["session_open"] is False

    def test_stale_underlying_propagated(self):
        underlying = _underlying_state(is_stale=True)
        flags = _derive_state_flags(underlying, [], BASE_CONFIG)
        assert flags["stale_underlying"] is True

    def test_data_complete_when_all_have_quotes(self):
        rows = [self._make_row("2026-12-19", bid=14.0, ask=16.0),
                self._make_row("2026-12-19", bid=12.0, ask=13.0)]
        flags = _derive_state_flags(_underlying_state(), rows, BASE_CONFIG)
        assert flags["data_complete"] is True
        assert flags["option_coverage_pct"] == 1.0

    def test_data_incomplete_when_some_missing(self):
        rows = [self._make_row("2026-12-19", bid=14.0, ask=16.0),
                self._make_row("2026-12-19")]   # no quote
        flags = _derive_state_flags(_underlying_state(), rows, BASE_CONFIG)
        assert flags["data_complete"] is False
        assert flags["option_coverage_pct"] == 0.5

    def test_empty_options_not_complete(self):
        flags = _derive_state_flags(_underlying_state(), [], BASE_CONFIG)
        assert flags["data_complete"] is False
        assert flags["option_coverage_pct"] == 0.0
        assert flags["option_count"] == 0

    def test_completeness_by_maturity_computed(self):
        rows = [
            self._make_row("2026-12-19", bid=14.0, ask=16.0),
            self._make_row("2026-12-19"),  # no quote
            self._make_row("2027-03-19", bid=10.0, ask=11.0),
        ]
        flags = _derive_state_flags(_underlying_state(), rows, BASE_CONFIG)
        mat = flags["completeness_by_maturity"]
        assert "2026-12-19" in mat
        assert mat["2026-12-19"]["total"] == 2
        assert mat["2026-12-19"]["with_quotes"] == 1
        assert mat["2026-12-19"]["coverage_pct"] == 0.5
        assert mat["2027-03-19"]["coverage_pct"] == 1.0

    def test_options_with_quotes_counted(self):
        rows = [self._make_row("2026-12-19", bid=14.0, ask=16.0),
                self._make_row("2026-12-19")]
        flags = _derive_state_flags(_underlying_state(), rows, BASE_CONFIG)
        assert flags["options_with_quotes"] == 1
        assert flags["option_count"] == 2


# ---------------------------------------------------------------------------
# build_snapshot — end-to-end determinism (acceptance criterion)
# ---------------------------------------------------------------------------


class TestBuildSnapshot:

    def _make_events(self, underlying_ts=990.0, opt_ts=985.0,
                     source="live") -> list[RawEvent]:
        return [
            _evt(UNDERLYING_KEY, "bid", 449.5, underlying_ts, source),
            _evt(UNDERLYING_KEY, "ask", 450.5, underlying_ts, source),
            _evt(UNDERLYING_KEY, "last", 450.0, underlying_ts, source),
            _evt(OPT_KEY, "bid", 14.5, opt_ts, source),
            _evt(OPT_KEY, "ask", 15.5, opt_ts, source),
        ]

    def _config(self) -> dict:
        return {
            **BASE_CONFIG,
            "option_contracts": [OPT_KEY, OPT_KEY_PUT],
        }

    def test_returns_market_state_snapshot(self):
        events = self._make_events()
        snap = build_snapshot(events, SNAP_TS, self._config())
        assert isinstance(snap, MarketStateSnapshot)
        assert snap.snapshot_ts == SNAP_TS

    def test_underlying_state_populated(self):
        snap = build_snapshot(self._make_events(), SNAP_TS, self._config())
        u = snap.underlying_state
        assert u.reference_type == "mid"
        assert abs(u.reference_spot - 450.0) < 1e-8

    def test_option_rows_built(self):
        snap = build_snapshot(self._make_events(), SNAP_TS, self._config())
        assert len(snap.option_rows) == 2
        call = snap.get_call(450.0, "2026-12-19")
        assert call is not None
        assert abs(call.mid - 15.0) < 1e-8

    def test_flags_included(self):
        snap = build_snapshot(self._make_events(), SNAP_TS, self._config())
        assert "session_open" in snap.flags
        assert "option_coverage_pct" in snap.flags

    def test_determinism_same_inputs(self):
        """Same raw events + params must produce identical snapshots — acceptance criterion."""
        events = self._make_events()
        config = self._config()
        snap1 = build_snapshot(events, SNAP_TS, config)
        snap2 = build_snapshot(events, SNAP_TS, config)
        assert snap1.snapshot_ts == snap2.snapshot_ts
        assert snap1.underlying_state == snap2.underlying_state
        assert len(snap1.option_rows) == len(snap2.option_rows)
        for r1, r2 in zip(snap1.option_rows, snap2.option_rows):
            assert r1 == r2
        assert snap1.flags == snap2.flags

    def test_determinism_live_vs_replay(self):
        """Replay events produce the same snapshot as live events (source field ignored)."""
        live_events = self._make_events(source="live")
        replay_events = self._make_events(source="replay")
        config = self._config()
        snap_live = build_snapshot(live_events, SNAP_TS, config)
        snap_replay = build_snapshot(replay_events, SNAP_TS, config)
        assert snap_live.underlying_state.reference_spot == snap_replay.underlying_state.reference_spot
        assert snap_live.underlying_state.reference_type == snap_replay.underlying_state.reference_type
        assert len(snap_live.option_rows) == len(snap_replay.option_rows)
        for rl, rr in zip(snap_live.option_rows, snap_replay.option_rows):
            assert rl.bid == rr.bid
            assert rl.mid == rr.mid
            assert rl.maturity_years == rr.maturity_years

    def test_snapshot_excludes_future_events(self):
        events = [
            _evt(UNDERLYING_KEY, "bid", 449.5, 990.0),
            _evt(UNDERLYING_KEY, "ask", 450.5, 990.0),
            _evt(UNDERLYING_KEY, "bid", 999.9, 1_500.0),  # after snapshot_ts — excluded
            _evt(UNDERLYING_KEY, "ask", 1001.0, 1_500.0),  # after snapshot_ts — excluded
        ]
        config = {**BASE_CONFIG, "option_contracts": []}
        snap = build_snapshot(events, SNAP_TS, config)
        assert abs(snap.underlying_state.bid - 449.5) < 1e-8

    def test_snapshot_version_set(self):
        snap = build_snapshot(self._make_events(), SNAP_TS, self._config())
        assert snap.snapshot_version == "1.0"

    def test_get_options_by_expiry(self):
        snap = build_snapshot(self._make_events(), SNAP_TS, self._config())
        rows = snap.get_options_by_expiry("2026-12-19")
        assert len(rows) == 2

    def test_carry_forward_spot_when_no_quotes(self):
        config = {
            **BASE_CONFIG,
            "option_contracts": [],
            "carry_forward_spot": 440.0,
        }
        snap = build_snapshot([], SNAP_TS, config)
        assert snap.underlying_state.reference_type == "carry_forward"
        assert snap.underlying_state.reference_spot == 440.0

    def test_stale_underlying_flag_set(self):
        config = {**BASE_CONFIG, "option_contracts": [], "max_underlying_age_seconds": 5}
        events = [
            _evt(UNDERLYING_KEY, "bid", 449.5, 900.0),  # 100s old
            _evt(UNDERLYING_KEY, "ask", 450.5, 900.0),  # 100s old
        ]
        snap = build_snapshot(events, SNAP_TS, config)
        assert snap.flags["stale_underlying"] is True


# ---------------------------------------------------------------------------
# Euro Stoxx 50 snapshot coverage
# ---------------------------------------------------------------------------

ESTX50_KEY = "ESTX50|IND|EUREX|EUR"
ESTX50_OPT_KEY = "ESTX50|OPT|EUREX|EUR|20261219|5000|C|10"
ESTX50_OPT_PUT_KEY = "ESTX50|OPT|EUREX|EUR|20261219|5000|P|10"


class TestESTX50Snapshot:
    """Snapshot with ESTX50 contracts covers Euro Stoxx 50 index key."""

    _SNAP_DATE = date(2026, 1, 2)
    _TS = 2_000.0

    def _make_estx50_config(self) -> dict:
        return {
            "underlying_symbol": "ESTX50",
            "underlying_key": ESTX50_KEY,
            "max_underlying_age_seconds": 30,
            "max_option_age_seconds": 60,
            "max_spread_pct_for_mid": 0.05,
            "session_open": True,
            "snapshot_date": self._SNAP_DATE,
            "option_contracts": [ESTX50_OPT_KEY, ESTX50_OPT_PUT_KEY],
        }

    def _make_events(self) -> list[RawEvent]:
        return [
            _evt(ESTX50_KEY, "bid", 4998.0, self._TS - 5),
            _evt(ESTX50_KEY, "ask", 5002.0, self._TS - 5),
            _evt(ESTX50_KEY, "last", 5000.0, self._TS - 5),
            _evt(ESTX50_OPT_KEY, "bid", 80.0, self._TS - 3),
            _evt(ESTX50_OPT_KEY, "ask", 82.0, self._TS - 3),
            _evt(ESTX50_OPT_PUT_KEY, "bid", 75.0, self._TS - 3),
            _evt(ESTX50_OPT_PUT_KEY, "ask", 77.0, self._TS - 3),
        ]

    def test_snapshot_uses_estx50_index_key(self) -> None:
        snap = build_snapshot(self._make_events(), self._TS, self._make_estx50_config())
        assert snap.underlying_state.instrument_key == ESTX50_KEY

    def test_snapshot_mid_computed_for_estx50(self) -> None:
        snap = build_snapshot(self._make_events(), self._TS, self._make_estx50_config())
        assert abs(snap.underlying_state.reference_spot - 5000.0) < 1e-6
        assert snap.underlying_state.reference_type == "mid"

    def test_option_rows_include_call_and_put(self) -> None:
        snap = build_snapshot(self._make_events(), self._TS, self._make_estx50_config())
        assert len(snap.option_rows) == 2
        call = snap.get_call(5000.0, "2026-12-19")
        put = snap.get_put(5000.0, "2026-12-19")
        assert call is not None
        assert put is not None

    def test_option_rows_have_correct_mid(self) -> None:
        snap = build_snapshot(self._make_events(), self._TS, self._make_estx50_config())
        call = snap.get_call(5000.0, "2026-12-19")
        put = snap.get_put(5000.0, "2026-12-19")
        assert call is not None and abs(call.mid - 81.0) < 1e-6
        assert put is not None and abs(put.mid - 76.0) < 1e-6

    def test_option_rows_instrument_keys_reference_estx50(self) -> None:
        snap = build_snapshot(self._make_events(), self._TS, self._make_estx50_config())
        for row in snap.option_rows:
            assert row.instrument_key.startswith("ESTX50")

    def test_determinism_estx50(self) -> None:
        events = self._make_events()
        config = self._make_estx50_config()
        snap1 = build_snapshot(events, self._TS, config)
        snap2 = build_snapshot(events, self._TS, config)
        assert snap1.underlying_state.reference_spot == snap2.underlying_state.reference_spot
        assert len(snap1.option_rows) == len(snap2.option_rows)
        for r1, r2 in zip(snap1.option_rows, snap2.option_rows):
            assert r1 == r2

    def test_flags_include_option_coverage_pct(self) -> None:
        snap = build_snapshot(self._make_events(), self._TS, self._make_estx50_config())
        assert "option_coverage_pct" in snap.flags
        assert snap.flags["option_coverage_pct"] == 1.0  # both options have quotes
