"""Regression tests for the EOD analytics pipeline on calm-market fixture data.

Covers calm_day (full chain, SVI fit), event_heavy (high-frequency ticks),
and disconnect_recovery (merged part1 + part2 event stream).
All three should produce two fitted slices without PCHIP fallback.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from src.collectors.raw_collector import RawEvent
from src.snapshots.builder import build_snapshot
from src.forwards.engine import estimate_forward_curve
from src.iv.solver import solve_iv_batch
from src.surfaces.calibration import fit_surface
from src.surfaces.models import IVPoint

FIXTURES = Path(__file__).parent / "fixtures"
IV_CFG: dict = {"max_iterations": 100, "tolerance": 1e-6}
SURF_CFG: dict = {"min_points_per_slice": 5}
RATE = 0.04
UNDERLYING_KEY = "SX5E|IND|EUREX|EUR||||"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_events(jsonl_path: Path) -> list[RawEvent]:
    return [RawEvent.from_dict(json.loads(line)) for line in jsonl_path.read_text().splitlines() if line.strip()]


def snap_cfg(events: list[RawEvent]) -> dict:
    """Build the minimal build_snapshot config from the event stream."""
    option_keys = sorted({e.instrument_key for e in events if "|OPT|" in e.instrument_key})
    return {
        "underlying_key": UNDERLYING_KEY,
        "underlying_symbol": "SX5E",
        "option_contracts": option_keys,
    }


def run_pipeline(events: list[RawEvent], expected: dict):
    """Build snapshot → forwards → IV → surface; return (surface, n_converged)."""
    snapshot_ts = max(e.receipt_ts for e in events)
    cfg = snap_cfg(events)
    snapshot = build_snapshot(events, snapshot_ts, cfg)

    assert snapshot is not None
    assert snapshot.underlying_state is not None

    seen: dict[str, float] = {}
    for r in snapshot.option_rows:
        if r.maturity_years and r.expiry_str not in seen:
            seen[r.expiry_str] = r.maturity_years
    maturities = sorted(seen.items(), key=lambda x: x[1])
    assert maturities, "no option maturities in snapshot"

    fwd_results = estimate_forward_curve(snapshot, maturities, RATE, {})
    fwd_by_expiry = {f.expiry_str: f.chosen_forward for f in fwd_results if f.chosen_forward}

    spot = snapshot.underlying_state.reference_spot
    records = []
    for opt in snapshot.option_rows:
        if opt.maturity_years is None or opt.mid is None:
            continue
        F = fwd_by_expiry.get(opt.expiry_str) or spot
        q_implied = RATE - math.log(F / spot) / max(opt.maturity_years, 1e-9)
        records.append({
            "market_price": opt.mid,
            "S": spot,
            "K": opt.strike,
            "T": opt.maturity_years,
            "r": RATE,
            "q": q_implied,
            "option_type": opt.option_right,
            "contract_key": opt.instrument_key,
            "snapshot_ts": snapshot_ts,
        })

    iv_results = solve_iv_batch(records, IV_CFG)
    n_converged = sum(1 for r in iv_results if r.converged and r.implied_vol)
    assert n_converged >= expected["n_iv_converged_min"], (
        f"only {n_converged} IVs converged, expected ≥{expected['n_iv_converged_min']}"
    )

    # Build IVPoint list for surface fitting
    opt_by_key = {o.instrument_key: o for o in snapshot.option_rows}
    iv_points = []
    for r in iv_results:
        if not (r.converged and r.implied_vol and r.contract_key):
            continue
        opt = opt_by_key.get(r.contract_key)
        if opt is None or opt.maturity_years is None:
            continue
        F = fwd_by_expiry.get(opt.expiry_str) or spot
        lm = math.log(opt.strike / F) if (opt.strike and F and F > 0) else 0.0
        T = opt.maturity_years
        iv_points.append(IVPoint(
            contract_key=r.contract_key,
            snapshot_ts=snapshot_ts,
            expiry_str=opt.expiry_str,
            maturity_years=T,
            strike=opt.strike,
            forward=F,
            log_moneyness=lm,
            implied_vol=r.implied_vol,
            total_variance=r.implied_vol ** 2 * T,
            weight=1.0,
            qc_status="usable",
        ))

    surface = fit_surface(iv_points, SURF_CFG, underlying="SX5E", snapshot_ts=snapshot_ts)
    return surface, n_converged


def _atm_vol(sl) -> float | None:
    """ATM vol from SVI params at k=0; None for spline / failed slices."""
    if sl.params is not None and sl.maturity_years > 0:
        try:
            return sl.params.implied_vol(0.0, sl.maturity_years)
        except Exception:
            return None
    return None


def assert_surface(surface, expected: dict):
    exp_slices = {s["expiry"]: s for s in expected["slices"]}
    assert len(surface.slices) == expected["n_slices"], (
        f"expected {expected['n_slices']} slices, got {len(surface.slices)}"
    )
    for sl in surface.slices:
        assert sl.expiry_str in exp_slices, f"unexpected slice {sl.expiry_str}"
        bounds = exp_slices[sl.expiry_str]
        assert sl.n_accepted >= bounds["n_points_min"], (
            f"{sl.expiry_str}: n_accepted={sl.n_accepted} < {bounds['n_points_min']}"
        )
        assert sl.rmse <= bounds["rmse_max"], (
            f"{sl.expiry_str}: rmse={sl.rmse:.5f} > {bounds['rmse_max']}"
        )
        is_fallback = sl.model == "spline"
        assert is_fallback == bounds["is_fallback"], (
            f"{sl.expiry_str}: model={sl.model!r}, expected is_fallback={bounds['is_fallback']}"
        )
        atm = _atm_vol(sl)
        if atm is not None:
            assert bounds["atm_vol_min"] <= atm <= bounds["atm_vol_max"], (
                f"{sl.expiry_str}: atm_vol={atm:.4f} out of "
                f"[{bounds['atm_vol_min']}, {bounds['atm_vol_max']}]"
            )


# ---------------------------------------------------------------------------
# calm_day
# ---------------------------------------------------------------------------

class TestCalmDay:
    @pytest.fixture(scope="class")
    def fixture_dir(self):
        return FIXTURES / "calm_day"

    @pytest.fixture(scope="class")
    def events(self, fixture_dir):
        return load_events(fixture_dir / "raw_events.jsonl")

    @pytest.fixture(scope="class")
    def expected(self, fixture_dir):
        return json.loads((fixture_dir / "expected_surface.json").read_text())

    @pytest.fixture(scope="class")
    def pipeline_result(self, events, expected):
        return run_pipeline(events, expected)

    def test_event_count(self, events):
        assert len(events) >= 100

    def test_snapshot_has_options(self, events):
        snapshot_ts = max(e.receipt_ts for e in events)
        snap = build_snapshot(events, snapshot_ts, snap_cfg(events))
        assert snap is not None
        assert len(snap.option_rows) >= 10

    def test_surface_structure(self, pipeline_result, expected):
        surface, _ = pipeline_result
        assert_surface(surface, expected)

    def test_svi_not_fallback(self, pipeline_result):
        surface, _ = pipeline_result
        for sl in surface.slices:
            assert sl.model == "svi", f"{sl.expiry_str} fell back to {sl.model!r} unexpectedly"

    def test_iv_convergence_rate(self, pipeline_result, expected):
        _, n_converged = pipeline_result
        assert n_converged >= expected["n_iv_converged_min"]

    def test_atm_vols_in_range(self, pipeline_result, expected):
        surface, _ = pipeline_result
        assert_surface(surface, expected)

    def test_rmse_acceptable(self, pipeline_result, expected):
        surface, _ = pipeline_result
        for sl in surface.slices:
            if sl.rmse is not None:
                bounds = next(s for s in expected["slices"] if s["expiry"] == sl.expiry_str)
                assert sl.rmse <= bounds["rmse_max"]


# ---------------------------------------------------------------------------
# event_heavy
# ---------------------------------------------------------------------------

class TestEventHeavy:
    @pytest.fixture(scope="class")
    def fixture_dir(self):
        return FIXTURES / "event_heavy"

    @pytest.fixture(scope="class")
    def events(self, fixture_dir):
        return load_events(fixture_dir / "raw_events.jsonl")

    @pytest.fixture(scope="class")
    def expected(self, fixture_dir):
        return json.loads((fixture_dir / "expected_surface.json").read_text())

    @pytest.fixture(scope="class")
    def pipeline_result(self, events, expected):
        return run_pipeline(events, expected)

    def test_event_count(self, events):
        assert len(events) >= 400

    def test_snapshot_uses_latest_quote(self, events):
        snapshot_ts = max(e.receipt_ts for e in events)
        snap = build_snapshot(events, snapshot_ts, snap_cfg(events))
        assert snap is not None
        latest_bid = max(
            (e for e in events if e.instrument_key == UNDERLYING_KEY and e.field_name == "bid"),
            key=lambda e: e.receipt_ts,
        )
        assert snap.underlying_state.bid == pytest.approx(latest_bid.field_value)

    def test_surface_slice_count(self, pipeline_result, expected):
        surface, _ = pipeline_result
        assert len(surface.slices) == expected["n_slices"]

    def test_iv_convergence(self, pipeline_result, expected):
        _, n_converged = pipeline_result
        assert n_converged >= expected["n_iv_converged_min"]


# ---------------------------------------------------------------------------
# disconnect_recovery
# ---------------------------------------------------------------------------

class TestDisconnectRecovery:
    @pytest.fixture(scope="class")
    def fixture_dir(self):
        return FIXTURES / "disconnect_recovery"

    @pytest.fixture(scope="class")
    def events(self, fixture_dir):
        return (
            load_events(fixture_dir / "raw_events_part1.jsonl")
            + load_events(fixture_dir / "raw_events_part2.jsonl")
        )

    @pytest.fixture(scope="class")
    def expected(self, fixture_dir):
        return json.loads((fixture_dir / "expected_surface.json").read_text())

    def test_merged_event_count(self, events, expected):
        assert len(events) == expected["n_events_part1"] + expected["n_events_part2"]

    def test_snapshot_from_merged_stream(self, events):
        snapshot_ts = max(e.receipt_ts for e in events)
        snap = build_snapshot(events, snapshot_ts, snap_cfg(events))
        assert snap is not None
        assert len(snap.option_rows) >= 10

    def test_latest_quote_wins_after_reconnect(self, events):
        snapshot_ts = max(e.receipt_ts for e in events)
        snap = build_snapshot(events, snapshot_ts, snap_cfg(events))
        latest_ask = max(
            (e for e in events if e.instrument_key == UNDERLYING_KEY and e.field_name == "ask"),
            key=lambda e: e.receipt_ts,
        )
        assert snap.underlying_state.ask == pytest.approx(latest_ask.field_value)

    def test_surface_fit(self, events, expected):
        surface, n_converged = run_pipeline(events, expected)
        assert_surface(surface, expected)
        assert n_converged >= expected["n_iv_converged_min"]
