"""
Risk router — Page 2: RiskAnalysis.
All endpoints fall back to synthetic data when live positions are unavailable.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from src.risk.var import compute_historical_var
from src.risk.pnl_attribution import compute_pnl_attribution
from src.risk.correlation import compute_correlation
from src.risk.portfolio_state import get_portfolio_greeks, PORTFOLIO_NAV, PORTFOLIO_SPOT

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/greeks")
def greeks() -> dict:
    """Aggregate portfolio Greeks. Synthetic until live IBKR positions are loaded."""
    return get_portfolio_greeks()


@router.get("/var")
def var() -> dict:
    """Historical-simulation VaR at 95% and 99% confidence."""
    return compute_historical_var(
        ticker="^STOXX50E",
        portfolio_value=PORTFOLIO_NAV,
        window_days=252,
    )


@router.get("/pnl-attribution")
def pnl_attribution() -> dict:
    """Greek-based PnL attribution using yesterday's realized spot move."""
    return compute_pnl_attribution(portfolio_greeks=get_portfolio_greeks())


@router.get("/correlation")
def correlation(tickers: str = "SX5E,ASML,MC.PA,SAP,TTE") -> dict:
    """Pearson return correlation matrix (252-day window) for listed tickers."""
    ticker_list = [t.strip() for t in tickers.split(",")]
    return compute_correlation(tickers=ticker_list)


@router.get("/uam")
def uam() -> dict:
    """
    UAM shock grid: +-5% spot x +-30% vol (3x3 scenarios).
    UAM ratio and worst-case PnL computed by src.risk.uam.compute_uam().
    The 3x3 display grid is built from the same PositionRisk object's Greek fields.
    """
    import time
    from src.risk.uam import compute_uam
    from src.risk.models import PositionRisk

    g    = get_portfolio_greeks()
    spot = PORTFOLIO_SPOT
    nav  = PORTFOLIO_NAV

    # Synthetic aggregate PositionRisk from portfolio state.
    # Replace with real PositionRisk rows from IBKR positions (Priority 7).
    agg = PositionRisk(
        portfolio_id="SYNTHETIC",
        contract_key="AGG_SX5E",
        underlying_symbol="SX5E",
        quantity=1.0,
        multiplier=1.0,
        snapshot_ts=time.time(),
        spot=spot,
        forward=spot,
        implied_vol=0.142,
        maturity_years=0.25,
        model_price=0.0,
        market_value=float(nav),
        delta=g["portfolio_delta"] / spot,
        gamma=g["dollar_gamma"] / (spot ** 2),
        vega_per_point=float(g["vega"]),
        theta_per_day=float(g["theta"]),
        dollar_delta=float(g["portfolio_delta"]),
        dollar_gamma=float(g["dollar_gamma"]),
        dollar_vega=float(g["vega"]),
    )

    # compute_uam() covers the 4 corner scenarios per professor spec.
    # Its uam_ratio and worst_case_pnl are used for the footer stats.
    uam_result = compute_uam(
        position_risks=[agg],
        config={"spot_shock_pct": 0.05, "vol_shock_abs": 0.30},
    )

    # Build the full 3x3 display grid using the PositionRisk Greek fields.
    spot_shifts = [-0.05, 0.0,  0.05]
    vol_shifts  = [-0.30, 0.0,  0.30]
    spot_labels = ["Spot -5%", "Spot Unchanged (Base)", "Spot +5%"]
    vol_labels  = ["-30 ΔVol Shock", "ATM Baseline", "+30 ΔVol Shock"]

    def cell_pnl(dS_pct: float, d_sigma: float) -> int:
        dS = agg.spot * dS_pct
        return int(
            agg.dollar_delta / agg.spot * dS
            + 0.5 * agg.dollar_gamma / agg.spot ** 2 * dS ** 2
            + agg.dollar_vega * d_sigma
        )

    rows = []
    for si, dS_pct in enumerate(spot_shifts):
        cells = []
        for d_sigma in vol_shifts:
            pnl = cell_pnl(dS_pct, d_sigma)
            cells.append({"pnl": pnl, "tone": "pos" if pnl > 0 else ("neg" if pnl < 0 else "neu")})
        rows.append({"label": spot_labels[si], "cells": cells})

    return {
        "rows":           rows,
        "vol_col_labels": vol_labels,
        "uam_pct":        round(uam_result.uam_ratio, 4),
        "worst_case_pnl": int(uam_result.worst_case_pnl),
        "portfolio_nav":  nav,
    }


