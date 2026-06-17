# Volatility Infrastructure Platform — Runbook

> Albert School · AI for Algo Trading  
> Last updated: 2026-06-15

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Backend Setup & Run](#backend-setup--run)
4. [Frontend Setup & Run](#frontend-setup--run)
5. [Running Both Together](#running-both-together)
6. [API Reference](#api-reference)
7. [Running Tests](#running-tests)
8. [Environment Variables](#environment-variables)
9. [Project Structure](#project-structure)
10. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
Browser (localhost:5173)
    │
    │  /api/* requests (Vite dev-server proxy)
    ▼
FastAPI (localhost:8000)           ← uvicorn src.api.main:app
    │
    ├── /api/market/*   ← index matrix, vol surface, options chain
    ├── /api/risk/*     ← Greeks, VaR, PnL attribution, correlation, UAM
    ├── /api/strategy/* ← positions, order book, hedge engine, execution
    ├── /api/backtest/* ← backtest engine, Monte Carlo, shock presets
    └── /api/shock/*    ← scenario repricer (3×3 matrix)
         │
         └── Data sources:
             ├── yfinance  (spots, realized vol, historical returns)
             ├── synthetic (GBM fallback when yfinance unavailable)
             └── IBKR      (live adapter — paper account, optional)
```

**Frontend:** TanStack Start SSR app in `/frontend`. The UI is a 5-tab terminal shell
(`TerminalShell.tsx`) whose views map one-to-one to the 5 FastAPI routers above.

> `backend/src/dashboard/app.py` is a legacy Streamlit tool — it is **not** the frontend.
> Do not use it; use the `/frontend` React app instead.

All frontend API calls go through `/api/*`. Vite proxies them to `http://localhost:8000` in development — no CORS configuration is needed in the browser.

---

## Prerequisites

| Tool | Minimum version | Check |
|------|----------------|-------|
| Python | 3.11 | `python3 --version` |
| Node.js | 18 | `node --version` |
| npm | 9 | `npm --version` |
| pip | any | `pip --version` |

> **Note:** Backend requires Python 3.11+ per `pyproject.toml`.

---

## Backend Setup & Run

All commands run from the `backend/` directory.

### 1. Create and activate a virtual environment

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Core packages installed: `fastapi`, `uvicorn[standard]`, `numpy`, `pandas`, `scipy`, `yfinance`, `pydantic`, `structlog`, `pyarrow`.

### 3. Start the FastAPI server

```bash
uvicorn src.api.main:app --reload --port 8000
```

| Flag | Purpose |
|------|---------|
| `--reload` | Auto-restart on file save (dev mode) |
| `--port 8000` | Port the frontend proxy points to |

**Expected output:**
```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process
INFO:     Application startup complete.
```

### 4. Verify the backend is alive

```bash
curl http://localhost:8000/api/market/index-matrix
curl http://localhost:8000/api/risk/greeks
```

The interactive API docs are available at `http://localhost:8000/docs` (Swagger UI).

---

## Frontend Setup & Run

All commands run from the `frontend/` directory.

### 1. Install dependencies

```bash
cd frontend
npm install
```

This installs React 19, TanStack Router, TanStack Query v5, recharts, Tailwind CSS v4, Radix UI, and Lucide React icons.

### 2. Start the dev server

```bash
npm run dev
```

**Expected output:**
```
  ➜  Local:   http://localhost:5173/
  ➜  Network: use --host to expose
```

Open `http://localhost:5173` in the browser.

> The Vite config (`vite.config.ts`) proxies all `/api/*` requests to `http://localhost:8000`, so the backend must be running first.

### Other frontend commands

```bash
npm run build        # production build → .output/
npm run build:dev    # development build
npm run preview      # preview the production build locally
npm run lint         # ESLint
npm run format       # Prettier
```

---

## Running Both Together

Open two terminal tabs:

**Tab 1 — Backend:**
```bash
cd backend
source .venv/bin/activate
uvicorn src.api.main:app --reload --port 8000
```

**Tab 2 — Frontend:**
```bash
cd frontend
npm run dev
```

Then open `http://localhost:5173`.

### Quick sanity check

```bash
# In a third terminal, while both servers are running:
curl -s http://localhost:8000/api/market/engine-status | python3 -m json.tool
curl -s http://localhost:8000/api/risk/var | python3 -m json.tool
```

---

## API Reference

### Market — `/api/market/*`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/market/index-matrix` | Spot + ATM vol for 8 tickers (SX5E, SPX, NDX, DAX, NKY, ASML, MC.PA, SAP) |
| GET | `/api/market/options-chain` | Options chain with Greeks + QC per row. Params: `ticker`, `expiry` |
| GET | `/api/market/vol-surface` | 9-strike × 5-maturity implied vol grid + 30D smile slice. Param: `ticker` |
| GET | `/api/market/engine-status` | Spot ingestion latency, forward curve, calibration RMSE, engine load % |
| GET | `/api/market/greeks-summary` | Portfolio total Δ, Γ, V, Θ |

### Risk — `/api/risk/*`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/risk/greeks` | Portfolio Δ, Γ, $Γ, Vega, Θ, ρ |
| GET | `/api/risk/var` | Historical-simulation VaR: 1D 95%, 1D 99%, 7D 99% (EUR) |
| GET | `/api/risk/pnl-attribution` | Greek-decomposed PnL: delta, gamma, vega, theta, rho |
| GET | `/api/risk/correlation` | Pearson correlation matrix. Param: `tickers` (comma-separated) |
| GET | `/api/risk/uam` | UAM 3×3 shock grid: ±5% spot × ±30% vol |
| GET | `/api/risk/qc-log` | Latest pipeline QC events |

### Strategy — `/api/strategy/*`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/strategy/positions` | Active strategy positions (straddle + dispersion basket) |
| GET | `/api/strategy/orderbook` | L2 order book snapshot, refreshes every ~2s. Param: `ticker` |
| GET | `/api/strategy/hedge-suggestions` | Delta imbalance + vega roll alerts |
| POST | `/api/strategy/roll` | Roll a strategy to next maturity. Body: `{"strategy_id": "strat_001"}` |
| POST | `/api/strategy/hedge` | Place a delta hedge. Body: `{"strategy_id": "strat_001", "target_delta": 0.0}` |
| POST | `/api/strategy/liquidate` | Close all legs. Body: `{"strategy_id": "strat_001"}` |
| POST | `/api/strategy/execute-hedge` | Fire a hedge suggestion. Body: `{"action": "Sell 120 SX5E Futs", "strategy_id": "strat_001"}` |

### Backtest — `/api/backtest/*`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/backtest/strategies` | List of available strategy IDs |
| POST | `/api/backtest/run` | Run a full backtest. Body: `{"strategy_id", "start_date", "end_date", "shock_preset"}` |
| POST | `/api/backtest/shock-preset` | Run backtest over a named shock window. Body: `{"preset": "2008 Crash"}` |
| GET | `/api/backtest/monte-carlo` | GBM Monte Carlo, 500 paths, 95% VaR. Params: `n_paths`, `strategy_id` |

Available shock presets: `"2008 Crash"`, `"2020 Liquidity Shock"`, `"BREXIT"`, `"COVID Vol Spike"`.

### Shock — `/api/shock/*`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/shock/reprice` | 3×3 scenario matrix (±5% spot × ±30% vol + manual offsets). Returns `pnl_eur` and `nav_bps` per cell |

Request body:
```json
{
  "spot_stress": -0.05,
  "vol_stress": 0.10,
  "rate_stress_bps": 50,
  "methodology": "parallel_grid_shift",
  "active_methods": 1
}
```

---

## Running Tests

All commands from the `backend/` directory with the virtualenv active.

### API integration tests (39 tests, ~1s)

```bash
pytest tests/test_api/ -v
```

Covers all 23 endpoints across 5 routers. Runs fully in-process via FastAPI `TestClient` — no network required.

### Full unit test suite

```bash
pytest tests/unit/ -q --ignore=tests/unit/test_ibkr_loader.py --ignore=tests/unit/test_yfinance_loader.py
```

> `test_ibkr_loader.py` and `test_yfinance_loader.py` require live network access and are excluded in offline environments.

### All tests

```bash
pytest tests/ -q --ignore=tests/unit/test_ibkr_loader.py --ignore=tests/unit/test_yfinance_loader.py
```

Expected: **~1520 passed** in ~4s.

### With coverage

```bash
pytest tests/test_api/ --cov=src/api --cov-report=term-missing
```

---

## Environment Variables

The backend uses no required environment variables in demo mode — all data falls back to synthetic values. For live trading these should be set:

| Variable | Purpose | Example |
|----------|---------|---------|
| `IBKR_HOST` | TWS/IB Gateway host | `127.0.0.1` |
| `IBKR_PORT` | TWS/IB Gateway port (paper: 7497, live: 7496) | `7497` |
| `IBKR_CLIENT_ID` | Unique client ID | `1` |
| `LOG_LEVEL` | Python logging level | `INFO` |

Create a `.env` file in `backend/` (never commit it):
```bash
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=1
LOG_LEVEL=INFO
```

---

## Project Structure

```
ai_for_algo_trading/
├── RUNBOOK.md                          ← this file
├── FRONTEND_BACKEND_INTEGRATION.md    ← full integration spec
│
├── backend/
│   ├── src/
│   │   ├── api/
│   │   │   ├── main.py                ← FastAPI app + CORS + router mounting
│   │   │   └── routers/
│   │   │       ├── market.py          ← Page 1: DataOverview
│   │   │       ├── risk.py            ← Page 2: RiskAnalysis
│   │   │       ├── strategy.py        ← Page 3: StrategyExecution
│   │   │       ├── backtest.py        ← Page 4: Backtesting
│   │   │       └── shock.py           ← Page 5: ShockSimulator
│   │   ├── backtest/
│   │   │   ├── engine.py              ← rolling straddle backtest (yfinance + GBM fallback)
│   │   │   └── monte_carlo.py         ← GBM Monte Carlo, 500 paths, 95% VaR
│   │   ├── connectivity/
│   │   │   ├── market_depth.py        ← L2 order book simulator
│   │   │   └── options_chain.py       ← options chain builder (skew model)
│   │   ├── risk/
│   │   │   ├── var.py                 ← historical-simulation VaR (252-day window)
│   │   │   ├── pnl_attribution.py     ← Greek-decomposed PnL
│   │   │   ├── correlation.py         ← Pearson correlation matrix (yfinance)
│   │   │   └── hedge_suggest.py       ← delta imbalance + vega roll alerts
│   │   ├── surfaces/
│   │   │   └── atm_vol.py             ← ATM vol per ticker (realized vol, TTL cache)
│   │   ├── strategy/
│   │   │   └── straddle.py            ← straddle builder, roll logic, reconciliation
│   │   ├── execution/
│   │   │   └── order_manager.py       ← paper-trading order submit/cancel
│   │   └── historical/
│   │       └── yfinance_loader.py     ← daily OHLCV loader (^STOXX50E, constituents)
│   ├── tests/
│   │   ├── test_api/
│   │   │   └── test_routers.py        ← 39 API integration tests (TestClient)
│   │   └── unit/                      ← 1481+ pre-existing unit tests
│   ├── requirements.txt
│   └── pyproject.toml
│
└── frontend/
    ├── src/
    │   ├── components/terminal/
    │   │   ├── views/
    │   │   │   ├── DataOverview.tsx    ← Page 1 (live index matrix, chain, vol surface)
    │   │   │   ├── RiskAnalysis.tsx    ← Page 2 (Greeks, VaR, PnL, correlation, UAM)
    │   │   │   ├── StrategyExecution.tsx ← Page 3 (positions, L2 book, hedge engine)
    │   │   │   ├── Backtesting.tsx     ← Page 4 (backtest chart, Monte Carlo)
    │   │   │   └── ShockSimulator.tsx  ← Page 5 (3×3 repricer, manual sliders)
    │   │   └── ui.tsx                  ← Panel, StatusPill, Chip, Label, Chip
    │   └── styles.css                  ← vc-slider, vc-blink, global Tailwind
    ├── vite.config.ts                  ← proxy /api → localhost:8000
    └── package.json
```

---

## Troubleshooting

### Backend won't start — `ModuleNotFoundError: No module named 'src'`

Run uvicorn from the `backend/` directory, not from the repo root:
```bash
cd backend
uvicorn src.api.main:app --reload --port 8000
```

### `No module named 'yfinance'`

Install it:
```bash
pip install yfinance
```

Without yfinance all endpoints still work — they fall back to synthetic/cached data automatically. A warning is logged but no endpoint returns an error.

### Frontend shows "COMPUTING…" forever / blank charts

1. Confirm the backend is running on port 8000: `curl http://localhost:8000/api/market/engine-status`
2. Confirm the Vite proxy is active (look for `[vite] http proxy error` in the terminal)
3. Open browser DevTools → Network tab → filter by `/api` — check the response

### `npm run dev` fails with `Cannot find module '@lovable.dev/vite-tanstack-config'`

Run `npm install` first:
```bash
cd frontend
npm install
npm run dev
```

### Port 8000 already in use

```bash
lsof -ti:8000 | xargs kill -9    # macOS / Linux
uvicorn src.api.main:app --reload --port 8001   # or use a different port
```

If you change the backend port, update `vite.config.ts`:
```ts
proxy: { "/api": "http://localhost:8001" }
```

### CORS errors in browser console

CORS is configured in `backend/src/api/main.py` to allow `http://localhost:5173`. If you run the frontend on a different port, add it to `allow_origins`:
```python
allow_origins=["http://localhost:5173", "http://localhost:3000"]
```

### Tests fail with `ImportError` on collection

The two network-dependent test files must be excluded:
```bash
pytest tests/ -q --ignore=tests/unit/test_ibkr_loader.py --ignore=tests/unit/test_yfinance_loader.py
```

### Order book doesn't refresh in the UI

The L2 order book panel polls `/api/strategy/orderbook` every 2 seconds via TanStack Query's `refetchInterval`. If it's stuck, hard-refresh the page (`Cmd+Shift+R`) to clear the query cache.
