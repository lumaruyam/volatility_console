"""
Volatility Infrastructure Dashboard — Streamlit app.

Loads data from the storage layer (StorageReader) and renders panels:
  1. Vol Surface Heatmap
  2. IV Term Structure / Smile
  3. Greeks by Position
  4. Scenario PnL
  5. ATR Straddle Status          ← calls src/strategy/straddle.py
  6. UAM Gauge                    ← calls src/risk/uam.py
  7. Historical Prices            ← yfinance loader
  8. Options Chain                ← iv_points + configs/pricing.yaml

Run with:  streamlit run src/dashboard/app.py

No live IBKR connection required — all data comes from persisted Parquet
partitions written by the analytics pipeline.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

# Ensure project root is on sys.path so `src.*` imports work when Streamlit
# launches the file directly (it does not inherit the caller's PYTHONPATH).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.dashboard.plots import (
    build_straddle_summary,
    plot_greek_pnl_contributions,
    plot_greeks_by_position,
    plot_historical_prices,
    plot_iv_term_structure,
    plot_scenario_pnl,
    plot_straddle_status,
    plot_uam_gauge,
    plot_vol_surface_heatmap,
)
from src.risk.models import PositionRisk, UAMResult
from src.risk.uam import compute_uam
from src.storage.reader import StorageReader


# ---------------------------------------------------------------------------
# Straddle position persistence
# ---------------------------------------------------------------------------

def save_straddle_position(
    position,
    storage_root: str,
    portfolio_id: str,
) -> None:
    """
    Persist a StraddlePosition (dataclass or dict) as JSON.
    Written to <storage_root>/positions/<portfolio_id>.json.
    Call this after open_straddle() or roll_straddle() so the dashboard
    can load and display the current position.
    """
    path = Path(storage_root) / "positions" / f"{portfolio_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dataclasses.asdict(position) if dataclasses.is_dataclass(position) else dict(position)
    path.write_text(json.dumps(data, indent=2, default=str))


def load_straddle_position(
    storage_root: str,
    portfolio_id: str,
) -> Optional[dict]:
    """
    Load the persisted StraddlePosition dict from JSON.
    Returns None if no position file exists yet.
    The returned dict matches the shape expected by build_straddle_summary()
    and plot_straddle_status() in plots.py.
    """
    path = Path(storage_root) / "positions" / f"{portfolio_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# UAM computation from stored analytics
# ---------------------------------------------------------------------------

def build_position_risks(
    pricing_rows: list[dict],
    snapshot_rows: list[dict],
    position_rows: list[dict],
    portfolio_id: str,
    multiplier: float = 10.0,
    underlying: str = "",
) -> list[PositionRisk]:
    """
    Assemble PositionRisk objects from pre-computed storage rows.

    pricing_rows  — from reader.read_pricing_results(): delta, gamma, vega, model_price …
    snapshot_rows — from reader.read_snapshots(): reference_spot, maturity_years …
    position_rows — from reader.read_positions(): contract_key → quantity mapping
                    (falls back to qty=1.0 per contract when table is empty)
    """
    # Reference spot: use the most recent snapshot that has a positive value.
    # Accept both "reference_spot" (analytics format) and "mid" (raw snapshot format).
    spot = 0.0
    for row in sorted(snapshot_rows, key=lambda r: float(r.get("snapshot_ts") or 0)):
        rs = row.get("reference_spot") or row.get("mid")
        if rs is not None and float(rs) > 0:
            spot = float(rs)
    if spot == 0.0:
        return []

    qty_map: dict[str, float] = {
        r["contract_key"]: float(r.get("quantity", 1.0))
        for r in position_rows
        if r.get("contract_key")
    }
    mat_map: dict[str, float] = {}
    for row in snapshot_rows:
        k = row.get("instrument_key") or row.get("contract_key")
        if k and row.get("maturity_years") is not None:
            mat_map[str(k)] = float(row["maturity_years"])

    risks: list[PositionRisk] = []
    for row in pricing_rows:
        if underlying and row.get("underlying") != underlying:
            continue
        key = str(row.get("contract_key") or "")
        if not key:
            continue

        qty = qty_map.get(key, 1.0)
        mat = mat_map.get(key, 1.0)
        fwd = float(row.get("forward_used") or spot)
        sigma = float(row.get("sigma_used") or 0.0)
        delta = float(row.get("delta") or 0.0)
        gamma = float(row.get("gamma") or 0.0)
        vega = float(row.get("vega_per_point") or 0.0)
        theta = float(row.get("theta_per_day") or 0.0)
        price = float(row.get("model_price") or 0.0)

        # Use stored dollar Greeks when available; fall back to formula
        d_gamma = float(row.get("dollar_gamma") or gamma * spot ** 2 * qty * multiplier)
        d_vega = float(row.get("dollar_vega") or vega * qty * multiplier)

        risks.append(PositionRisk(
            portfolio_id=portfolio_id,
            contract_key=key,
            underlying_symbol=str(row.get("underlying") or underlying),
            quantity=qty,
            multiplier=multiplier,
            snapshot_ts=float(row.get("snapshot_ts") or 0.0),
            spot=spot,
            forward=fwd,
            implied_vol=sigma,
            maturity_years=mat,
            model_price=price,
            market_value=price * qty * multiplier,
            delta=delta,
            gamma=gamma,
            vega_per_point=vega,
            theta_per_day=theta,
            dollar_delta=delta * spot * qty * multiplier,
            dollar_gamma=d_gamma,
            dollar_vega=d_vega,
        ))
    return risks


def compute_uam_from_storage(
    reader: StorageReader,
    trade_date: str,
    underlying: str,
    portfolio_id: str,
    uam_config: dict,
    multiplier: float = 10.0,
) -> Optional[UAMResult]:
    """
    Load pricing results + snapshots from storage, build PositionRisk objects,
    and call compute_uam() from src/risk/uam.py.
    Returns None when no pricing data is available for the date.
    """
    pricing_rows = reader.read_pricing_results(trade_date, underlying)
    snapshot_rows = reader.read_snapshots(trade_date, underlying)
    try:
        position_rows = reader.read_positions(trade_date, portfolio_id)
    except AttributeError:
        position_rows = []

    risks = build_position_risks(
        pricing_rows, snapshot_rows, position_rows,
        portfolio_id=portfolio_id,
        multiplier=multiplier,
        underlying=underlying,
    )
    if not risks:
        return None

    snapshot_ts = max(r.snapshot_ts for r in risks)
    return compute_uam(risks, uam_config, portfolio_id=portfolio_id,
                       snapshot_ts=snapshot_ts)


# ---------------------------------------------------------------------------
# Dashboard data loader
# ---------------------------------------------------------------------------

def load_dashboard_data(
    reader: StorageReader,
    trade_date: str,
    underlying: str,
    portfolio_id: str = "STRADDLE_PAPER",
    uam_config: Optional[dict] = None,
    multiplier: float = 10.0,
) -> dict:
    """
    Load all data required for the dashboard from the storage layer.
    Returns a dict; each key feeds one panel.
    No IBKR session or live connection needed.

    Straddle position is read from <storage_root>/positions/<portfolio_id>.json.
    UAM result is computed from stored pricing_results + market_state_snapshots.
    """
    surface_grid = reader.read_surface_grid(trade_date, underlying)
    iv_points = reader.read_iv_points(trade_date, underlying)
    snapshots = reader.read_snapshots(trade_date, underlying)
    surface_params = reader.read_surface_parameters(trade_date, underlying)
    forward_curve = reader.read_forward_curve(trade_date, underlying)
    pricing_results = reader.read_pricing_results(trade_date, underlying)
    scenario_results = reader.read_scenario_results(trade_date, underlying)

    straddle_position = load_straddle_position(
        str(reader.storage_root), portfolio_id
    )
    uam_result = compute_uam_from_storage(
        reader, trade_date, underlying,
        portfolio_id=portfolio_id,
        uam_config=uam_config or {},
        multiplier=multiplier,
    )

    return {
        "trade_date": trade_date,
        "underlying": underlying,
        "surface_grid": surface_grid,
        "iv_points": iv_points,
        "snapshots": snapshots,
        "surface_params": surface_params,
        "forward_curve": forward_curve,
        "pricing_results": pricing_results,
        "position_risks": [],       # built on demand in the Greeks tab
        "scenario_results": scenario_results,
        "straddle_position": straddle_position,
        "uam_result": uam_result,
    }


def make_sample_uam_result(portfolio_id: str = "STRADDLE_PAPER") -> dict:
    """Return a zero-value UAM dict for the no-data fallback display."""
    return {
        "portfolio_id": portfolio_id,
        "uam_ratio": 0.0,
        "margin_requirement": 0.0,
        "portfolio_gross_value": 0.0,
        "worst_case_pnl": 0.0,
        "scenario_pnls": {
            "up_vol_up": 0.0, "up_vol_dn": 0.0,
            "dn_vol_up": 0.0, "dn_vol_dn": 0.0,
        },
    }


def _read_risk_free_rate() -> float:
    """Return rates.risk_free_rate from configs/pricing.yaml, falling back to 0.05."""
    import yaml
    cfg_path = Path(__file__).resolve().parent.parent.parent / "configs" / "pricing.yaml"
    try:
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        return float(cfg["rates"]["risk_free_rate"])
    except Exception:
        return 0.05


def available_dates(reader: StorageReader) -> list[str]:
    """List trade dates for which surface_grid data exists."""
    return reader.list_partitions("analytics", "surface_grid")


def default_trade_date(reader: StorageReader) -> str:
    """Return the most recent available date, or today."""
    dates = available_dates(reader)
    return dates[-1] if dates else date.today().isoformat()


# ---------------------------------------------------------------------------
# Streamlit app entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import streamlit as st

    @st.cache_data(ttl=3600)
    def _fetch_hist(ticker: str, start: str) -> "pd.DataFrame":
        from src.historical.yfinance_loader import fetch_index_history
        return fetch_index_history(ticker, start=start)

    st.set_page_config(
        page_title="Vol Infra Dashboard",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("Volatility Infrastructure Dashboard")

    # ------------------------------------------------------------------ #
    # Sidebar — storage root + controls                                   #
    # ------------------------------------------------------------------ #
    st.sidebar.header("Data Source")
    storage_root = st.sidebar.text_input(
        "Storage root", value="data", key="storage_root"
    )
    portfolio_id = st.sidebar.text_input(
        "Portfolio ID", value="STRADDLE_PAPER", key="portfolio_id"
    )
    config: dict = {}
    try:
        reader = StorageReader(storage_root, config)
        dates = available_dates(reader)
        default_date = dates[-1] if dates else date.today().isoformat()
    except Exception:
        reader = None
        dates = []
        default_date = date.today().isoformat()

    if dates:
        trade_date = st.sidebar.selectbox(
            "Trade date", options=list(reversed(dates)),
            index=0, key="trade_date"
        )
    else:
        trade_date = st.sidebar.text_input(
            "Trade date (YYYY-MM-DD)", value=default_date, key="trade_date_text"
        )
        st.sidebar.warning("No data found. Run `python3 scripts/seed_dashboard_data.py --days 5`")
    underlying = st.sidebar.text_input("Underlying", value="ESTX50")

    st.sidebar.markdown("---")
    st.sidebar.subheader("UAM Config")
    spot_shock = st.sidebar.slider("Spot shock %", 1, 20, 5) / 100
    vol_shock = st.sidebar.slider("Vol shock (pts)", 5, 40, 20) / 100
    uam_config = {"spot_shock_pct": spot_shock, "vol_shock_abs": vol_shock}

    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Dashboard reads from persisted Parquet partitions. "
        "No live IBKR connection required."
    )
    if st.sidebar.button("Refresh"):
        st.rerun()

    # ------------------------------------------------------------------ #
    # Load data                                                           #
    # ------------------------------------------------------------------ #
    data: dict = {
        "trade_date": trade_date,
        "underlying": underlying,
        "surface_grid": [],
        "iv_points": [],
        "snapshots": [],
        "surface_params": [],
        "forward_curve": [],
        "pricing_results": [],
        "position_risks": [],
        "scenario_results": [],
        "straddle_position": None,
        "uam_result": None,
    }

    if reader is not None:
        try:
            data = load_dashboard_data(
                reader, trade_date, underlying,
                portfolio_id=portfolio_id,
                uam_config=uam_config,
            )
        except Exception as exc:
            st.error(f"Failed to load data from {storage_root}: {exc}")

    # ------------------------------------------------------------------ #
    # Position risks — built once, shared by Greeks + Scenario PnL tabs  #
    # ------------------------------------------------------------------ #
    position_risks = data.get("position_risks") or []
    if not position_risks and data.get("pricing_results"):
        position_risks = build_position_risks(
            data["pricing_results"],
            data["snapshots"],
            [],
            portfolio_id=portfolio_id,
            underlying=underlying,
        )
    risk_dicts = [
        dataclasses.asdict(r) if dataclasses.is_dataclass(r) else r
        for r in position_risks
    ]

    # ------------------------------------------------------------------ #
    # Tabs                                                                #
    # ------------------------------------------------------------------ #
    (tab_surface, tab_smile, tab_greeks, tab_scenario,
     tab_straddle, tab_uam, tab_hist, tab_chain) = st.tabs([
        "Vol Surface", "IV Smile", "Greeks", "Scenario PnL",
        "Straddle Status", "UAM Gauge", "Historical Prices", "Options Chain",
    ])

    with tab_surface:
        st.subheader(f"Implied Vol Surface — {underlying} {trade_date}")
        fig = plot_vol_surface_heatmap(data["surface_grid"], underlying, trade_date)
        st.pyplot(fig)

    with tab_smile:
        st.subheader(f"IV Term Structure — {underlying} {trade_date}")
        fig = plot_iv_term_structure(data["iv_points"], underlying, trade_date)
        st.pyplot(fig)

    with tab_greeks:
        st.subheader("Dollar Greeks by Position")
        fig = plot_greeks_by_position(risk_dicts, underlying)
        st.pyplot(fig)

    with tab_scenario:
        st.subheader("Scenario PnL")
        fig = plot_scenario_pnl(
            data["scenario_results"],
            title=f"Scenario PnL — {underlying} {trade_date}",
        )
        st.pyplot(fig)

        st.divider()
        st.subheader("Greek PnL Contributions by Scenario")

        scenario_ids = sorted({
            r.get("scenario_id", "") for r in data["scenario_results"]
            if r.get("scenario_id")
        })
        if not scenario_ids:
            st.info("No scenario results available.")
        elif not risk_dicts:
            st.info(
                "No position risk data available for contribution breakdown. "
                "Run the EOD pricing pipeline or re-seed with `seed_dashboard_data.py`."
            )
        else:
            col_sel, col_rate = st.columns([3, 1])
            selected_scenario = col_sel.selectbox(
                "Scenario",
                scenario_ids,
                key="greek_pnl_scenario",
                help="Parseable IDs use sm/sp/vm/vp encoding, e.g. 'sm25_vm15'",
            )
            rate_shock_bp = col_rate.number_input(
                "Rate shock (bp)", value=25, min_value=1, max_value=200, step=5,
                key="rate_shock_bp",
            )
            fig2 = plot_greek_pnl_contributions(
                risk_dicts,
                selected_scenario,
                rate_shock_bp=float(rate_shock_bp),
            )
            st.pyplot(fig2)

    with tab_straddle:
        st.subheader("ATR Straddle Position Status")
        pos = data.get("straddle_position")
        if pos is None:
            st.info(
                "No straddle position file found at "
                f"`{storage_root}/positions/{portfolio_id}.json`. "
                "Run the strategy to open an initial position — it will be saved here."
            )
        else:
            summary = build_straddle_summary(pos, trade_date)
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("DTE", f"{summary['dte']} days")
            col2.metric("Total PnL", f"€{summary['total_pnl']:+,.0f}")
            col3.metric("Status", summary["status"])
            col4.metric("Strike", f"{summary['strike']:,.0f}")
            fig = plot_straddle_status(pos, trade_date)
            st.pyplot(fig)

    with tab_uam:
        st.subheader("UAM — Utilisation des Actifs Margés")
        uam = data.get("uam_result")
        if uam is None:
            st.info(
                "No pricing results found for this date — UAM cannot be computed. "
                "Run the EOD analytics pipeline first."
            )
            uam = make_sample_uam_result(portfolio_id)

        if isinstance(uam, dict):
            ratio = uam.get("uam_ratio", 0.0)
        else:
            ratio = getattr(uam, "uam_ratio", 0.0)

        colour = "normal" if ratio < 0.5 else ("off" if ratio < 1.0 else "inverse")
        st.metric("UAM Ratio", f"{ratio:.4f}", delta_color=colour)
        fig = plot_uam_gauge(uam)
        st.pyplot(fig)

    with tab_hist:
        st.subheader("Historical Index Prices")
        col_tk, col_st = st.columns([1, 1])
        hist_ticker = col_tk.text_input("Ticker", value="^STOXX50E", key="hist_ticker")
        hist_start  = col_st.text_input("Start date (YYYY-MM-DD)", value="2022-01-01",
                                        key="hist_start")
        with st.spinner(f"Fetching {hist_ticker} from {hist_start}…"):
            hist_df = _fetch_hist(hist_ticker, hist_start)
        fig = plot_historical_prices(hist_df, hist_ticker)
        st.pyplot(fig)
        if hist_df is not None and not hist_df.empty:
            start_d = str(hist_df.index[0])[:10]
            end_d   = str(hist_df.index[-1])[:10]
            st.caption(f"{len(hist_df)} trading days  |  {start_d} → {end_d}")

    with tab_chain:
        import pandas as pd

        st.subheader(f"Options Chain — {underlying} {trade_date}")

        iv_rows = data.get("iv_points") or []
        if not iv_rows:
            st.info(
                "No IV points found for this date and underlying. "
                "Run the EOD pipeline or re-seed: `python scripts/seed_dashboard.py`"
            )
        else:
            risk_free_rate = _read_risk_free_rate()

            # Build flat DataFrame with the requested columns plus interest_rate
            chain_df = pd.DataFrame([
                {
                    "expiry":         r.get("expiry_str", ""),
                    "maturity_years": float(r.get("maturity_years") or 0.0),
                    "strike":         float(r.get("strike") or 0.0),
                    "option_right":   r.get("option_right", ""),
                    "implied_vol":    float(r.get("implied_vol") or 0.0),
                    "log_moneyness":  float(r.get("log_moneyness") or 0.0),
                    "interest_rate":  risk_free_rate,
                }
                for r in iv_rows
            ])
            chain_df = chain_df.sort_values(
                ["expiry", "strike", "option_right"]
            ).reset_index(drop=True)

            # Expiry selectbox
            expiries = sorted(chain_df["expiry"].unique().tolist())
            col_sel, col_m, col_r, col_n = st.columns([3, 2, 2, 2])
            selected_expiry = col_sel.selectbox(
                "Expiry", expiries, key="chain_expiry"
            )

            display_df = chain_df[
                chain_df["expiry"] == selected_expiry
            ].reset_index(drop=True)

            mat = display_df["maturity_years"].iloc[0] if len(display_df) else 0.0
            dte = int(round(mat * 365))
            col_m.metric("Maturity", f"{mat:.4f} yr")
            col_r.metric("Rate (r)", f"{risk_free_rate:.4f}")
            col_n.metric("Rows", f"{len(display_df)}")

            st.dataframe(display_df, use_container_width=True, hide_index=True)
            st.caption(
                f"DTE {dte} days  |  "
                f"{len(chain_df)} total rows across {len(expiries)} expiries  |  "
                f"interest_rate = {risk_free_rate:.4f} (configs/pricing.yaml)"
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# Streamlit executes this module at top level on every script rerun.
# Guard: only call main() when streamlit's script-run context is active —
# prevents execution on plain `python -c "import app"` and during pytest.
def _running_under_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False

if _running_under_streamlit() and "pytest" not in sys.modules:
    main()
