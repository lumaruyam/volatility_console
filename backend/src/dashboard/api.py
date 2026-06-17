"""Volatility Infrastructure Dashboard — FastAPI backend.

Serves the static single-page app and exposes JSON API endpoints that
read from the Parquet storage layer via StorageReader.

Run:
    uvicorn src.dashboard.api:app --reload --port 8000

All endpoints accept:
    date        trade date "YYYY-MM-DD"
    underlying  instrument symbol, e.g. "SPX"
    storage_root  path to data directory (default "data")
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Optional

# Ensure project root is on sys.path when Uvicorn launches this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.dashboard.plots import build_surface_matrix, _parse_scenario_shocks
from src.storage.reader import StorageReader

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Vol Infra Dashboard", version="1.0.0")

_STATIC = Path(__file__).resolve().parent.parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(_STATIC / "index.html"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reader(storage_root: str) -> StorageReader:
    return StorageReader(storage_root, config={})


def _safe(x):
    """Convert NaN/Inf to None so FastAPI can JSON-serialise."""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def _clean(rows: list[dict]) -> list[dict]:
    return [{k: _safe(v) for k, v in r.items()} for r in rows]


# ---------------------------------------------------------------------------
# /api/dates
# ---------------------------------------------------------------------------

@app.get("/api/dates")
def get_dates(
    underlying: str = Query("SPX"),
    storage_root: str = Query("data"),
):
    reader = _reader(storage_root)
    dates = reader.list_partitions("analytics", "surface_grid")
    return {"dates": sorted(dates, reverse=True)}


# ---------------------------------------------------------------------------
# /api/surface  — 3D chart data
# ---------------------------------------------------------------------------

@app.get("/api/surface")
def get_surface(
    date: Optional[str] = Query(None),
    underlying: str = Query("SPX"),
    storage_root: str = Query("data"),
):
    if not date:
        return {"x_values": [], "maturities": [], "iv_matrix": [], "x_label": "Log-Moneyness"}
    reader = _reader(storage_root)
    rows = reader.read_surface_grid(date, underlying)
    if not rows:
        return {"x_values": [], "maturities": [], "iv_matrix": [], "x_label": "Log-Moneyness"}

    m = build_surface_matrix(rows)
    iv_list = [
        [(_safe(v) if not (isinstance(v, float) and math.isnan(v)) else None)
         for v in row]
        for row in m["iv_matrix"].tolist()
    ]
    return {
        "x_values": m["strikes"],
        "x_label": m.get("x_label", "Strike"),
        "x_key": m.get("x_key", "strike"),
        "maturities": m["maturities"],
        "iv_matrix": iv_list,       # [n_maturities][n_strikes]
        "n_points": len(rows),
    }


# ---------------------------------------------------------------------------
# /api/iv  — smile per maturity
# ---------------------------------------------------------------------------

@app.get("/api/iv")
def get_iv(
    date: Optional[str] = Query(None),
    underlying: str = Query("SPX"),
    storage_root: str = Query("data"),
):
    if not date:
        return {"by_maturity": {}, "maturities": []}
    reader = _reader(storage_root)
    rows = reader.read_iv_points(date, underlying)
    if not rows:
        return {"by_maturity": {}, "maturities": []}

    has_strike = any(r.get("strike") is not None for r in rows)
    x_key = "strike" if has_strike else "log_moneyness"

    by_mat: dict[str, list] = {}
    for r in rows:
        mat = str(round(float(r["maturity_years"]), 4)) if r.get("maturity_years") else "?"
        x = _safe(r.get(x_key))
        iv = _safe(r.get("implied_vol") or r.get("iv"))
        if x is None or iv is None:
            continue
        by_mat.setdefault(mat, []).append({"x": x, "iv": iv})

    for pts in by_mat.values():
        pts.sort(key=lambda p: p["x"])

    maturities = sorted(by_mat.keys(), key=float)
    return {"by_maturity": by_mat, "maturities": maturities, "x_key": x_key}


# ---------------------------------------------------------------------------
# /api/greeks  — position Greeks
# ---------------------------------------------------------------------------

@app.get("/api/greeks")
def get_greeks(
    date: Optional[str] = Query(None),
    underlying: str = Query("SPX"),
    storage_root: str = Query("data"),
):
    if not date:
        return {"positions": []}
    reader = _reader(storage_root)
    rows = reader.read_pricing_results(date, underlying)
    if not rows:
        return {"positions": []}

    positions = []
    for r in rows:
        positions.append({
            "contract_key": r.get("contract_key", r.get("instrument_key", "?")),
            "option_type":  r.get("option_type", "?"),
            "strike":       _safe(r.get("strike")),
            "maturity":     _safe(r.get("maturity_years")),
            "delta":        _safe(r.get("delta")),
            "gamma":        _safe(r.get("gamma")),
            "vega":         _safe(r.get("vega_per_point") or r.get("vega")),
            "theta":        _safe(r.get("theta_per_day") or r.get("theta")),
            "dollar_delta": _safe(r.get("dollar_delta")),
            "dollar_gamma": _safe(r.get("dollar_gamma")),
            "dollar_vega":  _safe(r.get("dollar_vega")),
        })
    return {"positions": positions}


# ---------------------------------------------------------------------------
# /api/scenarios  — scenario PnL (77-grid or however many are stored)
# ---------------------------------------------------------------------------

@app.get("/api/scenarios")
def get_scenarios(
    date: Optional[str] = Query(None),
    underlying: str = Query("SPX"),
    portfolio_id: str = Query("STRADDLE_PAPER"),
    storage_root: str = Query("data"),
):
    if not date:
        return {"scenarios": [], "heatmap": None}
    reader = _reader(storage_root)
    rows = reader.read_scenario_results(date, underlying)
    if not rows:
        return {"scenarios": [], "heatmap": None}

    rows = [r for r in rows if r.get("portfolio_id") == portfolio_id or not r.get("portfolio_id")]

    scenarios = []
    for r in rows:
        scenarios.append({
            "scenario_id":    r.get("scenario_id", ""),
            "spot_shift_pct": _safe(r.get("spot_shift_pct")),
            "vol_shift_abs":  _safe(r.get("vol_shift_abs")),
            "total_pnl":      _safe(r.get("total_pnl", 0.0)),
        })

    # Build heatmap matrix if spot/vol axes are available
    heatmap = _build_heatmap(scenarios)

    worst = min(scenarios, key=lambda s: s["total_pnl"] or 0) if scenarios else None
    best  = max(scenarios, key=lambda s: s["total_pnl"] or 0) if scenarios else None
    return {"scenarios": scenarios, "heatmap": heatmap, "worst": worst, "best": best}


def _build_heatmap(scenarios: list[dict]) -> Optional[dict]:
    numeric = [s for s in scenarios
               if s["spot_shift_pct"] is not None and s["vol_shift_abs"] is not None]
    if not numeric:
        return None

    spot_vals = sorted({round(s["spot_shift_pct"], 4) for s in numeric})
    vol_vals  = sorted({round(s["vol_shift_abs"],  4) for s in numeric})
    pnl_map   = {(round(s["spot_shift_pct"], 4), round(s["vol_shift_abs"], 4)): s["total_pnl"]
                 for s in numeric}

    matrix = [
        [_safe(pnl_map.get((sv, vv))) for vv in vol_vals]
        for sv in spot_vals
    ]
    return {
        "spot_shocks": [round(v * 100, 1) for v in spot_vals],   # as %
        "vol_shocks":  [round(v * 100, 1) for v in vol_vals],    # as pts
        "pnl_matrix":  matrix,   # [n_spot][n_vol]
    }


# ---------------------------------------------------------------------------
# /api/straddle
# ---------------------------------------------------------------------------

@app.get("/api/straddle")
def get_straddle(
    date: Optional[str] = Query(None),
    underlying: str = Query("SPX"),
    portfolio_id: str = Query("STRADDLE_PAPER"),
    storage_root: str = Query("data"),
):
    from src.dashboard.app import load_straddle_position
    position = load_straddle_position(storage_root, portfolio_id)
    if not position:
        return {"position": None}

    snapshots = _reader(storage_root).read_snapshots(date, underlying) if date else []
    current_spot = None
    for r in sorted(snapshots, key=lambda x: float(x.get("snapshot_ts") or 0)):
        v = r.get("reference_spot") or r.get("mid")
        if v is not None and float(v) > 0:
            current_spot = float(v)

    return {
        "position": {
            k: (_safe(v) if isinstance(v, float) else v)
            for k, v in position.items()
        },
        "current_spot": current_spot,
    }


# ---------------------------------------------------------------------------
# /api/uam
# ---------------------------------------------------------------------------

@app.get("/api/uam")
def get_uam(
    date: Optional[str] = Query(None),
    underlying: str = Query("SPX"),
    portfolio_id: str = Query("STRADDLE_PAPER"),
    storage_root: str = Query("data"),
):
    if not date:
        return {"uam": None}
    from src.dashboard.app import compute_uam_from_storage

    reader = _reader(storage_root)
    uam_config = {"spot_shock_pct": 0.05, "vol_shock_abs": 0.20, "margin_rate": 0.10}
    result = compute_uam_from_storage(
        reader, date, underlying, portfolio_id, uam_config
    )
    if result is None:
        return {"uam": None}

    import dataclasses
    d = dataclasses.asdict(result) if dataclasses.is_dataclass(result) else dict(result)
    return {"uam": {k: _safe(v) if isinstance(v, float) else v for k, v in d.items()}}


# ---------------------------------------------------------------------------
# Helpers for new panels
# ---------------------------------------------------------------------------

def _risk_free_rate() -> float:
    import yaml
    cfg = Path(__file__).resolve().parent.parent.parent / "configs" / "pricing.yaml"
    try:
        with open(cfg) as f:
            return float(yaml.safe_load(f)["rates"]["risk_free_rate"])
    except Exception:
        return 0.05


def _read_risk_aggregates(storage_root: str, trade_date: str) -> list[dict]:
    import pandas as pd
    base = Path(storage_root) / "analytics" / "risk_aggregates" / f"dt={trade_date}"
    if not base.exists():
        return []
    for v_dir in sorted(base.iterdir(), reverse=True):
        p = v_dir / "data.parquet"
        if p.exists():
            try:
                return pd.read_parquet(p).to_dict("records")
            except Exception:
                pass
    return []


# ---------------------------------------------------------------------------
# /api/greeks_contributions
# ---------------------------------------------------------------------------

@app.get("/api/greeks_contributions")
def get_greeks_contributions(
    date: Optional[str] = Query(None),
    underlying: str = Query("ESTX50"),
    spot_shift_pct: float = Query(-0.10),
    vol_shift_abs: float = Query(0.15),
    multiplier: float = Query(10.0),
    storage_root: str = Query("data"),
):
    if not date:
        return {"positions": [], "n_positions": 0}

    reader = _reader(storage_root)
    rows = _read_risk_aggregates(storage_root, date)
    if not rows:
        return {"positions": [], "n_positions": 0,
                "note": "No risk aggregates — run seed script"}

    spot = 5100.0
    for r in sorted(reader.read_snapshots(date, underlying),
                    key=lambda x: float(x.get("snapshot_ts") or 0)):
        v = r.get("reference_spot") or r.get("mid")
        if v is not None and float(v) > 0:
            spot = float(v)

    dS = spot * spot_shift_pct

    positions = []
    for r in rows:
        qty     = float(r.get("quantity")   or 1.0)
        mult    = float(r.get("multiplier") or multiplier)
        delta   = float(r.get("delta")      or 0.0)
        gamma   = float(r.get("gamma")      or 0.0)
        vega_pp = float(r.get("vega")       or 0.0)
        theta   = float(r.get("theta")      or 0.0)

        key   = str(r.get("contract_key") or "")
        parts = key.split("|")
        label = "|".join(parts[-2:]) if len(parts) >= 2 else key[-20:] or "pos"

        positions.append({
            "label":     label,
            "delta_pnl": _safe(abs(delta             * dS       * qty * mult)),
            "gamma_pnl": _safe(abs(0.5 * gamma       * dS ** 2  * qty * mult)),
            "vega_pnl":  _safe(abs(vega_pp * vol_shift_abs       * qty * mult)),
            "theta_pnl": _safe(abs(theta                         * qty * mult)),
            "rho_pnl":   0.0,
        })

    return {
        "positions":      positions,
        "n_positions":    len(positions),
        "spot":           _safe(spot),
        "dS":             _safe(dS),
        "spot_shift_pct": spot_shift_pct,
        "vol_shift_abs":  vol_shift_abs,
    }


# ---------------------------------------------------------------------------
# /api/historical
# ---------------------------------------------------------------------------

@app.get("/api/historical")
def get_historical(
    ticker: str = Query("^STOXX50E"),
    start: str = Query("2022-01-01"),
):
    from src.historical.yfinance_loader import fetch_index_history
    df = fetch_index_history(ticker, start=start)
    if df.empty:
        return {"dates": [], "closes": [], "ma200": [], "ticker": ticker, "n_points": 0}

    col    = "Adj Close" if "Adj Close" in df.columns else "Close"
    series = df[col].dropna()
    dates  = [str(d)[:10] for d in series.index]
    closes = [_safe(float(v)) for v in series.values]

    ma_raw = series.rolling(200).mean()
    ma200  = [
        (None if math.isnan(float(v)) else _safe(float(v)))
        for v in ma_raw.values
    ]

    return {"dates": dates, "closes": closes, "ma200": ma200,
            "ticker": ticker, "n_points": len(closes)}


# ---------------------------------------------------------------------------
# /api/constituents
# ---------------------------------------------------------------------------

@app.get("/api/constituents")
def get_constituents():
    from datetime import date as _date, timedelta
    import pandas as _pd
    from src.historical.yfinance_loader import EURO_STOXX_50_TICKERS, fetch_constituents_history

    start  = (_date.today() - timedelta(days=45)).isoformat()
    sample = EURO_STOXX_50_TICKERS[:20]

    try:
        df = fetch_constituents_history(sample, start=start)
    except Exception:
        return {"rows": [], "n_tickers": 0, "error": "Fetch failed"}

    if df is None or df.empty:
        return {"rows": [], "n_tickers": 0}

    try:
        if isinstance(df.columns, _pd.MultiIndex):
            lvl0 = df.columns.get_level_values(0)
            close_df = df["Close"] if "Close" in lvl0 else df
        else:
            close_df = df["Close"] if "Close" in df.columns else df
    except Exception:
        return {"rows": [], "n_tickers": 0, "error": "Column parsing failed"}

    rows = []
    for ticker in close_df.columns:
        series = close_df[ticker].dropna()
        if len(series) < 2:
            continue
        last_close = float(series.iloc[-1])
        prev_close = float(series.iloc[-2])
        ret_1d     = (last_close - prev_close) / prev_close if prev_close else 0.0
        rows.append({
            "ticker":     str(ticker),
            "last_close": _safe(last_close),
            "ret_1d":     _safe(ret_1d),
            "last_date":  str(series.index[-1])[:10],
        })

    rows.sort(key=lambda r: r["ret_1d"] or 0, reverse=True)
    return {"rows": rows, "n_tickers": len(rows)}


# ---------------------------------------------------------------------------
# /api/options_chain
# ---------------------------------------------------------------------------

@app.get("/api/options_chain")
def get_options_chain(
    date: Optional[str] = Query(None),
    underlying: str = Query("ESTX50"),
    expiry: Optional[str] = Query(None),
    storage_root: str = Query("data"),
):
    rfr = _risk_free_rate()
    if not date:
        return {"expiries": [], "rows": [], "risk_free_rate": rfr}

    reader  = _reader(storage_root)
    iv_rows = reader.read_iv_points(date, underlying)
    if not iv_rows:
        return {"expiries": [], "rows": [], "risk_free_rate": rfr}

    expiries = sorted({r.get("expiry_str", "") for r in iv_rows if r.get("expiry_str")})
    target   = expiry if expiry in expiries else (expiries[0] if expiries else None)

    if not target:
        return {"expiries": expiries, "rows": [], "risk_free_rate": rfr}

    filtered = [r for r in iv_rows if r.get("expiry_str") == target]
    rows = sorted(
        [
            {
                "expiry":         r.get("expiry_str", ""),
                "maturity_years": round(float(r.get("maturity_years") or 0), 4),
                "strike":         float(r.get("strike") or 0),
                "option_right":   r.get("option_right", ""),
                "implied_vol":    round(float(r.get("implied_vol") or 0), 4),
                "log_moneyness":  round(float(r.get("log_moneyness") or 0), 4),
                "interest_rate":  rfr,
            }
            for r in filtered
        ],
        key=lambda r: (r["strike"], r["option_right"]),
    )

    return {
        "expiries":        expiries,
        "selected_expiry": target,
        "rows":            rows,
        "risk_free_rate":  rfr,
        "n_rows":          len(rows),
    }
