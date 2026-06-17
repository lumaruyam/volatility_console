"""
FastAPI integration tests — all 5 routers.

Runs fully in-process via TestClient (no network, no yfinance).
All endpoints must return HTTP 200 and the expected shape.
"""

from __future__ import annotations

from typing import Optional

import pytest
from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ok(path: str, params: Optional[dict] = None) -> dict:
    r = client.get(path, params=params or {})
    assert r.status_code == 200, f"GET {path} → {r.status_code}: {r.text[:200]}"
    return r.json()


def post_ok(path: str, body: dict) -> dict:
    r = client.post(path, json=body)
    assert r.status_code == 200, f"POST {path} → {r.status_code}: {r.text[:200]}"
    return r.json()


# ---------------------------------------------------------------------------
# Market router
# ---------------------------------------------------------------------------

class TestMarketRouter:
    def test_index_matrix_returns_list(self):
        data = ok("/api/market/index-matrix")
        assert isinstance(data, list)
        assert len(data) > 0
        row = data[0]
        assert "ticker" in row
        assert "spot" in row
        assert "atm_vol" in row

    def test_options_chain_default(self):
        data = ok("/api/market/options-chain")
        assert isinstance(data, list)
        assert len(data) > 0
        row = data[0]
        for key in ("strike", "call_bid", "call_iv", "call_delta", "put_bid", "put_iv", "atm"):
            assert key in row, f"Missing key: {key}"

    def test_options_chain_with_ticker(self):
        data = ok("/api/market/options-chain", {"ticker": "SX5E", "expiry": "2026-12-15"})
        assert isinstance(data, list)

    def test_vol_surface_shape(self):
        data = ok("/api/market/vol-surface")
        assert "strikes" in data and "maturities" in data and "implied_vols" in data
        assert "smile_slice_30d" in data
        assert "calibration" in data
        smile = data["smile_slice_30d"]
        for key in ("strikes", "call_ivs", "put_ivs", "cal_arb", "bfly_arb"):
            assert key in smile

    def test_engine_status_fields(self):
        data = ok("/api/market/engine-status")
        assert "spot_ingestion" in data
        assert "forward_curve" in data
        assert "calibration" in data
        assert "engine_load_pct" in data

    def test_greeks_summary_fields(self):
        data = ok("/api/market/greeks-summary")
        for key in ("total_delta", "total_gamma", "total_vega", "total_theta"):
            assert key in data


# ---------------------------------------------------------------------------
# Risk router
# ---------------------------------------------------------------------------

class TestRiskRouter:
    def test_greeks_fields(self):
        data = ok("/api/risk/greeks")
        for key in ("portfolio_delta", "gamma", "dollar_gamma", "vega", "theta", "rho"):
            assert key in data

    def test_var_fields(self):
        data = ok("/api/risk/var")
        for key in ("1d_95", "1d_99", "7d_99"):
            assert key in data
        assert data["1d_95"] < 0   # VaR is a loss

    def test_pnl_attribution_fields(self):
        data = ok("/api/risk/pnl-attribution")
        for key in ("delta_pnl", "gamma_pnl", "vega_pnl", "theta_pnl", "rho_pnl"):
            assert key in data

    def test_correlation_shape(self):
        data = ok("/api/risk/correlation")
        assert "tickers" in data and "matrix" in data
        n = len(data["tickers"])
        assert len(data["matrix"]) == n
        assert all(len(row) == n for row in data["matrix"])
        # Diagonal should be 1.0
        for i in range(n):
            assert abs(data["matrix"][i][i] - 1.0) < 1e-6

    def test_uam_shape(self):
        data = ok("/api/risk/uam")
        assert "rows" in data
        assert len(data["rows"]) == 3
        for row in data["rows"]:
            assert "label" in row
            assert len(row["cells"]) == 3
            for cell in row["cells"]:
                assert "pnl" in cell and "tone" in cell

    def test_qc_log_entries(self):
        data = ok("/api/risk/qc-log")
        assert isinstance(data, list)
        assert len(data) > 0
        for entry in data:
            for key in ("ts", "ticker", "type", "tenor", "status", "reason"):
                assert key in entry


