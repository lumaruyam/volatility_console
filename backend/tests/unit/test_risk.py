"""
Comprehensive tests for Step 11: Greeks and per-position risk.

Acceptance criteria (PLAN):
  - Aggregates reconcile to line-level sums.
  - UAM metric computed and logged.
"""

from __future__ import annotations

import math
import pytest

from src.pricing.european import EuropeanInputs, price_european
from src.risk.models import Position, PositionRisk, RiskAggregates, UAMResult
from src.risk.aggregation import (
    aggregate_risk,
    compute_local_pnl_attribution,
    compute_position_risk,
    reconcile_with_broker_greeks,
)
from src.risk.uam import compute_uam


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CFG = {"analytics_version": "1.0"}
UAM_CFG = {"spot_shock_pct": 0.05, "vol_shock_abs": 0.20, "config_version": "1.0"}

S0 = 5000.0   # ESTX50-like spot


def _snap(K=5000.0, T=0.5, sigma=0.20, opt="C", S=S0,
          r=0.03, q=0.02, mult=10.0, ts=1000.0) -> dict:
    return {
        "S": S, "K": K, "T": T, "r": r, "q": q,
        "sigma": sigma, "option_type": opt,
        "multiplier": mult, "forward": S * math.exp((r - q) * T),
        "snapshot_ts": ts,
    }


def _pos(qty=1.0, pid="PORT_A", sym="ESTX50", key="ESTX50|OPT|EUREX|EUR|20261219|5000|C|10") -> Position:
    return Position(portfolio_id=pid, contract_key=key,
                    underlying_symbol=sym, quantity=qty)


def _risk(qty=1.0, **snap_kw) -> PositionRisk:
    pos = _pos(qty=qty)
    return compute_position_risk(pos, _snap(**snap_kw), price_european, CFG)


# ---------------------------------------------------------------------------
# TestComputePositionRisk
# ---------------------------------------------------------------------------

class TestComputePositionRisk:
    def test_returns_position_risk(self):
        r = _risk()
        assert isinstance(r, PositionRisk)

    def test_portfolio_id_stored(self):
        assert _risk().portfolio_id == "PORT_A"

    def test_contract_key_stored(self):
        assert _risk().contract_key == "ESTX50|OPT|EUREX|EUR|20261219|5000|C|10"

    def test_quantity_stored(self):
        assert _risk(qty=5.0).quantity == pytest.approx(5.0)

    def test_multiplier_from_snapshot(self):
        assert _risk(mult=10.0).multiplier == pytest.approx(10.0)

    def test_snapshot_ts_stored(self):
        assert _risk(ts=9999.0).snapshot_ts == pytest.approx(9999.0)

    def test_spot_stored(self):
        assert _risk(S=4800.0).spot == pytest.approx(4800.0)

    def test_implied_vol_stored(self):
        assert _risk(sigma=0.25).implied_vol == pytest.approx(0.25)

    def test_maturity_years_stored(self):
        assert _risk(T=0.75).maturity_years == pytest.approx(0.75)

    def test_model_price_positive(self):
        assert _risk().model_price > 0.0

    def test_market_value_formula(self):
        r = _risk(qty=3.0, mult=10.0)
        assert r.market_value == pytest.approx(r.model_price * 3.0 * 10.0, rel=1e-8)

    def test_market_value_negative_for_short(self):
        r = _risk(qty=-2.0)
        assert r.market_value < 0.0

    # -- Raw Greeks sign / range checks --

    def test_call_delta_in_range(self):
        r = _risk(opt="C")
        assert 0.0 <= r.delta <= 1.0

    def test_put_delta_in_range(self):
        r = _risk(opt="P")
        assert -1.0 <= r.delta <= 0.0

    def test_gamma_positive(self):
        assert _risk().gamma > 0.0

    def test_vega_positive_per_point(self):
        assert _risk().vega_per_point > 0.0

    def test_theta_negative_atm(self):
        assert _risk().theta_per_day < 0.0

    # -- Dollar Greeks formula checks --

    def test_dollar_delta_formula(self):
        r = _risk(qty=2.0, mult=10.0)
        expected = r.delta * r.spot * 2.0 * 10.0
        assert r.dollar_delta == pytest.approx(expected, abs=1e-8)

    def test_dollar_gamma_formula(self):
        r = _risk(qty=2.0, mult=10.0)
        expected = r.gamma * r.spot ** 2 * 2.0 * 10.0
        assert r.dollar_gamma == pytest.approx(expected, abs=1e-8)

    def test_dollar_vega_formula(self):
        r = _risk(qty=2.0, mult=10.0)
        expected = r.vega_per_point * 0.01 * 2.0 * 10.0
        assert r.dollar_vega == pytest.approx(expected, abs=1e-8)

    def test_dollar_delta_sign_long_call(self):
        r = _risk(qty=1.0, opt="C")
        assert r.dollar_delta > 0.0

    def test_dollar_delta_sign_short_call(self):
        r = _risk(qty=-1.0, opt="C")
        assert r.dollar_delta < 0.0

    def test_dollar_delta_sign_long_put(self):
        r = _risk(qty=1.0, opt="P")
        assert r.dollar_delta < 0.0

    def test_dollar_gamma_positive_long(self):
        r = _risk(qty=1.0)
        assert r.dollar_gamma > 0.0

    def test_dollar_gamma_negative_short(self):
        r = _risk(qty=-1.0)
        assert r.dollar_gamma < 0.0

    def test_analytics_version_from_config(self):
        cfg = {"analytics_version": "2.0"}
        r = compute_position_risk(_pos(), _snap(), price_european, cfg)
        assert r.analytics_version == "2.0"


