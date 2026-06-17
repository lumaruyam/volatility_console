"""
Comprehensive tests for Step 9: Surface engine.

Acceptance criteria (PLAN):
  - Reproducible params: same inputs → same fit params.
  - Calendar diagnostic computed: check_calendar_monotonicity returns violations.
  - Fit error metrics exposed: rmse, max_error, n_accepted in SliceFitResult.
"""

from __future__ import annotations

import math
import pytest
import numpy as np

from src.surfaces.models import IVPoint, SVIParameters, SliceFitResult, SurfaceFitResult
from src.surfaces.calibration import (
    fit_slice,
    fit_surface,
    check_calendar_monotonicity,
    plot_slice,
)
from src.surfaces.interpolation import (
    evaluate_slice_variance,
    interpolate_surface_at,
    interpolate_surface_grid,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CFG = {
    "min_points_per_slice": 5,
    "max_rmse": 0.02,
    "grid_k_min": -1.5,
    "grid_k_max": 1.5,
    "grid_n_points": 50,
    "calendar_check_moneyness": [-0.5, 0.0, 0.5],
}


def _point(k: float, sigma: float, T: float = 1.0,
           expiry: str = "20260116", qc: str = "usable") -> IVPoint:
    """Build one IVPoint from (k, sigma, T) with consistent derived fields."""
    w = sigma ** 2 * T
    K = 100.0 * math.exp(k)
    return IVPoint(
        contract_key=f"TEST|OPT|EXCH|USD|{expiry}|{K:.0f}|C|100",
        snapshot_ts=0.0, expiry_str=expiry, maturity_years=T,
        strike=K, forward=100.0,
        log_moneyness=k, implied_vol=sigma, total_variance=w,
        weight=1.0, qc_status=qc,
    )


def _flat_slice(sigma: float = 0.25, T: float = 1.0, n: int = 15,
                expiry: str = "20260116") -> list[IVPoint]:
    """n points on a flat σ(k) = sigma surface."""
    return [_point(k, sigma, T, expiry) for k in np.linspace(-0.5, 0.5, n)]


def _svi_slice(params: SVIParameters, T: float, n: int = 15,
               expiry: str = "20260116") -> list[IVPoint]:
    """Generate IVPoints from known SVI params — used for round-trip tests."""
    points = []
    for k in np.linspace(-0.5, 0.5, n):
        w = params.total_variance(k)
        sigma = math.sqrt(max(w / T, 1e-8))
        points.append(_point(k, sigma, T, expiry))
    return points


def _make_slice_result(expiry: str, T: float, a: float) -> SliceFitResult:
    """Pre-built SliceFitResult with SVI params — used for calendar tests."""
    return SliceFitResult(
        expiry_str=expiry, maturity_years=T,
        model="svi",
        params=SVIParameters(a=a, b=0.1, rho=0.0, m=0.0, sigma=0.1),
        grid_log_moneyness=[], grid_total_variance=[],
        raw_points=[], accepted_points=[], rejected_points=[],
        rmse=0.001, max_error=0.002, n_accepted=10,
    )


# ---------------------------------------------------------------------------
# TestIVPointModel
# ---------------------------------------------------------------------------

class TestIVPointModel:
    def test_fields_present(self):
        pt = _point(0.0, 0.20)
        assert pt.log_moneyness == pytest.approx(0.0)
        assert pt.implied_vol == pytest.approx(0.20)
        assert pt.total_variance == pytest.approx(0.04)

    def test_total_variance_formula(self):
        sigma, T = 0.25, 0.5
        pt = _point(0.0, sigma, T)
        assert pt.total_variance == pytest.approx(sigma ** 2 * T, abs=1e-10)

    def test_qc_status_default(self):
        pt = _point(0.0, 0.20)
        assert pt.qc_status == "usable"

    def test_weight_default(self):
        pt = _point(0.0, 0.20)
        assert pt.weight == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# TestSVIParameters
# ---------------------------------------------------------------------------

class TestSVIParameters:
    def test_atm_flat_surface(self):
        """w(k=0) = a + b*σ when ρ=0, m=0."""
        p = SVIParameters(a=0.04, b=0.1, rho=0.0, m=0.0, sigma=0.1)
        assert p.total_variance(0.0) == pytest.approx(0.04 + 0.1 * 0.1, abs=1e-10)

    def test_non_negative_over_grid(self):
        """SVI total variance must be non-negative for reasonable parameters."""
        p = SVIParameters(a=0.02, b=0.15, rho=-0.3, m=0.0, sigma=0.1)
        for k in np.linspace(-3.0, 3.0, 200):
            assert p.total_variance(k) >= 0.0

    def test_implied_vol_formula(self):
        p = SVIParameters(a=0.04, b=0.1, rho=0.0, m=0.0, sigma=0.1)
        T = 1.0
        iv = p.implied_vol(0.0, T)
        w = p.total_variance(0.0)
        assert iv ** 2 * T == pytest.approx(w, abs=1e-10)

    def test_implied_vol_nan_when_negative_variance(self):
        p = SVIParameters(a=-1.0, b=0.01, rho=0.0, m=0.0, sigma=0.1)
        assert math.isnan(p.implied_vol(0.0, 1.0))

    def test_frozen_params(self):
        p = SVIParameters(a=0.04, b=0.1, rho=-0.2, m=0.0, sigma=0.1)
        with pytest.raises((AttributeError, TypeError)):
            p.a = 0.05  # type: ignore[misc]

    def test_symmetric_when_rho_zero(self):
        """With ρ=0, m=0: w(k) == w(-k)."""
        p = SVIParameters(a=0.04, b=0.1, rho=0.0, m=0.0, sigma=0.1)
        for k in [0.1, 0.3, 0.5, 1.0]:
            assert p.total_variance(k) == pytest.approx(p.total_variance(-k), abs=1e-10)

    def test_skew_breaks_symmetry(self):
        p = SVIParameters(a=0.04, b=0.1, rho=-0.5, m=0.0, sigma=0.1)
        assert p.total_variance(-0.3) != pytest.approx(p.total_variance(0.3))

    def test_wing_behaviour(self):
        """Total variance grows with |k| (convexity)."""
        p = SVIParameters(a=0.04, b=0.1, rho=0.0, m=0.0, sigma=0.1)
        assert p.total_variance(1.0) > p.total_variance(0.0)
        assert p.total_variance(-1.0) > p.total_variance(0.0)


# ---------------------------------------------------------------------------
# TestFitSlice — SVI path
# ---------------------------------------------------------------------------

class TestFitSlice:
    def test_flat_surface_converges(self):
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert result.model in ("svi", "spline")
        assert result.rmse < 0.01

    def test_rmse_exposed(self):
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert isinstance(result.rmse, float)
        assert not math.isnan(result.rmse)

    def test_max_error_exposed(self):
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert isinstance(result.max_error, float)
        assert not math.isnan(result.max_error)

    def test_n_accepted_exposed(self):
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert result.n_accepted == 15

    def test_grid_values_present(self):
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert len(result.grid_log_moneyness) > 0
        assert len(result.grid_total_variance) == len(result.grid_log_moneyness)

    def test_grid_variance_non_negative(self):
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert all(w >= 0.0 for w in result.grid_total_variance)

    def test_all_point_categories_stored(self):
        """raw, accepted, rejected all present even when all points pass QC."""
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert len(result.raw_points) == 15
        assert len(result.accepted_points) == 15
        assert len(result.rejected_points) == 0

    def test_rejected_points_excluded_from_fit(self):
        pts = _flat_slice()
        pts[0] = _point(pts[0].log_moneyness, pts[0].implied_vol, qc="reject")
        result = fit_slice(pts, "20260116", CFG)
        assert len(result.rejected_points) == 1
        assert len(result.accepted_points) == 14
        assert len(result.raw_points) == 15

    def test_caution_points_included_in_fit(self):
        """caution rows must be accepted into the fit (not treated as rejected)."""
        pts = _flat_slice()
        pts[0] = _point(pts[0].log_moneyness, pts[0].implied_vol, qc="caution")
        result = fit_slice(pts, "20260116", CFG)
        assert len(result.accepted_points) == 15
        assert len(result.rejected_points) == 0

    def test_expiry_str_stored(self):
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert result.expiry_str == "20260116"

    def test_maturity_years_stored(self):
        result = fit_slice(_flat_slice(T=0.5), "20260116", CFG)
        assert result.maturity_years == pytest.approx(0.5)

    def test_quality_flag_ok_for_good_fit(self):
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert result.quality_flag in ("ok", "sparse")

    def test_svi_roundtrip_recovers_params(self):
        """Fit SVI-generated points → recovered params close to seed."""
        seed = SVIParameters(a=0.04, b=0.15, rho=-0.3, m=0.0, sigma=0.15)
        pts = _svi_slice(seed, T=1.0, n=20)
        result = fit_slice(pts, "20261219", CFG)
        assert result.model == "svi"
        assert result.params is not None
        # Verify that recovered SVI evaluates close to seed at key k values
        for k in [-0.3, 0.0, 0.3]:
            w_seed = seed.total_variance(k)
            w_fit = result.params.total_variance(k)
            assert abs(w_fit - w_seed) < 1e-3, (
                f"k={k}: seed_w={w_seed:.5f}, fit_w={w_fit:.5f}"
            )

    def test_reproducible_params(self):
        """Same inputs → identical fit params (deterministic optimizer)."""
        pts = _flat_slice()
        r1 = fit_slice(pts, "20260116", CFG)
        r2 = fit_slice(pts, "20260116", CFG)
        if r1.params and r2.params:
            assert r1.params.a == pytest.approx(r2.params.a, abs=1e-6)
            assert r1.params.b == pytest.approx(r2.params.b, abs=1e-6)
            assert r1.params.rho == pytest.approx(r2.params.rho, abs=1e-6)


# ---------------------------------------------------------------------------
# TestFitSliceSparse — spline / failed paths
# ---------------------------------------------------------------------------

class TestFitSliceSparse:
    def test_sparse_returns_fallback_model(self):
        pts = [_point(k, 0.25) for k in [-0.1, 0.0, 0.1]]
        result = fit_slice(pts, "20260116", CFG)
        assert result.quality_flag in ("sparse", "failed")

    def test_single_point_fails(self):
        result = fit_slice([_point(0.0, 0.25)], "20260116", CFG)
        assert result.quality_flag in ("sparse", "failed")

    def test_empty_slice_fails(self):
        result = fit_slice([], "20260116", CFG)
        assert result.model == "failed"

    def test_sparse_stores_all_raw_points(self):
        pts = [_point(k, 0.25) for k in [-0.1, 0.0, 0.1]]
        result = fit_slice(pts, "20260116", CFG)
        assert len(result.raw_points) == 3

    def test_spline_grid_non_negative(self):
        # Give exactly 2 accepted points — hits spline fallback (n < min_points=5)
        pts = [_point(k, 0.25, 1.0) for k in np.linspace(-0.5, 0.5, 4)]
        result = fit_slice(pts, "20260116", CFG)
        if result.model == "spline":
            assert all(w >= 0.0 for w in result.grid_total_variance)

    def test_fit_warnings_populated_on_sparse(self):
        pts = [_point(k, 0.25) for k in [-0.1, 0.0, 0.1]]
        result = fit_slice(pts, "20260116", CFG)
        assert len(result.fit_warnings) > 0


# ---------------------------------------------------------------------------
# TestFitSurface
# ---------------------------------------------------------------------------

class TestFitSurface:
    def _multi_slice_points(self) -> list[IVPoint]:
        maturities = [(0.25, "20260918"), (0.5, "20261219"), (1.0, "20270618")]
        pts = []
        for T, exp in maturities:
            pts.extend(_flat_slice(sigma=0.20, T=T, expiry=exp))
        return pts

    def test_returns_surface_fit_result(self):
        result = fit_surface(self._multi_slice_points(), CFG, underlying="TEST")
        assert isinstance(result, SurfaceFitResult)

    def test_three_maturities_three_slices(self):
        result = fit_surface(self._multi_slice_points(), CFG)
        assert len(result.slices) == 3

    def test_slices_sorted_by_expiry(self):
        result = fit_surface(self._multi_slice_points(), CFG)
        expiries = [s.expiry_str for s in result.slices]
        assert expiries == sorted(expiries)

    def test_underlying_stored(self):
        result = fit_surface(self._multi_slice_points(), CFG, underlying="ESTX50")
        assert result.underlying == "ESTX50"

    def test_snapshot_ts_stored(self):
        result = fit_surface(self._multi_slice_points(), CFG, snapshot_ts=123.456)
        assert result.snapshot_ts == pytest.approx(123.456)

    def test_calendar_violations_field_present(self):
        result = fit_surface(self._multi_slice_points(), CFG)
        assert isinstance(result.calendar_violations, list)

    def test_no_calendar_violations_when_flat(self):
        """Flat surface at constant vol → variance increases with T → no violations."""
        result = fit_surface(self._multi_slice_points(), CFG)
        assert result.calendar_violations == []

    def test_surface_reproducible(self):
        pts = self._multi_slice_points()
        r1 = fit_surface(pts, CFG, underlying="X")
        r2 = fit_surface(pts, CFG, underlying="X")
        for s1, s2 in zip(r1.slices, r2.slices):
            assert s1.rmse == pytest.approx(s2.rmse, abs=1e-8)

    def test_error_metrics_all_finite(self):
        result = fit_surface(self._multi_slice_points(), CFG)
        for s in result.slices:
            assert not math.isnan(s.rmse)
            assert not math.isnan(s.max_error)


# ---------------------------------------------------------------------------
# TestCalendarMonotonicity
# ---------------------------------------------------------------------------

class TestCalendarMonotonicity:
    def test_no_violation_when_increasing(self):
        slices = [_make_slice_result("20260116", 0.1, a=0.01),
                  _make_slice_result("20260417", 0.3, a=0.03)]
        v = check_calendar_monotonicity(slices, {"calendar_check_moneyness": [0.0]})
        assert len(v) == 0

    def test_detects_violation(self):
        slices = [_make_slice_result("20260116", 0.1, a=0.05),
                  _make_slice_result("20260417", 0.3, a=0.01)]
        v = check_calendar_monotonicity(slices, {"calendar_check_moneyness": [0.0]})
        assert len(v) > 0

    def test_violation_fields_present(self):
        slices = [_make_slice_result("20260116", 0.1, a=0.05),
                  _make_slice_result("20260417", 0.3, a=0.01)]
        v = check_calendar_monotonicity(slices, {"calendar_check_moneyness": [0.0]})
        assert "expiry_1" in v[0]
        assert "expiry_2" in v[0]
        assert "log_moneyness" in v[0]
        assert "variance_1" in v[0]
        assert "variance_2" in v[0]
        assert "deficit" in v[0]
        assert v[0]["deficit"] > 0

    def test_violation_expiry_order(self):
        slices = [_make_slice_result("20260116", 0.1, a=0.05),
                  _make_slice_result("20260417", 0.3, a=0.01)]
        v = check_calendar_monotonicity(slices, {"calendar_check_moneyness": [0.0]})
        assert v[0]["expiry_1"] == "20260116"
        assert v[0]["expiry_2"] == "20260417"

    def test_multiple_k_checks(self):
        slices = [_make_slice_result("20260116", 0.1, a=0.05),
                  _make_slice_result("20260417", 0.3, a=0.01)]
        k_check = [-0.5, 0.0, 0.5]
        v = check_calendar_monotonicity(slices, {"calendar_check_moneyness": k_check})
        assert len(v) == 3  # one violation per k point

    def test_failed_slices_skipped(self):
        failed = SliceFitResult(
            "20260116", 0.1, "failed", None, [], [], [], [], [],
            float("nan"), float("nan"), 0,
        )
        good = _make_slice_result("20260417", 0.3, a=0.03)
        v = check_calendar_monotonicity([failed, good], {"calendar_check_moneyness": [0.0]})
        assert v == []

    def test_three_slices_adjacent_pairs_checked(self):
        slices = [
            _make_slice_result("20260116", 0.25, a=0.01),
            _make_slice_result("20260619", 0.50, a=0.02),
            _make_slice_result("20261218", 1.00, a=0.04),
        ]
        v = check_calendar_monotonicity(slices, {"calendar_check_moneyness": [0.0]})
        assert v == []


# ---------------------------------------------------------------------------
# TestEvaluateSliceVariance
# ---------------------------------------------------------------------------

class TestEvaluateSliceVariance:
    def test_svi_slice_evaluates(self):
        s = _make_slice_result("20260116", 1.0, a=0.04)
        w = evaluate_slice_variance(s, 0.0)
        assert w is not None
        assert w == pytest.approx(s.params.total_variance(0.0), abs=1e-10)

    def test_failed_slice_returns_none(self):
        failed = SliceFitResult(
            "X", 1.0, "failed", None, [], [], [], [], [],
            float("nan"), float("nan"), 0,
        )
        assert evaluate_slice_variance(failed, 0.0) is None

    def test_spline_slice_evaluates_within_range(self):
        pts = _flat_slice(n=8, sigma=0.20)
        result = fit_slice(pts, "X", {**CFG, "min_points_per_slice": 2})
        if result.model == "spline":
            k_mid = (result.grid_log_moneyness[0] + result.grid_log_moneyness[-1]) / 2
            w = evaluate_slice_variance(result, k_mid)
            assert w is not None
            assert w > 0

    def test_spline_outside_range_returns_none(self):
        pts = [_point(k, 0.20) for k in np.linspace(-0.3, 0.3, 4)]
        cfg2 = {**CFG, "min_points_per_slice": 2}
        result = fit_slice(pts, "X", cfg2)
        if result.model == "spline":
            w = evaluate_slice_variance(result, 5.0)  # far outside grid
            assert w is None

    def test_variance_non_negative(self):
        s = _make_slice_result("X", 1.0, a=0.04)
        for k in np.linspace(-2.0, 2.0, 50):
            w = evaluate_slice_variance(s, k)
            assert w >= 0.0


# ---------------------------------------------------------------------------
# TestInterpolateSurfaceAt
# ---------------------------------------------------------------------------

class TestInterpolateSurfaceAt:
    def _two_slices(self):
        return [
            _make_slice_result("20260619", 0.5, a=0.02),
            _make_slice_result("20261218", 1.0, a=0.04),
        ]

    def test_interpolation_between_slices(self):
        slices = self._two_slices()
        w = interpolate_surface_at(slices, maturity_years=0.75, log_moneyness=0.0)
        assert w is not None
        # Should be between the two slice variances
        w1 = evaluate_slice_variance(slices[0], 0.0)
        w2 = evaluate_slice_variance(slices[1], 0.0)
        assert min(w1, w2) <= w <= max(w1, w2)

    def test_exact_at_slice_maturity(self):
        slices = self._two_slices()
        w_direct = evaluate_slice_variance(slices[0], 0.0)
        w_interp = interpolate_surface_at(slices, maturity_years=0.5, log_moneyness=0.0)
        assert w_interp == pytest.approx(w_direct, abs=1e-8)

    def test_flat_extrapolation_below_range(self):
        slices = self._two_slices()
        w_at_min = evaluate_slice_variance(slices[0], 0.0)
        w_below = interpolate_surface_at(slices, maturity_years=0.1, log_moneyness=0.0)
        assert w_below == pytest.approx(w_at_min, abs=1e-8)

    def test_flat_extrapolation_above_range(self):
        slices = self._two_slices()
        w_at_max = evaluate_slice_variance(slices[1], 0.0)
        w_above = interpolate_surface_at(slices, maturity_years=2.0, log_moneyness=0.0)
        assert w_above == pytest.approx(w_at_max, abs=1e-8)

    def test_no_valid_slices_returns_none(self):
        failed = SliceFitResult("X", 1.0, "failed", None, [], [], [], [], [],
                                float("nan"), float("nan"), 0)
        assert interpolate_surface_at([failed], 1.0, 0.0) is None

    def test_empty_slice_list_returns_none(self):
        assert interpolate_surface_at([], 1.0, 0.0) is None

    def test_midpoint_is_average(self):
        """At T halfway between T1 and T2, w should be the average."""
        slices = self._two_slices()
        w1 = evaluate_slice_variance(slices[0], 0.0)
        w2 = evaluate_slice_variance(slices[1], 0.0)
        w_mid = interpolate_surface_at(slices, maturity_years=0.75, log_moneyness=0.0)
        assert w_mid == pytest.approx((w1 + w2) / 2, abs=1e-8)

    def test_three_slices_uses_correct_bracket(self):
        slices = [
            _make_slice_result("A", 0.25, a=0.01),
            _make_slice_result("B", 0.50, a=0.02),
            _make_slice_result("C", 1.00, a=0.04),
        ]
        # Between B and C (T=0.75)
        w = interpolate_surface_at(slices, 0.75, 0.0)
        w_B = evaluate_slice_variance(slices[1], 0.0)
        w_C = evaluate_slice_variance(slices[2], 0.0)
        assert min(w_B, w_C) <= w <= max(w_B, w_C)


# ---------------------------------------------------------------------------
# TestInterpolateSurfaceGrid
# ---------------------------------------------------------------------------

class TestInterpolateSurfaceGrid:
    def _slices(self):
        return [
            _make_slice_result("A", 0.25, a=0.01),
            _make_slice_result("B", 1.00, a=0.04),
        ]

    def test_grid_shape(self):
        T_grid = [0.25, 0.5, 1.0]
        k_grid = [-0.5, 0.0, 0.5]
        result = interpolate_surface_grid(self._slices(), T_grid, k_grid)
        assert len(result) == 3
        assert all(len(row) == 3 for row in result)

    def test_grid_values_non_negative(self):
        T_grid = np.linspace(0.25, 1.0, 5).tolist()
        k_grid = np.linspace(-0.5, 0.5, 7).tolist()
        result = interpolate_surface_grid(self._slices(), T_grid, k_grid)
        for row in result:
            for w in row:
                if w is not None:
                    assert w >= 0.0

    def test_empty_maturity_grid(self):
        result = interpolate_surface_grid(self._slices(), [], [-0.5, 0.0, 0.5])
        assert result == []

    def test_empty_k_grid(self):
        result = interpolate_surface_grid(self._slices(), [0.5], [])
        assert result == [[]]


# ---------------------------------------------------------------------------
# TestPlotSlice
# ---------------------------------------------------------------------------

class TestPlotSlice:
    def test_returns_figure(self):
        pytest.importorskip("matplotlib")
        import matplotlib.pyplot as plt
        result = fit_slice(_flat_slice(), "20260116", CFG)
        fig = plot_slice(result, CFG)
        assert fig is not None
        plt.close(fig)

    def test_custom_title(self):
        pytest.importorskip("matplotlib")
        import matplotlib.pyplot as plt
        result = fit_slice(_flat_slice(), "20260116", CFG)
        fig = plot_slice(result, CFG, title="My Custom Title")
        ax = fig.axes[0]
        assert "My Custom Title" in ax.get_title()
        plt.close(fig)

    def test_failed_slice_plot_does_not_crash(self):
        pytest.importorskip("matplotlib")
        import matplotlib.pyplot as plt
        failed = SliceFitResult(
            "20260116", 1.0, "failed", None, [], [], [], [], [],
            float("nan"), float("nan"), 0,
        )
        fig = plot_slice(failed, CFG)
        plt.close(fig)


# ---------------------------------------------------------------------------
# TestAcceptanceCriterion
# ---------------------------------------------------------------------------

class TestAcceptanceCriterion:
    """PLAN acceptance: reproducible params; calendar diagnostic computed; metrics exposed."""

    def test_reproducible_surface_params(self):
        pts = []
        for T, exp in [(0.25, "A"), (0.5, "B"), (1.0, "C")]:
            pts.extend(_flat_slice(sigma=0.20, T=T, expiry=exp))
        r1 = fit_surface(pts, CFG)
        r2 = fit_surface(pts, CFG)
        for s1, s2 in zip(r1.slices, r2.slices):
            assert s1.rmse == pytest.approx(s2.rmse, abs=1e-8)
            if s1.params and s2.params:
                assert s1.params.a == pytest.approx(s2.params.a, abs=1e-6)

    def test_calendar_diagnostic_computed(self):
        pts = []
        for T, exp in [(0.25, "A"), (0.5, "B"), (1.0, "C")]:
            pts.extend(_flat_slice(sigma=0.20, T=T, expiry=exp))
        result = fit_surface(pts, CFG)
        assert isinstance(result.calendar_violations, list)

    def test_calendar_violation_detected_when_present(self):
        slices = [
            _make_slice_result("20260116", 0.25, a=0.08),  # high variance at short maturity
            _make_slice_result("20260619", 1.00, a=0.02),  # lower variance at long maturity
        ]
        v = check_calendar_monotonicity(slices, {"calendar_check_moneyness": [0.0]})
        assert len(v) > 0, "Expected calendar violation was not detected"

    def test_fit_error_metrics_exposed(self):
        result = fit_slice(_flat_slice(), "20260116", CFG)
        assert hasattr(result, "rmse")
        assert hasattr(result, "max_error")
        assert hasattr(result, "n_accepted")
        assert not math.isnan(result.rmse)
        assert not math.isnan(result.max_error)
        assert result.n_accepted == 15
