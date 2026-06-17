# Volatility Infrastructure — Operational Runbooks

All commands run from the **project root** (`ai_for_algo_trading/`).
The virtual environment must be activated first (Step 0 below).

---

## Table of Contents

0. [First-Time Setup (run once)](#0-first-time-setup-run-once)
1. [Dashboard — Seed & Launch](#1-dashboard--seed--launch)
2. [Start of Day — Live Session](#2-start-of-day--live-session)
3. [Intraday Monitoring](#3-intraday-monitoring)
4. [End of Day](#4-end-of-day)
5. [Historical Replay](#5-historical-replay)
6. [Incident Response](#6-incident-response)

---

## 0. First-Time Setup (run once)

### 0.1 Create and activate virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Verify:

```bash
python3 --version     # Python 3.12.x
pip show streamlit    # Version: 1.x
```

### 0.2 Set secrets

Create `.env` in the project root (never commit it):

```bash
cat > .env <<'EOF'
VOL_INFRA_IBKR__HOST=127.0.0.1
VOL_INFRA_IBKR__PORT=4002
VOL_INFRA_IBKR__CLIENT_ID=1
VOL_INFRA_IBKR__READ_ONLY=true
EOF
```

### 0.3 Verify config loads

```bash
python3 -c "from src.utils.config import load_config; c = load_config(); print('port:', c.ibkr.port)"
# Expected:  port: 4002
```

### 0.4 Verify universe config

```bash
python3 -c "
from src.universe.discovery import load_universe_config
cfg = load_universe_config('configs/')
print('underlyings:', [u.symbol for u in cfg.underlyings])
print('strike_selection_mode:', cfg.strike_selection_mode)
"
```

Expected:

```
underlyings: ['ESTX50', 'SPY']
strike_selection_mode: delta_based
```

### 0.5 Run test suite

```bash
python3 -m pytest tests/ -q --ignore=tests/unit/test_yfinance_loader.py
# Expected: 1446 passed, ~5 skipped, 6 known failures in test_step2.py
```

The 6 `test_step2` failures are pre-existing (constituent list stub) and do not
affect live functionality.

---

## 1. Dashboard — Seed & Launch

> **No IBKR connection required.** All data comes from the local storage layer.

### 1.1 Activate the environment

```bash
source .venv/bin/activate
```

### 1.2 Seed historical data

Run once, or re-run whenever you want to refresh the local dataset.
`--days N` seeds the last N business days ending today (weekends skipped).

```bash
python3 scripts/seed_dashboard_data.py --days 10
```

Expected output (spot path will vary slightly each run):

```
Anchor: trade_date=2026-06-11, spot=5098.41, days=10
Storage root: .../ai_for_algo_trading/data

Straddle opened 2026-05-29 at spot 5186  →  PnL call -514  put +318  total -196 EUR

  [1/10] 2026-05-29  spot=5186.05  iv_shift=+0.0000
  ...
  [10/10] 2026-06-11  spot=5098.41  iv_shift=+0.0007

Done. 10 day(s) seeded.
```

What gets written for each date under `data/analytics/`:

| Table | Path |
|---|---|
| Market snapshots (spot) | `market_state_snapshots/dt=<date>/underlying=ESTX50/` |
| Forward curve | `forward_curve/dt=<date>/underlying=ESTX50/v=seed/` |
| IV surface grid | `surface_grid/dt=<date>/underlying=ESTX50/v=seed/` |
| IV points (per option) | `iv_points/dt=<date>/underlying=ESTX50/v=seed/` |
| BS prices + Greeks | `pricing_results/dt=<date>/v=seed/` |
| Scenario PnL (UAM) | `scenario_results/dt=<date>/v=seed/` |
| Position quantities | `positions/dt=<date>/` |

Straddle position JSON: `data/positions/STRADDLE_PAPER.json`

**To add more days later:**

```bash
python3 scripts/seed_dashboard_data.py --days 20   # extends history
```

**To seed a specific past date (must be a business day):**

```bash
python3 scripts/seed_dashboard_data.py --trade-date 2026-05-15 --days 1
```

### 1.3 Launch the dashboard

```bash
streamlit run src/dashboard/app.py
```

Open `http://localhost:8501` in your browser.

**Sidebar controls:**

| Control | What to set |
|---|---|
| Storage root | `data` (default — do not change) |
| Portfolio ID | `STRADDLE_PAPER` (default) |
| Trade date | Pick any available date from the dropdown |
| Underlying | `ESTX50` (default) |

**Six panels:**

| Tab | Shows |
|---|---|
| Vol Surface | IV heatmap across log-moneyness × maturity (SVI model output) |
| IV Smile | Per-expiry IV smile curves |
| Greeks | Dollar delta and dollar vega per option position |
| Scenario PnL | ±5 % spot × ±20 vol-pts stress scenarios |
| Straddle Status | Current leg prices, DTE, call/put/total PnL |
| UAM Gauge | Margin utilisation ratio with warn/critical bands |

**Expected values (2026-06-11 seed):**

- UAM ratio: ~0.12 (well below 0.5 warning threshold)
- Straddle DTE: ~365 days (2027-06-11 expiry)
- Straddle total PnL: ~−196 EUR (position opened 2026-05-29, spot fell)
- Vol Surface: 7 moneyness columns × 4 maturities, IV ~18–22 %

---

## 2. Start of Day — Live Session

**When:** Before EUREX open (09:00 CET). Allow 15 minutes.
**Requires:** IB Gateway running, venv activated.

### 2.1 Start IB Gateway

1. Launch IB Gateway in **paper trading** mode and log in.
2. Confirm **Socket port = 4002** and **Enable ActiveX and Socket Clients** is ticked.
3. Verify the gateway heartbeat indicator is green.

### 2.2 Run bootstrap smoke test

```bash
# Config validation only (no IB Gateway needed)
PYTHONPATH=. python3 scripts/bootstrap_smoke_test.py --mock

# Full check with live IB Gateway
PYTHONPATH=. python3 scripts/bootstrap_smoke_test.py
```

Expected: `overall: PASS`.
Manifest written to `artifacts/bootstrap_<run_id>.json`.

If `connectivity` stage fails → see [6.1 IBKR Connectivity Failure](#61-ibkr-connectivity-failure).

### 2.3 Start live data collection

```bash
python3 -m src.collectors.raw_collector \
  --underlying ESTX50 \
  --session-id "$(date +%Y%m%d)_SOD" \
  --config-dir configs/
```

Writes to `data/raw/dt=<YYYY-MM-DD>/session=<id>/events.jsonl`.

---

## 3. Intraday Monitoring

**Frequency:** Every 30–60 minutes during trading hours.

### 3.1 Check QC metrics

```bash
python3 -m src.orchestration.jobs \
  --job job_qc_run \
  --underlying ESTX50 \
  --trade-date "$(date +%Y-%m-%d)"
```

If `status = fail` on a critical check → see [6.2 Data Quality Failure](#62-data-quality-failure).

### 3.2 Inspect QC triage table

```python
from src.qc.validation import build_triage_table
# reports = list of DailyQCReport loaded from storage
table = build_triage_table(reports)
for row in table[:10]:
    print(row)
```

`reason_code` is always populated for non-pass rows.

### 3.3 Visual check via dashboard

```bash
streamlit run src/dashboard/app.py
```

- **Vol Surface**: look for NaN holes or IV spikes above 50 %
- **IV Smile**: confirm no calendar arbitrage (smile should be smooth)
- **UAM Gauge**: ratio should be < 0.5 in normal conditions

### 3.4 Monitor IBKR pacing

IBKR enforces ≤ 40 messages/second. If the collector log shows `PACING_VIOLATION`:

1. Increase `throttle_ms` in `configs/broker.yaml`.
2. Restart the collector after a 60-second pause.

---

## 4. End of Day

**When:** After EUREX close (17:30 CET).

### 4.1 Stop live collection

```bash
# Ctrl+C in the collector terminal, or:
kill -TERM $(pgrep -f raw_collector)
```

The collector writes a session-close record before exiting.

### 4.2 Run EOD analytics pipeline

```bash
python3 -m src.orchestration.jobs \
  --job job_eod_pipeline \
  --trade-date "$(date +%Y-%m-%d)" \
  --code-version "$(cat src/__version__)" \
  --run-id "eod_$(date +%Y%m%d)"
```

Pipeline stages: snapshot building → forward curve → IV solver → surface
calibration → pricing + Greeks → risk aggregation → scenario PnL → UAM → QC.
Manifest written to `data/manifests/`. Check `status = "ok"`.

### 4.3 Run EOD reconciliation

```bash
python3 -m src.orchestration.jobs \
  --job job_eod_reconciliation \
  --trade-date "$(date +%Y-%m-%d)"
```

If `missing_in_broker` is non-empty → see [6.3 Reconciliation Failure](#63-reconciliation-failure).

### 4.4 Roll straddle if DTE ≤ 270 days

```python
from src.strategy.straddle import should_roll
import json, pathlib

pos = json.loads(pathlib.Path("data/positions/STRADDLE_PAPER.json").read_text())
trade_date = "2026-06-11"           # today's date
config = {"roll_dte_days": 270}     # from configs/strategy.yaml

if should_roll(pos, trade_date, config):
    print("Roll required — open new straddle via order_manager")
```

### 4.5 Verify idempotency

Re-running the EOD pipeline with the same `trade_date` + `code_version` must
return `status = "skipped"` for all steps:

```bash
python3 -m src.orchestration.jobs --job job_eod_pipeline \
  --trade-date "$(date +%Y-%m-%d)" \
  --code-version "$(cat src/__version__)"
# Expected: {"status": "skipped", ...} for each step
```

---

## 5. Historical Replay

Use replay to regenerate analytics with a newer code version (after a model fix)
without touching original partitions.

### 5.1 Single-day replay

```python
from src.orchestration.replay import replay_day
from src.utils.config import load_config
from src.storage.reader import StorageReader
from src.storage.writer import StorageWriter

config = load_config()
reader = StorageReader("data", config.__dict__)
writer = StorageWriter("data", config.__dict__)

result = replay_day(
    trade_date="2026-06-01",
    code_version="1.0.1",          # new version tag
    config=config,
    reader=reader,
    writer=writer,
    expected_symbols=["ESTX50"],
    analytics_base="analytics",
)
print(result["status"])            # "ok", "partial", or "skipped"
```

Writes to `analytics/v=1.0.1/dt=2026-06-01/` — never overwrites
`v=1.0.0` partitions.

### 5.2 Date-range replay

```python
from src.orchestration.replay import replay_date_range

results = replay_date_range(
    start_date="2026-05-01",
    end_date="2026-05-31",
    code_version="1.0.1",
    config=config,
    reader=reader,
    writer=writer,
    skip_weekends=True,
)
```

### 5.3 Compare replay vs live

```python
from src.orchestration.replay import compare_replay_vs_live

comparison = compare_replay_vs_live(
    trade_date="2026-06-01",
    replay_version="1.0.1",
    live_version="1.0.0",
    reader=reader,
    tolerance=1e-8,
)
print(comparison.matching_count)
print(comparison.differing_keys)   # empty = deterministic ✓
```

---

## 6. Incident Response

### 6.1 IBKR Connectivity Failure

**Symptoms:** `bootstrap_smoke_test.py` exits with code 3; session stuck in
`CONNECTING` or `DISCONNECTED`.

1. Confirm IB Gateway is running (check process list and gateway UI).
2. Verify port: `4002` paper / `4001` live.
3. Check `.env`: `VOL_INFRA_IBKR__HOST` and `VOL_INFRA_IBKR__PORT`.
4. Check `configs/broker.yaml` for correct defaults.
5. Restart IB Gateway; allow 30 seconds to stabilise.
6. Re-run `PYTHONPATH=. python3 scripts/bootstrap_smoke_test.py`.
7. After 3 failed attempts, run `--mock` to isolate whether it is config or
   network, then raise a ticket with IBKR support.

**Impact:** Live data collection stopped. Dashboard (seeded data) and historical
replay are unaffected.

---

### 6.2 Data Quality Failure

**Symptoms:** QC report `status = "fail"` on a critical check.

1. Read `reason_code` from the triage table (§3.2).

| reason_code | Meaning | Action |
|---|---|---|
| `HIGH_SPREAD_PCT` | Bid-ask too wide | Market illiquid — widen threshold or skip session |
| `HIGH_STALE_RATIO` | Too many stale quotes | Check IBKR entitlements + pacing |
| `GAP_TOO_LARGE` | Collector session gap | Check collector logs for interruption |
| `LOW_CONVERGENCE_RATIO` | IV solver failing | Check for circuit breakers / extreme vol |
| `HIGH_SURFACE_RMSE` | Surface fit poor | Try SVI → spline fallback |
| `CALENDAR_VIOLATION` | Total variance not monotone | Calendar arbitrage in market |

2. For transient failures: annotate in manifest, widen threshold, document in `logs/incidents/`.
3. For systemic failures: fix root cause, replay affected dates with new code version.

---

### 6.3 Reconciliation Failure

**Symptoms:** EOD reconciliation reports `missing_in_broker` or `quantity_mismatches`.

1. Identify which `contract_key` is mismatched.
2. Cross-check IBKR position statement (IB Portal or TWS) against
   `ReconciliationReport.expected_legs`.

| Symptom | Cause | Fix |
|---|---|---|
| `missing_in_broker` | Order not filled | Check `OrderResult.status`; re-submit |
| `extra_in_broker` | Manual trade outside system | Document; reconcile manually |
| `quantity_mismatches` | Partial fill | Update expected qty; do not re-submit whole order |

3. Set `read_only = true` in `configs/strategy.yaml` while investigating.
4. Never silently update expected positions to match broker — always investigate first.

---

### 6.4 Dashboard Shows No Data

**Symptoms:** All panels empty or show "No data available".

1. Check the **Storage root** in the sidebar is `data` (not `data/storage`).
2. Check the **Trade date** dropdown has entries — if empty, no data has been seeded.
3. Re-run the seed script:

```bash
python3 scripts/seed_dashboard_data.py --days 10
```

4. Refresh the browser or click **Refresh** in the sidebar.

---

### 6.5 Replay Divergence

**Symptoms:** `compare_replay_vs_live` reports non-empty `differing_keys`.

1. Check same raw data: `completeness.coverage_pct` should be 1.0 for both versions.
2. Check `config_hash` in both manifests — a config change causes legitimate divergence.
3. If intentional (model fix): document before/after in the manifest; both partitions
   are kept (versioned paths never overwrite).
4. If unexpected: add a unit test for the diverging key, trace the code path, fix.

**Acceptance criterion:** same code version + same raw data → identical output.

---

## Appendix A — Exit Code Reference

| Code | Meaning |
|---|---|
| 0 | All checks passed |
| 2 | Config / environment error |
| 3 | IBKR connectivity error |
| 4 | Data retrieval error |
| 5 | Health check failure |

## Appendix B — Key Config Files

| File | Purpose |
|---|---|
| `configs/environment.yaml` | Log level, paths, clock-drift tolerance |
| `configs/broker.yaml` | IBKR host/port, reconnect policy, pacing limits |
| `configs/universe.yaml` | Underlyings, strike ladder, maturity ladder |
| `configs/qc.yaml` | QC thresholds, anomaly detection settings |
| `configs/scenarios.yaml` | Named stress scenarios |
| `configs/pricing.yaml` | Black-Scholes + CRR tree settings |
| `configs/strategy.yaml` | ATR Straddle parameters, roll threshold, sizing |

## Appendix C — Storage Layout

```
data/
├── analytics/
│   ├── market_state_snapshots/dt=YYYY-MM-DD/underlying=ESTX50/data.parquet
│   ├── forward_curve/dt=YYYY-MM-DD/underlying=ESTX50/v=<ver>/data.parquet
│   ├── iv_points/dt=YYYY-MM-DD/underlying=ESTX50/v=<ver>/data.parquet
│   ├── surface_grid/dt=YYYY-MM-DD/underlying=ESTX50/v=<ver>/data.parquet
│   ├── surface_parameters/dt=YYYY-MM-DD/underlying=ESTX50/v=<ver>/data.parquet
│   ├── pricing_results/dt=YYYY-MM-DD/v=<ver>/data.parquet
│   ├── scenario_results/dt=YYYY-MM-DD/v=<ver>/data.parquet
│   └── positions/dt=YYYY-MM-DD/data.parquet
├── positions/
│   └── STRADDLE_PAPER.json          ← straddle status panel
├── raw/dt=YYYY-MM-DD/session=<id>/events.jsonl
├── manifests/<run_id>.json
└── metadata.db                      ← lineage + write log (SQLite)
```