# ---------------------------------------------------------------------------
# TestAggregateRisk — acceptance criterion: aggregates reconcile to line sums
# ---------------------------------------------------------------------------

class TestAggregateRisk:
    def _portfolio(self) -> list[PositionRisk]:
        """Three positions: 2 ESTX50 calls (long/short) + 1 put."""
        return [
            _risk(qty=5.0, opt="C", K=5000),
            _risk(qty=-2.0, opt="C", K=5500),
            _risk(qty=3.0, opt="P", K=4500),
        ]

    def test_returns_list(self):
        rows = self._portfolio()
        aggs = aggregate_risk(rows, ["underlying_symbol"])
        assert isinstance(aggs, list)

    def test_single_group_one_aggregate(self):
        rows = self._portfolio()
        aggs = aggregate_risk(rows, ["underlying_symbol"])
        underlying_aggs = [a for a in aggs if a.group_key == "underlying_symbol"]
        assert len(underlying_aggs) == 1  # all same underlying

    def test_position_count_correct(self):
        rows = self._portfolio()
        aggs = aggregate_risk(rows, ["underlying_symbol"])
        assert aggs[0].position_count == 3

    def test_net_market_value_reconciles(self):
        rows = self._portfolio()
        aggs = aggregate_risk(rows, ["underlying_symbol"])
        expected_mv = sum(r.market_value for r in rows)
        assert aggs[0].net_market_value == pytest.approx(expected_mv, abs=1e-6)

    def test_net_dollar_delta_reconciles(self):
        rows = self._portfolio()
        aggs = aggregate_risk(rows, ["underlying_symbol"])
        expected = sum(r.dollar_delta for r in rows)
        assert aggs[0].net_dollar_delta == pytest.approx(expected, abs=1e-6)

    def test_net_dollar_gamma_reconciles(self):
        rows = self._portfolio()
        aggs = aggregate_risk(rows, ["underlying_symbol"])
        expected = sum(r.dollar_gamma for r in rows)
        assert aggs[0].net_dollar_gamma == pytest.approx(expected, abs=1e-6)

    def test_net_dollar_vega_reconciles(self):
        rows = self._portfolio()
        aggs = aggregate_risk(rows, ["underlying_symbol"])
        expected = sum(r.dollar_vega for r in rows)
        assert aggs[0].net_dollar_vega == pytest.approx(expected, abs=1e-6)

    def test_net_delta_reconciles(self):
        rows = self._portfolio()
        aggs = aggregate_risk(rows, ["underlying_symbol"])
        expected = sum(r.delta * r.quantity * r.multiplier for r in rows)
        assert aggs[0].net_delta == pytest.approx(expected, abs=1e-6)

    def test_multiple_group_keys(self):
        rows = self._portfolio()
        aggs = aggregate_risk(rows, ["underlying_symbol", "portfolio_id"])
        keys = {a.group_key for a in aggs}
        assert "underlying_symbol" in keys
        assert "portfolio_id" in keys

    def test_two_portfolios_separate_aggregates(self):
        rows = [
            compute_position_risk(
                _pos(pid="A", key="K1"), _snap(), price_european, CFG
            ),
            compute_position_risk(
                _pos(pid="B", key="K2"), _snap(), price_european, CFG
            ),
        ]
        aggs = aggregate_risk(rows, ["portfolio_id"])
        pids = {a.group_value for a in aggs}
        assert "A" in pids and "B" in pids

    def test_empty_input_returns_empty(self):
        assert aggregate_risk([], ["underlying_symbol"]) == []

    def test_single_position_aggregate_equals_line(self):
        row = _risk(qty=3.0)
        agg = aggregate_risk([row], ["underlying_symbol"])[0]
        assert agg.net_market_value == pytest.approx(row.market_value, abs=1e-6)
        assert agg.net_dollar_delta == pytest.approx(row.dollar_delta, abs=1e-6)

    def test_delta_neutral_portfolio(self):
        """Long ATM call + short ATM call same size → net_delta ≈ 0."""
        call_long = _risk(qty=1.0, opt="C")
        call_short = _risk(qty=-1.0, opt="C")
        agg = aggregate_risk([call_long, call_short], ["underlying_symbol"])[0]
        assert abs(agg.net_dollar_delta) < 1e-6


