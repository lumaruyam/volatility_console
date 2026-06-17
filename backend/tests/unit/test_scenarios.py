"""
Comprehensive tests for Step 12: Scenario engine.

Acceptance criterion (PLAN):
  Reports are deterministic given positions + snapshot + scenario version.
"""

from __future__ import annotations

import math
import pytest

from src.pricing.european import EuropeanInputs, price_european
from src.risk.models import Position
from src.risk.scenarios import (
    Scenario,
    ScenarioResult,
    compute_worst_case,
    extract_top_contributors,
    load_scenarios_from_config,
    run_scenario,
    run_scenario_grid,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CFG = {
    "analytics_version": "1.0",
    "valuation_ts": 1000.0,
    "snapshot_ts": 900.0,
    "top_contributors_n": 5,
}

S0 = 5000.0


def _snap(K=5000.0, T=0.5, sigma=0.20, opt="C",
          S=S0, r=0.03, q=0.02, mult=10.0) -> dict:
    return {
        "S": S, "K": K, "T": T, "r": r, "q": q,
        "sigma": sigma, "option_type": opt,
        "multiplier": mult,
        "forward": S * math.exp((r - q) * T),
        "snapshot_ts": 900.0,
    }


def _pos(contract_key="K1", sym="ESTX50", pid="PORT_A", qty=1.0) -> Position:
    return Position(portfolio_id=pid, contract_key=contract_key,
                    underlying_symbol=sym, quantity=qty)


def _positions_and_snapshots(n=1, qty=1.0, opt="C") -> tuple[list[Position], dict]:
    """Build n positions with matching snapshots."""
    positions = [_pos(contract_key=f"K{i}", qty=qty, sym="ESTX50")
                 for i in range(n)]
    snaps = {f"K{i}": _snap(K=5000 + i * 100, opt=opt) for i in range(n)}
    return positions, snaps


def _scenario(sid="test", spot=0.0, vol=0.0, days=0, ver="1.0") -> Scenario:
    return Scenario(scenario_id=sid, spot_shift_pct=spot,
                    vol_shift_abs=vol, time_roll_days=days, version=ver)


# ---------------------------------------------------------------------------
# TestScenarioModel
# ---------------------------------------------------------------------------

class TestScenarioModel:
    def test_fields_present(self):
        s = _scenario("s1", spot=-0.10, vol=0.05, days=1)
        assert s.scenario_id == "s1"
        assert s.spot_shift_pct == pytest.approx(-0.10)
        assert s.vol_shift_abs == pytest.approx(0.05)
        assert s.time_roll_days == 1

    def test_default_version(self):
        assert _scenario().version == "1.0"

    def test_frozen(self):
        s = _scenario()
        with pytest.raises((AttributeError, TypeError)):
            s.scenario_id = "other"  # type: ignore[misc]

    def test_description_default_empty(self):
        s = Scenario(scenario_id="x", spot_shift_pct=0.0,
                     vol_shift_abs=0.0, time_roll_days=0)
        assert s.description == ""


# ---------------------------------------------------------------------------
# TestLoadScenariosFromConfig
# ---------------------------------------------------------------------------

class TestLoadScenariosFromConfig:
    def _cfg(self) -> dict:
        return {
            "version": "2.0",
            "scenarios": [
                {"scenario_id": "dn10", "spot_shift_pct": -0.10,
                 "vol_shift_abs": 0.0, "time_roll_days": 0,
                 "description": "down 10"},
                {"scenario_id": "crash", "spot_shift_pct": -0.15,
                 "vol_shift_abs": 0.10, "time_roll_days": 1},
            ],
        }

    def test_loads_correct_count(self):
        scenarios = load_scenarios_from_config(self._cfg())
        assert len(scenarios) == 2

    def test_scenario_ids(self):
        scenarios = load_scenarios_from_config(self._cfg())
        assert scenarios[0].scenario_id == "dn10"
        assert scenarios[1].scenario_id == "crash"

    def test_spot_shift_loaded(self):
        s = load_scenarios_from_config(self._cfg())[0]
        assert s.spot_shift_pct == pytest.approx(-0.10)

    def test_vol_shift_loaded(self):
        s = load_scenarios_from_config(self._cfg())[1]
        assert s.vol_shift_abs == pytest.approx(0.10)

    def test_time_roll_loaded(self):
        s = load_scenarios_from_config(self._cfg())[1]
        assert s.time_roll_days == 1

    def test_description_loaded(self):
        s = load_scenarios_from_config(self._cfg())[0]
        assert "down 10" in s.description

    def test_version_inherited_from_grid(self):
        """Scenarios without own version inherit the grid version."""
        scenarios = load_scenarios_from_config(self._cfg())
        for s in scenarios:
            assert s.version == "2.0"

    def test_version_overridden_per_scenario(self):
        cfg = {"version": "1.0", "scenarios": [
            {"scenario_id": "x", "spot_shift_pct": 0.0,
             "vol_shift_abs": 0.0, "version": "3.0"},
        ]}
        s = load_scenarios_from_config(cfg)[0]
        assert s.version == "3.0"

    def test_empty_scenarios(self):
        assert load_scenarios_from_config({"scenarios": []}) == []

    def test_yaml_scenarios_file_loads(self):
        """Smoke-test that the committed configs/scenarios.yaml is valid."""
        import yaml
        from pathlib import Path
        path = Path("configs/scenarios.yaml")
        if not path.exists():
            pytest.skip("configs/scenarios.yaml not found")
        cfg = yaml.safe_load(path.read_text())
        scenarios = load_scenarios_from_config(cfg)
        assert len(scenarios) >= 5


# ---------------------------------------------------------------------------
# TestRunScenarioFullReprice
# ---------------------------------------------------------------------------

class TestRunScenarioFullReprice:
    def _run(self, spot=0.0, vol=0.0, days=0, qty=1.0,
             opt="C", **snap_kw) -> ScenarioResult:
        pos = [_pos(qty=qty)]
        snaps = {"K1": _snap(opt=opt, **snap_kw)}
        s = _scenario(spot=spot, vol=vol, days=days)
        return run_scenario(s, pos, snaps, price_european, CFG)

    def test_returns_scenario_result(self):
        assert isinstance(self._run(), ScenarioResult)

    def test_method_is_full_reprice(self):
        assert self._run().method == "full_reprice"

    def test_scenario_id_stored(self):
        pos = [_pos()]
        snaps = {"K1": _snap()}
        result = run_scenario(_scenario(sid="MY_ID"), pos, snaps, price_european, CFG)
        assert result.scenario_id == "MY_ID"

    def test_scenario_version_stored(self):
        pos = [_pos()]
        snaps = {"K1": _snap()}
        result = run_scenario(_scenario(ver="3.0"), pos, snaps, price_european, CFG)
        assert result.scenario_version == "3.0"

    def test_valuation_ts_from_config(self):
        assert self._run().valuation_ts == pytest.approx(1000.0)

    def test_snapshot_ts_from_config(self):
        assert self._run().snapshot_ts == pytest.approx(900.0)

    def test_one_line_per_position(self):
        pos, snaps = _positions_and_snapshots(n=3)
        result = run_scenario(_scenario(), pos, snaps, price_european, CFG)
        assert len(result.line_results) == 3

    def test_line_result_fields(self):
        result = self._run()
        rec = result.line_results[0]
        for f in ("contract_key", "quantity", "multiplier",
                  "base_price", "stressed_price", "base_value",
                  "stressed_value", "pnl"):
            assert f in rec

    def test_total_pnl_equals_sum_of_lines(self):
        pos, snaps = _positions_and_snapshots(n=3)
        result = run_scenario(_scenario(spot=-0.05), pos, snaps, price_european, CFG)
        expected = sum(r["pnl"] for r in result.line_results)
        assert result.total_pnl == pytest.approx(expected, abs=1e-8)

    def test_spot_dn_hurts_long_call(self):
        result = self._run(spot=-0.10, opt="C", qty=1.0)
        assert result.total_pnl < 0.0

    def test_spot_up_helps_long_call(self):
        result = self._run(spot=0.10, opt="C", qty=1.0)
        assert result.total_pnl > 0.0

    def test_spot_dn_helps_long_put(self):
        result = self._run(spot=-0.10, opt="P", qty=1.0)
        assert result.total_pnl > 0.0

    def test_spot_up_hurts_long_put(self):
        result = self._run(spot=0.10, opt="P", qty=1.0)
        assert result.total_pnl < 0.0

    def test_vol_up_helps_long_option(self):
        result = self._run(vol=0.05, opt="C", qty=1.0)
        assert result.total_pnl > 0.0

    def test_vol_dn_hurts_long_option(self):
        result = self._run(vol=-0.05, opt="C", qty=1.0)
        assert result.total_pnl < 0.0

    def test_time_roll_hurts_long_option(self):
        result = self._run(days=5, opt="C", qty=1.0)
        assert result.total_pnl < 0.0

    def test_short_position_reverses_sign(self):
        long_r = self._run(spot=-0.10, opt="C", qty=1.0)
        short_r = self._run(spot=-0.10, opt="C", qty=-1.0)
        assert long_r.total_pnl == pytest.approx(-short_r.total_pnl, abs=1e-6)

    def test_larger_position_larger_pnl(self):
        r1 = self._run(spot=0.10, qty=1.0)
        r5 = self._run(spot=0.10, qty=5.0)
        assert abs(r5.total_pnl) == pytest.approx(abs(r1.total_pnl) * 5, rel=1e-6)

    def test_zero_shock_pnl_near_zero(self):
        result = self._run(spot=0.0, vol=0.0, days=0)
        assert abs(result.total_pnl) < 1e-8

    def test_missing_snapshot_skips_position(self):
        pos = [_pos(contract_key="MISSING")]
        snaps = {}  # no entry for "MISSING"
        result = run_scenario(_scenario(spot=-0.10), pos, snaps, price_european, CFG)
        assert len(result.line_results) == 0
        assert result.total_pnl == pytest.approx(0.0)

    def test_deterministic(self):
        pos, snaps = _positions_and_snapshots(n=2)
        s = _scenario(spot=-0.10, vol=0.05)
        r1 = run_scenario(s, pos, snaps, price_european, CFG)
        r2 = run_scenario(s, pos, snaps, price_european, CFG)
        assert r1.total_pnl == pytest.approx(r2.total_pnl, abs=1e-10)
        for l1, l2 in zip(r1.line_results, r2.line_results):
            assert l1["pnl"] == pytest.approx(l2["pnl"], abs=1e-10)

    def test_worst_contributors_present(self):
        pos, snaps = _positions_and_snapshots(n=3)
        result = run_scenario(_scenario(spot=-0.10), pos, snaps, price_european, CFG)
        assert len(result.worst_contributors) <= 3

    def test_worst_contributors_sorted_by_abs_pnl(self):
        pos, snaps = _positions_and_snapshots(n=5)
        result = run_scenario(_scenario(spot=-0.15), pos, snaps, price_european, CFG)
        pnls = [abs(r["pnl"]) for r in result.worst_contributors]
        assert pnls == sorted(pnls, reverse=True)

    def test_line_base_price_positive(self):
        result = self._run()
        for r in result.line_results:
            assert r["base_price"] > 0.0

    def test_line_stressed_price_positive(self):
        result = self._run(spot=-0.05)
        for r in result.line_results:
            assert r["stressed_price"] >= 0.0

    def test_invalid_method_raises(self):
        pos = [_pos()]
        snaps = {"K1": _snap()}
        with pytest.raises(ValueError, match="method"):
            run_scenario(_scenario(), pos, snaps, price_european, CFG,
                         method="bad_method")


# ---------------------------------------------------------------------------
# TestRunScenarioGreekApprox
# ---------------------------------------------------------------------------

class TestRunScenarioGreekApprox:
    def _run(self, spot=0.0, vol=0.0, days=0, qty=1.0, opt="C") -> ScenarioResult:
        pos = [_pos(qty=qty)]
        snaps = {"K1": _snap(opt=opt)}
        s = _scenario(spot=spot, vol=vol, days=days)
        return run_scenario(s, pos, snaps, price_european, CFG, method="greek_approx")

    def test_returns_scenario_result(self):
        assert isinstance(self._run(), ScenarioResult)

    def test_method_is_greek_approx(self):
        assert self._run().method == "greek_approx"

    def test_spot_dn_hurts_long_call(self):
        assert self._run(spot=-0.10, opt="C").total_pnl < 0.0

    def test_spot_up_helps_long_call(self):
        assert self._run(spot=0.05, opt="C").total_pnl > 0.0

    def test_vol_up_helps_long(self):
        assert self._run(vol=0.05, opt="C").total_pnl > 0.0

    def test_time_roll_hurts_long(self):
        assert self._run(days=1, opt="C").total_pnl < 0.0

    def test_zero_shock_zero_pnl(self):
        assert self._run(spot=0.0, vol=0.0, days=0).total_pnl == pytest.approx(0.0, abs=1e-8)

    def test_stressed_price_none(self):
        result = self._run(spot=-0.05)
        assert result.line_results[0]["stressed_price"] is None

    def test_pnl_attribution_fields_present(self):
        result = self._run(spot=-0.05, vol=0.02, days=1)
        rec = result.line_results[0]
        for key in ("delta_pnl", "gamma_pnl", "vega_pnl", "theta_pnl"):
            assert key in rec

    def test_approx_close_to_full_reprice_small_shock(self):
        """For a 1% spot shock, approx and full reprice should be within 5%."""
        pos = [_pos()]
        snaps = {"K1": _snap()}
        s = _scenario(spot=0.01)
        r_full = run_scenario(s, pos, snaps, price_european, CFG, method="full_reprice")
        r_approx = run_scenario(s, pos, snaps, price_european, CFG, method="greek_approx")
        if abs(r_full.total_pnl) > 1e-4:
            rel_err = abs(r_approx.total_pnl - r_full.total_pnl) / abs(r_full.total_pnl)
            assert rel_err < 0.05

    def test_deterministic(self):
        pos = [_pos()]
        snaps = {"K1": _snap()}
        s = _scenario(spot=-0.05, vol=0.02)
        r1 = run_scenario(s, pos, snaps, price_european, CFG, method="greek_approx")
        r2 = run_scenario(s, pos, snaps, price_european, CFG, method="greek_approx")
        assert r1.total_pnl == pytest.approx(r2.total_pnl, abs=1e-10)


# ---------------------------------------------------------------------------
# TestRunScenarioGrid
# ---------------------------------------------------------------------------

class TestRunScenarioGrid:
    def _grid_scenarios(self) -> list[Scenario]:
        return [
            _scenario("dn10", spot=-0.10),
            _scenario("flat"),
            _scenario("up10", spot=0.10),
            _scenario("crash", spot=-0.15, vol=0.10),
        ]

    def test_returns_one_per_scenario(self):
        pos, snaps = _positions_and_snapshots(n=2)
        results = run_scenario_grid(self._grid_scenarios(), pos, snaps, price_european, CFG)
        assert len(results) == 4

    def test_scenario_ids_preserved(self):
        pos, snaps = _positions_and_snapshots(n=1)
        results = run_scenario_grid(self._grid_scenarios(), pos, snaps, price_european, CFG)
        ids = [r.scenario_id for r in results]
        assert ids == ["dn10", "flat", "up10", "crash"]

    def test_different_pnls_per_scenario(self):
        pos, snaps = _positions_and_snapshots(n=1)
        results = run_scenario_grid(self._grid_scenarios(), pos, snaps, price_european, CFG)
        pnls = [r.total_pnl for r in results]
        # down-10 should be worse than flat, flat worse than up-10
        dn10 = next(r for r in results if r.scenario_id == "dn10")
        flat = next(r for r in results if r.scenario_id == "flat")
        up10 = next(r for r in results if r.scenario_id == "up10")
        assert dn10.total_pnl < flat.total_pnl < up10.total_pnl

    def test_empty_scenario_list(self):
        pos, snaps = _positions_and_snapshots(n=1)
        results = run_scenario_grid([], pos, snaps, price_european, CFG)
        assert results == []

    def test_method_propagated(self):
        pos, snaps = _positions_and_snapshots(n=1)
        scenarios = [_scenario("x", spot=0.05)]
        results = run_scenario_grid(scenarios, pos, snaps, price_european, CFG,
                                    method="greek_approx")
        assert results[0].method == "greek_approx"


# ---------------------------------------------------------------------------
# TestExtractTopContributors
# ---------------------------------------------------------------------------

class TestExtractTopContributors:
    def _lines(self) -> list[dict]:
        return [
            {"contract_key": f"K{i}", "pnl": float(i - 2)}
            for i in range(5)
        ]

    def test_returns_top_n_by_abs(self):
        lines = [
            {"contract_key": "A", "pnl": 1.0},
            {"contract_key": "B", "pnl": -5.0},
            {"contract_key": "C", "pnl": 3.0},
        ]
        top2 = extract_top_contributors(lines, n=2)
        assert len(top2) == 2
        assert top2[0]["contract_key"] == "B"   # |pnl|=5 is largest
        assert top2[1]["contract_key"] == "C"   # |pnl|=3

    def test_returns_all_when_n_exceeds_count(self):
        lines = [{"contract_key": "A", "pnl": 1.0}]
        assert len(extract_top_contributors(lines, n=10)) == 1

    def test_empty_input(self):
        assert extract_top_contributors([], n=5) == []

    def test_descending_order(self):
        lines = self._lines()
        top = extract_top_contributors(lines, n=5)
        abs_pnls = [abs(r["pnl"]) for r in top]
        assert abs_pnls == sorted(abs_pnls, reverse=True)

    def test_default_n_is_ten(self):
        lines = [{"contract_key": f"K{i}", "pnl": float(i)} for i in range(20)]
        top = extract_top_contributors(lines)
        assert len(top) == 10


# ---------------------------------------------------------------------------
# TestComputeWorstCase
# ---------------------------------------------------------------------------

class TestComputeWorstCase:
    def _result(self, total_pnl: float, sid: str = "x") -> ScenarioResult:
        return ScenarioResult(
            portfolio_id="P", scenario_id=sid, scenario_version="1.0",
            valuation_ts=0.0, snapshot_ts=0.0, line_results=[],
            total_pnl=total_pnl, worst_contributors=[], method="full_reprice",
        )

    def test_returns_minimum_pnl(self):
        results = [self._result(-10.0, "bad"), self._result(5.0, "ok"),
                   self._result(-50.0, "worst")]
        worst = compute_worst_case(results)
        assert worst.scenario_id == "worst"

    def test_single_result(self):
        r = self._result(-3.0, "only")
        assert compute_worst_case([r]).scenario_id == "only"

    def test_empty_returns_none(self):
        assert compute_worst_case([]) is None

    def test_all_positive_returns_least_positive(self):
        results = [self._result(1.0, "a"), self._result(5.0, "b")]
        worst = compute_worst_case(results)
        assert worst.scenario_id == "a"


# ---------------------------------------------------------------------------
# TestAcceptanceCriterion
# ---------------------------------------------------------------------------

class TestAcceptanceCriterion:
    """PLAN: Reports deterministic given positions + snapshot + scenario version."""

    def test_same_version_same_result(self):
        pos, snaps = _positions_and_snapshots(n=3)
        s = _scenario("crash", spot=-0.15, vol=0.10, days=0, ver="2.0")
        r1 = run_scenario(s, pos, snaps, price_european, CFG)
        r2 = run_scenario(s, pos, snaps, price_european, CFG)
        assert r1.total_pnl == pytest.approx(r2.total_pnl, abs=1e-10)
        assert r1.scenario_version == r2.scenario_version == "2.0"
        for l1, l2 in zip(r1.line_results, r2.line_results):
            assert l1["pnl"] == pytest.approx(l2["pnl"], abs=1e-10)

    def test_different_version_same_params_same_pnl(self):
        """Scenario version is a tag, not a parameter — same params → same PnL."""
        pos = [_pos()]
        snaps = {"K1": _snap()}
        s1 = _scenario(spot=-0.10, ver="1.0")
        s2 = _scenario(spot=-0.10, ver="2.0")
        r1 = run_scenario(s1, pos, snaps, price_european, CFG)
        r2 = run_scenario(s2, pos, snaps, price_european, CFG)
        assert r1.total_pnl == pytest.approx(r2.total_pnl, abs=1e-10)

    def test_grid_results_include_all_scenarios(self):
        scenarios = load_scenarios_from_config({
            "version": "1.0",
            "scenarios": [
                {"scenario_id": "s1", "spot_shift_pct": -0.05, "vol_shift_abs": 0.0},
                {"scenario_id": "s2", "spot_shift_pct":  0.05, "vol_shift_abs": 0.0},
                {"scenario_id": "s3", "spot_shift_pct":  0.00, "vol_shift_abs": 0.05},
            ],
        })
        pos, snaps = _positions_and_snapshots(n=2)
        results = run_scenario_grid(scenarios, pos, snaps, price_european, CFG)
        assert {r.scenario_id for r in results} == {"s1", "s2", "s3"}

    def test_worst_case_identified_correctly(self):
        scenarios = [
            _scenario("dn10", spot=-0.10),
            _scenario("dn5", spot=-0.05),
            _scenario("up5", spot=0.05),
        ]
        pos = [_pos(qty=1.0)]
        snaps = {"K1": _snap(opt="C")}
        results = run_scenario_grid(scenarios, pos, snaps, price_european, CFG)
        worst = compute_worst_case(results)
        # Long call → worst scenario is spot down most
        assert worst.scenario_id == "dn10"

    def test_all_pnl_finite(self):
        scenarios = [
            _scenario("dn15", spot=-0.15, vol=0.10, days=0),
            _scenario("up15", spot=0.15, vol=-0.05, days=0),
            _scenario("time", days=5),
        ]
        pos, snaps = _positions_and_snapshots(n=3)
        results = run_scenario_grid(scenarios, pos, snaps, price_european, CFG)
        for r in results:
            assert math.isfinite(r.total_pnl)
            for line in r.line_results:
                assert math.isfinite(line["pnl"])
