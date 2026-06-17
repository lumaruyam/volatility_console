"""
Seed the dashboard with synthetic data so all panels show content
without a live IBKR connection.

Run:  python scripts/seed_dashboard.py
Then: uvicorn src.dashboard.api:app --reload --port 8000
"""

from __future__ import annotations

import math
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.storage.writer import StorageWriter

TRADE_DATE = date.today().isoformat()
UNDERLYING = "ESTX50"
SPOT       = 5100.0
RATE       = 0.03
CARRY      = 0.02
MULT       = 10.0          # ESTX50 option multiplier
random.seed(42)

writer = StorageWriter("data/storage", {})
snap_ts = time.time()

# 9-maturity ladder  ×  16 strikes  =  144 surface nodes
MATURITIES     = [10, 30, 90, 180, 270, 365, 548, 730, 1095]   # days
STRIKE_OFFSETS = list(range(-600, 601, 80))                     # 16 values: -600…+600 step 80

# ---------------------------------------------------------------------------
# 1. Snapshots
# ---------------------------------------------------------------------------
print("Writing snapshots...")
snapshots = []
for i in range(20):
    ts   = snap_ts - (20 - i) * 180
    spot = SPOT + random.gauss(0, 15)
    snapshots.append({
        "instrument_key": f"{UNDERLYING}|IND|EUREX|EUR",
        "snapshot_ts":    ts,
        "bid":            spot - 0.5,
        "ask":            spot + 0.5,
        "mid":            spot,
        "last":           spot,
        "reference_type": "mid",
        "source":         "seed",
    })
writer.write_snapshots(snapshots, TRADE_DATE, UNDERLYING)
print(f"  {len(snapshots)} rows")

# ---------------------------------------------------------------------------
# 2. Forward curve  (all 9 maturities)
# ---------------------------------------------------------------------------
print("Writing forward curve...")
fwd_rows = []
for days in MATURITIES:
    T      = days / 365
    F      = SPOT * math.exp((RATE - CARRY) * T)
    expiry = (date.today() + timedelta(days=days)).isoformat()
    fwd_rows.append({
        "underlying":              UNDERLYING,
        "snapshot_ts":             snap_ts,
        "maturity_years":          T,
        "expiry_str":              expiry,
        "chosen_forward":          F,
        "weighted_mean_forward":   F,
        "median_forward":          F,
        "confidence_score":        0.95,
        "candidates_before_filter": 12,
        "candidates_after_filter":  10,
    })
writer.write_forward_curve(fwd_rows, TRADE_DATE, UNDERLYING, "v1.0")
print(f"  {len(fwd_rows)} rows")

# ---------------------------------------------------------------------------
# 3. IV points  — both C and P for every (maturity, strike) cell
#    9 maturities × 16 strikes × 2 option types = 288 rows
# ---------------------------------------------------------------------------
print("Writing IV points...")
iv_rows = []
for days in MATURITIES:
    T      = days / 365
    F      = SPOT * math.exp((RATE - CARRY) * T)
    expiry = (date.today() + timedelta(days=days)).isoformat()
    atm_vol = 0.18 + 0.02 * math.sqrt(T)
    for offset in STRIKE_OFFSETS:
        K  = SPOT + offset
        k  = math.log(K / F)
        iv = max(0.05, atm_vol - 0.05 * k + 0.04 * k * k + random.gauss(0, 0.002))
        for opt in ("C", "P"):
            contract_key = f"{UNDERLYING}|OPT|EUREX|EUR|{expiry}|{K:.0f}|{opt}"
            iv_rows.append({
                "contract_key":  contract_key,
                "underlying":    UNDERLYING,
                "snapshot_ts":   snap_ts,
                "expiry_str":    expiry,
                "maturity_years": T,
                "strike":        K,
                "option_right":  opt,
                "implied_vol":   iv,
                "log_moneyness": k,
                "total_variance": iv * iv * T,
                "converged":     True,
                "iterations":    8,
                "residual":      random.uniform(0, 1e-6),
            })
writer.write_iv_points(iv_rows, TRADE_DATE, UNDERLYING, "v1.0")
print(f"  {len(iv_rows)} rows  ({len(MATURITIES)} maturities × {len(STRIKE_OFFSETS)} strikes × 2 types)")

# ---------------------------------------------------------------------------
# 4. Surface parameters  (one SVI fit per maturity)
# ---------------------------------------------------------------------------
print("Writing surface parameters...")
surf_param_rows = []
for days in MATURITIES:
    T      = days / 365
    expiry = (date.today() + timedelta(days=days)).isoformat()
    atm_vol = 0.18 + 0.02 * math.sqrt(T)
    surf_param_rows.append({
        "underlying":   UNDERLYING,
        "snapshot_ts":  snap_ts,
        "expiry_str":   expiry,
        "maturity_years": T,
        "model":        "svi",
        "a":            atm_vol ** 2 * T * 0.9,
        "b":            0.15,
        "rho":          -0.3,
        "m":            0.0,
        "sigma":        0.25,
        "rmse":         random.uniform(0.001, 0.008),
        "n_points":     len(STRIKE_OFFSETS),
        "fit_status":   "ok",
    })