# ---------------------------------------------------------------------------
# TestReconcileWithBrokerGreeks
# ---------------------------------------------------------------------------

class TestReconcileWithBrokerGreeks:
    def test_returns_list(self):
        rows = [_risk()]
        bg = {}
        result = reconcile_with_broker_greeks(rows, bg)
        assert isinstance(result, list)

    def test_no_discrepancy_when_equal(self):
        row = _risk()
        bg = {row.contract_key: {"delta": row.delta, "vega": row.vega_per_point}}
        result = reconcile_with_broker_greeks([row], bg)
        for rec in result:
            assert abs(rec["abs_diff"]) < 1e-8

    def test_discrepancy_detected(self):
        row = _risk()
        bg = {row.contract_key: {"delta": row.delta + 0.05}}
        result = reconcile_with_broker_greeks([row], bg)
        delta_rec = next(r for r in result if r["greek"] == "delta")
        assert abs(delta_rec["abs_diff"]) == pytest.approx(0.05, abs=1e-6)

    def test_missing_contract_skipped(self):
        row = _risk()
        bg = {"OTHER_KEY": {"delta": 0.5}}
        result = reconcile_with_broker_greeks([row], bg)
        assert result == []

    def test_discrepancy_record_fields(self):
        row = _risk()
        bg = {row.contract_key: {"delta": 0.0}}
        result = reconcile_with_broker_greeks([row], bg)
        rec = result[0]
        assert "contract_key" in rec
        assert "greek" in rec
        assert "platform_value" in rec
        assert "broker_value" in rec
        assert "abs_diff" in rec
        assert "rel_diff_pct" in rec

    def test_multiple_positions_multiple_records(self):
        r1 = _risk(qty=1.0)
        r2 = compute_position_risk(
            _pos(key="ESTX50|OPT|EUREX|EUR|20261219|5500|C|10"),
            _snap(K=5500), price_european, CFG
        )
        bg = {
            r1.contract_key: {"delta": 0.0},
            r2.contract_key: {"delta": 0.0},
        }
        result = reconcile_with_broker_greeks([r1, r2], bg)
        assert len(result) >= 2


# ---------------------------------------------------------------------------
# TestLocalPnlAttribution
# ---------------------------------------------------------------------------

