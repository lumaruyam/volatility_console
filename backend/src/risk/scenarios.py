"""
Scenario engine — stress PnL under configured spot/vol/time shocks.

Scenario grids are version-controlled in configs/scenarios.yaml.
Full repricing is the reference path; local Greek approximation is the speed path.
The scenario definition is part of data lineage — queryable alongside outputs.

Two computation paths
---------------------
full_reprice (reference):
    Restress the analytics snapshot, call the pricer twice (base + stressed),
    PnL = (stressed_price - base_price) × quantity × multiplier.
    Exact for the given pricer model; slower.

greek_approx (speed):
    ΔV ≈ Δ·dS + ½·Γ·dS² + ν·dσ + Θ·dt
    Uses PositionRisk dollar Greeks from Step 11.
    Fast; accurate for small shocks; deviates for large moves due to higher-order terms.

Acceptance criterion: same Scenario + same snapshots → identical ScenarioResult (pure functions).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.risk.aggregation import compute_position_risk
from src.risk.models import Position

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    """
    One named stress scenario — immutable and version-tagged.
    Must be defined in configs/scenarios.yaml, never as a notebook cell.
    """
    scenario_id: str
    spot_shift_pct: float     # e.g. -0.10 = spot down 10 %
    vol_shift_abs: float      # e.g.  0.05 = vol up 5 vol points
    time_roll_days: int       # e.g.  1    = age the book by 1 calendar day
    description: str = ""
    version: str = "1.0"


@dataclass
class ScenarioResult:
    """Repricing result for one scenario applied to one portfolio."""
    portfolio_id: str
    scenario_id: str
    scenario_version: str
    valuation_ts: float
    snapshot_ts: float
    line_results: list[dict]      # Per-position: see _make_line_record docstring
    total_pnl: float
    worst_contributors: list[dict]  # Top N by |pnl|, descending
    method: str                   # "full_reprice" | "greek_approx"
    analytics_version: str = "1.0"


# ---------------------------------------------------------------------------
# Core: run one scenario
# ---------------------------------------------------------------------------

def run_scenario(
    scenario: Scenario,
    positions: list[Position],
    analytics_snapshots: dict,
    pricer: Callable,
    config: dict,
    method: str = "full_reprice",
) -> ScenarioResult:
    """
    Apply one scenario to all positions and return a full attribution result.

    Args:
        scenario:             Immutable Scenario definition (version-tagged).
        positions:            List of Position objects.
        analytics_snapshots:  Mapping contract_key → analytics dict.
                              Required keys: S, K, T, r, q, sigma, option_type.
                              Optional: multiplier (default 100), snapshot_ts.
        pricer:               Callable(EuropeanInputs) → PricingResult.
        config:               Risk config dict.
                                valuation_ts     (default 0.0)
                                snapshot_ts      (default 0.0)
                                top_contributors_n (default 10)
                                analytics_version  (default "1.0")
        method:               "full_reprice" or "greek_approx".

    Returns:
        ScenarioResult with per-position line_results and top contributors.

    Raises:
        ValueError for unknown method.
    """
    if method not in ("full_reprice", "greek_approx"):
        raise ValueError(f"Unknown method {method!r}. Use 'full_reprice' or 'greek_approx'")

    valuation_ts = float(config.get("valuation_ts", 0.0))
    snapshot_ts = float(config.get("snapshot_ts", 0.0))
    top_n = int(config.get("top_contributors_n", 10))
    analytics_version = config.get("analytics_version", "1.0")
    portfolio_id = positions[0].portfolio_id if positions else ""

    line_results: list[dict] = []

    for position in positions:
        snap = analytics_snapshots.get(position.contract_key)
        if snap is None:
            logger.warning("run_scenario: no snapshot for %s — skipping", position.contract_key)
            continue

        if method == "full_reprice":
            record = _full_reprice_line(position, snap, scenario, pricer)
        else:
            record = _greek_approx_line(position, snap, scenario, pricer, config)

        line_results.append(record)

    total_pnl = sum(r["pnl"] for r in line_results)
    top_contributors = extract_top_contributors(line_results, n=top_n)

    return ScenarioResult(
        portfolio_id=portfolio_id,
        scenario_id=scenario.scenario_id,
        scenario_version=scenario.version,
        valuation_ts=valuation_ts,
        snapshot_ts=snapshot_ts,
        line_results=line_results,
        total_pnl=total_pnl,
        worst_contributors=top_contributors,
        method=method,
        analytics_version=analytics_version,
    )


def _full_reprice_line(
    position: Position,
    snap: dict,
    scenario: Scenario,
    pricer: Callable,
) -> dict:
    """
    Full-reprice path for one position line.
    PnL = (stressed_price − base_price) × quantity × multiplier.
    """
    from src.pricing.european import EuropeanInputs

    mult = float(snap.get("multiplier", 100.0))
    Q = position.quantity

    base_inputs = EuropeanInputs(
        S=snap["S"], K=snap["K"], T=snap["T"],
        r=snap["r"], q=snap["q"], sigma=snap["sigma"],
        option_type=snap["option_type"], multiplier=mult,
    )
    base_price = pricer(base_inputs).price

    S_stressed = snap["S"] * (1.0 + scenario.spot_shift_pct)
    sigma_stressed = max(snap["sigma"] + scenario.vol_shift_abs, 1e-4)
    T_stressed = max(snap["T"] - scenario.time_roll_days / 365.0, 1e-6)

    stressed_inputs = EuropeanInputs(
        S=S_stressed, K=snap["K"], T=T_stressed,
        r=snap["r"], q=snap["q"], sigma=sigma_stressed,
        option_type=snap["option_type"], multiplier=mult,
    )
    stressed_price = pricer(stressed_inputs).price

    base_value = base_price * Q * mult
    stressed_value = stressed_price * Q * mult
    pnl = stressed_value - base_value

    return {
        "contract_key": position.contract_key,
        "underlying_symbol": position.underlying_symbol,
        "quantity": Q,
        "multiplier": mult,
        "base_price": base_price,
        "stressed_price": stressed_price,
        "base_value": base_value,
        "stressed_value": stressed_value,
        "pnl": pnl,
    }


def _greek_approx_line(
    position: Position,
    snap: dict,
    scenario: Scenario,
    pricer: Callable,
    config: dict,
) -> dict:
    """
    Greek approximation path for one position line.
    ΔV ≈ Δ·dS + ½·Γ·dS² + ν·dσ + Θ·dt
    """
    pos_risk = compute_position_risk(position, snap, pricer, config)

    S = snap["S"]
    Q = position.quantity
    mult = float(snap.get("multiplier", 100.0))
    dS = S * scenario.spot_shift_pct
    d_sigma = scenario.vol_shift_abs
    dt = float(scenario.time_roll_days)

    delta_pnl = pos_risk.dollar_delta / S * dS
    gamma_pnl = 0.5 * pos_risk.dollar_gamma / (S ** 2) * dS ** 2
    vega_pnl = pos_risk.dollar_vega * d_sigma
    theta_pnl = pos_risk.theta_per_day * Q * mult * dt
    pnl = delta_pnl + gamma_pnl + vega_pnl + theta_pnl

    return {
        "contract_key": position.contract_key,
        "underlying_symbol": position.underlying_symbol,
        "quantity": Q,
        "multiplier": mult,
        "base_price": pos_risk.model_price,
        "stressed_price": None,    # not computed in approx mode
        "base_value": pos_risk.market_value,
        "stressed_value": pos_risk.market_value + pnl,
        "pnl": pnl,
        "delta_pnl": delta_pnl,
        "gamma_pnl": gamma_pnl,
        "vega_pnl": vega_pnl,
        "theta_pnl": theta_pnl,
    }


# ---------------------------------------------------------------------------
# Grid runner
# ---------------------------------------------------------------------------

def run_scenario_grid(
    scenarios: list[Scenario],
    positions: list[Position],
    analytics_snapshots: dict,
    pricer: Callable,
    config: dict,
    method: str = "full_reprice",
) -> list[ScenarioResult]:
    """
    Run all scenarios in the grid. Returns one ScenarioResult per scenario.
    """
    results = []
    for scenario in scenarios:
        result = run_scenario(scenario, positions, analytics_snapshots,
                              pricer, config, method=method)
        results.append(result)
        logger.info("scenario.done id=%s total_pnl=%.2f method=%s",
                    scenario.scenario_id, result.total_pnl, method)
    return results


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_scenarios_from_config(config: dict) -> list[Scenario]:
    """
    Load named scenarios from a scenarios.yaml config dict.
    Applies the top-level version as each scenario's version when not overridden.
    """
    grid_version = str(config.get("version", "1.0"))
    scenarios = []
    for s_dict in config.get("scenarios", []):
        scenarios.append(Scenario(
            scenario_id=s_dict["scenario_id"],
            spot_shift_pct=float(s_dict["spot_shift_pct"]),
            vol_shift_abs=float(s_dict["vol_shift_abs"]),
            time_roll_days=int(s_dict.get("time_roll_days", 0)),
            description=s_dict.get("description", ""),
            version=s_dict.get("version", grid_version),
        ))
    return scenarios


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def extract_top_contributors(line_results: list[dict], n: int = 10) -> list[dict]:
    """Return the top N positions by absolute PnL impact, descending."""
    return sorted(line_results, key=lambda x: abs(x.get("pnl", 0.0)), reverse=True)[:n]


def compute_worst_case(scenario_results: list[ScenarioResult]) -> Optional[ScenarioResult]:
    """Return the scenario with the most negative total PnL."""
    if not scenario_results:
        return None
    return min(scenario_results, key=lambda r: r.total_pnl)
