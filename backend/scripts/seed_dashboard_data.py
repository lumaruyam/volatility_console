"""
Seed the storage layer with synthetic analytics data so the dashboard has
something to display without running the full EOD pipeline.

What this writes for each trade date:
  • analytics/surface_grid/dt=<date>/underlying=ESTX50/v=seed/data.parquet
  • analytics/iv_points/dt=<date>/underlying=ESTX50/v=seed/data.parquet
  • analytics/market_state_snapshots/dt=<date>/underlying=ESTX50/data.parquet
  • analytics/forward_curve/dt=<date>/underlying=ESTX50/v=seed/data.parquet
  • analytics/pricing_results/dt=<date>/v=seed/data.parquet
  • analytics/scenario_results/dt=<date>/v=seed/data.parquet
  • analytics/positions/dt=<date>/data.parquet
  • data/positions/STRADDLE_PAPER.json   (straddle status panel)

Usage:
    # Seed today only (default)
    python3 scripts/seed_dashboard_data.py

    # Seed today + 4 previous business days
    python3 scripts/seed_dashboard_data.py --days 5

    # Specific date only
    python3 scripts/seed_dashboard_data.py --trade-date 2026-06-10
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.pricing.european import EuropeanInputs, price_european
from src.storage.writer import StorageWriter

MULTIPLIER  = 10.0
R           = 0.02
Q           = 0.03
PORTFOLIO_ID = "STRADDLE_PAPER"
VERSION     = "seed"
UNDERLYING  = "ESTX50"

# Log-moneyness grid for the synthetic surface (same as SVI calibrator output)
LM_GRID = [-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3]

# Expiry ladder (must be future relative to earliest seed date)
EXPIRY_LADDER = ["2026-07-11", "2026-09-09", "2026-12-08", "2027-06-11"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _business_days(end: date, n: int) -> list[date]:
    """Return exactly n business days ending on `end` (inclusive)."""
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _maturity(trade_date: date, expiry_str: str) -> float:
    exp = date.fromisoformat(expiry_str)
    days = (exp - trade_date).days
    return max(days / 365.0, 1e-6)


def _iv_level(base_iv: float, day_idx: int, rng: random.Random) -> float:
    """Small random level shift per day (±1 vol-pt max cumulative)."""
    shift = rng.gauss(0, 0.003)         # ~0.3 vol-pt std per day
    return max(0.05, base_iv + shift * day_idx)


def _spot_path(spot_today: float, n_days: int, rng: random.Random) -> list[float]:
    """
    Simulate n_days spot prices ending at spot_today via a simple GBM.
    Returns list oldest→newest so path[-1] == spot_today (approx).
    """
    ann_vol = 0.18
    dt = 1 / 252
    path = [spot_today]
    for _ in range(n_days - 1):
        dW = rng.gauss(0, 1)
        r = math.exp(-ann_vol * math.sqrt(dt) * dW)   # drift=0, backward walk
        path.append(path[-1] * r)
    return list(reversed(path))          # oldest first


# ---------------------------------------------------------------------------
# Per-date table generators
# ---------------------------------------------------------------------------

def make_snapshots(spot: float, ts: float) -> list[dict]:
    return [{"instrument_key": f"{UNDERLYING}|IND|EUREX|EUR",
             "snapshot_ts": ts, "bid": spot - 0.5, "ask": spot + 0.5,
             "mid": spot, "last": spot, "reference_type": "mid", "source": "seed"}]


def make_forward_curve(spot: float, ts: float, trade_date: date) -> list[dict]:
    rows = []
    for exp_str in EXPIRY_LADDER:
        T = _maturity(trade_date, exp_str)
        fwd = spot * math.exp((R - Q) * T)
        rows.append({"underlying": UNDERLYING, "snapshot_ts": ts,
                     "maturity_years": T, "expiry_str": exp_str,
                     "chosen_forward": fwd, "weighted_mean_forward": fwd,
                     "median_forward": fwd, "confidence_score": 0.95,
                     "candidates_before_filter": 10, "candidates_after_filter": 8})
    return rows


def make_surface_grid(spot: float, ts: float, trade_date: date,
                      iv_shift: float) -> list[dict]:
    rows = []
    for exp_str in EXPIRY_LADDER:
        T = _maturity(trade_date, exp_str)
        for lm in LM_GRID:
            base_iv = 0.20 + 0.03 * abs(lm) + 0.01 * T
            iv = max(0.05, base_iv + iv_shift)
            rows.append({"underlying": UNDERLYING, "snapshot_ts": ts,
                         "expiry_str": exp_str, "maturity_years": T,
                         "log_moneyness": lm, "implied_vol": iv,
                         "total_variance": iv ** 2 * T, "model": "svi"})
    return rows


def make_iv_points(spot: float, ts: float, trade_date: date,
                   iv_shift: float) -> list[dict]:
    rows = []
    for exp_str in EXPIRY_LADDER:
        T = _maturity(trade_date, exp_str)
        fwd = spot * math.exp((R - Q) * T)
        for lm in LM_GRID:
            strike = fwd * math.exp(lm)
            base_iv = 0.20 + 0.03 * abs(lm) + 0.01 * T
            iv = max(0.05, base_iv + iv_shift)
            for opt in ("C", "P"):
                rows.append({
                    "contract_key": (f"{UNDERLYING}|OPT|EUREX|EUR"
                                     f"|{exp_str}|{strike:.0f}|{opt}"),
                    "underlying": UNDERLYING, "snapshot_ts": ts,
                    "expiry_str": exp_str, "maturity_years": T,
                    "strike": strike, "option_right": opt,
                    "implied_vol": iv, "log_moneyness": lm,
                    "total_variance": iv ** 2 * T,
                    "converged": True, "iterations": 8, "residual": 1e-8,
                })
    return rows


def compute_pricing_results(iv_rows: list[dict], spot: float, ts: float) -> list[dict]:
    out = []
    for r in iv_rows:
        T = float(r["maturity_years"])
        K = float(r["strike"])
        sig = float(r["implied_vol"])
        opt = str(r["option_right"])
        if T <= 0 or sig <= 0:
            continue
        res = price_european(EuropeanInputs(
            S=spot, K=K, T=T, r=R, q=Q,
            sigma=sig, option_type=opt, multiplier=MULTIPLIER))
        fwd = spot * math.exp((R - Q) * T)
        out.append({
            "contract_key": r["contract_key"],
            "underlying": UNDERLYING,
            "expiry_str": r["expiry_str"],
            "maturity_years": T,
            "strike": K,
            "option_type": opt,
            "snapshot_ts": ts,
            "sigma_used": sig,
            "forward_used": fwd,
            "model_price": res.price,
            "delta": res.delta,
            "gamma": res.gamma,
            "vega_per_point": res.vega,
            "theta_per_day": res.theta,
            "dollar_gamma": res.dollar_gamma,
            "dollar_vega": res.dollar_vega,
            "pricer": "black_scholes",
        })
    return out


def compute_scenario_results(pricing_rows: list[dict],
                              spot: float, ts: float) -> list[dict]:
    # 11 spot shocks x 7 vol shocks = 77 combinations (matches configs/scenarios.yaml v2.0)
    spot_shocks = [-0.25, -0.20, -0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15, 0.20, 0.25]
    vol_shocks  = [-0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15]

    def _spot_tag(v: float) -> str:
        p = int(round(v * 100))
        if p < 0: return f"sm{abs(p)}"
        if p > 0: return f"sp{p}"
        return "s0"

    def _vol_tag(v: float) -> str:
        p = int(round(v * 100))
        if p < 0: return f"vm{abs(p)}"
        if p > 0: return f"vp{p}"
        return "v0"

    out = []
    for dS_pct in spot_shocks:
        for dV_abs in vol_shocks:
            dS = spot * dS_pct
            total = sum(
                (pr["delta"] * dS
                 + 0.5 * pr["gamma"] * dS ** 2
                 + pr["vega_per_point"] * dV_abs) * MULTIPLIER
                for pr in pricing_rows
            )
            sid = f"{_spot_tag(dS_pct)}_{_vol_tag(dV_abs)}"
            out.append({
                "scenario_id": sid,
                "underlying": UNDERLYING,
                "snapshot_ts": ts,
                "portfolio_id": PORTFOLIO_ID,
                "spot_shift_pct": dS_pct,
                "vol_shift_abs": dV_abs,
                "total_pnl": total,
            })
    return out


def make_straddle_position(spot: float, trade_date: str,
                            target_expiry: str = "2027-06-11") -> dict:
    d = date.fromisoformat(trade_date)
    T = _maturity(d, target_expiry)
    K = spot   # ATM
    sigma = 0.20
    call = price_european(EuropeanInputs(
        S=spot, K=K, T=T, r=R, q=Q,
        sigma=sigma, option_type="C", multiplier=MULTIPLIER))
    put = price_european(EuropeanInputs(
        S=spot, K=K, T=T, r=R, q=Q,
        sigma=sigma, option_type="P", multiplier=MULTIPLIER))
    base = f"{UNDERLYING}|OPT|EUREX|EUR|{target_expiry}|{K:.0f}"
    return {
        "position_id": f"straddle_{target_expiry}_{K:.0f}",
        "underlying": UNDERLYING,
        "status": "open",
        "open_date": trade_date,
        "target_expiry": target_expiry,
        "notional": spot * MULTIPLIER,
        "call_leg": {"contract_key": f"{base}|C", "option_type": "C",
                     "strike": K, "expiry_str": target_expiry,
                     "quantity": 1.0, "open_price": call.price,
                     "current_price": call.price, "multiplier": MULTIPLIER},
        "put_leg":  {"contract_key": f"{base}|P", "option_type": "P",
                     "strike": K, "expiry_str": target_expiry,
                     "quantity": 1.0, "open_price": put.price,
                     "current_price": put.price, "multiplier": MULTIPLIER},
    }


def _write_positions(storage_root: Path, straddle: dict, trade_date: str) -> None:
    ts = datetime.now(timezone.utc).timestamp()
    rows = [
        {"portfolio_id": PORTFOLIO_ID, "trade_date": trade_date,
         "contract_key": straddle["call_leg"]["contract_key"],
         "underlying": UNDERLYING, "option_type": "C",
         "strike": straddle["call_leg"]["strike"],
         "expiry_str": straddle["target_expiry"],
         "quantity": 1.0, "snapshot_ts": ts},
        {"portfolio_id": PORTFOLIO_ID, "trade_date": trade_date,
         "contract_key": straddle["put_leg"]["contract_key"],
         "underlying": UNDERLYING, "option_type": "P",
         "strike": straddle["put_leg"]["strike"],
         "expiry_str": straddle["target_expiry"],
         "quantity": 1.0, "snapshot_ts": ts},
    ]
    path = (storage_root / "analytics" / "positions"
            / f"dt={trade_date}" / "data.parquet")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


# ---------------------------------------------------------------------------
# Seed one trade date
# ---------------------------------------------------------------------------

def seed_one_day(trade_date: str, spot: float, iv_shift: float,
                 storage_root: Path, writer: StorageWriter,
                 straddle_open_date: str, straddle_spot: float) -> None:
    d = date.fromisoformat(trade_date)
    ts = datetime.combine(d, datetime.min.time(),
                          tzinfo=timezone.utc).timestamp() + 9 * 3600  # 09:00 UTC

    snaps  = make_snapshots(spot, ts)
    fwds   = make_forward_curve(spot, ts, d)
    sg     = make_surface_grid(spot, ts, d, iv_shift)
    iv_pts = make_iv_points(spot, ts, d, iv_shift)
    prices = compute_pricing_results(iv_pts, spot, ts)
    scens  = compute_scenario_results(prices, spot, ts)

    # --- snapshots (no write_snapshots method → write direct parquet) ---
    snap_path = (storage_root / "analytics" / "market_state_snapshots"
                 / f"dt={trade_date}" / f"underlying={UNDERLYING}" / "data.parquet")
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(snaps).to_parquet(snap_path, index=False)

    # --- surface_grid (writer needs underlying) ---
    sg_path = (storage_root / "analytics" / "surface_grid"
               / f"dt={trade_date}" / f"underlying={UNDERLYING}"
               / f"v={VERSION}" / "data.parquet")
    sg_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(sg).to_parquet(sg_path, index=False)

    # --- forward_curve ---
    fwd_path = (storage_root / "analytics" / "forward_curve"
                / f"dt={trade_date}" / f"underlying={UNDERLYING}"
                / f"v={VERSION}" / "data.parquet")
    fwd_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(fwds).to_parquet(fwd_path, index=False)

    # --- iv_points ---
    iv_path = (storage_root / "analytics" / "iv_points"
               / f"dt={trade_date}" / f"underlying={UNDERLYING}"
               / f"v={VERSION}" / "data.parquet")
    iv_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(iv_pts).to_parquet(iv_path, index=False)

    # --- pricing_results and scenario_results (versioned without underlying) ---
    writer.write_pricing_results(prices, trade_date, VERSION)
    writer.write_scenario_results(scens, trade_date, VERSION)

    # --- positions for this date ---
    straddle = make_straddle_position(straddle_spot, straddle_open_date)
    _write_positions(storage_root, straddle, trade_date)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default=date.today().isoformat(),
                        help="Anchor trade date (default: today)")
    parser.add_argument("--storage-root", default="data")
    parser.add_argument("--days", type=int, default=1,
                        help="Number of business days to seed ending on --trade-date")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility")
    args = parser.parse_args()

    anchor      = date.fromisoformat(args.trade_date)
    storage_root = ROOT / args.storage_root
    rng         = random.Random(args.seed)
    writer      = StorageWriter(str(storage_root), {})

    # ---- Get anchor spot from existing snapshots (if present) or default -----
    snap_path = (storage_root / "analytics" / "market_state_snapshots"
                 / f"dt={args.trade_date}" / f"underlying={UNDERLYING}"
                 / "data.parquet")
    if snap_path.exists():
        df = pd.read_parquet(snap_path)
        anchor_spot = float(df[df["instrument_key"].str.startswith(UNDERLYING)]
                            .sort_values("snapshot_ts")["mid"].iloc[-1])
    else:
        anchor_spot = 5100.0   # sensible ESTX50 default

    print(f"Anchor: trade_date={anchor}, spot={anchor_spot:.2f}, days={args.days}")
    print(f"Storage root: {storage_root}\n")

    # ---- Simulate spot path (oldest → newest = anchor) ----------------------
    business_days = _business_days(anchor, args.days)
    spot_path = _spot_path(anchor_spot, len(business_days), rng)

    # Straddle opens on the oldest seeded date at that day's spot
    open_date = business_days[0].isoformat()
    open_spot = spot_path[0]

    # Write straddle JSON once (reflects latest close)
    straddle_json = make_straddle_position(spot_path[-1], business_days[-1].isoformat())
    # Update PnL: call/put current prices at current spot, open prices at open spot
    for leg, opt in [("call_leg", "C"), ("put_leg", "P")]:
        K     = straddle_json[leg]["strike"]
        T_now = _maturity(anchor, straddle_json["target_expiry"])
        T_open = _maturity(business_days[0], straddle_json["target_expiry"])
        open_res = price_european(EuropeanInputs(
            S=open_spot, K=K, T=T_open, r=R, q=Q, sigma=0.20, option_type=opt))
        curr_res = price_european(EuropeanInputs(
            S=spot_path[-1], K=K, T=T_now, r=R, q=Q, sigma=0.20, option_type=opt))
        straddle_json[leg]["open_price"]    = open_res.price
        straddle_json[leg]["current_price"] = curr_res.price
    straddle_json["open_date"] = open_date

    json_path = storage_root / "positions" / f"{PORTFOLIO_ID}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(straddle_json, indent=2))

    # ---- Seed each business day ---------------------------------------------
    total_pnl_call = (straddle_json["call_leg"]["current_price"]
                      - straddle_json["call_leg"]["open_price"]) * MULTIPLIER
    total_pnl_put  = (straddle_json["put_leg"]["current_price"]
                      - straddle_json["put_leg"]["open_price"]) * MULTIPLIER
    print(f"Straddle opened {open_date} at spot {open_spot:.0f}  "
          f"→  PnL call {total_pnl_call:+.0f}  put {total_pnl_put:+.0f}  "
          f"total {total_pnl_call+total_pnl_put:+.0f} EUR")
    print()

    for i, (d, spot) in enumerate(zip(business_days, spot_path)):
        iv_shift = rng.gauss(0, 0.002) * i      # accumulated IV drift
        print(f"  [{i+1}/{len(business_days)}] {d.isoformat()}  spot={spot:.2f}"
              f"  iv_shift={iv_shift:+.4f}")
        seed_one_day(
            trade_date    = d.isoformat(),
            spot          = spot,
            iv_shift      = iv_shift,
            storage_root  = storage_root,
            writer        = writer,
            straddle_open_date = open_date,
            straddle_spot = open_spot,
        )

    print(f"\nDone. {len(business_days)} day(s) seeded.")
    print(f"  streamlit run src/dashboard/app.py")
    print(f"  Available dates: {[d.isoformat() for d in business_days]}")


if __name__ == "__main__":
    main()
