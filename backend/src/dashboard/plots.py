"""
Pure plot functions for the Volatility Infrastructure Dashboard.

All functions accept plain dicts / lists and return matplotlib Figure objects.
No Streamlit or IBKR imports — fully testable in isolation.

Panels produced:
  plot_vol_surface_heatmap       — IV heatmap across strike × maturity
  plot_iv_term_structure         — IV smile per expiry
  plot_greeks_by_position        — dollar_delta / dollar_vega bar chart per contract
  plot_scenario_pnl              — PnL bar chart for each named scenario
  plot_greek_pnl_contributions   — absolute Greek PnL decomposition per position for a scenario
  plot_historical_prices         — adjusted-close line chart with 200-day MA
  plot_straddle_status           — ATR Straddle legs, DTE countdown, PnL
  plot_uam_gauge                 — UAM ratio gauge with threshold line
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Optional

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

matplotlib.use("Agg")  # headless; no display needed


# ---------------------------------------------------------------------------
# Surface helpers
# ---------------------------------------------------------------------------

def build_surface_matrix(rows: list[dict]) -> dict:
    """
    Parse surface_grid rows into arrays for a heatmap.

    Accepts two x-axis formats (auto-detected):
      • "strike"         — absolute strike price (preferred when present)
      • "log_moneyness"  — ln(K/F), used by the SVI surface calibrator output

    Each row also needs: "maturity_years", "iv" or "implied_vol".
    Returns dict with keys:
      strikes, maturities, iv_matrix (2D, NaN where data missing),
      n_strikes, n_maturities, x_key, x_label
    """
    # Prefer 'strike'; fall back to 'log_moneyness'
    has_strike = any(r.get("strike") is not None for r in rows)
    x_key = "strike" if has_strike else "log_moneyness"
    x_label = "Strike" if has_strike else "Log-Moneyness  ln(K/F)"

    x_vals = sorted({float(r[x_key]) for r in rows if r.get(x_key) is not None})
    maturities = sorted({float(r["maturity_years"]) for r in rows
                         if r.get("maturity_years") is not None})

    if not x_vals or not maturities:
        return {
            "strikes": [], "maturities": [],
            "iv_matrix": np.empty((0, 0)), "n_strikes": 0, "n_maturities": 0,
            "x_key": x_key, "x_label": x_label,
        }

    x_idx = {x: i for i, x in enumerate(x_vals)}
    mat_idx = {m: i for i, m in enumerate(maturities)}
    matrix = np.full((len(maturities), len(x_vals)), np.nan)

    for r in rows:
        x = r.get(x_key)
        if x is None or r.get("maturity_years") is None:
            continue
        iv = r.get("iv") or r.get("implied_vol")
        if iv is None:
            continue
        xi = x_idx.get(float(x))
        mi = mat_idx.get(float(r["maturity_years"]))
        if xi is not None and mi is not None:
            matrix[mi, xi] = float(iv)

    return {
        "strikes": x_vals,      # kept as "strikes" for backward compatibility
        "maturities": maturities,
        "iv_matrix": matrix,
        "n_strikes": len(x_vals),
        "n_maturities": len(maturities),
        "x_key": x_key,
        "x_label": x_label,
    }


# ---------------------------------------------------------------------------
# Vol surface heatmap
# ---------------------------------------------------------------------------

def plot_vol_surface_heatmap(
    rows: list[dict],
    underlying: str = "",
    trade_date: str = "",
) -> plt.Figure:
    """
    Implied vol heatmap: x = strike, y = maturity (years), colour = IV.

    If rows is empty, returns a figure with an informative "no data" message.
    """
    fig, ax = plt.subplots(figsize=(10, 5))
    surface = build_surface_matrix(rows)

    if surface["n_strikes"] == 0 or surface["n_maturities"] == 0:
        ax.text(0.5, 0.5, "No surface data available",
                ha="center", va="center", transform=ax.transAxes, fontsize=13)
        ax.set_axis_off()
        fig.suptitle(f"Vol Surface — {underlying} {trade_date}")
        return fig

    matrix = surface["iv_matrix"]
    strikes = surface["strikes"]
    maturities = surface["maturities"]

    im = ax.imshow(matrix, aspect="auto", origin="lower", cmap="RdYlGn_r",
                   vmin=np.nanmin(matrix) * 0.95, vmax=np.nanmax(matrix) * 1.05)
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("Implied Vol")

    x_label = surface.get("x_label", "Strike")
    is_moneyness = surface.get("x_key") == "log_moneyness"
    x_fmt = (lambda x: f"{x:+.2f}") if is_moneyness else (lambda x: f"{x:.0f}")

    ax.set_xticks(range(len(strikes)))
    ax.set_xticklabels([x_fmt(s) for s in strikes], rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(maturities)))
    ax.set_yticklabels([f"{m:.2f}y" for m in maturities], fontsize=8)
    ax.set_xlabel(x_label)
    ax.set_ylabel("Maturity (years)")
    fig.suptitle(f"Implied Vol Surface — {underlying} {trade_date}")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# IV term structure / smile
# ---------------------------------------------------------------------------

def plot_iv_term_structure(
    iv_point_rows: list[dict],
    underlying: str = "",
    trade_date: str = "",
) -> plt.Figure:
    """
    IV smile curves grouped by expiry.
    Each row must have: "expiry_str", "strike", "implied_vol", "option_type".
    """
    fig, ax = plt.subplots(figsize=(10, 5))

    if not iv_point_rows:
        ax.text(0.5, 0.5, "No IV data available",
                ha="center", va="center", transform=ax.transAxes, fontsize=13)
        ax.set_axis_off()
        fig.suptitle(f"IV Term Structure — {underlying} {trade_date}")
        return fig

    expiries = sorted({r["expiry_str"] for r in iv_point_rows
                       if r.get("expiry_str") and r.get("implied_vol") is not None})
    colors = plt.cm.plasma(np.linspace(0.15, 0.85, max(len(expiries), 1)))

    for expiry, color in zip(expiries, colors):
        subset = sorted(
            [r for r in iv_point_rows
             if r.get("expiry_str") == expiry
             and r.get("implied_vol") is not None
             and r.get("strike") is not None],
            key=lambda r: r["strike"],
        )
        if not subset:
            continue
        strikes = [r["strike"] for r in subset]
        ivs = [r["implied_vol"] for r in subset]
        ax.plot(strikes, ivs, marker="o", markersize=3, linewidth=1.5,
                color=color, label=expiry)

    ax.set_xlabel("Strike")
    ax.set_ylabel("Implied Vol")
    ax.legend(loc="upper right", fontsize=7, title="Expiry")
    ax.grid(True, alpha=0.3)
    fig.suptitle(f"IV Smile — {underlying} {trade_date}")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Greeks by position
# ---------------------------------------------------------------------------

def plot_greeks_by_position(
    position_risk_rows: list[dict],
    underlying: str = "",
) -> plt.Figure:
    """
    Grouped bar chart: dollar_delta and dollar_vega per contract_key.
    Rows must have: contract_key, dollar_delta, dollar_vega.
    """
    fig, ax = plt.subplots(figsize=(10, 5))

    rows = [
        r for r in position_risk_rows
        if r.get("contract_key") and r.get("underlying_symbol", underlying) == underlying
    ] if underlying else position_risk_rows

    if not rows:
        ax.text(0.5, 0.5, "No position risk data",
                ha="center", va="center", transform=ax.transAxes, fontsize=13)
        ax.set_axis_off()
        fig.suptitle(f"Greeks by Position — {underlying}")
        return fig

    labels = [r["contract_key"] for r in rows]
    delta_vals = [float(r.get("dollar_delta", 0.0) or 0.0) for r in rows]
    vega_vals = [float(r.get("dollar_vega", 0.0) or 0.0) for r in rows]

    x = np.arange(len(labels))
    width = 0.35

    ax.bar(x - width / 2, delta_vals, width, label="$ Delta", color="#2196F3", alpha=0.8)
    ax.bar(x + width / 2, vega_vals, width, label="$ Vega", color="#FF9800", alpha=0.8)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("USD")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    fig.suptitle(f"Dollar Greeks by Position — {underlying}")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Scenario PnL bar chart
# ---------------------------------------------------------------------------

def plot_scenario_pnl(
    scenario_rows: list[dict],
    title: str = "Scenario PnL",
) -> plt.Figure:
    """
    Horizontal bar chart of PnL per named scenario.
    Each row must have: "scenario_id", "total_pnl" (or "pnl").
    Bars coloured green (positive) / red (negative).
    """
    fig, ax = plt.subplots(figsize=(10, 5))

    if not scenario_rows:
        ax.text(0.5, 0.5, "No scenario results available",
                ha="center", va="center", transform=ax.transAxes, fontsize=13)
        ax.set_axis_off()
        fig.suptitle(title)
        return fig

    sorted_rows = sorted(scenario_rows, key=lambda r: float(r.get("total_pnl") or r.get("pnl") or 0.0))
    labels = [r.get("scenario_id", "?") for r in sorted_rows]
    pnls = [float(r.get("total_pnl") or r.get("pnl") or 0.0) for r in sorted_rows]
    colors = ["#4CAF50" if v >= 0 else "#F44336" for v in pnls]

    y = np.arange(len(labels))
    ax.barh(y, pnls, color=colors, alpha=0.85)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("PnL (USD)")
    ax.grid(True, axis="x", alpha=0.3)
    fig.suptitle(title)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Greek PnL contribution breakdown
# ---------------------------------------------------------------------------

def _parse_scenario_shocks(scenario_id: str) -> tuple[Optional[float], Optional[float]]:
    """
    Return (spot_shift_pct, vol_shift_abs) from a scenario_id produced by the seeder.

    Encoding (from scripts/seed_dashboard_data.py _spot_tag / _vol_tag):
      sm<N>  spot −N%    sp<N>  spot +N%    s0  spot flat
      vm<N>  vol  −N/100  vp<N>  vol  +N/100  v0  vol  flat
    Examples:
      "sm25_vm15" → (−0.25, −0.15)   "sp10_vp5" → (0.10, 0.05)
      "s0_v0"     → (0.00,  0.00)    "sm10_v0"  → (−0.10, 0.00)
    Returns (None, None) when the id does not follow this pattern.
    """
    spot_m = re.search(r's(m|p)(\d+)', scenario_id)
    if spot_m:
        spot: Optional[float] = (-1.0 if spot_m.group(1) == 'm' else 1.0) * int(spot_m.group(2)) / 100
    elif scenario_id.startswith('s0') or '_s0' in scenario_id:
        spot = 0.0
    else:
        spot = None

    vol_m = re.search(r'v(m|p)(\d+)', scenario_id)
    if vol_m:
        vol: Optional[float] = (-1.0 if vol_m.group(1) == 'm' else 1.0) * int(vol_m.group(2)) / 100
    elif scenario_id.endswith('v0') or '_v0' in scenario_id:
        vol = 0.0
    else:
        vol = None

    return spot, vol


def plot_greek_pnl_contributions(
    position_risk_rows: list[dict],
    scenario_id: str,
    spot_shift_pct: Optional[float] = None,
    vol_shift_abs: Optional[float] = None,
    rate_shock_bp: float = 25.0,
) -> plt.Figure:
    """
    Grouped bar chart: absolute Greek PnL contribution per position for a scenario.

    For each position five bars are drawn: |ΔPnL|, |ΓPnL|, |νPnL|, |ΘPnL|, |ρPnL|.

    Formulas (consistent with scripts/seed_dashboard_data.py scenario computation):
      |ΔPnL| = |delta × dS × qty × mult|             dS = spot × spot_shift_pct
      |ΓPnL| = |½ × gamma × dS² × qty × mult|
      |νPnL| = |vega_per_point × vol_shift_abs × qty × mult|
      |ΘPnL| = |theta_per_day × qty × mult|           1 calendar day
      |ρPnL| = |dollar_rho × qty × rate_shock_bp|     0 when not in risk rows

    spot_shift_pct / vol_shift_abs may be supplied explicitly or will be parsed
    from scenario_id (e.g. "sm25_vm15" → −0.25, −0.15).
    """
    fig, ax = plt.subplots(figsize=(12, 5))

    # --- Parse shocks -------------------------------------------------------
    if spot_shift_pct is None or vol_shift_abs is None:
        parsed_spot, parsed_vol = _parse_scenario_shocks(scenario_id)
        if spot_shift_pct is None:
            spot_shift_pct = parsed_spot
        if vol_shift_abs is None:
            vol_shift_abs = parsed_vol

    if spot_shift_pct is None or vol_shift_abs is None:
        ax.text(0.5, 0.5,
                f"Cannot parse shocks from '{scenario_id}'.\n"
                "Scenario IDs must use sm/sp/vm/vp encoding, e.g. 'sm25_vm15'.",
                ha="center", va="center", transform=ax.transAxes, fontsize=11,
                wrap=True)
        ax.set_axis_off()
        fig.suptitle(f"Greek PnL Contributions — {scenario_id}")
        return fig

    if not position_risk_rows:
        ax.text(0.5, 0.5, "No position risk data available",
                ha="center", va="center", transform=ax.transAxes, fontsize=13)
        ax.set_axis_off()
        fig.suptitle(f"Greek PnL Contributions — {scenario_id}")
        return fig

    # --- Compute per-position contributions ---------------------------------
    _GREEK_NAMES   = ["|Δ PnL|", "|Γ PnL|", "|ν PnL|", "|Θ PnL|", "|ρ PnL|"]
    _GREEK_COLORS  = ["#2196F3", "#9C27B0", "#FF9800", "#F44336", "#4CAF50"]

    labels: list[str] = []
    contribs: list[list[float]] = []   # [position_index][greek_index]
    rho_available = False

    for row in position_risk_rows:
        spot      = float(row.get("spot") or 5100.0)
        qty       = float(row.get("quantity") or 1.0)
        mult      = float(row.get("multiplier") or 100.0)
        delta     = float(row.get("delta") or 0.0)
        gamma     = float(row.get("gamma") or 0.0)
        vega_pp   = float(row.get("vega_per_point") or row.get("vega") or 0.0)
        theta_pd  = float(row.get("theta_per_day") or row.get("theta") or 0.0)
        d_rho     = float(row.get("dollar_rho") or 0.0)
        if d_rho != 0.0:
            rho_available = True

        dS = spot * spot_shift_pct
        contribs.append([
            abs(delta   * dS            * qty * mult),
            abs(0.5 * gamma * dS ** 2   * qty * mult),
            abs(vega_pp * vol_shift_abs  * qty * mult),
            abs(theta_pd                * qty * mult),   # 1 calendar day
            abs(d_rho * qty * rate_shock_bp),            # rate_shock_bp bps
        ])

        key   = str(row.get("contract_key") or "")
        parts = key.split("|")
        labels.append("|".join(parts[-2:]) if len(parts) >= 2 else key[-20:] or "pos")

    # --- Draw grouped bars --------------------------------------------------
    n_pos    = len(labels)
    n_greeks = len(_GREEK_NAMES)
    x        = np.arange(n_pos)
    bar_w    = min(0.15, 0.8 / n_greeks)
    offsets  = (np.arange(n_greeks) - (n_greeks - 1) / 2.0) * bar_w

    for gi, (name, color, offset) in enumerate(zip(_GREEK_NAMES, _GREEK_COLORS, offsets)):
        vals = [contribs[pi][gi] for pi in range(n_pos)]
        ax.bar(x + offset, vals, bar_w, label=name, color=color, alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=9)
    ax.set_ylabel("Absolute PnL (EUR)")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, axis="y", alpha=0.3)
    ax.axhline(0, color="black", linewidth=0.6)

    spot_tag = f"{spot_shift_pct * 100:+.0f}%"
    vol_tag  = f"{vol_shift_abs:+.2f}σ"
    rho_tag  = f"  |  ρ: {rate_shock_bp:.0f}bp" if rho_available else "  |  ρ: not in risk rows"
    fig.suptitle(
        f"Greek PnL Contributions — {scenario_id}\n"
        f"Spot {spot_tag}  |  Vol {vol_tag}{rho_tag}",
        fontsize=11,
    )
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Historical price chart
# ---------------------------------------------------------------------------

def plot_historical_prices(
    df: "pd.DataFrame",  # type: ignore[name-defined]
    ticker: str = "",
) -> plt.Figure:
    """
    Line chart of adjusted-close prices with a 200-day moving average overlay.

    Args:
        df:     DataFrame returned by yfinance_loader.fetch_index_history().
                Expected columns: "Adj Close" (preferred) or "Close".
                Index must be DatetimeIndex.
        ticker: Display label for the title and legend.

    Returns:
        matplotlib Figure. Returns a "no data" figure when df is empty or
        the expected price column is absent.
    """
    import pandas as pd  # local import — plots.py has no top-level pandas dep

    fig, ax = plt.subplots(figsize=(12, 5))

    if df is None or (hasattr(df, "empty") and df.empty):
        ax.text(0.5, 0.5, f"No price data available for {ticker or 'ticker'}",
                ha="center", va="center", transform=ax.transAxes, fontsize=13)
        ax.set_axis_off()
        fig.suptitle(f"Historical Prices — {ticker}")
        return fig

    # Prefer Adj Close; fall back to Close
    if "Adj Close" in df.columns:
        series = df["Adj Close"].dropna()
        price_label = "Adj Close"
    elif "Close" in df.columns:
        series = df["Close"].dropna()
        price_label = "Close"
    else:
        ax.text(0.5, 0.5, "DataFrame has no 'Adj Close' or 'Close' column",
                ha="center", va="center", transform=ax.transAxes, fontsize=11)
        ax.set_axis_off()
        fig.suptitle(f"Historical Prices — {ticker}")
        return fig

    if series.empty:
        ax.text(0.5, 0.5, "Price series is empty after dropping NaNs",
                ha="center", va="center", transform=ax.transAxes, fontsize=13)
        ax.set_axis_off()
        fig.suptitle(f"Historical Prices — {ticker}")
        return fig

    # Normalise index to tz-naive dates for cleaner x-axis labels
    dates = series.index
    if hasattr(dates, "tz") and dates.tz is not None:
        dates = dates.tz_convert(None)

    ax.plot(dates, series.values, linewidth=1.4, color="#1565C0",
            label=f"{ticker} {price_label}")

    # 200-day MA (only meaningful when there are enough bars)
    if len(series) >= 200:
        ma200 = series.rolling(200).mean()
        ax.plot(dates, ma200.values, linewidth=1.0, linestyle="--",
                color="#FF7043", alpha=0.85, label="200-day MA")

    # Date range annotation in title
    start_str = str(series.index[0].date()) if hasattr(series.index[0], "date") else str(series.index[0])[:10]
    end_str   = str(series.index[-1].date()) if hasattr(series.index[-1], "date") else str(series.index[-1])[:10]

    ax.set_xlabel("Date")
    ax.set_ylabel("Price")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.25)
    fig.suptitle(
        f"Historical Prices — {ticker}  ({start_str} → {end_str}, {len(series)} days)",
        fontsize=11,
    )
    fig.autofmt_xdate(rotation=30, ha="right")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# ATR Straddle status panel
# ---------------------------------------------------------------------------

def build_straddle_summary(position_dict: dict, as_of_date: str) -> dict:
    """
    Compute display-ready summary for a straddle position dict.
    position_dict keys: position_id, underlying, call_leg, put_leg,
                         open_date, target_expiry, status, notional.
    call_leg / put_leg are dicts with: contract_key, option_type, strike,
                                       expiry_str, quantity, open_price.
    Returns dict with display fields: dte, pnl_call, pnl_put, total_pnl,
                                       status, legs summary.
    """
    target_expiry = position_dict.get("target_expiry", "")
    dte = 0
    if target_expiry:
        try:
            expiry_dt = date.fromisoformat(target_expiry)
            ref_dt = date.fromisoformat(as_of_date)
            dte = max(0, (expiry_dt - ref_dt).days)
        except ValueError:
            pass

    call = position_dict.get("call_leg") or {}
    put = position_dict.get("put_leg") or {}

    call_open = float(call.get("open_price") or 0.0)
    put_open = float(put.get("open_price") or 0.0)
    qty = float(call.get("quantity") or 1.0)
    mult = float(call.get("multiplier") or 10.0)
    call_current = float(call.get("current_price") or call_open)
    put_current = float(put.get("current_price") or put_open)

    pnl_call = (call_current - call_open) * qty * mult
    pnl_put = (put_current - put_open) * qty * mult

    return {
        "position_id": position_dict.get("position_id", ""),
        "underlying": position_dict.get("underlying", ""),
        "status": position_dict.get("status", ""),
        "open_date": position_dict.get("open_date", ""),
        "target_expiry": target_expiry,
        "dte": dte,
        "strike": float(call.get("strike") or 0.0),
        "quantity": qty,
        "notional": float(position_dict.get("notional") or 0.0),
        "call_contract": call.get("contract_key", ""),
        "put_contract": put.get("contract_key", ""),
        "call_open_price": call_open,
        "put_open_price": put_open,
        "call_current_price": call_current,
        "put_current_price": put_current,
        "pnl_call": pnl_call,
        "pnl_put": pnl_put,
        "total_pnl": pnl_call + pnl_put,
    }


def plot_straddle_status(
    position_dict: dict,
    as_of_date: str,
) -> plt.Figure:
    """
    Panel showing ATR Straddle position: legs, DTE countdown, open/current PnL.
    """
    summary = build_straddle_summary(position_dict, as_of_date)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    ax_info, ax_pnl = axes

    # Left: position info table
    ax_info.set_axis_off()
    info_rows = [
        ["Position ID", summary["position_id"]],
        ["Underlying", summary["underlying"]],
        ["Status", summary["status"]],
        ["Strike", f"{summary['strike']:,.0f}"],
        ["Expiry", summary["target_expiry"]],
        ["DTE", f"{summary['dte']} days"],
        ["Quantity", f"{summary['quantity']:.1f} contracts"],
        ["Notional", f"€{summary['notional']:,.0f}"],
        ["Call", summary["call_contract"]],
        ["Put", summary["put_contract"]],
    ]
    table = ax_info.table(
        cellText=info_rows,
        colLabels=["Field", "Value"],
        cellLoc="left",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.4)
    ax_info.set_title("Position Details")

    # Right: PnL bars
    legs = ["Call", "Put", "Total"]
    pnls = [summary["pnl_call"], summary["pnl_put"], summary["total_pnl"]]
    colors = ["#2196F3", "#9C27B0",
              "#4CAF50" if summary["total_pnl"] >= 0 else "#F44336"]
    x = np.arange(len(legs))
    ax_pnl.bar(x, pnls, color=colors, alpha=0.85)
    ax_pnl.axhline(0, color="black", linewidth=0.8)
    ax_pnl.set_xticks(x)
    ax_pnl.set_xticklabels(legs)
    ax_pnl.set_ylabel("PnL (EUR)")
    ax_pnl.set_title("Unrealised PnL")
    ax_pnl.grid(True, axis="y", alpha=0.3)
    for xi, pnl in zip(x, pnls):
        ax_pnl.text(xi, pnl + (max(abs(p) for p in pnls) * 0.02 if pnls else 1),
                    f"{pnl:+,.0f}", ha="center", va="bottom", fontsize=9)

    fig.suptitle(
        f"ATR Straddle — {summary['underlying']} | DTE: {summary['dte']} | "
        f"PnL: €{summary['total_pnl']:+,.0f}",
        fontsize=11,
    )
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# UAM gauge
# ---------------------------------------------------------------------------

def plot_uam_gauge(
    uam_result: dict,
    warn_threshold: float = 0.5,
    critical_threshold: float = 1.0,
) -> plt.Figure:
    """
    Horizontal gauge showing UAM ratio with zone colouring.
    uam_result dict keys: uam_ratio, margin_requirement, portfolio_gross_value,
                           worst_case_pnl, scenario_pnls, portfolio_id.
    Also accepts UAMResult dataclass objects (accessed via attribute).
    """
    def _get(obj, key, default=0.0):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    ratio = float(_get(uam_result, "uam_ratio", 0.0))
    margin = float(_get(uam_result, "margin_requirement", 0.0))
    gross = float(_get(uam_result, "portfolio_gross_value", 0.0))
    worst_pnl = float(_get(uam_result, "worst_case_pnl", 0.0))
    portfolio_id = _get(uam_result, "portfolio_id", "")

    # Scenario breakdown
    scenario_pnls_raw = _get(uam_result, "scenario_pnls", {})
    scenario_pnls = dict(scenario_pnls_raw) if scenario_pnls_raw else {}

    fig, (ax_gauge, ax_scenarios) = plt.subplots(1, 2, figsize=(12, 4))

    # --- Gauge bar ---
    max_display = max(ratio * 1.3, critical_threshold * 1.2, 0.1)
    ax_gauge.barh([0], [warn_threshold], color="#4CAF50", height=0.5, alpha=0.7,
                  label="Safe")
    ax_gauge.barh([0], [critical_threshold - warn_threshold], left=warn_threshold,
                  color="#FF9800", height=0.5, alpha=0.7, label="Warn")
    ax_gauge.barh([0], [max_display - critical_threshold], left=critical_threshold,
                  color="#F44336", height=0.5, alpha=0.7, label="Critical")

    # Needle
    ax_gauge.axvline(ratio, color="black", linewidth=3, linestyle="-", zorder=5)
    ax_gauge.text(ratio, 0.35, f"{ratio:.3f}",
                  ha="center", va="bottom", fontsize=12, fontweight="bold",
                  bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="black"))

    ax_gauge.set_xlim(0, max_display)
    ax_gauge.set_yticks([])
    ax_gauge.set_xlabel("UAM Ratio  (margin_req / portfolio_gross_value)")
    ax_gauge.legend(loc="upper right", fontsize=8)
    ax_gauge.set_title(
        f"UAM — {portfolio_id}\n"
        f"Margin Req: €{margin:,.0f}  |  Portfolio: €{gross:,.0f}  |  "
        f"Worst PnL: €{worst_pnl:+,.0f}"
    )
    ax_gauge.grid(True, axis="x", alpha=0.3)

    # --- Scenario breakdown ---
    if scenario_pnls:
        sc_labels = list(scenario_pnls.keys())
        sc_vals = [float(scenario_pnls[k]) for k in sc_labels]
        sc_colors = ["#4CAF50" if v >= 0 else "#F44336" for v in sc_vals]
        y = np.arange(len(sc_labels))
        ax_scenarios.barh(y, sc_vals, color=sc_colors, alpha=0.85)
        ax_scenarios.axvline(0, color="black", linewidth=0.8)
        ax_scenarios.set_yticks(y)
        ax_scenarios.set_yticklabels(sc_labels, fontsize=9)
        ax_scenarios.set_xlabel("Scenario PnL (EUR)")
        ax_scenarios.set_title("UAM Scenario Breakdown")
        ax_scenarios.grid(True, axis="x", alpha=0.3)
    else:
        ax_scenarios.text(0.5, 0.5, "No scenario data",
                          ha="center", va="center", transform=ax_scenarios.transAxes)
        ax_scenarios.set_axis_off()
        ax_scenarios.set_title("UAM Scenario Breakdown")

    fig.tight_layout()
    return fig
