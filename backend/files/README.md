# vol-infra

Institutional-grade volatility infrastructure platform. IBKR-based blueprint covering data capture, forward reconstruction, implied-volatility inversion, surface construction, pricing, Greeks, scenarios, and validation.

The platform is strategy-agnostic. It produces reusable analytics primitives. Downstream consumers (research, risk, execution) are intentionally not encoded here.

## Repository layout

```
configs/         # Environment, broker, universe, QC, scenario, pricing config
docs/            # Runbooks, architecture notes, module READMEs
scripts/         # CLI entry points (bootstrap, replay, EOD jobs)
src/
  connectivity/  # IBKR adapter, session state machine, heartbeats
  universe/      # Contract resolution, option-chain discovery, master tables
  collectors/    # Raw event capture
  snapshots/     # Normalized market-state builders
  forwards/      # Parity forward engine, carry diagnostics
  iv/            # Pricing inversion
  surfaces/      # Surface fitters, no-arb diagnostics
  pricing/       # European and American pricers
  risk/          # Greeks aggregation, scenarios
  storage/       # Schemas and read/write adapters
  orchestration/ # Job entry points, scheduler wrappers
  qc/            # Validation checks, anomaly detection
  utils/         # Time, calendars, logging, math helpers
tests/           # Unit, integration, regression, operational tests
```

## Step 1 quickstart

The current build covers Step 1 of the roadmap: access, environments, security, and a connectivity bootstrap smoke test.

### Prerequisites

1. Python 3.11 or newer
2. `uv` package manager (install: `curl -LsSf https://astral.sh/uv/install.sh | sh` or `pip install uv`)
3. Interactive Brokers account with API access enabled
4. IB Gateway or TWS running locally (Gateway preferred for unattended use)

### Setup

```bash
# 1. Create and activate virtualenv, install dependencies
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# 2. Configure secrets
cp .env.example .env
# Edit .env: set IBKR host, port, client ID, optional account

# 3. Start IB Gateway (paper trading recommended for first run)
#    - Login, enable "Read-Only API" if you only want read access for now
#    - In Configure > API > Settings: enable ActiveX and Socket Clients, set socket port to match VOL_INFRA_IBKR__PORT

# 4. Run the bootstrap smoke test
python -m scripts.bootstrap_smoke_test
```

### Expected output

The smoke test prints session state, current UTC time, contract resolution for the configured test underlying (default: SPY), and a single market-data retrieval. It exits 0 on success and emits structured logs to `./logs/` and a JSON manifest to `./artifacts/`.

### Mock mode (no broker required)

For environment validation without IBKR:

```bash
python -m scripts.bootstrap_smoke_test --mock
```

This exercises config loading, logging setup, the session state machine, and adapter normalization paths against a recorded fake event stream.

## Documentation

- `docs/environment.md`: full setup runbook, every manual step documented
- `docs/architecture.md`: data flow and service boundaries (forthcoming)
- Per-module READMEs: under each `src/<module>/README.md` (added as steps are completed)

## Versioning

- Code versioned via `pyproject.toml` (semantic versioning).
- Configuration versioned separately via config-hash logging into every derived table.
- Both versions are recorded in every job manifest.