class TestLocalPnlAttribution:
    def _long_call_portfolio(self) -> list[PositionRisk]:
        return [_risk(qty=1.0, opt="C")]

    def _long_put_portfolio(self) -> list[PositionRisk]:
        return [_risk(qty=1.0, opt="P")]

    def test_returns_dict_with_keys(self):
        rows = self._long_call_portfolio()
        result = compute_local_pnl_attribution(rows, dS=10.0, d_sigma=0.0, dt_days=0.0)
        assert "delta_pnl" in result
        assert "gamma_pnl" in result
        assert "vega_pnl" in result
        assert "theta_pnl" in result
        assert "total_approx_pnl" in result

    def test_total_equals_sum(self):
        rows = self._long_call_portfolio()
        r = compute_local_pnl_attribution(rows, dS=50.0, d_sigma=1.0, dt_days=1.0)
        expected = r["delta_pnl"] + r["gamma_pnl"] + r["vega_pnl"] + r["theta_pnl"]
        assert r["total_approx_pnl"] == pytest.approx(expected, abs=1e-8)

    def test_spot_up_positive_delta_pnl_for_long_call(self):
        rows = self._long_call_portfolio()
        r = compute_local_pnl_attribution(rows, dS=25.0, d_sigma=0.0, dt_days=0.0)
        assert r["delta_pnl"] > 0.0

    def test_spot_dn_negative_delta_pnl_for_long_call(self):
        rows = self._long_call_portfolio()
        r = compute_local_pnl_attribution(rows, dS=-25.0, d_sigma=0.0, dt_days=0.0)
        assert r["delta_pnl"] < 0.0

    def test_spot_up_negative_delta_pnl_for_long_put(self):
        rows = self._long_put_portfolio()
        r = compute_local_pnl_attribution(rows, dS=25.0, d_sigma=0.0, dt_days=0.0)
        assert r["delta_pnl"] < 0.0

    def test_gamma_pnl_always_positive_for_long(self):
        rows = self._long_call_portfolio()
        r_up = compute_local_pnl_attribution(rows, dS=100.0, d_sigma=0.0, dt_days=0.0)
        r_dn = compute_local_pnl_attribution(rows, dS=-100.0, d_sigma=0.0, dt_days=0.0)
        assert r_up["gamma_pnl"] > 0.0
        assert r_dn["gamma_pnl"] > 0.0

    def test_gamma_pnl_negative_for_short(self):
        rows = [_risk(qty=-1.0, opt="C")]
        r = compute_local_pnl_attribution(rows, dS=100.0, d_sigma=0.0, dt_days=0.0)
        assert r["gamma_pnl"] < 0.0

    def test_vol_up_positive_vega_pnl_for_long(self):
        rows = self._long_call_portfolio()
        r = compute_local_pnl_attribution(rows, dS=0.0, d_sigma=1.0, dt_days=0.0)
        assert r["vega_pnl"] > 0.0

    def test_vol_up_negative_vega_pnl_for_short(self):
        rows = [_risk(qty=-1.0, opt="C")]
        r = compute_local_pnl_attribution(rows, dS=0.0, d_sigma=1.0, dt_days=0.0)
        assert r["vega_pnl"] < 0.0

    def test_theta_pnl_negative_for_long_over_time(self):
        rows = self._long_call_portfolio()
        r = compute_local_pnl_attribution(rows, dS=0.0, d_sigma=0.0, dt_days=1.0)
        assert r["theta_pnl"] < 0.0

    def test_zero_shocks_zero_pnl(self):
        rows = self._long_call_portfolio()
        r = compute_local_pnl_attribution(rows, dS=0.0, d_sigma=0.0, dt_days=0.0)
        assert r["total_approx_pnl"] == pytest.approx(0.0, abs=1e-10)

    def test_linearity_in_delta_pnl(self):
        """delta_pnl should scale linearly with dS."""
        rows = self._long_call_portfolio()
        r1 = compute_local_pnl_attribution(rows, dS=10.0, d_sigma=0.0, dt_days=0.0)
        r2 = compute_local_pnl_attribution(rows, dS=20.0, d_sigma=0.0, dt_days=0.0)
        assert r2["delta_pnl"] == pytest.approx(r1["delta_pnl"] * 2, rel=1e-6)

    def test_attribution_multi_position(self):
        """Multi-position: total should be sum of individual PnLs."""
        r1 = _risk(qty=1.0, opt="C")
        r2 = _risk(qty=2.0, opt="P")
        combined = compute_local_pnl_attribution([r1, r2], dS=10.0, d_sigma=0.5, dt_days=0.0)
        ind1 = compute_local_pnl_attribution([r1], dS=10.0, d_sigma=0.5, dt_days=0.0)
        ind2 = compute_local_pnl_attribution([r2], dS=10.0, d_sigma=0.5, dt_days=0.0)
        assert combined["total_approx_pnl"] == pytest.approx(
            ind1["total_approx_pnl"] + ind2["total_approx_pnl"], abs=1e-6
        )


# ---------------------------------------------------------------------------
# TestComputeUAM
# ---------------------------------------------------------------------------

