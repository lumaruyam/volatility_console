"""
Tests for Step 17: Dashboard — plots.py pure functions and data-loading helpers.

Acceptance criterion (PLAN):
  Dashboard loads from stored data without live IBKR connection.

All chart tests operate without Streamlit or IBKR.
All data-loading tests use a mock StorageReader (no filesystem needed).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pytest

matplotlib.use("Agg")

import pandas as pd

from src.dashboard.plots import (
    _parse_scenario_shocks,
    build_straddle_summary,
    build_surface_matrix,
    plot_greek_pnl_contributions,
    plot_greeks_by_position,
    plot_historical_prices,
    plot_iv_term_structure,
    plot_scenario_pnl,
    plot_straddle_status,
    plot_uam_gauge,
    plot_vol_surface_heatmap,
)
from src.dashboard.app import (
    _read_risk_free_rate,
    available_dates,
    build_position_risks,
    compute_uam_from_storage,
    default_trade_date,
    load_dashboard_data,
    load_straddle_position,
    make_sample_uam_result,
    save_straddle_position,
)


# ---------------------------------------------------------------------------
# Shared test data helpers
# ---------------------------------------------------------------------------

def _surface_rows(n_strikes=4, n_mats=3) -> list[dict]:
    rows = []
    strikes = [4800 + i * 100 for i in range(n_strikes)]
    maturities = [0.25, 0.50, 1.0][:n_mats]
    for m in maturities:
        for k in strikes:
            rows.append({
                "strike": float(k),
                "maturity_years": m,
                "iv": 0.18 + 0.02 * abs(k - 5000) / 1000 + 0.01 * m,
            })
    return rows


def _iv_rows(n_expiries=2) -> list[dict]:
    rows = []
    expiries = ["2025-06-20", "2025-12-19"][:n_expiries]
    for exp in expiries:
        for k in [4800, 4900, 5000, 5100, 5200]:
            rows.append({
                "expiry_str": exp,
                "strike": float(k),
                "implied_vol": 0.20 + 0.01 * abs(k - 5000) / 100,
                "option_type": "C",
            })
    return rows


def _position_risk_rows() -> list[dict]:
    return [
        {
            "contract_key": "ESTX50_2026_C_5000",
            "underlying_symbol": "ESTX50",
            "dollar_delta": 25000.0,
            "dollar_vega": 8000.0,
        },
        {
            "contract_key": "ESTX50_2026_P_5000",
            "underlying_symbol": "ESTX50",
            "dollar_delta": -23000.0,
            "dollar_vega": 8200.0,
        },
    ]


def _scenario_rows() -> list[dict]:
    return [
        {"scenario_id": "spot_up_5pct", "total_pnl": 1500.0},
        {"scenario_id": "spot_dn_5pct", "total_pnl": -1200.0},
        {"scenario_id": "vol_up_20pts", "total_pnl": 3000.0},
        {"scenario_id": "vol_dn_20pts", "total_pnl": -2800.0},
    ]


def _straddle_pos_dict(dte_days: int = 300) -> dict:
    from datetime import date, timedelta
    expiry = (date(2025, 1, 15) + timedelta(days=dte_days)).isoformat()
    return {
        "position_id": "abc12345",
        "underlying": "ESTX50",
        "status": "open",
        "open_date": "2025-01-15",
        "target_expiry": expiry,
        "notional": 100_000.0,
        "call_leg": {
            "contract_key": "ESTX50_C_5000",
            "option_type": "C",
            "strike": 5000.0,
            "expiry_str": expiry,
            "quantity": 2.0,
            "open_price": 150.0,
            "multiplier": 10.0,
            "current_price": 165.0,
        },
        "put_leg": {
            "contract_key": "ESTX50_P_5000",
            "option_type": "P",
            "strike": 5000.0,
            "expiry_str": expiry,
            "quantity": 2.0,
            "open_price": 145.0,
            "multiplier": 10.0,
            "current_price": 130.0,
        },
    }


def _uam_dict() -> dict:
    return {
        "portfolio_id": "STRADDLE_PAPER",
        "uam_ratio": 0.35,
        "margin_requirement": 35_000.0,
        "portfolio_gross_value": 100_000.0,
        "worst_case_pnl": -35_000.0,
        "scenario_pnls": {
            "up_vol_up": 5000.0,
            "up_vol_dn": -12000.0,
            "dn_vol_up": -35000.0,
            "dn_vol_dn": -28000.0,
        },
    }


_SENTINEL = object()

def _mock_reader(
    surface_grid=None, iv_points=None, snapshots=None,
    surface_params=None, forward_curve=None, dates=_SENTINEL,
    pricing_results=None, positions=None, scenario_results=None,
    storage_root="/tmp/test_storage",
) -> MagicMock:
    r = MagicMock()
    r.storage_root = storage_root
    r.read_surface_grid.return_value = surface_grid or []
    r.read_iv_points.return_value = iv_points or []
    r.read_snapshots.return_value = snapshots or []
    r.read_surface_parameters.return_value = surface_params or []
    r.read_forward_curve.return_value = forward_curve or []
    r.list_partitions.return_value = ["2025-01-15"] if dates is _SENTINEL else dates
    r.read_pricing_results.return_value = pricing_results or []
    r.read_positions.return_value = positions or []
    r.read_scenario_results.return_value = scenario_results or []
    return r


def _pricing_rows() -> list[dict]:
    return [
        {
            "contract_key": "ESTX50_C_5000",
            "underlying": "ESTX50",
            "delta": 0.5,
            "gamma": 0.001,
            "vega_per_point": 80.0,
            "theta_per_day": -4.0,
            "model_price": 150.0,
            "sigma_used": 0.20,
            "forward_used": 5000.0,
            "snapshot_ts": 1700000000.0,
        },
        {
            "contract_key": "ESTX50_P_5000",
            "underlying": "ESTX50",
            "delta": -0.5,
            "gamma": 0.001,
            "vega_per_point": 80.0,
            "theta_per_day": -4.0,
            "model_price": 145.0,
            "sigma_used": 0.20,
            "forward_used": 5000.0,
            "snapshot_ts": 1700000000.0,
        },
    ]


def _snapshot_rows_with_spot(spot: float = 5000.0) -> list[dict]:
    return [
        {
            "reference_spot": spot,
            "snapshot_ts": 1700000000.0,
            "instrument_key": "ESTX50_C_5000",
            "maturity_years": 1.0,
        },
        {
            "reference_spot": spot,
            "snapshot_ts": 1700000001.0,
            "instrument_key": "ESTX50_P_5000",
            "maturity_years": 1.0,
        },
    ]


# ===========================================================================
# TestBuildSurfaceMatrix
# ===========================================================================

class TestBuildSurfaceMatrix:
    def test_returns_dict(self):
        result = build_surface_matrix(_surface_rows())
        assert isinstance(result, dict)
        assert "strikes" in result
        assert "maturities" in result
        assert "iv_matrix" in result

    def test_correct_shape(self):
        result = build_surface_matrix(_surface_rows(n_strikes=4, n_mats=3))
        assert result["n_strikes"] == 4
        assert result["n_maturities"] == 3
        assert result["iv_matrix"].shape == (3, 4)

    def test_empty_rows_returns_empty(self):
        result = build_surface_matrix([])
        assert result["n_strikes"] == 0
        assert result["n_maturities"] == 0
        assert result["iv_matrix"].shape == (0, 0)

    def test_no_nan_when_fully_populated(self):
        result = build_surface_matrix(_surface_rows())
        assert not np.any(np.isnan(result["iv_matrix"]))

    def test_nan_for_missing_cell(self):
        rows = _surface_rows(n_strikes=3, n_mats=2)
        rows.pop(0)  # remove one cell
        result = build_surface_matrix(rows)
        assert np.any(np.isnan(result["iv_matrix"]))

    def test_strikes_sorted(self):
        result = build_surface_matrix(_surface_rows())
        assert result["strikes"] == sorted(result["strikes"])

    def test_maturities_sorted(self):
        result = build_surface_matrix(_surface_rows())
        assert result["maturities"] == sorted(result["maturities"])

    def test_accepts_implied_vol_key(self):
        rows = [{"strike": 5000.0, "maturity_years": 0.5, "implied_vol": 0.20}]
        result = build_surface_matrix(rows)
        assert result["iv_matrix"][0, 0] == pytest.approx(0.20)

    def test_skips_rows_missing_iv(self):
        rows = [{"strike": 5000.0, "maturity_years": 0.5, "iv": None}]
        result = build_surface_matrix(rows)
        assert np.isnan(result["iv_matrix"][0, 0])


# ===========================================================================
# TestPlotVolSurfaceHeatmap
# ===========================================================================

class TestPlotVolSurfaceHeatmap:
    def test_returns_figure(self):
        fig = plot_vol_surface_heatmap(_surface_rows())
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_data_returns_figure(self):
        fig = plot_vol_surface_heatmap([])
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_has_axes(self):
        fig = plot_vol_surface_heatmap(_surface_rows())
        assert len(fig.axes) >= 1
        plt.close(fig)

    def test_title_contains_underlying(self):
        fig = plot_vol_surface_heatmap(_surface_rows(), underlying="ESTX50", trade_date="2025-01-15")
        suptitle = fig.texts[0].get_text() if fig.texts else ""
        assert "ESTX50" in suptitle
        plt.close(fig)

    def test_title_contains_date(self):
        fig = plot_vol_surface_heatmap(_surface_rows(), underlying="ESTX50", trade_date="2025-01-15")
        suptitle = fig.texts[0].get_text() if fig.texts else ""
        assert "2025-01-15" in suptitle
        plt.close(fig)

    def test_no_ibkr_needed(self):
        # Should produce a figure without any live connection
        fig = plot_vol_surface_heatmap(_surface_rows(n_strikes=5, n_mats=4))
        assert fig is not None
        plt.close(fig)


# ===========================================================================
# TestPlotIvTermStructure
# ===========================================================================

class TestPlotIvTermStructure:
    def test_returns_figure(self):
        fig = plot_iv_term_structure(_iv_rows())
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_returns_figure(self):
        fig = plot_iv_term_structure([])
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_has_legend_when_data_present(self):
        fig = plot_iv_term_structure(_iv_rows(n_expiries=2))
        ax = fig.axes[0]
        legend = ax.get_legend()
        assert legend is not None
        plt.close(fig)

    def test_title_contains_underlying(self):
        fig = plot_iv_term_structure(_iv_rows(), underlying="ESTX50")
        suptitle = fig.texts[0].get_text() if fig.texts else ""
        assert "ESTX50" in suptitle
        plt.close(fig)

    def test_one_line_per_expiry(self):
        rows = _iv_rows(n_expiries=2)
        fig = plot_iv_term_structure(rows)
        ax = fig.axes[0]
        assert len(ax.lines) == 2
        plt.close(fig)


# ===========================================================================
# TestPlotGreeksByPosition
# ===========================================================================

class TestPlotGreeksByPosition:
    def test_returns_figure(self):
        fig = plot_greeks_by_position(_position_risk_rows(), "ESTX50")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_returns_figure(self):
        fig = plot_greeks_by_position([], "ESTX50")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_bars_present(self):
        fig = plot_greeks_by_position(_position_risk_rows(), "ESTX50")
        ax = fig.axes[0]
        assert len(ax.patches) > 0
        plt.close(fig)

    def test_two_bars_per_contract(self):
        rows = _position_risk_rows()
        fig = plot_greeks_by_position(rows, "ESTX50")
        ax = fig.axes[0]
        # 2 contracts × 2 greek bars = 4 patches
        assert len(ax.patches) == 4
        plt.close(fig)

    def test_no_filter_without_underlying(self):
        fig = plot_greeks_by_position(_position_risk_rows())
        assert isinstance(fig, plt.Figure)
        plt.close(fig)


# ===========================================================================
# TestPlotScenarioPnl
# ===========================================================================

class TestPlotScenarioPnl:
    def test_returns_figure(self):
        fig = plot_scenario_pnl(_scenario_rows())
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_returns_figure(self):
        fig = plot_scenario_pnl([])
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_bars_count_matches_scenarios(self):
        rows = _scenario_rows()
        fig = plot_scenario_pnl(rows)
        ax = fig.axes[0]
        assert len(ax.patches) == len(rows)
        plt.close(fig)

    def test_title_shown(self):
        fig = plot_scenario_pnl(_scenario_rows(), title="My Scenarios")
        suptitle = fig.texts[0].get_text() if fig.texts else ""
        assert "My Scenarios" in suptitle
        plt.close(fig)

    def test_accepts_pnl_key_fallback(self):
        rows = [{"scenario_id": "s1", "pnl": 1000.0}]
        fig = plot_scenario_pnl(rows)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_sorted_by_pnl(self):
        rows = [
            {"scenario_id": "worst", "total_pnl": -5000.0},
            {"scenario_id": "best", "total_pnl": 3000.0},
        ]
        fig = plot_scenario_pnl(rows)
        ax = fig.axes[0]
        labels = [t.get_text() for t in ax.get_yticklabels()]
        assert labels[0] == "worst"  # lowest pnl first (bottom of horizontal bar)
        plt.close(fig)


# ===========================================================================
# TestBuildStraddleSummary
# ===========================================================================

class TestBuildStraddleSummary:
    def test_returns_dict(self):
        s = build_straddle_summary(_straddle_pos_dict(), "2025-01-15")
        assert isinstance(s, dict)

    def test_dte_calculation(self):
        from datetime import date, timedelta
        pos = _straddle_pos_dict(dte_days=300)
        s = build_straddle_summary(pos, "2025-01-15")
        assert s["dte"] == 300

    def test_dte_zero_past_expiry(self):
        pos = _straddle_pos_dict(dte_days=5)
        s = build_straddle_summary(pos, "2025-01-25")  # way past expiry
        assert s["dte"] == 0

    def test_pnl_call_computed(self):
        # call open=150, current=165, qty=2, mult=10 → pnl = (165-150)*2*10 = 300
        pos = _straddle_pos_dict()
        s = build_straddle_summary(pos, "2025-01-15")
        assert s["pnl_call"] == pytest.approx(300.0)

    def test_pnl_put_computed(self):
        # put open=145, current=130, qty=2, mult=10 → pnl = (130-145)*2*10 = -300
        pos = _straddle_pos_dict()
        s = build_straddle_summary(pos, "2025-01-15")
        assert s["pnl_put"] == pytest.approx(-300.0)

    def test_total_pnl_is_sum(self):
        pos = _straddle_pos_dict()
        s = build_straddle_summary(pos, "2025-01-15")
        assert s["total_pnl"] == pytest.approx(s["pnl_call"] + s["pnl_put"])

    def test_fields_present(self):
        s = build_straddle_summary(_straddle_pos_dict(), "2025-01-15")
        for key in ("position_id", "underlying", "status", "open_date",
                    "target_expiry", "dte", "strike", "quantity",
                    "notional", "call_contract", "put_contract"):
            assert key in s

    def test_empty_dict_no_crash(self):
        s = build_straddle_summary({}, "2025-01-15")
        assert s["dte"] == 0
        assert s["total_pnl"] == pytest.approx(0.0)

    def test_missing_current_price_uses_open(self):
        pos = _straddle_pos_dict()
        pos["call_leg"].pop("current_price", None)
        s = build_straddle_summary(pos, "2025-01-15")
        assert s["pnl_call"] == pytest.approx(0.0)


# ===========================================================================
# TestPlotStraddleStatus
# ===========================================================================

class TestPlotStraddleStatus:
    def test_returns_figure(self):
        fig = plot_straddle_status(_straddle_pos_dict(), "2025-01-15")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_has_two_subplots(self):
        fig = plot_straddle_status(_straddle_pos_dict(), "2025-01-15")
        assert len(fig.axes) == 2
        plt.close(fig)

    def test_empty_dict_no_crash(self):
        fig = plot_straddle_status({}, "2025-01-15")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_dte_in_suptitle(self):
        pos = _straddle_pos_dict(dte_days=300)
        fig = plot_straddle_status(pos, "2025-01-15")
        suptitle = fig.texts[0].get_text() if fig.texts else ""
        assert "300" in suptitle
        plt.close(fig)

    def test_underlying_in_suptitle(self):
        fig = plot_straddle_status(_straddle_pos_dict(), "2025-01-15")
        suptitle = fig.texts[0].get_text() if fig.texts else ""
        assert "ESTX50" in suptitle
        plt.close(fig)


# ===========================================================================
# TestPlotUamGauge
# ===========================================================================

class TestPlotUamGauge:
    def test_returns_figure(self):
        fig = plot_uam_gauge(_uam_dict())
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_dict_no_crash(self):
        fig = plot_uam_gauge({})
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_has_two_subplots(self):
        fig = plot_uam_gauge(_uam_dict())
        assert len(fig.axes) == 2
        plt.close(fig)

    def test_accepts_dataclass_like_object(self):
        class UAM:
            portfolio_id = "P1"
            uam_ratio = 0.42
            margin_requirement = 42_000.0
            portfolio_gross_value = 100_000.0
            worst_case_pnl = -42_000.0
            scenario_pnls = {"up_vol_up": 1000.0, "dn_vol_dn": -42_000.0}
        fig = plot_uam_gauge(UAM())
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_high_ratio_renders(self):
        uam = {**_uam_dict(), "uam_ratio": 1.8}
        fig = plot_uam_gauge(uam)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_scenario_bars_count(self):
        fig = plot_uam_gauge(_uam_dict())
        ax = fig.axes[1]  # scenario subplot
        assert len(ax.patches) == 4  # 4 scenarios
        plt.close(fig)


# ===========================================================================
# TestLoadDashboardData
# ===========================================================================

class TestLoadDashboardData:
    def test_returns_dict(self):
        reader = _mock_reader(surface_grid=_surface_rows(), iv_points=_iv_rows())
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert isinstance(data, dict)

    def test_keys_present(self):
        reader = _mock_reader()
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        for key in ("trade_date", "underlying", "surface_grid", "iv_points",
                    "snapshots", "surface_params", "forward_curve"):
            assert key in data

    def test_trade_date_stored(self):
        reader = _mock_reader()
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert data["trade_date"] == "2025-01-15"

    def test_underlying_stored(self):
        reader = _mock_reader()
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert data["underlying"] == "ESTX50"

    def test_surface_grid_passed_through(self):
        rows = _surface_rows()
        reader = _mock_reader(surface_grid=rows)
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert len(data["surface_grid"]) == len(rows)

    def test_no_ibkr_session_needed(self):
        reader = _mock_reader()
        # Should not touch anything IBKR-related
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert data is not None

    def test_calls_reader_methods(self):
        reader = _mock_reader()
        load_dashboard_data(reader, "2025-01-15", "ESTX50")
        reader.read_surface_grid.assert_called_once_with("2025-01-15", "ESTX50")
        reader.read_iv_points.assert_called_once_with("2025-01-15", "ESTX50")

    def test_empty_storage_returns_empty_lists(self):
        reader = _mock_reader()
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert data["surface_grid"] == []
        assert data["iv_points"] == []


# ===========================================================================
# TestAvailableDates
# ===========================================================================

class TestAvailableDates:
    def test_returns_list(self):
        reader = _mock_reader(dates=["2025-01-14", "2025-01-15"])
        assert isinstance(available_dates(reader), list)

    def test_delegates_to_reader(self):
        reader = _mock_reader(dates=["2025-01-15"])
        dates = available_dates(reader)
        reader.list_partitions.assert_called_once_with("analytics", "surface_grid")
        assert dates == ["2025-01-15"]

    def test_empty_when_no_data(self):
        reader = _mock_reader(dates=[])
        assert available_dates(reader) == []


class TestDefaultTradeDate:
    def test_returns_last_date_when_data_exists(self):
        reader = _mock_reader(dates=["2025-01-13", "2025-01-14", "2025-01-15"])
        assert default_trade_date(reader) == "2025-01-15"

    def test_returns_today_string_when_no_data(self):
        from datetime import date
        reader = _mock_reader(dates=[])
        result = default_trade_date(reader)
        # Should be a valid ISO date string
        date.fromisoformat(result)  # raises if invalid


# ===========================================================================
# TestMakeSampleUamResult
# ===========================================================================

class TestMakeSampleUamResult:
    def test_returns_dict(self):
        assert isinstance(make_sample_uam_result(), dict)

    def test_keys_present(self):
        r = make_sample_uam_result()
        for key in ("portfolio_id", "uam_ratio", "margin_requirement",
                    "portfolio_gross_value", "worst_case_pnl", "scenario_pnls"):
            assert key in r

    def test_portfolio_id_used(self):
        r = make_sample_uam_result("MY_PORTFOLIO")
        assert r["portfolio_id"] == "MY_PORTFOLIO"

    def test_four_scenarios(self):
        r = make_sample_uam_result()
        assert len(r["scenario_pnls"]) == 4


# ===========================================================================
# TestAcceptanceCriterion
# ===========================================================================

class TestAcceptanceCriterion:
    """PLAN: Dashboard loads from stored data without live IBKR connection."""

    def test_full_dashboard_pipeline_no_ibkr(self):
        """End-to-end: mock reader → load data → build all charts. Zero IBKR."""
        reader = _mock_reader(
            surface_grid=_surface_rows(),
            iv_points=_iv_rows(),
            dates=["2025-01-15"],
        )
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert data["surface_grid"]

        fig1 = plot_vol_surface_heatmap(data["surface_grid"], "ESTX50", "2025-01-15")
        fig2 = plot_iv_term_structure(data["iv_points"], "ESTX50", "2025-01-15")
        fig3 = plot_greeks_by_position(_position_risk_rows(), "ESTX50")
        fig4 = plot_scenario_pnl(_scenario_rows(), "Test Scenarios")
        fig5 = plot_straddle_status(_straddle_pos_dict(), "2025-01-15")
        fig6 = plot_uam_gauge(_uam_dict())

        for fig in (fig1, fig2, fig3, fig4, fig5, fig6):
            assert isinstance(fig, plt.Figure)
            plt.close(fig)

    def test_empty_storage_does_not_crash(self):
        """If storage has no data, every panel still returns a valid figure."""
        reader = _mock_reader()
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")

        fig1 = plot_vol_surface_heatmap(data["surface_grid"])
        fig2 = plot_iv_term_structure(data["iv_points"])
        fig3 = plot_greeks_by_position(data["position_risks"])
        fig4 = plot_scenario_pnl(data["scenario_results"])

        for fig in (fig1, fig2, fig3, fig4):
            assert isinstance(fig, plt.Figure)
            plt.close(fig)

    def test_app_module_imports_without_ibkr(self):
        """Importing app.py must not require a live IBKR connection."""
        import importlib
        # Re-import to check the import doesn't crash
        import src.dashboard.app as app_mod
        assert hasattr(app_mod, "main")
        assert hasattr(app_mod, "load_dashboard_data")

    def test_straddle_panel_shows_dte(self):
        pos = _straddle_pos_dict(dte_days=200)
        summary = build_straddle_summary(pos, "2025-01-15")
        assert summary["dte"] == 200

    def test_uam_gauge_renderable_at_various_ratios(self):
        for ratio in (0.0, 0.3, 0.7, 1.0, 1.5, 2.0):
            uam = {**_uam_dict(), "uam_ratio": ratio}
            fig = plot_uam_gauge(uam)
            assert isinstance(fig, plt.Figure)
            plt.close(fig)


# ===========================================================================
# TestSaveLoadStraddlePosition
# ===========================================================================

class TestSaveLoadStraddlePosition:
    def test_round_trip_dict(self, tmp_path):
        pos = _straddle_pos_dict()
        save_straddle_position(pos, str(tmp_path), "STRADDLE_PAPER")
        loaded = load_straddle_position(str(tmp_path), "STRADDLE_PAPER")
        assert loaded is not None
        assert loaded["position_id"] == pos["position_id"]
        assert loaded["underlying"] == pos["underlying"]

    def test_creates_positions_dir(self, tmp_path):
        pos = _straddle_pos_dict()
        save_straddle_position(pos, str(tmp_path), "STRADDLE_PAPER")
        assert (tmp_path / "positions" / "STRADDLE_PAPER.json").exists()

    def test_load_missing_returns_none(self, tmp_path):
        result = load_straddle_position(str(tmp_path), "NO_SUCH_ID")
        assert result is None

    def test_save_dataclass(self, tmp_path):
        from dataclasses import dataclass

        @dataclass
        class FakePosition:
            position_id: str = "dc_pos"
            underlying: str = "ESTX50"

        pos = FakePosition()
        save_straddle_position(pos, str(tmp_path), "DC_PORTFOLIO")
        loaded = load_straddle_position(str(tmp_path), "DC_PORTFOLIO")
        assert loaded["position_id"] == "dc_pos"

    def test_load_corrupt_json_returns_none(self, tmp_path):
        path = tmp_path / "positions" / "BAD.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json")
        result = load_straddle_position(str(tmp_path), "BAD")
        assert result is None

    def test_overwrite_existing(self, tmp_path):
        pos1 = _straddle_pos_dict()
        pos1["position_id"] = "v1"
        save_straddle_position(pos1, str(tmp_path), "PF")
        pos2 = _straddle_pos_dict()
        pos2["position_id"] = "v2"
        save_straddle_position(pos2, str(tmp_path), "PF")
        loaded = load_straddle_position(str(tmp_path), "PF")
        assert loaded["position_id"] == "v2"

    def test_different_portfolio_ids(self, tmp_path):
        pos_a = _straddle_pos_dict()
        pos_a["position_id"] = "aa"
        pos_b = _straddle_pos_dict()
        pos_b["position_id"] = "bb"
        save_straddle_position(pos_a, str(tmp_path), "PF_A")
        save_straddle_position(pos_b, str(tmp_path), "PF_B")
        assert load_straddle_position(str(tmp_path), "PF_A")["position_id"] == "aa"
        assert load_straddle_position(str(tmp_path), "PF_B")["position_id"] == "bb"


# ===========================================================================
# TestBuildPositionRisks
# ===========================================================================

class TestBuildPositionRisks:
    def test_empty_pricing_rows_returns_empty(self):
        result = build_position_risks([], _snapshot_rows_with_spot(), [], "PF")
        assert result == []

    def test_no_snapshot_rows_returns_empty(self):
        result = build_position_risks(_pricing_rows(), [], [], "PF")
        assert result == []

    def test_zero_spot_returns_empty(self):
        snaps = [{"reference_spot": 0.0, "snapshot_ts": 1.0}]
        result = build_position_risks(_pricing_rows(), snaps, [], "PF")
        assert result == []

    def test_returns_position_risk_objects(self):
        from src.risk.models import PositionRisk
        result = build_position_risks(
            _pricing_rows(), _snapshot_rows_with_spot(), [], "PF"
        )
        assert len(result) == 2
        assert all(isinstance(r, PositionRisk) for r in result)

    def test_spot_set_correctly(self):
        result = build_position_risks(
            _pricing_rows(), _snapshot_rows_with_spot(spot=4800.0), [], "PF"
        )
        assert all(r.spot == pytest.approx(4800.0) for r in result)

    def test_portfolio_id_assigned(self):
        result = build_position_risks(
            _pricing_rows(), _snapshot_rows_with_spot(), [], "MY_PF"
        )
        assert all(r.portfolio_id == "MY_PF" for r in result)

    def test_multiplier_passed_through(self):
        result = build_position_risks(
            _pricing_rows(), _snapshot_rows_with_spot(), [], "PF", multiplier=100.0
        )
        assert all(r.multiplier == pytest.approx(100.0) for r in result)

    def test_quantity_from_position_rows(self):
        position_rows = [
            {"contract_key": "ESTX50_C_5000", "quantity": 3.0},
            {"contract_key": "ESTX50_P_5000", "quantity": 5.0},
        ]
        result = build_position_risks(
            _pricing_rows(), _snapshot_rows_with_spot(), position_rows, "PF"
        )
        qty_map = {r.contract_key: r.quantity for r in result}
        assert qty_map["ESTX50_C_5000"] == pytest.approx(3.0)
        assert qty_map["ESTX50_P_5000"] == pytest.approx(5.0)

    def test_default_quantity_is_one(self):
        result = build_position_risks(
            _pricing_rows(), _snapshot_rows_with_spot(), [], "PF"
        )
        assert all(r.quantity == pytest.approx(1.0) for r in result)

    def test_filter_by_underlying(self):
        other_row = {
            "contract_key": "SPY_C_500",
            "underlying": "SPY",
            "delta": 0.5, "gamma": 0.001, "vega_per_point": 50.0,
            "theta_per_day": -2.0, "model_price": 10.0,
            "sigma_used": 0.15, "forward_used": 500.0, "snapshot_ts": 1700000000.0,
        }
        result = build_position_risks(
            _pricing_rows() + [other_row],
            _snapshot_rows_with_spot(),
            [],
            "PF",
            underlying="ESTX50",
        )
        assert all(r.underlying_symbol == "ESTX50" for r in result)
        assert len(result) == 2

    def test_dollar_delta_computed(self):
        rows = [_pricing_rows()[0]]
        snaps = [{"reference_spot": 5000.0, "snapshot_ts": 1.0}]
        result = build_position_risks(rows, snaps, [], "PF", multiplier=10.0)
        r = result[0]
        expected_dd = 0.5 * 5000.0 * 1.0 * 10.0
        assert r.dollar_delta == pytest.approx(expected_dd)

    def test_rows_missing_contract_key_skipped(self):
        bad_row = {
            "underlying": "ESTX50", "delta": 0.5, "model_price": 100.0,
            "sigma_used": 0.20, "snapshot_ts": 1.0,
        }
        result = build_position_risks(
            [bad_row], _snapshot_rows_with_spot(), [], "PF"
        )
        assert result == []

    def test_maturity_from_snapshot(self):
        result = build_position_risks(
            [_pricing_rows()[0]], _snapshot_rows_with_spot(), [], "PF"
        )
        assert result[0].maturity_years == pytest.approx(1.0)

    def test_latest_snapshot_wins_for_spot(self):
        snaps = [
            {"reference_spot": 4000.0, "snapshot_ts": 100.0},
            {"reference_spot": 5000.0, "snapshot_ts": 200.0},
        ]
        result = build_position_risks(_pricing_rows(), snaps, [], "PF")
        assert result[0].spot == pytest.approx(5000.0)


# ===========================================================================
# TestComputeUamFromStorage
# ===========================================================================

class TestComputeUamFromStorage:
    def test_no_pricing_data_returns_none(self):
        reader = _mock_reader(pricing_results=[], snapshots=[])
        result = compute_uam_from_storage(
            reader, "2025-01-15", "ESTX50", "PF", {}
        )
        assert result is None

    def test_no_snapshot_returns_none(self):
        reader = _mock_reader(pricing_results=_pricing_rows(), snapshots=[])
        result = compute_uam_from_storage(
            reader, "2025-01-15", "ESTX50", "PF", {}
        )
        assert result is None

    def test_returns_uam_result_with_data(self):
        from src.risk.models import UAMResult
        reader = _mock_reader(
            pricing_results=_pricing_rows(),
            snapshots=_snapshot_rows_with_spot(),
        )
        result = compute_uam_from_storage(
            reader, "2025-01-15", "ESTX50", "PF",
            {"spot_shock_pct": 0.05, "vol_shock_abs": 0.20}
        )
        assert isinstance(result, UAMResult)

    def test_uam_ratio_is_float(self):
        from src.risk.models import UAMResult
        reader = _mock_reader(
            pricing_results=_pricing_rows(),
            snapshots=_snapshot_rows_with_spot(),
        )
        result = compute_uam_from_storage(
            reader, "2025-01-15", "ESTX50", "PF", {}
        )
        assert isinstance(result, UAMResult)
        assert isinstance(result.uam_ratio, float)

    def test_four_scenario_pnls(self):
        reader = _mock_reader(
            pricing_results=_pricing_rows(),
            snapshots=_snapshot_rows_with_spot(),
        )
        result = compute_uam_from_storage(
            reader, "2025-01-15", "ESTX50", "PF", {}
        )
        assert len(result.scenario_pnls) == 4

    def test_portfolio_id_assigned(self):
        reader = _mock_reader(
            pricing_results=_pricing_rows(),
            snapshots=_snapshot_rows_with_spot(),
        )
        result = compute_uam_from_storage(
            reader, "2025-01-15", "ESTX50", "MY_PF", {}
        )
        assert result.portfolio_id == "MY_PF"

    def test_reader_methods_called(self):
        reader = _mock_reader(pricing_results=[], snapshots=[])
        compute_uam_from_storage(reader, "2025-01-15", "ESTX50", "PF", {})
        reader.read_pricing_results.assert_called_once_with("2025-01-15", "ESTX50")
        reader.read_snapshots.assert_called_once_with("2025-01-15", "ESTX50")


# ===========================================================================
# Additional TestLoadDashboardData coverage
# ===========================================================================

class TestLoadDashboardDataExtended:
    def test_straddle_position_key_present(self):
        reader = _mock_reader()
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert "straddle_position" in data

    def test_uam_result_key_present(self):
        reader = _mock_reader()
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert "uam_result" in data

    def test_no_straddle_position_when_no_file(self, tmp_path):
        reader = _mock_reader(storage_root=str(tmp_path))
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert data["straddle_position"] is None

    def test_straddle_position_loaded_when_file_exists(self, tmp_path):
        pos = _straddle_pos_dict()
        save_straddle_position(pos, str(tmp_path), "STRADDLE_PAPER")
        reader = _mock_reader(storage_root=str(tmp_path))
        data = load_dashboard_data(
            reader, "2025-01-15", "ESTX50", portfolio_id="STRADDLE_PAPER"
        )
        assert data["straddle_position"] is not None
        assert data["straddle_position"]["position_id"] == pos["position_id"]

    def test_uam_result_none_when_no_pricing(self, tmp_path):
        reader = _mock_reader(pricing_results=[], snapshots=[], storage_root=str(tmp_path))
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert data["uam_result"] is None

    def test_uam_result_populated_with_pricing_data(self, tmp_path):
        from src.risk.models import UAMResult
        reader = _mock_reader(
            pricing_results=_pricing_rows(),
            snapshots=_snapshot_rows_with_spot(),
            storage_root=str(tmp_path),
        )
        data = load_dashboard_data(reader, "2025-01-15", "ESTX50")
        assert isinstance(data["uam_result"], UAMResult)

    def test_custom_portfolio_id(self, tmp_path):
        pos = _straddle_pos_dict()
        pos["position_id"] = "custom_id"
        save_straddle_position(pos, str(tmp_path), "CUSTOM_PF")
        reader = _mock_reader(storage_root=str(tmp_path))
        data = load_dashboard_data(
            reader, "2025-01-15", "ESTX50", portfolio_id="CUSTOM_PF"
        )
        assert data["straddle_position"]["position_id"] == "custom_id"

    def test_calls_read_pricing_results(self):
        reader = _mock_reader()
        load_dashboard_data(reader, "2025-01-15", "ESTX50")
        # Called at least once (also called internally by compute_uam_from_storage)
        reader.read_pricing_results.assert_any_call("2025-01-15", "ESTX50")


# ---------------------------------------------------------------------------
# Helpers shared by Greek-contribution tests
# ---------------------------------------------------------------------------

def _risk_row(contract_key="ESTX50|OPT|EUREX|EUR|2027-06-01|5100|C",
              delta=0.52, gamma=0.0008, vega_per_point=18.5, theta_per_day=-4.2,
              spot=5100.0, qty=1.0, mult=10.0, dollar_rho=None):
    row = {
        "contract_key": contract_key,
        "underlying_symbol": "ESTX50",
        "spot": spot,
        "quantity": qty,
        "multiplier": mult,
        "delta": delta,
        "gamma": gamma,
        "vega_per_point": vega_per_point,
        "theta_per_day": theta_per_day,
    }
    if dollar_rho is not None:
        row["dollar_rho"] = dollar_rho
    return row


class TestParseScenarioShocks:

    def test_spot_minus_vol_minus(self):
        s, v = _parse_scenario_shocks("sm25_vm15")
        assert s == pytest.approx(-0.25)
        assert v == pytest.approx(-0.15)

    def test_spot_plus_vol_plus(self):
        s, v = _parse_scenario_shocks("sp10_vp5")
        assert s == pytest.approx(0.10)
        assert v == pytest.approx(0.05)

    def test_spot_zero_vol_zero(self):
        s, v = _parse_scenario_shocks("s0_v0")
        assert s == pytest.approx(0.0)
        assert v == pytest.approx(0.0)

    def test_spot_zero_vol_minus(self):
        s, v = _parse_scenario_shocks("s0_vm5")
        assert s == pytest.approx(0.0)
        assert v == pytest.approx(-0.05)

    def test_spot_minus_vol_zero(self):
        s, v = _parse_scenario_shocks("sm10_v0")
        assert s == pytest.approx(-0.10)
        assert v == pytest.approx(0.0)

    def test_unrecognised_returns_none(self):
        assert _parse_scenario_shocks("crash") == (None, None)
        assert _parse_scenario_shocks("spot_dn_10") == (None, None)
        assert _parse_scenario_shocks("theta_1d") == (None, None)


class TestPlotGreekPnlContributions:

    def _rows(self):
        return [
            _risk_row("ESTX50|OPT|EUREX|EUR|2027-06-01|5100|C",
                      delta=0.52, gamma=0.0008, vega_per_point=18.5, theta_per_day=-4.2),
            _risk_row("ESTX50|OPT|EUREX|EUR|2027-06-01|5100|P",
                      delta=-0.48, gamma=0.0008, vega_per_point=18.3, theta_per_day=-4.1),
        ]

    def test_returns_figure(self):
        fig = plot_greek_pnl_contributions(self._rows(), "sm25_vm15")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_rows_returns_figure(self):
        fig = plot_greek_pnl_contributions([], "sm25_vm15")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_unparseable_scenario_returns_figure(self):
        fig = plot_greek_pnl_contributions(self._rows(), "crash")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_has_correct_number_of_bar_groups(self):
        rows = self._rows()
        fig = plot_greek_pnl_contributions(rows, "sm25_vm15")
        ax = fig.axes[0]
        # 5 Greeks × 2 positions = 10 bar containers
        containers = ax.containers
        assert len(containers) == 5
        plt.close(fig)

    def test_delta_pnl_positive(self):
        """All contributions are absolute values — bars must be non-negative."""
        fig = plot_greek_pnl_contributions(self._rows(), "sm25_vm15")
        ax = fig.axes[0]
        for container in ax.containers:
            for bar in container:
                assert bar.get_height() >= 0.0
        plt.close(fig)

    def test_explicit_shocks_override_scenario_id(self):
        """Explicit spot_shift_pct / vol_shift_abs bypass id parsing."""
        fig = plot_greek_pnl_contributions(
            self._rows(), "crash",
            spot_shift_pct=-0.10, vol_shift_abs=-0.05,
        )
        # Should render a real chart, not the "can't parse" message
        ax = fig.axes[0]
        assert len(ax.containers) == 5
        plt.close(fig)

    def test_rho_bar_zero_when_not_in_rows(self):
        """Row without dollar_rho must yield |ρ PnL| = 0."""
        fig = plot_greek_pnl_contributions(self._rows(), "sm10_vp5")
        ax = fig.axes[0]
        rho_container = ax.containers[4]   # 5th group = ρ
        heights = [b.get_height() for b in rho_container]
        assert all(h == pytest.approx(0.0) for h in heights)
        plt.close(fig)

    def test_rho_bar_nonzero_when_provided(self):
        """Row with dollar_rho must yield a non-zero |ρ PnL| bar."""
        rows = [_risk_row(dollar_rho=2.55)]
        fig = plot_greek_pnl_contributions(rows, "sm10_vp5", rate_shock_bp=25.0)
        ax = fig.axes[0]
        rho_heights = [b.get_height() for b in ax.containers[4]]
        assert any(h > 0 for h in rho_heights)
        plt.close(fig)

    def test_formula_delta_contribution(self):
        """
        |ΔPnL| = |delta × dS × qty × mult|
        delta=0.52, spot=5100, spot_shift=-25% → dS=-1275,
        |0.52 × −1275 × 1 × 10| = 6630.
        """
        row = _risk_row(delta=0.52, gamma=0.0, vega_per_point=0.0, theta_per_day=0.0)
        fig = plot_greek_pnl_contributions([row], "sm25_v0")
        ax = fig.axes[0]
        delta_bar_height = ax.containers[0][0].get_height()
        expected = abs(0.52 * (5100 * -0.25) * 1.0 * 10.0)
        assert delta_bar_height == pytest.approx(expected, rel=1e-6)
        plt.close(fig)


# ---------------------------------------------------------------------------
# Helpers for historical price tests
# ---------------------------------------------------------------------------

def _price_df(n: int = 300, col: str = "Adj Close") -> pd.DataFrame:
    """Synthetic daily price DataFrame with a DatetimeIndex."""
    idx = pd.date_range("2022-01-03", periods=n, freq="B", tz="UTC")
    prices = 4000.0 + np.cumsum(np.random.default_rng(0).normal(0, 20, n))
    df = pd.DataFrame({col: prices, "Close": prices * 0.999, "Volume": 1_000_000},
                      index=idx)
    return df


class TestPlotHistoricalPrices:

    def test_returns_figure_with_data(self):
        fig = plot_historical_prices(_price_df(), "^STOXX50E")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_returns_figure_empty_df(self):
        fig = plot_historical_prices(pd.DataFrame(), "^STOXX50E")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_returns_figure_none(self):
        fig = plot_historical_prices(None, "^STOXX50E")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_returns_figure_missing_column(self):
        df = pd.DataFrame({"Volume": [1, 2, 3]})
        fig = plot_historical_prices(df, "TEST")
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_uses_adj_close_over_close(self):
        """When both columns exist, Adj Close is preferred."""
        df = _price_df(col="Adj Close")
        df["Close"] = df["Adj Close"] * 1.1   # deliberately different
        fig = plot_historical_prices(df, "^STOXX50E")
        ax = fig.axes[0]
        labels = [l.get_label() for l in ax.get_lines()]
        assert any("Adj Close" in lbl for lbl in labels)
        plt.close(fig)

    def test_falls_back_to_close(self):
        """DataFrame with only Close column must render without error."""
        df = _price_df(col="Close")
        fig = plot_historical_prices(df, "^STOXX50E")
        ax = fig.axes[0]
        assert len(ax.get_lines()) >= 1
        plt.close(fig)

    def test_200d_ma_drawn_when_enough_bars(self):
        """With ≥ 200 rows the MA line is added (two lines in axes)."""
        fig = plot_historical_prices(_price_df(n=300), "^STOXX50E")
        ax = fig.axes[0]
        visible_lines = [l for l in ax.get_lines() if len(l.get_xdata()) > 0]
        assert len(visible_lines) == 2
        plt.close(fig)

    def test_no_ma_with_few_bars(self):
        """With < 200 rows only the price line is drawn."""
        fig = plot_historical_prices(_price_df(n=50), "^STOXX50E")
        ax = fig.axes[0]
        visible_lines = [l for l in ax.get_lines() if len(l.get_xdata()) > 0]
        assert len(visible_lines) == 1
        plt.close(fig)

    def test_title_contains_ticker(self):
        fig = plot_historical_prices(_price_df(), "^STOXX50E")
        assert "^STOXX50E" in fig.texts[0].get_text()
        plt.close(fig)


class TestReadRiskFreeRate:

    def test_reads_real_config(self):
        """Reads the actual configs/pricing.yaml and returns a positive float."""
        r = _read_risk_free_rate()
        assert isinstance(r, float)
        assert 0.0 < r < 1.0  # sanity: between 0 % and 100 %

    def test_returns_correct_value(self):
        """Value matches the literal in configs/pricing.yaml (0.05)."""
        assert _read_risk_free_rate() == pytest.approx(0.05)

    def test_fallback_on_missing_file(self, tmp_path, monkeypatch):
        """When pricing.yaml is missing, returns the 0.05 fallback."""
        import src.dashboard.app as app_mod
        from pathlib import Path as _Path
        original = _Path(__file__).resolve()  # just a path that exists

        # Monkeypatch __file__ on the module so cfg_path points to a non-existent dir
        monkeypatch.setattr(app_mod, "__file__",
                            str(tmp_path / "src" / "dashboard" / "app.py"))
        assert _read_risk_free_rate() == pytest.approx(0.05)

    def test_fallback_on_malformed_yaml(self, tmp_path, monkeypatch):
        """Malformed YAML triggers the except branch and returns 0.05."""
        import src.dashboard.app as app_mod

        # Build the expected configs path the function will resolve
        fake_root = tmp_path
        cfg_dir = fake_root / "configs"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "pricing.yaml").write_text("rates: [not: a: dict]")

        monkeypatch.setattr(app_mod, "__file__",
                            str(fake_root / "src" / "dashboard" / "app.py"))
        assert _read_risk_free_rate() == pytest.approx(0.05)

    def test_reads_custom_value(self, tmp_path, monkeypatch):
        """A custom rate in a temp YAML is returned correctly."""
        import src.dashboard.app as app_mod

        fake_root = tmp_path
        cfg_dir = fake_root / "configs"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "pricing.yaml").write_text(
            "rates:\n  risk_free_rate: 0.03\n"
        )
        monkeypatch.setattr(app_mod, "__file__",
                            str(fake_root / "src" / "dashboard" / "app.py"))
        assert _read_risk_free_rate() == pytest.approx(0.03)