writer.write_surface_parameters(surf_param_rows, TRADE_DATE, UNDERLYING, "v1.0")
print(f"  {len(surf_param_rows)} rows")

# ---------------------------------------------------------------------------
# 5. Surface grid  — one row per (maturity, strike), no option type
#    9 × 16 = 144 nodes
# ---------------------------------------------------------------------------
print("Writing surface grid...")
surf_grid_rows = []
for days in MATURITIES:
    T      = days / 365
    F      = SPOT * math.exp((RATE - CARRY) * T)
    expiry = (date.today() + timedelta(days=days)).isoformat()
    atm_vol = 0.18 + 0.02 * math.sqrt(T)
    for offset in STRIKE_OFFSETS:
        K  = SPOT + offset
        k  = math.log(K / F)
        iv = max(0.05, atm_vol - 0.05 * k + 0.04 * k * k)
        surf_grid_rows.append({
            "underlying":     UNDERLYING,
            "snapshot_ts":    snap_ts,
            "expiry_str":     expiry,
            "maturity_years": T,
            "strike":         K,        # required by plot_vol_surface_heatmap
            "log_moneyness":  k,
            "implied_vol":    iv,
            "iv":             iv,       # alias also checked by build_surface_matrix
            "total_variance": iv * iv * T,
            "model":          "svi",
        })
writer.write_surface_grid(surf_grid_rows, TRADE_DATE, UNDERLYING, "v1.0")
print(f"  {len(surf_grid_rows)} rows  ({len(MATURITIES)} maturities × {len(STRIKE_OFFSETS)} strikes)")

# ---------------------------------------------------------------------------
# 6. Scenario results  (77-combo grid matching configs/scenarios.yaml v2.0)
# ---------------------------------------------------------------------------
print("Writing scenario results...")
scenarios = [
    ("spot_dn_10", -4200),
    ("spot_dn_5",  -1800),
    ("spot_up_5",  -1600),
    ("spot_up_10", -3800),
    ("vol_up_5pts",  2100),
    ("vol_dn_5pts", -1900),
    ("crash",       -8500),
    ("melt_up",     -4100),
    ("theta_1d",     -320),
]
scenario_rows = []
for scenario_id, pnl in scenarios:
    scenario_rows.append({
        "scenario_id":  scenario_id,
        "portfolio_id": "STRADDLE_PAPER",
        "snapshot_ts":  snap_ts,
        "total_pnl":    pnl + random.gauss(0, 50),
        "pnl":          pnl + random.gauss(0, 50),
        "n_positions":  2,
        "version":      "v1.0",
    })
writer.write_scenario_results(scenario_rows, TRADE_DATE, "v1.0")
print(f"  {len(scenario_rows)} rows")

# ---------------------------------------------------------------------------
# 7. Risk aggregates / Greeks by position  (straddle at 270-day expiry)
# ---------------------------------------------------------------------------
print("Writing risk aggregates (Greeks)...")
atm_strike = 5100.0
expiry_9m  = (date.today() + timedelta(days=270)).isoformat()
position_risk_rows = [
    {
        "contract_key":    f"{UNDERLYING}|OPT|EUREX|EUR|{expiry_9m}|{atm_strike:.0f}|C",
        "underlying_symbol": UNDERLYING,
        "portfolio_id":    "STRADDLE_PAPER",
        "snapshot_ts":     snap_ts,
        "quantity":        1.0,
        "multiplier":      MULT,
        "delta":           0.52,
        "gamma":           0.0008,
        "vega":            18.5,
        "theta":           -4.2,
        "dollar_delta":    0.52 * SPOT * MULT,
        "dollar_gamma":    0.0008 * SPOT ** 2 * MULT,
        "dollar_vega":     18.5 * 0.01 * MULT,
    },
    {
        "contract_key":    f"{UNDERLYING}|OPT|EUREX|EUR|{expiry_9m}|{atm_strike:.0f}|P",
        "underlying_symbol": UNDERLYING,
        "portfolio_id":    "STRADDLE_PAPER",
        "snapshot_ts":     snap_ts,
        "quantity":        1.0,
        "multiplier":      MULT,
        "delta":           -0.48,
        "gamma":           0.0008,
        "vega":            18.3,
        "theta":           -4.1,
        "dollar_delta":    -0.48 * SPOT * MULT,
        "dollar_gamma":    0.0008 * SPOT ** 2 * MULT,
        "dollar_vega":     18.3 * 0.01 * MULT,
    },
]
writer.write_risk_aggregates(position_risk_rows, TRADE_DATE, "v1.0")
print(f"  {len(position_risk_rows)} rows")

# ---------------------------------------------------------------------------
print(f"\nSeed complete → {TRADE_DATE} / {UNDERLYING}")
print(f"  Surface grid : {len(MATURITIES)} maturities × {len(STRIKE_OFFSETS)} strikes = {len(surf_grid_rows)} nodes")
print(f"  IV chain     : {len(iv_rows)} rows (C + P per strike)")
print("  Run: uvicorn src.dashboard.api:app --reload --port 8000")
print("  Sidebar: storage root = data/storage, date =", TRADE_DATE, ", underlying = ESTX50")