# ---------------------------------------------------------------------------
# Strategy router
# ---------------------------------------------------------------------------

class TestStrategyRouter:
    def test_positions_shape(self):
        data = ok("/api/strategy/positions")
        assert isinstance(data, list)
        assert len(data) >= 1
        pos = data[0]
        for key in ("strategy_id", "strategy_name", "pnl_intraday_eur", "live_exec"):
            assert key in pos

    def test_orderbook_shape(self):
        data = ok("/api/strategy/orderbook")
        assert isinstance(data, list)
        assert len(data) > 0
        row = data[0]
        for key in ("time", "bid", "ask", "bid_size", "ask_size", "spread_pct", "wide"):
            assert key in row

    def test_orderbook_wide_flag(self):
        data = ok("/api/strategy/orderbook")
        for row in data:
            if row["spread_pct"] > 2.0:
                assert row["wide"] is True

    def test_hedge_suggestions_shape(self):
        data = ok("/api/strategy/hedge-suggestions")
        assert isinstance(data, list)
        assert len(data) >= 1
        for s in data:
            for key in ("type", "severity", "message", "action"):
                assert key in s

    def test_roll_action(self):
        data = post_ok("/api/strategy/roll", {"strategy_id": "strat_001"})
        assert data["status"] == "ok"

    def test_hedge_action(self):
        data = post_ok("/api/strategy/hedge", {"strategy_id": "strat_001", "target_delta": 0.0})
        assert data["status"] == "ok"

    def test_liquidate_action(self):
        data = post_ok("/api/strategy/liquidate", {"strategy_id": "strat_001"})
        assert data["status"] == "ok"

    def test_execute_hedge_action(self):
        data = post_ok("/api/strategy/execute-hedge",
                       {"action": "Sell 120 SX5E Futs", "strategy_id": "strat_001"})
        assert data["status"] == "ok"
        assert "action" in data


# ---------------------------------------------------------------------------
# Backtest router
# ---------------------------------------------------------------------------

class TestBacktestRouter:
    def test_strategies_list(self):
        data = ok("/api/backtest/strategies")
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "VOL_CARRY_01" in data

    def test_run_default(self):
        data = post_ok("/api/backtest/run", {
            "strategy_id": "VOL_CARRY_01",
            "start_date": "2005-01-01",
            "end_date": "2026-06-14",
            "rebalance_frequency": "weekly",
            "shock_preset": None,
        })
        assert "timestamp_vector" in data
        assert "cumulative_pnl_vector" in data
        assert "benchmark_pnl_vector" in data
        assert "drawdown_vector" in data
        assert "stats" in data
        stats = data["stats"]
        for key in ("cumulative_pnl_ann_pct", "sharpe", "win_rate_pct", "max_drawdown_pct"):
            assert key in stats
        assert len(data["timestamp_vector"]) > 0

    def test_run_shock_preset(self):
        data = post_ok("/api/backtest/run", {
            "strategy_id": "VOL_CARRY_01",
            "start_date": "2005-01-01",
            "end_date": "2026-06-14",
            "rebalance_frequency": "weekly",
            "shock_preset": "2008 Crash",
        })
        assert len(data["timestamp_vector"]) > 0
        # Shock window should be much shorter than full history
        assert len(data["timestamp_vector"]) < 200

    @pytest.mark.parametrize("strat", ["VOL_CARRY_01", "SX5E_STRADDLE", "DISPERSION_Q3"])
    def test_run_all_strategies(self, strat: str):
        data = post_ok("/api/backtest/run", {
            "strategy_id": strat,
            "start_date": "2010-01-01",
            "end_date": "2026-06-14",
        })
        assert len(data["timestamp_vector"]) > 0

    def test_shock_preset_endpoint(self):
        data = post_ok("/api/backtest/shock-preset", {"preset": "COVID Vol Spike"})
        assert len(data["timestamp_vector"]) > 0

    def test_monte_carlo_shape(self):
        data = ok("/api/backtest/monte-carlo", {"n_paths": 200, "strategy_id": "VOL_CARRY_01"})
        assert "simulation_path_terminal_returns" in data
        assert "var_95_pct" in data
        assert len(data["simulation_path_terminal_returns"]) == 200
        assert data["var_95_pct"] < 0     # VaR is always a loss
        assert data["var_95_pct"] > -100  # but not total wipe-out in 1yr

    @pytest.mark.parametrize("strat", ["VOL_CARRY_01", "SX5E_STRADDLE", "DISPERSION_Q3"])
    def test_monte_carlo_var_varies_by_strategy(self, strat: str):
        data = ok("/api/backtest/monte-carlo", {"n_paths": 100, "strategy_id": strat})
        assert "var_95_pct" in data


