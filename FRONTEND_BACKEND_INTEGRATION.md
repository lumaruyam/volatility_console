# Frontend ↔ Backend Integration

> Volatility Infrastructure Platform — Albert School · AI for Algo Trading  
> Last updated: 2026-06-17

---

## Architecture

```
frontend/                              backend/
  src/components/terminal/views/         src/api/routers/
  ┌─────────────────────┐                ┌────────────────┐
  │ DataOverview.tsx    │ ←── /api/market/* ──→ │ market.py      │ ←─ surfaces/, pricing/, connectivity/
  │ RiskAnalysis.tsx    │ ←── /api/risk/*   ──→ │ risk.py        │ ←─ risk/var, correlation, pnl_attribution
  │ StrategyExecution   │ ←── /api/strategy/* → │ strategy.py    │ ←─ connectivity/market_depth, risk/hedge_suggest
  │ Backtesting.tsx     │ ←── /api/backtest/* → │ backtest.py    │ ←─ backtest/engine, backtest/monte_carlo
  │ ShockSimulator.tsx  │ ←── /api/shock/*  ──→ │ shock.py       │ ←─ portfolio_state (shared Greeks)
  └─────────────────────┘                └────────────────┘
```

**Dev routing:** Vite (`frontend/vite.config.ts`) proxies all `/api/*` requests to
`http://localhost:8000`. CORS in `backend/src/api/main.py` allows `http://localhost:5173`.

> **Note:** `backend/src/dashboard/app.py` is a legacy Streamlit analytics tool.
> It is NOT the frontend. All UI lives in `/frontend`.

---

## Data Source Hierarchy

All market data flows through `src/historical/data_fetcher.py`. IBKR is the primary source;
yfinance is used as a fallback or for long backtests; Parquet disk cache is the offline safety net.

```
FastAPI startup (lifespan in src/api/main.py)
      │
      └── build_adapter_from_env() → IbkrAdapter.connect()
                │
                └── stored in src/connectivity/adapter_registry.py

Every data request:
      │
      ▼
data_fetcher.fetch_history() / fetch_spot()
      │
      ├── Tier 1: get_adapter() returns healthy adapter + window ≤ 3Y
      │         └──→ ibkr_loader.fetch_index_history(adapter, ...)   [IBKR PRIMARY]
      │                   └── saves to disk cache on success
      │                   └── empty / error ──→ Tier 2
      │
      ├── Tier 2: yfinance_loader.fetch_index_history(...)            [long history / offline fallback]
      │                   └── saves to disk cache on success
      │                   └── failure ──→ Tier 3
      │
      └── Tier 3: disk_cache.load(ticker, start, end)                 [offline safety net]
                    data/cache/ohlcv/{ticker}.parquet
                    Pre-populate with: python scripts/seed_cache.py
```

**Offline presentation at school:** Port 7497 is blocked → IBKR and yfinance both fail →
Tier 3 disk cache serves all endpoints. Pre-seed with `python scripts/seed_cache.py` (56 tickers, ~13 MB).

**IBKR connection config (environment variables):**

| Variable | Default | Description |
|----------|---------|-------------|
| `IBKR_HOST` | `127.0.0.1` | TWS or IB Gateway host |
| `IBKR_PORT` | `7497` | Paper: 7497 · Live: 7496 |
| `IBKR_CLIENT_ID` | `1` | Unique client session ID |

---

## Legend

| Status | Meaning |
|--------|---------|
| ✅ IBKR→yf→cache | Calls IBKR; falls back to yfinance; falls back to disk cache |
| ✅ Dynamic | Real module called; synthetic inputs when live data unavailable |
| ⚙️ Computed | Deterministic formula on `portfolio_state` Greeks — no live positions yet |
| ⚠️ Synthetic | Fully hardcoded; no real module wired |
| ✅ ACK | POST endpoint logs the action and returns `{"status":"ok"}` |

---

## Page 1 — DataOverview.tsx

**Frontend file:** `frontend/src/components/terminal/views/DataOverview.tsx`  
**Router:** `backend/src/api/routers/market.py`

