"""
Options chain builder with Black-Scholes Greeks and live IV inversion.

Priority:
  1. IBKR live snapshot (bid/ask) → Brent IV solver (src/iv/solver.py)
  2. Synthetic chain: BS pricing with skew-adjusted ATM vol from disk cache
"""

from __future__ import annotations

import math
import logging
from datetime import date, datetime
from typing import Any

from src.pricing.european import price_european, EuropeanInputs
from src.surfaces.atm_vol import get_atm_vol, get_spot

log = logging.getLogger(__name__)

_DEFAULT_RATE = 0.035
_DEFAULT_DIV_YIELD = 0.020
_IV_CONFIG = {"lower_vol": 0.01, "upper_vol": 5.0, "price_tolerance": 1e-6, "max_iterations": 100}


def fetch_options_chain(
    adapter: Any,
    ticker: str,
    expiry: str,
    risk_free_rate: float = _DEFAULT_RATE,
    dividend_yield: float = _DEFAULT_DIV_YIELD,
) -> list[dict]:
    """
    Return options chain rows with call/put prices, Greeks, and QC status.

    When IBKR adapter is healthy: requests a live bid/ask snapshot per strike,
    inverts the mid price via the Brent IV solver, and uses the solved IV to
    price Greeks via Black-Scholes. QC status reflects solver convergence.

    Falls back to synthetic BS chain (skew-adjusted ATM vol) when adapter is
    None, disconnected, or any per-strike snapshot fails.
    """
    from src.connectivity.adapter_registry import get_adapter
    from src.connectivity.state import CanonicalContract
    from src.iv.solver import solve_iv
    from src.iv.models import PricingInputs

    live_adapter = get_adapter()
    ibkr_ok = live_adapter is not None and live_adapter.is_healthy()

    expiry_ibkr = expiry.replace("-", "")   # "2026-12-15" → "20261215"

    spot = get_spot(ticker)
    T = _tte(expiry)
    if T <= 0:
        return []

    atm_vol = get_atm_vol(ticker, expiry)
    strikes = _build_strikes(spot)

    rows = []
    for K in strikes:
        k = math.log(K / spot)

        call_iv, put_iv = None, None
        call_qc, put_qc = "synthetic", "synthetic"

        if ibkr_ok:
            for right, currency in (("C", "EUR"), ("P", "EUR")):
                try:
                    contract = CanonicalContract(
                        underlying_symbol=ticker,
                        sec_type="OPT",
                        exchange="SMART",
                        currency=currency,
                        expiry=expiry_ibkr,
                        strike=float(K),
                        right=right,
                        multiplier=10,
                    )
                    snap = live_adapter.request_snapshot(contract, timeout_s=5.0)
                    if snap.bid is not None and snap.ask is not None and snap.bid < snap.ask:
                        mid = (snap.bid + snap.ask) / 2.0
                        inp = PricingInputs(
                            S=spot, K=float(K), T=T,
                            r=risk_free_rate, q=dividend_yield,
                            option_type=right,
                        )
                        res = solve_iv(mid, inp, _IV_CONFIG, contract_key=contract.instrument_key)
                        if res.converged and res.implied_vol is not None:
                            iv = res.implied_vol
                            if right == "C":
                                call_iv, call_qc = iv, "pass"
                            else:
                                put_iv, put_qc = iv, "pass"
                except Exception as exc:
                    log.debug("options_chain: IBKR snapshot failed K=%s right=%s: %s", K, right, exc)

        # Synthetic fallback: skew-adjusted ATM vol
        if call_iv is None:
            call_iv = max(0.05, atm_vol + 0.025 * (-k))
        if put_iv is None:
            put_iv = max(0.05, atm_vol + 0.040 * (-k))

        c = price_european(EuropeanInputs(
            S=spot, K=K, T=T, r=risk_free_rate, q=dividend_yield,
            sigma=call_iv, option_type="C",
        ))
        p = price_european(EuropeanInputs(
            S=spot, K=K, T=T, r=risk_free_rate, q=dividend_yield,
            sigma=put_iv, option_type="P",
        ))

        half_spread = 0.005
        is_atm = abs(K / spot - 1.0) < 0.015

        rows.append({
            "strike":      K,
            "call_bid":    round(c.price * (1 - half_spread), 2),
            "call_ask":    round(c.price * (1 + half_spread), 2),
            "call_iv":     round(call_iv * 100, 2),
            "call_delta":  round(c.delta, 4),
            "call_gamma":  round(c.gamma, 6),
            "call_vega":   round(c.vega, 2),
            "call_theta":  round(c.theta, 2),
            "call_qc":     call_qc,
            "put_bid":     round(p.price * (1 - half_spread), 2),
            "put_ask":     round(p.price * (1 + half_spread), 2),
            "put_iv":      round(put_iv * 100, 2),
            "put_delta":   round(p.delta, 4),
            "put_gamma":   round(p.gamma, 6),
            "put_vega":    round(p.vega, 2),
            "put_theta":   round(p.theta, 2),
            "put_qc":      put_qc,
            "atm":         is_atm,
        })

    return rows


def _tte(expiry: str) -> float:
    """Time-to-expiry in years from an ISO date string (strips trailing labels)."""
    expiry_clean = expiry.split(" ")[0].strip()
    try:
        expiry_date = datetime.strptime(expiry_clean, "%Y-%m-%d").date()
        return max(0.0, (expiry_date - date.today()).days / 365.0)
    except Exception:
        return 90 / 365.0


def _build_strikes(spot: float, n_each_side: int = 3) -> list[float]:
    """Symmetric strike grid around ATM."""
    if spot > 5_000:
        step = 100
    elif spot > 1_000:
        step = 50
    elif spot > 100:
        step = 10
    else:
        step = 5
    atm = round(spot / step) * step
    return [int(atm + i * step) for i in range(-n_each_side, n_each_side + 1)]
