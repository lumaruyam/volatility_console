# Environment runbook

This document records every manual step needed to provision a new machine and run the bootstrap smoke test. Per the roadmap Part III, Step 1: any manual step performed during setup must be documented immediately, otherwise the environment will become impossible to reproduce.

## 1. Operating system prerequisites

Supported and tested:
- macOS 13+ (Apple Silicon and Intel)
- Ubuntu 22.04 LTS and later
- Windows 11 via WSL2 (Ubuntu)

Not supported as a first-class target: native Windows (Python paths and IB Gateway integration are workable but add operational friction).

## 2. Python toolchain

```bash
# Install Python 3.11+ via pyenv (recommended) or system package manager
pyenv install 3.11.10
pyenv local 3.11.10

# Install uv (fast Python package manager with reproducible lock files)
curl -LsSf https://astral.sh/uv/install.sh | sh
# or
pip install uv
```

Verify:
```bash
python --version  # should print 3.11.x or newer
uv --version
```

## 3. Repository setup

```bash
cd /path/to/workspace
# Clone or copy the vol-infra repository here
cd vol-infra

# Create a virtual environment in .venv
uv venv

# Activate
source .venv/bin/activate           # Unix
# or
.venv\Scripts\activate              # Windows

# Install in editable mode with dev extras
uv pip install -e ".[dev]"

# Generate lock file for reproducibility
uv pip compile pyproject.toml --extra dev -o requirements-lock.txt
```

## 4. Configuration and secrets

```bash
cp .env.example .env
```

Edit `.env`:
- `VOL_INFRA_IBKR__HOST`: usually `127.0.0.1` for local Gateway/TWS
- `VOL_INFRA_IBKR__PORT`: `4002` (Gateway paper), `4001` (Gateway live), `7497` (TWS paper), `7496` (TWS live)
- `VOL_INFRA_IBKR__CLIENT_ID`: `1` for bootstrap (see client ID reservations in `.env.example`)
- `VOL_INFRA_IBKR__ACCOUNT`: leave blank to use default, or set explicit `DU...` / `U...` code
- `VOL_INFRA_RUNTIME__ENVIRONMENT`: `development` for local work

Never commit `.env`. It is gitignored by default.

For production, replace .env with a real secret manager (AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager). The pydantic-settings layer reads from environment variables, so any injection mechanism works.

## 5. IB Gateway or TWS setup

### Download
- IB Gateway: https://www.interactivebrokers.com/en/trading/ib-api.php
- Prefer Gateway over TWS for unattended operation per roadmap Step 1.

### Configure
1. Launch IB Gateway, log in with paper trading credentials for first-time setup.
2. Settings > API > Settings:
   - Enable: "Enable ActiveX and Socket Clients"
   - Socket port: must match `VOL_INFRA_IBKR__PORT`
   - Master API client ID: leave blank
   - Trusted IPs: `127.0.0.1` for local development
   - Read-Only API: enable for Step 1 (no orders will be placed)
   - Auto restart: enable for unattended use
3. Settings > Lock and Exit:
   - Set auto-logoff to extend session as needed (paper logs off daily by default)

### Validate manually
- Confirm Gateway shows green "API" indicator after restart
- Confirm port is reachable: `nc -zv 127.0.0.1 4002` (or whichever port)

## 6. Market-data entitlements

Before running collectors against options, the IBKR account must have market-data subscriptions for the relevant exchanges (e.g., OPRA for US options, Cboe One, etc.). Without entitlements, requests will return "delayed" or "no data" status. The bootstrap smoke test detects this and reports it as a structured warning.

For Step 1 smoke test against SPY, you need at minimum:
- US Securities Snapshot and Futures Value Bundle (NYSE, NASDAQ basic)
- US Options Snapshot Bundle (OPRA top of book), or use delayed data for development

Paper accounts typically receive delayed data by default. The bootstrap script flags this and continues.

## 7. Clock synchronization

The system relies on UTC timestamps. Ensure the host clock is NTP-synchronized.

```bash
# macOS
sudo sntp -sS time.apple.com
# Linux
timedatectl status
sudo timedatectl set-ntp true
```

The bootstrap script verifies that local clock drift is below 1000 ms and reports it.

## 8. Run the bootstrap smoke test

```bash
python -m scripts.bootstrap_smoke_test
```

Expected on success:
- Console output showing connection lifecycle, contract resolution, one quote
- `./logs/bootstrap_<timestamp>.jsonl`: structured log file
- `./artifacts/bootstrap_<timestamp>.json`: manifest with code version, config hash, results
- Exit code 0

Expected on failure:
- Structured error event in the log
- Non-zero exit code
- No partial writes left behind

For environment validation without an IBKR session:
```bash
python -m scripts.bootstrap_smoke_test --mock
```

## 9. Common failure modes for Step 1

| Symptom | Likely cause | Fix |
|---|---|---|
| `ConnectionRefusedError` on port 4002 | Gateway not running or wrong port | Start Gateway; verify port in Gateway settings matches `.env` |
| `API connection failed: Not connected` | API not enabled in Gateway settings | Enable "Enable ActiveX and Socket Clients" |
| `clientId already in use` | Two services sharing the same client ID | Use distinct IDs per service (see `.env.example` reservations) |
| `No security definition has been found` | Contract resolution failed | Confirm symbol, exchange (SMART works for SPY), and currency (USD) |
| Quote fields all NaN | No market-data entitlement or market closed | Use delayed data (`reqMarketDataType=3`) or run during US session |

## 10. Tear down

```bash
deactivate              # leave virtualenv
# Stop IB Gateway via its UI (do not kill the JVM, use Logout to close cleanly)
```

## 11. Change log for this runbook

| Date | Change | Author |
|---|---|---|
| 2026-06-05 | Initial Step 1 runbook | vol-infra team |