class TestComputeUAM:
    def _portfolio(self, qty=1.0) -> list[PositionRisk]:
        return [_risk(qty=qty, opt="C")]

    def test_returns_uam_result(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        assert isinstance(result, UAMResult)

    def test_four_scenarios_computed(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        assert len(result.scenario_pnls) == 4

    def test_scenario_labels_correct(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        expected = {"up_vol_up", "up_vol_dn", "dn_vol_up", "dn_vol_dn"}
        assert set(result.scenario_pnls.keys()) == expected

    def test_worst_case_is_minimum(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        assert result.worst_case_pnl == pytest.approx(
            min(result.scenario_pnls.values()), abs=1e-8
        )

    def test_margin_requirement_is_abs_worst(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        assert result.margin_requirement == pytest.approx(
            abs(result.worst_case_pnl), abs=1e-8
        )

    def test_portfolio_gross_value_positive(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        assert result.portfolio_gross_value > 0.0

    def test_uam_ratio_non_negative(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        assert result.uam_ratio >= 0.0

    def test_uam_ratio_formula(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        expected = result.margin_requirement / result.portfolio_gross_value
        assert result.uam_ratio == pytest.approx(expected, abs=1e-8)

    def test_shock_params_stored(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        assert result.spot_shock_pct == pytest.approx(0.05)
        assert result.vol_shock_abs == pytest.approx(0.20)

    def test_config_version_stored(self):
        result = compute_uam(self._portfolio(), UAM_CFG)
        assert result.config_version == "1.0"

    def test_snapshot_ts_stored(self):
        result = compute_uam(self._portfolio(), UAM_CFG, snapshot_ts=42.0)
        assert result.snapshot_ts == pytest.approx(42.0)

    def test_portfolio_id_stored(self):
        result = compute_uam(self._portfolio(), UAM_CFG, portfolio_id="MINE")
        assert result.portfolio_id == "MINE"

    def test_empty_portfolio_returns_zero(self):
        result = compute_uam([], UAM_CFG)
        assert result.worst_case_pnl == 0.0
        assert result.margin_requirement == 0.0
        assert result.uam_ratio == 0.0
        assert result.scenario_pnls == {}

    def test_long_call_worst_case_on_spot_down(self):
        """Long call loses most when spot falls (delta < 0 PnL)."""
        result = compute_uam(self._portfolio(qty=1.0), UAM_CFG)
        dn_pnls = {k: v for k, v in result.scenario_pnls.items() if k.startswith("dn")}
        assert result.worst_case_pnl in dn_pnls.values()

    def test_short_call_worst_case_on_spot_up(self):
        """Short call loses most when spot rises."""
        result = compute_uam(self._portfolio(qty=-1.0), UAM_CFG)
        up_pnls = {k: v for k, v in result.scenario_pnls.items() if k.startswith("up")}
        assert result.worst_case_pnl in up_pnls.values()

    def test_large_portfolio_higher_margin(self):
        small = compute_uam(self._portfolio(qty=1.0), UAM_CFG)
        large = compute_uam(self._portfolio(qty=10.0), UAM_CFG)
        assert large.margin_requirement > small.margin_requirement

    def test_custom_shock_params(self):
        cfg = {"spot_shock_pct": 0.10, "vol_shock_abs": 0.30, "config_version": "2.0"}
        result_10 = compute_uam(self._portfolio(), cfg)
        result_5 = compute_uam(self._portfolio(), UAM_CFG)
        # Larger shock → larger margin requirement
        assert result_10.margin_requirement >= result_5.margin_requirement


# ---------------------------------------------------------------------------
# TestAcceptanceCriterion
# ---------------------------------------------------------------------------

class TestAcceptanceCriterion:
    """PLAN: Aggregates reconcile to line-level sums; UAM metric computed."""

    def _straddle(self) -> list[PositionRisk]:
        """Long straddle: long call + long put at same strike."""
        return [
            _risk(qty=1.0, opt="C", K=5000),
            _risk(qty=1.0, opt="P", K=5000),
        ]

    def test_aggregate_delta_equals_line_sum(self):
        rows = self._straddle()
        agg = aggregate_risk(rows, ["underlying_symbol"])[0]
        expected = sum(r.delta * r.quantity * r.multiplier for r in rows)
        assert agg.net_delta == pytest.approx(expected, abs=1e-6)

    def test_aggregate_market_value_equals_line_sum(self):
        rows = self._straddle()
        agg = aggregate_risk(rows, ["underlying_symbol"])[0]
        expected = sum(r.market_value for r in rows)
        assert agg.net_market_value == pytest.approx(expected, abs=1e-6)

    def test_straddle_near_delta_neutral(self):
        """Long straddle call+put at ATM → net delta ≈ 0."""
        rows = self._straddle()
        agg = aggregate_risk(rows, ["underlying_symbol"])[0]
        assert abs(agg.net_delta) < agg.position_count  # small relative to position size

    def test_straddle_gamma_positive(self):
        """Long straddle: long gamma from both legs."""
        rows = self._straddle()
        agg = aggregate_risk(rows, ["underlying_symbol"])[0]
        assert agg.net_dollar_gamma > 0.0

    def test_uam_computed_and_has_ratio(self):
        rows = self._straddle()
        result = compute_uam(rows, UAM_CFG, portfolio_id="STRADDLE")
        assert isinstance(result.uam_ratio, float)
        assert math.isfinite(result.uam_ratio)

    def test_uam_logged_without_crash(self):
        """Just confirm compute_uam runs without exception and returns a result."""
        rows = self._straddle()
        result = compute_uam(rows, UAM_CFG)
        assert result is not None