| Endpoint | Status | What it does |
|----------|--------|--------------|
| `GET /api/market/index-matrix` | ✅ IBKR→yf→cache | `get_spot()` / `get_atm_vol()` → `data_fetcher` 3-tier hierarchy. 50 Euro Stoxx 50 constituents fetched in parallel via `ThreadPoolExecutor(max_workers=8)`. Disk cache pre-seeded with `seed_cache.py`. |
| `GET /api/market/options-chain` | ✅ IBKR→yf→cache | Live path: `get_adapter()` → `request_snapshot()` per strike → `solve_iv()` (Brent, `src/iv/solver.py`). `call_qc="pass"` when solver converges, `"synthetic"` on fallback. Offline path: skew-adjusted ATM vol from disk cache via `get_atm_vol()`. |
| `GET /api/market/vol-surface` | ✅ IBKR→yf→cache | Spot + ATM vol from `data_fetcher` 3-tier. Surface built by `fit_surface()` (`src/surfaces/calibration.py`) — real SVI per maturity slice. Reports actual RMSE. Falls back to inline SVI formula on calibration failure. |
| `GET /api/market/engine-status` | ✅ Dynamic | Checks `get_adapter().is_healthy()`. Reports real data source (`"IBKR"` vs `"disk_cache"`), measured Parquet read latency, `"IBKR parity forward"` vs `"SOFR-OIS + Div"` curve ID, today's trade date. `engine_load_pct` = 42 (live) / 15 (offline). |
| `GET /api/market/greeks-summary` | ⚠️ Synthetic | Returns hardcoded `total_delta=0.0435` etc. `src/risk/aggregation.py` not wired here. |

**Frontend contract verified:** All 5 response shapes match what DataOverview.tsx reads.

---

## Page 2 — RiskAnalysis.tsx

**Frontend file:** `frontend/src/components/terminal/views/RiskAnalysis.tsx`  
**Router:** `backend/src/api/routers/risk.py`

| Endpoint | Status | What it does |
|----------|--------|--------------|
| `GET /api/risk/greeks` | ⚙️ Computed | `get_portfolio_greeks()` from `src/risk/portfolio_state.py` — single source of truth shared with shock.py. Replace internals with `aggregate_risk()` once live IBKR positions are loaded (Priority 7 done for strategy; risk greeks pending). |
| `GET /api/risk/var` | ✅ IBKR→yf→cache | `compute_historical_var()` → `data_fetcher.fetch_history("^STOXX50E", 252-day)` → IBKR daily closes or yfinance or disk cache. Historical-sim VaR at 95%/99%. |
| `GET /api/risk/pnl-attribution` | ✅ IBKR→yf→cache | `compute_pnl_attribution()` → `data_fetcher.fetch_history("^STOXX50E", 7-day)` → last 2 closes for realized dS%, then Greek PnL formula. |
| `GET /api/risk/correlation` | ✅ IBKR→yf→cache | `compute_correlation()` → `data_fetcher.fetch_history()` per ticker → 252D Pearson matrix across SX5E, ASML, MC.PA, SAP, TTE. |
| `GET /api/risk/uam` | ✅ Dynamic | Calls `compute_uam()` from `src/risk/uam.py`. Builds synthetic aggregate `PositionRisk` from `portfolio_state`. Returns real UAM ratio + worst-case PnL + 3×3 display grid. |
| `GET /api/risk/qc-log` | ✅ Dynamic | Calls `run_daily_qc()` from `src/qc/validation.py` for SX5E, V2TX, DAX. Synthetic market snapshot tuned to produce realistic FAIL/WARN/OK mix. Returns 20 entries with real check names (`IV_SOLVER_CONV`, `GREEK_SANITY`, `QUOTE_HEALTH`, …), real reason codes, live UTC timestamps. |

**Frontend contract verified:** All 6 response shapes match what RiskAnalysis.tsx reads.

---

## Page 3 — StrategyExecution.tsx

**Frontend file:** `frontend/src/components/terminal/views/StrategyExecution.tsx`  
**Router:** `backend/src/api/routers/strategy.py`