@router.get("/qc-log")
def qc_log() -> list[dict]:
    """
    QC pipeline events from run_daily_qc() applied to a synthetic market snapshot.
    Uses real check logic (src/qc/validation.py) with deterministic synthetic inputs
    so results are realistic even without live IBKR data.
    """
    from datetime import datetime, timezone, timedelta
    import uuid
    from src.qc.validation import run_daily_qc

    now = datetime.now(timezone.utc)
    trade_date = now.strftime("%Y-%m-%d")

    # Synthetic snapshot tuned to produce a realistic mix of PASS / WARN / FAIL
    all_data = {
        # Events every 20s → max gap 20s < 30s threshold → PASS
        "raw_events": [
            {"timestamp": (now - timedelta(seconds=s)).timestamp()}
            for s in range(0, 3600, 20)
        ],
        # 4 stale out of 60 = 6.7% > 5% threshold → WARN (HIGH_STALE_RATIO)
        "snapshots": [
            {"spread_pct": 0.07 + 0.01 * (i % 6), "is_stale": (i % 15 == 0)}
            for i in range(60)
        ],
        # DEC26 all converge; JUN26 27/30 = 90% < 97% threshold → FAIL (LOW_CONVERGENCE_RATIO)
        "iv_points": (
            [
                {"expiry_str": "DEC26", "option_type": "C" if i % 2 else "P",
                 "qc_status": "usable", "converged": True}
                for i in range(30)
            ] + [
                {"expiry_str": "JUN26", "option_type": "C" if i % 2 else "P",
                 "qc_status": "usable", "converged": i < 27}
                for i in range(30)
            ]
        ),
        # Tight cluster → deviation < 0.5% → PASS (FORWARD_STABLE)
        "forward_rows": [{"forward": 4952.0 + 0.4 * i} for i in range(5)],
        # Monotone total-variance → calendar arbitrage-free → PASS
        "surface_params": [
            {"expiry_str": "JUN26", "rmse": 0.009, "maturity_years": 0.25, "atm_total_variance": 0.024},
            {"expiry_str": "DEC26", "rmse": 0.011, "maturity_years": 0.50, "atm_total_variance": 0.032},
            {"expiry_str": "MAR27", "rmse": 0.013, "maturity_years": 0.75, "atm_total_variance": 0.041},
        ],
        # 5 rows with analytic/fd delta diff = 0.012 > 0.01 threshold → FAIL (GREEK_DISCREPANCY)
        "pricing_rows": [
            {"analytic_delta": 0.45 + 0.001 * i,
             "fd_delta":       0.45 + 0.001 * i + (0.012 if i % 8 == 0 else 0.0)}
            for i in range(40)
        ],
        # All 4 UAM corner scenarios present → PASS
        "scenario_results": [
            {"scenario_id": s}
            for s in ("UAM_DN5_VDN30", "UAM_DN5_VUP30", "UAM_UP5_VDN30", "UAM_UP5_VUP30")
        ],
    }
    config = {
        "max_collector_gap_seconds": 30,
        "max_spread_pct": 0.15,
        "max_stale_ratio": 0.05,
        "min_calls_per_maturity": 5,
        "min_puts_per_maturity": 5,
        "min_iv_convergence_ratio": 0.97,
        "max_rmse": 0.02,
        "calendar_sanity_tolerance": 1e-6,
        "greek_sanity_tolerance": 0.01,
        "version": "2.1",
        "session_window_seconds": 27000,
    }
    expected_scenarios = [
        "UAM_DN5_VDN30", "UAM_DN5_VUP30", "UAM_UP5_VDN30", "UAM_UP5_VUP30",
    ]

    _STATUS_MAP = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}
    _TYPE_MAP = {
        "collector_continuity":    "COLLECTOR_CONT",
        "underlying_quote_health": "QUOTE_HEALTH",
        "iv_solver_convergence":   "IV_SOLVER_CONV",
        "forward_stability":       "FORWARD_STAB",
        "calendar_sanity":         "CALENDAR_SANITY",
        "surface_fit_error":       "SURFACE_FIT",
        "greek_sanity":            "GREEK_SANITY",
        "scenario_completeness":   "SCENARIO_COMPL",
    }

    entries: list[dict] = []
    offset_secs = 0
    for underlying in ("SX5E", "V2TX", "DAX"):
        report = run_daily_qc(
            trade_date=trade_date,
            underlying=underlying,
            run_id=uuid.uuid4().hex[:8],
            all_data=all_data,
            config=config,
            expected_scenarios=expected_scenarios,
        )
        for check in report.checks:
            evt_ts = now - timedelta(seconds=offset_secs)
            ts_str = evt_ts.strftime("%H:%M:%S.") + f"{(offset_secs * 137) % 1000:03d}"
            key = check.target_key
            if "/" in key:
                tenor = key.split("/")[-1]
            elif key in ("portfolio", underlying):
                tenor = "ALL"
            else:
                tenor = key
            entries.append({
                "ts":     ts_str,
                "ticker": underlying,
                "type":   _TYPE_MAP.get(check.check_name, check.check_name.upper()[:15]),
                "tenor":  tenor,
                "status": _STATUS_MAP.get(check.status, check.status.upper()),
                "reason": check.reason_code or "PASS",
            })
            offset_secs += 35

    entries.reverse()
    return entries[:20]
