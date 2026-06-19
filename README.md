# Volatility Infrastructure Platform

Albert School · AI for Algo Trading

Full-stack options analytics terminal: React/TanStack frontend + FastAPI backend. Covers vol surface calibration, portfolio Greeks, historical-simulation VaR, backtest engine, and shock scenarios.

Data sources cascade automatically — no manual switching needed:

1. **IBKR live** (requires TWS / IB Gateway on port 7497)
2. **yfinance** — HTTPS fallback, works anywhere with internet
3. **Disk cache** — Parquet files seeded at home, works fully offline

---

## Quick Start

**Requirements:** Python ≥ 3.11 · Node.js ≥ 18 · npm ≥ 9

### 1 — Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn src.api.main:app --reload --port 8000
```

### 2 — Frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**.

The Vite dev server proxies all `/api/*` requests to the backend — no CORS config needed in the browser.

---

## School / Offline Mode

The school network blocks port 7497 (IBKR). One command skips it and falls back to yfinance (HTTPS/443):

```bash
cd backend
cp .env.school .env
uvicorn src.api.main:app --reload --port 8000
```

For fully offline use (no internet at school), seed the disk cache at home first:

```bash
cd backend
source .venv/bin/activate
python scripts/seed_cache.py    # ~2 min — downloads 56 tickers to data/cache/ohlcv/
```

---

## Project Layout

```
ai_for_algo_trading/
├── backend/
│   ├── src/api/          ← FastAPI routers (market, risk, strategy, backtest, shock, orders)
│   ├── src/surfaces/     ← ATM vol, SVI calibration
│   ├── src/pricing/      ← Black-Scholes, IV solver
│   ├── src/risk/         ← VaR, Greeks, PnL attribution, correlation
│   ├── src/backtest/     ← rolling straddle engine, Monte Carlo
│   ├── src/historical/   ← yfinance loader, IBKR loader, disk cache
│   ├── src/connectivity/ ← IBKR adapter, options chain builder
│   ├── scripts/          ← seed_cache.py (offline data prep)
│   ├── data/cache/       ← Parquet OHLCV cache (gitignored, generated locally)
│   ├── requirements.txt
│   └── .env.school       ← preset for school use (IBKR_ENABLED=0)
│
└── frontend/
    ├── src/components/terminal/views/
    │   ├── DataOverview.tsx      ← index matrix, vol surface, options chain
    │   ├── RiskAnalysis.tsx      ← Greeks, VaR, PnL, correlation, UAM
    │   ├── StrategyExecution.tsx ← positions, L2 book, hedge engine
    │   ├── Backtesting.tsx       ← backtest chart, Monte Carlo
    │   ├── ShockSimulator.tsx    ← 3×3 repricer, manual sliders
    │   └── Orders.tsx            ← order management
    └── vite.config.ts            ← /api proxy → localhost:8000
```

---

## Tests

```bash
cd backend
source .venv/bin/activate

# API integration tests (no network required)
pytest tests/test_api/ -v

# Unit tests (exclude live-network tests)
pytest tests/unit/ -q \
  --ignore=tests/unit/test_ibkr_loader.py \
  --ignore=tests/unit/test_yfinance_loader.py
```

---

For full setup, API reference, environment variables, and troubleshooting see [RUNBOOK.md](RUNBOOK.md).
