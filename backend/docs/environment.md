# Environment Setup Runbook

## Prerequisites
- Python 3.11+
- IB Gateway (recommended) or TWS installed and running
- IBKR paper-trading account with EUREX market-data entitlement (for `^STOXX50E`)
- PostgreSQL 15+ (production) — SQLite used as dev baseline
- InfluxDB 2.x (production) — JSONL flat files used as dev baseline

## Step 1: Create virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

Key packages installed:
- `ib_async>=1.0.3` — IBKR async adapter
- `yfinance>=0.2.40` — Yahoo Finance EOD bars
- `pyarrow>=14.0.0` — Parquet I/O
- `pydantic>=2.8.0` — config validation
- `scipy`, `numpy`, `pandas` — analytics

## Step 2: Configure secrets

Create a `.env` file in the project root (never commit to git — it is in `.gitignore`):

```bash
# IBKR connectivity — override broker.yaml defaults
VOL_INFRA_IBKR__HOST=127.0.0.1
VOL_INFRA_IBKR__PORT=4002          # IB Gateway paper trading
VOL_INFRA_IBKR__CLIENT_ID=1
VOL_INFRA_IBKR__ACCOUNT=           # leave blank to use default paper account

# Runtime overrides (optional)
VOL_INFRA_RUNTIME__ENVIRONMENT=development
VOL_INFRA_RUNTIME__LOG_LEVEL=INFO
```

> **Convention:** `VOL_INFRA_IBKR__*` maps to `configs/broker.yaml` → `ibkr.*` section.
> Double underscore (`__`) separates YAML nesting levels.
> Secrets are **never** in YAML files — only defaults that are safe to commit.

## Step 3: Start IB Gateway

Recommended for this project (lighter than full TWS):

1. Download **IB Gateway** from [Interactive Brokers](https://www.interactivebrokers.com/en/trading/ibgateway.php)
2. Log in with your **paper-trading** credentials
3. Go to **Configure → Settings → API → Enable ActiveX and Socket Clients**
4. Set **Socket port** to `4002` (paper) — matches `broker.yaml` default
5. Set **Master Client ID** to 0 (allows any client_id to connect)
6. Tick **Read-Only API** during Step 1–2 (disable when execution is needed)

Port reference:
| Mode | Gateway | TWS |
|------|---------|-----|
| Paper trading | 4002 | 7497 |
| Live trading  | 4001 | 7496 |

### EUREX entitlement (required for Euro Stoxx 50)
In IBKR Account Management → Market Data Subscriptions, activate:
- **Euronext / EUREX** (covers `ESTX50` index and options)
- Without this, `bootstrap_smoke_test.py` will return delayed or no data for `^STOXX50E`

## Step 4: Run bootstrap smoke test

### Without IBKR (CI / offline validation)
```bash
python scripts/bootstrap_smoke_test.py --mock
```
Expected: exits 0, manifest written to `artifacts/bootstrap_*.json`

### With live IB Gateway connection
```bash
python scripts/bootstrap_smoke_test.py
```
Expected: exits 0, session state transitions printed, SPY quote snapshot logged

### Flags
| Flag | Purpose |
|------|---------|
| `--mock` | Use in-memory MockAdapter — no IBKR needed |
| `--skip-clock-check` | Skip NTP drift verification |
| `--config-dir PATH` | Override config directory (default: `configs/`) |

Exit codes: `0` = pass, `2` = config error, `3` = connectivity error, `4` = data error, `5` = health error

## Step 5: Run the test suite

```bash
# All 213 unit tests (no network, no IBKR needed)
.venv/bin/python -m pytest tests/ -v

# Storage layer only (Step 4)
.venv/bin/python -m pytest tests/unit/test_storage.py -v

# Yahoo Finance loader only (Step 3b)
.venv/bin/python -m pytest tests/unit/test_yfinance_loader.py -v
```

## Step 6: Verify config files

```bash
# Check config loads without error
python -c "from src.utils.config import load_config; c = load_config(); print(c.ibkr.port)"
# Expected: 4002
```

Key config files:
| File | Purpose |
|------|---------|
| `configs/environment.yaml` | Log level, paths, clock-drift tolerance |
| `configs/broker.yaml` | IBKR host/port, client IDs, reconnect policy, pacing |
| `configs/universe.yaml` | Euro Stoxx 50, delta-based strikes, maturity ladder |
| `configs/qc.yaml` | Quote filter thresholds, IV solver bounds |
| `configs/scenarios.yaml` | Named stress scenarios (UAM shocks included) |
| `configs/pricing.yaml` | Black-Scholes + CRR tree settings |

## Directory Structure

```
data/
  raw/              ← Immutable raw events (JSONL, one file per session per day)
  analytics/        ← Derived analytics (versioned Parquet partitions)
  manifests/        ← Job run manifests (JSON)
configs/            ← All YAML config files (version-controlled, no secrets)
logs/               ← Application logs (not version-controlled)
artifacts/          ← Bootstrap outputs, smoke test manifests
src/                ← All source modules
tests/              ← Unit tests (213 tests, all mocked — no network required)
scripts/            ← Operational scripts (bootstrap, backfill, etc.)
docs/               ← This runbook and architecture docs
```

## PostgreSQL Setup (production)

> Skip for development — SQLite baseline is sufficient for Steps 1–9.

```bash
# macOS
brew install postgresql@15
brew services start postgresql@15
createdb vol_infra

# Create tables (run after implementing postgres_writer.py)
python scripts/init_postgres.py --config configs/environment.yaml
```

Add to `.env`:
```bash
VOL_INFRA_STORAGE__POSTGRES_DSN=postgresql://localhost/vol_infra
```

## InfluxDB Setup (production)

> Skip for development — JSONL flat files are the dev baseline.

```bash
# macOS
brew install influxdb
brew services start influxdb

# Create org + bucket via UI at http://localhost:8086
# or via CLI:
influx setup --org vol_infra --bucket ticks --retention 90d --force
```

Add to `.env`:
```bash
VOL_INFRA_STORAGE__INFLUX_URL=http://localhost:8086
VOL_INFRA_STORAGE__INFLUX_TOKEN=your_token_here
VOL_INFRA_STORAGE__INFLUX_ORG=vol_infra
VOL_INFRA_STORAGE__INFLUX_BUCKET=ticks
```

## Known Issues / Limitations

- IB Gateway requires manual login unless configured for auto-login (use `ibgateway` headless mode for servers)
- EUREX market data available only during European exchange hours (09:00–17:30 CET)
- Option chain discovery can hit IBKR pacing limits — `broker.yaml` sets `max_messages_per_second: 40`
- Yahoo Finance provides **EOD bars only** — no intraday data, no options history
- `^STOXX50E` ticker on Yahoo Finance returns index level; individual options are IBKR-only
- IBKR paper account may have different entitlements than live — test entitlements before going live
