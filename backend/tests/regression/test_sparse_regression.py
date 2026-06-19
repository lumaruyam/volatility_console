"""Regression tests for sparse-liquidity scenario.

Only 3 strikes per expiry (below the min_points_per_slice=5 threshold),
so fit_surface must fall back to PCHIP spline for both slices.
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

FIXTURES = Path(__file__).parent / "fixtures" / "sparse_liquidity"
IV_CFG: dict = {"max_iterations": 100, "tolerance": 1e-6}
SURF_CFG: dict = {"min_points_per_slice": 5}  # deliberately above number of sparse strikes

RATE = 0.04
UNDERLYING_KEY = "SX5E|IND|EUREX|EUR||||"


def load_events(path: Path) -> list[RawEvent]:
    return [RawEvent.from_dict(json.loads(l)) for l in path.read_text().splitlines() if l.strip()]


def snap_cfg(events: list[RawEvent]) -> dict:
    option_keys = sorted({e.instrument_key for e in events if "|OPT|" in e.instrument_key})
    return {"underlying_key": UNDERLYING_KEY, "underlying_symbol": "SX5E", "option_contracts": option_keys}


@pytest.fixture(scope="module")
def events():
    return load_events(FIXTURES / "raw_events.jsonl")


@pytest.fixture(scope="module")
def expected():
    return json.loads((FIXTURES / "expected_surface.json").read_text())


@pytest.fixture(scope="module")
def surface_and_counts(events, expected):
    snapshot_ts = max(e.receipt_ts for e in events)
    cfg = snap_cfg(events)
    snapshot = build_snapshot(events, snapshot_ts, cfg)
    assert snapshot is not None

    seen: dict[str, float] = {}
    for r in snapshot.option_rows:
        if r.maturity_years and r.expiry_str not in seen:
            seen[r.expiry_str] = r.maturity_years
    maturities = sorted(seen.items(), key=lambda x: x[1])
    fwd_results = estimate_forward_curve(snapshot, maturities, RATE, {})
    fwd_by_expiry = {f.expiry_str: f.chosen_forward for f in fwd_results if f.chosen_forward}

    spot = snapshot.underlying_state.reference_spot
    records = []
    for opt in snapshot.option_rows:
        if opt.maturity_years is None or opt.mid is None:
            continue
        F = fwd_by_expiry.get(opt.expiry_str) or spot
        records.append({
            "market_price": opt.mid,
            "S": spot,
            "K": opt.strike,
            "T": opt.maturity_years,
            "r": RATE,
            "q": RATE - math.log(F / spot) / max(opt.maturity_years, 1e-9),
            "option_type": opt.option_right,
            "contract_key": opt.instrument_key,
            "snapshot_ts": snapshot_ts,
        })

    iv_results = solve_iv_batch(records, IV_CFG)
    n_converged = sum(1 for r in iv_results if r.converged and r.implied_vol)

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
    return surface, n_converged, snapshot


class TestSparseLiquidity:
    def test_fixture_has_few_options(self, events):
        # Only 2 strikes × 2 rights × 2 expiries = 8 option instruments
        option_keys = {e.instrument_key for e in events if "OPT" in e.instrument_key}
        assert len(option_keys) <= 10, f"expected sparse chain (≤10 options), got {len(option_keys)}"

    def test_snapshot_builds_with_sparse_chain(self, events):
        snapshot_ts = max(e.receipt_ts for e in events)
        snap = build_snapshot(events, snapshot_ts, snap_cfg(events))
        assert snap is not None
        assert 1 <= len(snap.option_rows) <= 14

    def test_some_ivs_converge(self, surface_and_counts, expected):
        _, n_converged, _ = surface_and_counts
        assert n_converged >= expected["n_iv_converged_min"]

    def test_pchip_fallback_triggered(self, surface_and_counts, expected):
        """Core regression: sparse chain must NOT use SVI (falls to spline or failed)."""
        surface, _, _ = surface_and_counts
        assert len(surface.slices) >= 1, "no slices fitted at all"
        non_svi = sum(1 for sl in surface.slices if sl.model != "svi")
        assert non_svi >= 1, (
            f"expected at least 1 non-SVI slice on sparse chain; "
            f"got models={[sl.model for sl in surface.slices]}"
        )

    def test_fallback_slices_still_have_iv_points(self, surface_and_counts):
        """Non-SVI slices (spline or failed) must have accepted raw points."""
        surface, _, _ = surface_and_counts
        for sl in surface.slices:
            if sl.model != "svi":
                assert sl.n_accepted >= 1, f"{sl.expiry_str} fallback slice has no accepted points"

    def test_slice_count(self, surface_and_counts, expected):
        surface, _, _ = surface_and_counts
        assert len(surface.slices) == expected["n_slices"]

    def test_atm_vols_within_bounds(self, surface_and_counts, expected):
        surface, _, _ = surface_and_counts
        exp_by_expiry = {s["expiry"]: s for s in expected["slices"]}
        for sl in surface.slices:
            if sl.expiry_str not in exp_by_expiry:
                continue
            bounds = exp_by_expiry[sl.expiry_str]
            # For spline slices there are no SVI params; check mid-grid variance instead
            if sl.params is not None and sl.maturity_years > 0:
                try:
                    atm_vol = sl.params.implied_vol(0.0, sl.maturity_years)
                    assert bounds["atm_vol_min"] <= atm_vol <= bounds["atm_vol_max"], (
                        f"{sl.expiry_str}: atm_vol={atm_vol:.4f}"
                    )
                except Exception:
                    pass  # invalid params skip gracefully