| Endpoint | Status | What it does |
|----------|--------|--------------|
| `GET /api/strategy/positions` | ✅ IBKR→synthetic | Checks `get_adapter().is_healthy()`. Live path: `ib.portfolio()` → groups option legs by (underlying, expiry, strike) into straddles → maps to strategy format. Falls back to synthetic `_POSITIONS` when IBKR offline. |
| `GET /api/strategy/orderbook` | ✅ Simulated | `fetch_order_book()` in `src/connectivity/market_depth.py` adds ±0.2% jitter per call (1.8s TTL). IBKR `reqMktDepth` would replace this when wired. |
| `GET /api/strategy/hedge-suggestions` | ⚙️ Computed | `compute_hedge_suggestions()` with `portfolio_delta` from `portfolio_state.get_portfolio_greeks()`. Alerts when |delta| > €4M threshold. Same Greek source as all other endpoints. |
| `POST /api/strategy/roll` | ✅ ACK | Logs + returns `{"status":"ok"}`. `src/connectivity/ibkr_adapter.py` execution pending. |
| `POST /api/strategy/hedge` | ✅ ACK | Logs + returns `{"status":"ok"}`. |
| `POST /api/strategy/liquidate` | ✅ ACK | Logs + returns `{"status":"ok"}`. |
| `POST /api/strategy/execute-hedge` | ✅ ACK | Logs + returns `{"status":"ok","action":...}`. |

**Frontend contract verified:** All 7 response shapes match what StrategyExecution.tsx reads.

---

## Page 4 — Backtesting.tsx

**Frontend file:** `frontend/src/components/terminal/views/Backtesting.tsx`  
**Router:** `backend/src/api/routers/backtest.py`

| Endpoint | Status | What it does |
|----------|--------|--------------|
| `GET /api/backtest/strategies` | ✅ Static | Returns `["VOL_CARRY_01", "SX5E_STRADDLE", "DISPERSION_Q3"]`. |
| `POST /api/backtest/run` | ✅ IBKR→yf→cache | `run_backtest()` → `data_fetcher.fetch_history("^STOXX50E", start, end)`. IBKR for windows ≤3Y; yfinance for longer (default 2005→today is 21Y so yfinance is primary here — correct). GBM synthetic fallback if both fail. 5-min cache. |
| `POST /api/backtest/shock-preset` | ✅ IBKR→yf→cache | Same as `/run` with crisis-window date clip. Shock windows (<1Y) use IBKR as primary. |
| `GET /api/backtest/monte-carlo` | ✅ Computed | `run_monte_carlo()` — pure GBM, no market data fetch. Returns 500 terminal returns + 95% VaR. |

**Note on backtest window:** The default date range (2005→today ≈ 21 years) intentionally exceeds the IBKR paper-account 3-year limit, so yfinance / disk cache is the correct source for the full history chart.

**Frontend contract verified:** Backtesting.tsx uses `POST /api/backtest/run` with `shock_preset` field for both normal and shock runs.

---

## Page 5 — ShockSimulator.tsx

**Frontend file:** `frontend/src/components/terminal/views/ShockSimulator.tsx`  
**Router:** `backend/src/api/routers/shock.py`

| Endpoint | Status | What it does |
|----------|--------|--------------|
| `POST /api/shock/reprice` | ⚙️ Computed | Builds 3×3 scenario matrix using `portfolio_state.get_portfolio_greeks()` — same Greek source as all other endpoints. PnL = Δ·dS + ½Γ·dS² + V·dσ + ρ·dr. Returns `pnl_eur` and `nav_bps` per cell. `src/risk/scenarios.py` full-repricing path not wired (Greek approx is sufficient for demonstration). |

**Frontend contract verified:** ShockSimulator.tsx sends `spot_stress`, `vol_stress`, `rate_stress_bps`, `methodology`, `active_methods` and reads `scenario_matrix[ri][ci].pnl_eur` and `.nav_bps`.

---

## Modules Still Not Wired

These modules exist and are tested but not yet called by any API endpoint.