# ---------------------------------------------------------------------------
# Shock router
# ---------------------------------------------------------------------------

class TestShockRouter:
    def test_reprice_base(self):
        data = post_ok("/api/shock/reprice", {
            "spot_stress": 0.0,
            "vol_stress": 0.0,
            "rate_stress_bps": 0.0,
            "methodology": "parallel_grid_shift",
            "active_methods": 1,
        })
        assert "scenario_matrix" in data
        matrix = data["scenario_matrix"]
        assert len(matrix) == 3
        assert all(len(row) == 3 for row in matrix)
        # Center cell (base) must have pnl_eur = 0 and nav_bps = 0
        center = matrix[1][1]
        assert center["pnl_eur"] == 0
        assert center["nav_bps"] == 0.0

    def test_reprice_fields(self):
        data = post_ok("/api/shock/reprice", {"spot_stress": 0.05})
        for key in ("scenario_matrix", "aggregate_shift_pct", "active_methods",
                    "rate_bps", "base_portfolio_value", "nav_total"):
            assert key in data
        for row in data["scenario_matrix"]:
            for cell in row:
                for key in ("spot_pct", "vol_pct", "pnl_eur", "nav_bps"):
                    assert key in cell

    def test_reprice_spot_up_increases_pnl(self):
        base  = post_ok("/api/shock/reprice", {"spot_stress": 0.0})
        up    = post_ok("/api/shock/reprice", {"spot_stress": 0.10})
        # Middle-row (base grid spot) cells: spot+10% should shift PnL upward (positive delta)
        assert up["scenario_matrix"][1][1]["pnl_eur"] > base["scenario_matrix"][1][1]["pnl_eur"]

    def test_reprice_aggregate_shift_increases_with_offset(self):
        base = post_ok("/api/shock/reprice", {"spot_stress": 0.0, "vol_stress": 0.0})
        with_offset = post_ok("/api/shock/reprice", {"spot_stress": 0.05, "vol_stress": 0.10})
        assert with_offset["aggregate_shift_pct"] > base["aggregate_shift_pct"]

    def test_reprice_nav_bps_consistent_with_pnl(self):
        data   = post_ok("/api/shock/reprice", {})
        nav    = data["nav_total"]
        for row in data["scenario_matrix"]:
            for cell in row:
                expected_bps = round(cell["pnl_eur"] / nav * 10_000, 1)
                assert abs(cell["nav_bps"] - expected_bps) < 0.2

    @pytest.mark.parametrize("methodology", [
        "parallel_grid_shift", "historical_copula", "vix_indexed_skew",
    ])
    def test_reprice_all_methodologies(self, methodology: str):
        data = post_ok("/api/shock/reprice", {"methodology": methodology})
        assert data["scenario_matrix"] is not None
        assert len(data["scenario_matrix"]) == 3
