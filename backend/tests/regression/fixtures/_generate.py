"""Run once to (re)generate all regression fixture files.

   python tests/regression/fixtures/_generate.py
"""

import json
import math
import uuid
import sys
from pathlib import Path

HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# Black-Scholes helpers
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, r, q, sigma, right):
    if T <= 0:
        if right == "C":
            return max(0.0, S - K)
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if right == "C":
        return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


# ---------------------------------------------------------------------------
# Event factory
# ---------------------------------------------------------------------------

def make_event(session_id, instrument_key, field_name, field_value, receipt_ts):
    return {
        "session_id": session_id,
        "event_id": uuid.uuid4().hex,
        "instrument_key": instrument_key,
        "field_name": field_name,
        "field_value": round(float(field_value), 4),
        "exchange_ts": None,
        "receipt_ts": float(receipt_ts),
        "source": "replay",
    }


def quote_events(session_id, instrument_key, bid, ask, ts, include_last=False, last=None):
    events = [
        make_event(session_id, instrument_key, "bid", bid, ts),
        make_event(session_id, instrument_key, "ask", ask, ts),
    ]
    if include_last:
        events.append(make_event(session_id, instrument_key, "last", last or (bid + ask) / 2, ts))
    return events


def write_jsonl(path, events):
    Path(path).write_text("\n".join(json.dumps(e) for e in events) + "\n")


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

S = 4950.0
r = 0.04
q = 0.02

# Expiries: use fixed dates relative to trade_date 2025-01-15
# JUN25 = 2025-06-20, T = 156/365; DEC25 = 2025-12-19, T = 337/365
# Expiry keys in YYYYMMDD for instrument keys; ISO for expected JSON (builder converts)
EXPIRIES_YYYYMMDD = ["20250620", "20251219"]
EXPIRIES_ISO = ["2025-06-20", "2025-12-19"]
EXPIRIES = [
    ("20250620", 156 / 365),  # T ≈ 0.427
    ("20251219", 337 / 365),  # T ≈ 0.923
]

# Synthetic smile: (strike, sigma_jun, sigma_dec)
SMILE = [
    (4700, 0.235, 0.230),
    (4800, 0.220, 0.220),
    (4900, 0.208, 0.212),
    (5000, 0.200, 0.208),
    (5100, 0.198, 0.205),
    (5200, 0.205, 0.208),
]

UNDERLYING_KEY = "SX5E|IND|EUREX|EUR||||"
BASE_TS = 1736928000.0  # 2025-01-15 08:00:00 UTC
SPREAD_PTS = 3.0        # bid/ask half-spread for options (3 index points each side)


def option_key(expiry, strike, right, mult=100):
    return f"SX5E|OPT|EUREX|EUR|{expiry}|{int(strike)}|{right}|{mult}"


def option_quotes(session_id, ts, n_expiries=2, strike_subset=None):
    """Generate bid+ask events for all (or subset of) option contracts."""
    events = []
    strikes = SMILE if strike_subset is None else [s for s in SMILE if s[0] in strike_subset]
    for i, (exp, T) in enumerate(EXPIRIES[:n_expiries]):
        for strike, sig_jun, sig_dec in strikes:
            sigma = sig_jun if i == 0 else sig_dec
            for right in ("C", "P"):
                mid = bs_price(S, strike, T, r, q, sigma, right)
                bid = max(0.05, mid - SPREAD_PTS)
                ask = mid + SPREAD_PTS
                events += quote_events(session_id, option_key(exp, strike, right), bid, ask, ts)
    return events


def underlying_quotes(session_id, ts):
    return [
        make_event(session_id, UNDERLYING_KEY, "bid",    4948.0, ts),
        make_event(session_id, UNDERLYING_KEY, "ask",    4952.0, ts),
        make_event(session_id, UNDERLYING_KEY, "last",   4950.0, ts),
        make_event(session_id, UNDERLYING_KEY, "volume", 25000.0, ts),
    ]


# ---------------------------------------------------------------------------
# Scenario: calm_day  (4 timestamps, full option chain)
# ---------------------------------------------------------------------------

def gen_calm_day():
    sid = "regression-calm-20250115"
    events = []
    for i in range(4):
        ts = BASE_TS + i * 300  # every 5 minutes
        events += underlying_quotes(sid, ts)
        events += option_quotes(sid, ts)
    write_jsonl(HERE / "calm_day" / "raw_events.jsonl", events)

    # Expected surface: 2 slices, SVI fit (not fallback), loose vol bounds
    expected = {
        "description": "calm ESTX50 day, full 6-strike chain, SVI converges",
        "n_slices": 2,
        "underlying_spot": S,
        "slices": [
            {
                "expiry": "2025-06-20",
                "atm_vol_min": 0.17,
                "atm_vol_max": 0.24,
                "rmse_max": 0.008,
                "is_fallback": False,
                "n_points_min": 6,
            },
            {
                "expiry": "2025-12-19",
                "atm_vol_min": 0.17,
                "atm_vol_max": 0.24,
                "rmse_max": 0.008,
                "is_fallback": False,
                "n_points_min": 6,
            },
        ],
        "n_iv_converged_min": 10,
    }
    (HERE / "calm_day" / "expected_surface.json").write_text(json.dumps(expected, indent=2))
    print(f"calm_day: {len(events)} events")


# ---------------------------------------------------------------------------
# Scenario: event_heavy  (12 timestamps, full option chain)
# ---------------------------------------------------------------------------