| Module | Path | Would replace / add |
|--------|------|---------------------|
| Greeks aggregation | `src/risk/aggregation.py::aggregate_risk()` | `portfolio_state.get_portfolio_greeks()` → real IBKR positions |
| Forward curve estimation | `src/forwards/engine.py::estimate_forward()` | `engine_status()` reports correct source/date but does not call `estimate_forward()` (requires `MarketStateSnapshot` with live IBKR data) |
| Scenario full-reprice | `src/risk/scenarios.py::run_scenario()` | `shock.py` uses Greek approximation |
| IBKR order execution | `src/connectivity/ibkr_adapter.py::placeOrder` | POST action endpoints return ACK only |
| Orchestration | `src/orchestration/jobs.py`, `scheduler.py` | No scheduled data refresh wired to API |

---

## Completed Implementation Plan

All 7 priorities have been implemented.

### ✅ Priority 1 — Shared Greeks Source (DONE)

`src/risk/portfolio_state.py` is the single source of truth for portfolio Greeks, NAV, and spot.
Both `risk.py` and `shock.py` import from it — values can never desync.

**Files changed:** `backend/src/risk/portfolio_state.py` (new), `backend/src/api/routers/risk.py`, `backend/src/api/routers/shock.py`, `backend/src/risk/pnl_attribution.py`

---

### ✅ Priority 2 — Wire `src/risk/uam.py` to `/api/risk/uam` (DONE)

`/api/risk/uam` now calls `compute_uam()` from `src/risk/uam.py`. Builds a synthetic aggregate
`PositionRisk` from `portfolio_state` values. Returns real `uam_ratio`, `worst_case_pnl`, and 3×3 display grid.

**File changed:** `backend/src/api/routers/risk.py`

---

### ✅ Priority 3 — Real QC Events to `/api/risk/qc-log` (DONE)

`/api/risk/qc-log` now calls `run_daily_qc()` from `src/qc/validation.py` for three underlyings
(SX5E, V2TX, DAX). Synthetic market snapshot tuned to produce a realistic FAIL/WARN/OK mix.
Returns 20 entries with real check names, reason codes, and live UTC timestamps that advance per call.

**File changed:** `backend/src/api/routers/risk.py`

---

### ✅ Priority 4 — Wire Vol Surface Calibration (DONE)

`_build_vol_surface()` in `market.py` now builds `IVPoint` objects from the inline SVI formula,
calls `fit_surface()` from `src/surfaces/calibration.py` for real SVI calibration per maturity
slice, and uses fitted params to populate the surface grid. Reports actual RMSE (~8e-5).
Inline formula stays as fallback.

**File changed:** `backend/src/api/routers/market.py`

---

### ✅ Priority 5 — Wire Forward Curve to Engine Status (DONE)

`/api/market/engine-status` now checks `get_adapter().is_healthy()`, measures real Parquet
read latency, reports the correct curve ID (`"IBKR parity forward"` vs `"SOFR-OIS + Div"`),
and includes today's trade date. `engine_load_pct` adapts to connection state.

Note: `estimate_forward()` from `src/forwards/engine.py` requires a live `MarketStateSnapshot`
(IBKR options chain); that wiring is deferred until live options data is available.

**File changed:** `backend/src/api/routers/market.py`

---

### ✅ Priority 6 — Live IV in Options Chain (DONE)

`fetch_options_chain()` in `src/connectivity/options_chain.py` now calls `get_adapter()`.
When IBKR is healthy: builds `CanonicalContract(sec_type="OPT")` per strike, requests a live
bid/ask snapshot, inverts the mid price via `solve_iv()` (Brent, `src/iv/solver.py`). `call_qc`
= `"pass"` when solver converges, `"synthetic"` on fallback. Fallback: skew-adjusted ATM vol.

**File changed:** `backend/src/connectivity/options_chain.py`

---

### ✅ Priority 7 — Live Positions from IBKR (DONE)

`/api/strategy/positions` now checks `get_adapter().is_healthy()`. Live path calls
`ib.portfolio()`, groups option legs by (underlying, expiry, strike) to detect straddles,
and maps each group to the strategy position format. Falls back to synthetic `_POSITIONS`
when IBKR is offline.

`/api/strategy/hedge-suggestions` now passes `portfolio_delta` from `portfolio_state.get_portfolio_greeks()`
— same Greek source as all other endpoints.

**Files changed:** `backend/src/api/routers/strategy.py`

---

## How to Run