def gen_event_heavy():
    sid = "regression-heavy-20250115"
    events = []
    for i in range(12):
        ts = BASE_TS + i * 100  # every 100 seconds
        # Slight intraday vol drift (+0.2 pts per tick to simulate vol move)
        drift = i * 0.002
        events += underlying_quotes(sid, ts)
        # Override spread to simulate wider during event
        spread = SPREAD_PTS + (3.0 if 3 <= i <= 7 else 0.0)
        for j, (exp, T) in enumerate(EXPIRIES):
            for strike, sig_jun, sig_dec in SMILE:
                sigma = (sig_jun if j == 0 else sig_dec) + drift * 0.5
                for right in ("C", "P"):
                    mid = bs_price(S, strike, T, r, q, sigma, right)
                    bid = max(0.05, mid - spread)
                    ask = mid + spread
                    events += quote_events(sid, option_key(exp, strike, right), bid, ask, ts)
    write_jsonl(HERE / "event_heavy" / "raw_events.jsonl", events)

    expected = {
        "description": "high-frequency updates, wider spreads during event window",
        "n_slices": 2,
        "underlying_spot": S,
        "slices": [
            {
                "expiry": "2025-06-20",
                "atm_vol_min": 0.17,
                "atm_vol_max": 0.27,
                "rmse_max": 0.015,
                "is_fallback": False,
                "n_points_min": 6,
            },
            {
                "expiry": "2025-12-19",
                "atm_vol_min": 0.17,
                "atm_vol_max": 0.27,
                "rmse_max": 0.015,
                "is_fallback": False,
                "n_points_min": 6,
            },
        ],
        "n_iv_converged_min": 8,
    }
    (HERE / "event_heavy" / "expected_surface.json").write_text(json.dumps(expected, indent=2))
    print(f"event_heavy: {len(events)} events")


# ---------------------------------------------------------------------------
# Scenario: sparse_liquidity  (only 3 strikes per expiry → PCHIP fallback)
# ---------------------------------------------------------------------------

def gen_sparse_liquidity():
    sid = "regression-sparse-20250115"
    events = []
    # Only 2 strikes × 2 rights = 4 IV points per expiry < min_points_per_slice=5
    # → PCHIP spline fallback triggered on both slices
    sparse_strikes = {4900, 5100}
    for i in range(3):
        ts = BASE_TS + i * 300
        events += underlying_quotes(sid, ts)
        events += option_quotes(sid, ts, strike_subset=sparse_strikes)
    write_jsonl(HERE / "sparse_liquidity" / "raw_events.jsonl", events)

    expected = {
        "description": "sparse chain: only 2 strikes (4 IV pts/expiry < 5 threshold) → PCHIP fallback",
        "n_slices": 2,
        "underlying_spot": S,
        "slices": [
            {
                "expiry": "2025-06-20",
                "atm_vol_min": 0.15,
                "atm_vol_max": 0.28,
                "rmse_max": 0.03,
                "is_fallback": True,
                "n_points_min": 1,
            },
            {
                "expiry": "2025-12-19",
                "atm_vol_min": 0.15,
                "atm_vol_max": 0.28,
                "rmse_max": 0.03,
                "is_fallback": True,
                "n_points_min": 1,
            },
        ],
        "n_iv_converged_min": 4,
    }
    (HERE / "sparse_liquidity" / "expected_surface.json").write_text(json.dumps(expected, indent=2))
    print(f"sparse_liquidity: {len(events)} events")


# ---------------------------------------------------------------------------
# Scenario: disconnect_recovery  (two event files, 600s gap)
# ---------------------------------------------------------------------------

def gen_disconnect_recovery():
    sid = "regression-disconnect-20250115"

    # Part 1: first 3 ticks at t=0, 300, 600
    part1 = []
    for i in range(3):
        ts = BASE_TS + i * 300
        part1 += underlying_quotes(sid, ts)
        part1 += option_quotes(sid, ts)
    write_jsonl(HERE / "disconnect_recovery" / "raw_events_part1.jsonl", part1)

    # Part 2: resumes after 600s gap (t=1500, 1800, 2100)
    part2 = []
    for i in range(3):
        ts = BASE_TS + 1500 + i * 300
        part2 += underlying_quotes(sid, ts)
        part2 += option_quotes(sid, ts)
    write_jsonl(HERE / "disconnect_recovery" / "raw_events_part2.jsonl", part2)

    expected = {
        "description": "kill-and-restart: two event files with 600s gap; merged snapshot must use latest quotes",
        "n_slices": 2,
        "underlying_spot": S,
        "gap_seconds": 600,
        "n_events_part1": len(part1),
        "n_events_part2": len(part2),
        "slices": [
            {
                "expiry": "2025-06-20",
                "atm_vol_min": 0.17,
                "atm_vol_max": 0.24,
                "rmse_max": 0.008,
                "is_fallback": False,
                "n_points_min": 6,
            },
            {
                "expiry": "2025-12-19",
                "atm_vol_min": 0.17,
                "atm_vol_max": 0.24,
                "rmse_max": 0.008,
                "is_fallback": False,
                "n_points_min": 6,
            },
        ],
        "n_iv_converged_min": 10,
    }
    (HERE / "disconnect_recovery" / "expected_surface.json").write_text(json.dumps(expected, indent=2))
    print(f"disconnect_recovery: {len(part1)} + {len(part2)} events")


if __name__ == "__main__":
    gen_calm_day()
    gen_event_heavy()
    gen_sparse_liquidity()
    gen_disconnect_recovery()
    print("Done.")