**1. Offline presentation (school — port 7497 blocked):**
```bash
cd backend
source .venv/bin/activate
python scripts/seed_cache.py        # pre-download 56 tickers → data/cache/ohlcv/
uvicorn src.api.main:app --reload --port 8000
```
All endpoints serve from Parquet disk cache. No internet required after seeding.

**2. Live with IBKR (at home):**

Start TWS or IB Gateway (paper account, port 7497) and log in, then:
```bash
cd backend
source .venv/bin/activate
export IBKR_HOST=127.0.0.1
export IBKR_PORT=7497
export IBKR_CLIENT_ID=1
uvicorn src.api.main:app --reload --port 8000
```
On startup you will see either:
```
startup: IBKR connected — primary data source is live
```
or (if TWS is not running):
```
startup: IBKR connection failed (...) — yfinance fallback active
```
All endpoints work in both cases.

**3. Frontend:**
```bash
cd frontend
npm install        # first time only
npm run dev        # http://localhost:5173
```

---

## API Summary

| Method | Path | Page | Status | Data source |
|--------|------|------|--------|-------------|
| GET | `/api/market/index-matrix` | DataOverview | ✅ | IBKR snapshot → yfinance → disk cache |
| GET | `/api/market/options-chain` | DataOverview | ✅ | IBKR bid/ask → BS IV solver → skew-adjusted ATM vol fallback |
| GET | `/api/market/vol-surface` | DataOverview | ✅ | IBKR spot/vol → SVI calibration (calibration.py) |
| GET | `/api/market/engine-status` | DataOverview | ✅ | Adapter health + disk cache latency + trade date |
| GET | `/api/market/greeks-summary` | DataOverview | ⚠️ | Hardcoded |
| GET | `/api/risk/greeks` | RiskAnalysis | ⚙️ | `portfolio_state.get_portfolio_greeks()` |
| GET | `/api/risk/var` | RiskAnalysis | ✅ | IBKR→yf→cache 252D OHLCV → historical-sim VaR |
| GET | `/api/risk/pnl-attribution` | RiskAnalysis | ✅ | IBKR→yf→cache last 2 closes → Greek PnL decomposition |
| GET | `/api/risk/correlation` | RiskAnalysis | ✅ | IBKR→yf→cache 252D OHLCV → Pearson matrix |
| GET | `/api/risk/uam` | RiskAnalysis | ✅ | `compute_uam()` on synthetic `PositionRisk` from `portfolio_state` |
| GET | `/api/risk/qc-log` | RiskAnalysis | ✅ | `run_daily_qc()` with real check names + live timestamps |
| GET | `/api/strategy/positions` | StrategyExecution | ✅ | IBKR `ib.portfolio()` → straddle grouping → synthetic fallback |
| GET | `/api/strategy/orderbook` | StrategyExecution | ✅ | Simulated jitter (IBKR `reqMktDepth` pending) |
| GET | `/api/strategy/hedge-suggestions` | StrategyExecution | ⚙️ | Delta threshold on `portfolio_state` Greeks |
| POST | `/api/strategy/roll` | StrategyExecution | ✅ ACK | Logs + `{"status":"ok"}` |
| POST | `/api/strategy/hedge` | StrategyExecution | ✅ ACK | Logs + `{"status":"ok"}` |
| POST | `/api/strategy/liquidate` | StrategyExecution | ✅ ACK | Logs + `{"status":"ok"}` |
| POST | `/api/strategy/execute-hedge` | StrategyExecution | ✅ ACK | Logs + `{"status":"ok","action":...}` |
| GET | `/api/backtest/strategies` | Backtesting | ✅ | Static list |
| POST | `/api/backtest/run` | Backtesting | ✅ | IBKR→yf→cache OHLCV · GBM synthetic fallback |
| POST | `/api/backtest/shock-preset` | Backtesting | ✅ | Same as /run with crisis-window clip |
| GET | `/api/backtest/monte-carlo` | Backtesting | ✅ | Pure GBM — no market data fetch |
| POST | `/api/shock/reprice` | ShockSimulator | ⚙️ | Greek approx on `portfolio_state` (same Greeks as all endpoints) |
