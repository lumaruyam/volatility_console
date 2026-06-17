# Volatility Infrastructure Platform — 2-Week Implementation Plan

## Overview
Build an institutional-grade volatility infrastructure platform on IBKR (Interactive Brokers).
The system is **strategy-agnostic**: it provides data, pricing, surface, and risk primitives only.

**Primary universe (professor requirement):** Euro Stoxx 50 (`^STOXX50E`) — 50 constituents + index.
**Demo strategy (professor requirement):** ATR Straddle — buy 1 ATM call + 1 ATM put, roll at 9-month maturity.
**Risk metric (professor requirement):** UAM (Utilisation des Actifs Margés) — margin shocks ±5% spot / ±20% vol.

## Architecture Layers (in dependency order)
```
1.  Connectivity      → IBKR session management
2.  Universe          → Instrument master and option chain discovery
3a. Collectors        → Raw market-data capture (IBKR live)
3b. Historical        → Yahoo Finance backfill (stocks/indices)  ← ADDED
4.  Storage           → PostgreSQL metadata + InfluxDB ticks + Parquet analytics
5.  Snapshots         → Normalized market-state builder
6.  Forwards          → Forward curve and implied carry
7.  QC                → Quote filtering and quality control
8.  IV                → Implied volatility solver
9.  Surfaces          → SVI/spline surface calibration
10. Pricing           → European and American pricers
11. Risk              → Greeks, aggregation, scenarios, UAM
12. Strategy          → ATR Straddle signal and sizing
13. Execution         → Paper-trading order manager (IBKR)
14. Orchestration     → Job scheduling and manifests
15. QC Framework      → Validation and anomaly detection
16. Dashboard         → Dash/Streamlit interactive UI
```

## Data Sources
| Data type | Source | When |
|---|---|---|
| Live option quotes | IBKR API (EUREX) | During market hours |
| Live underlying price (Euro Stoxx 50) | IBKR API | During market hours |
| Recent historical options/futures | IBKR `reqHistoricalData` | Anytime |
| Historical index / stock prices | **Yahoo Finance** (`yfinance`) | Anytime (backtest) |
| Strike selection | Delta-based: −30Δ to +30Δ, steps 10/15/20/25/30 | Per chain refresh |
| Maturity ladder | 10d, 1m, 3m, 6m, 9m, 12m, 18m, 2y, 3y | Fixed per `universe.yaml` |

---

## Week 1: Foundation (Steps 1–8)

### Day 1 — Step 1: Access, environments, security
**Files:** `configs/`, `src/connectivity/session.py`, `scripts/bootstrap_smoke_test.py`
- [x] Python environment pinned in `requirements.txt`
- [x] IB Gateway / TWS connectivity via `ib_async` (replaces `ib_insync`)
- [x] Secrets loaded from `.env` via `VOL_INFRA_IBKR__*` env vars (never hard-coded)
- [x] Config files: `environment.yaml`, `broker.yaml`, `universe.yaml`, `qc.yaml`, `scenarios.yaml`, `pricing.yaml`
- [x] Bootstrap smoke test: connect → resolve 1 contract → get 1 quote → write JSON manifest
- [x] `--mock` flag allows full CI run without IBKR connection (MockAdapter)
- [x] Session state machine with legal-transition enforcement
- [x] Client ID reservation table in `broker.yaml` (1=bootstrap, 10=underlying_collector, …)
- [ ] Confirm EUREX market-data entitlement active in IBKR account (needed for `^STOXX50E`)
- [x] `bootstrap_smoke_test.py --mock` passes in CI (no IBKR required)
- **Acceptance:** `python scripts/bootstrap_smoke_test.py --mock` exits 0; live run exits 0 with IBKR connected

### Day 1–2 — Step 2: Instrument master
**Files:** `src/universe/contracts.py`, `src/universe/discovery.py`
- [x] `UnderlyingContract` and `OptionContract` dataclasses with canonical key
- [x] `get_underlying(symbol)` → resolves IBKR contract
- [x] `get_option_chain(symbol, date)` → returns list of `OptionContract`
- [x] `resolve_contract(key)` → canonical key ↔ broker ID round-trip
- [x] `load_active_universe(session_date)` → reproducible from config
- [x] Persist raw broker payloads alongside normalized records
- [x] Euro Stoxx 50 index (`ESTX50 / ^STOXX50E`) resolves correctly on EUREX (MockAdapter + tests)
- [x] All 50 constituent equities from `EURO_STOXX_50_TICKERS` loadable as underlyings
- [x] Strike filtering applies delta-based selection (−30Δ to +30Δ) from `universe.yaml`
- [x] Maturity filtering applies fixed ladder from `universe.yaml` (`maturity_ladder_days`)
- [x] `filter_by_dte` and `filter_by_strike_range` validated against configured ladder
- **Acceptance:** Same universe reproduced on repeated runs; duplicates removed; Euro Stoxx 50 chain resolves on EUREX

### Day 2–3 — Step 3: Market-data ingestion
**Files:** `src/collectors/raw_collector.py`, `src/collectors/raw_writer.py`
- [x] `RawCollector` class: subscribe underlyings + options, lightweight callbacks
- [x] `RawWriter` class: append-only writes, schema validation before write
- [x] Event structure: `{instrument_key, field_name, value, exchange_ts, receipt_ts, session_id}`
- [x] Reconnect logic with exponential backoff + jitter
- [x] Malformed events quarantined with reason code (never silently dropped)
- [x] Daily session summary: event counts, reconnect count, coverage ratio
- [x] Collector subscribes to Euro Stoxx 50 index + at least 1 constituent end-to-end
- [x] Pacing limiter enforced (≤ 40 msg/s per `broker.yaml`) to avoid IBKR rate-limit bans
- **Acceptance:** Kill-restart test does not corrupt raw store; 1 day replayed from disk

### Day 3 — Step 3b: Yahoo Finance historical loader  ← NEW
**Files:** `src/historical/yfinance_loader.py`
- [x] `fetch_index_history(ticker, start, end)` → DataFrame (Euro Stoxx 50, S&P 500)
- [x] `fetch_constituents_history(tickers, start, end)` → multi-ticker DataFrame
- [x] `fetch_euro_stoxx_50(start, end)` → `{index, constituents}` convenience wrapper
- [x] `to_historical_bars(df, ticker)` → list of typed `HistoricalBar` objects
- [x] `get_close_series(df, adjusted)` → clean price series for backtesting
- [x] `validate_history(df, ticker)` → warning list (NaN check, gaps, negative prices)
- [x] EURO_STOXX_50_TICKERS constant (50 tickers)
- [x] INDEX_TICKERS constant (^STOXX50E, ^GSPC, ^GDAXI, ^FCHI)
- **Acceptance:** `pytest tests/unit/test_yfinance_loader.py` all pass (no network needed — mocked)
- **Note:** Yahoo Finance = stocks/indices only. Options/futures → always IBKR.

### Day 3 — Step 4: Persistent storage and data model
**Files:** `src/storage/schemas.py`, `src/storage/writer.py`, `src/storage/reader.py`
- [x] Schema definitions for all 12 table families (see data model below)
- [x] SQLite metadata store + Parquet partitioned file store (current baseline)
- [x] Partitioning: `data/raw/dt=YYYY-MM-DD/underlying=XXX/`
- [x] Write-ahead validation (reject malformed records early)
- [x] `lineage_query(snapshot_ts, underlying)` → source raw records
- [x] **PostgreSQL** metadata store (replaces SQLite) — `src/storage/postgres_writer.py` stub created
- [x] **InfluxDB** time-series store for raw tick events — `src/storage/influx_writer.py` stub created
- [x] Migration path: SQLite/JSONL baseline → PostgreSQL/InfluxDB without schema changes
- [x] `src/storage/postgres_writer.py` and `src/storage/influx_writer.py` stubs
- **Acceptance:** Replay and live writes use identical schemas; PostgreSQL + InfluxDB reachable via config

### Day 4 — Step 5: Spot builder and market-state snapshots
**Files:** `src/snapshots/builder.py`, `src/snapshots/models.py`
- [x] `build_snapshot(events, snapshot_ts, config)` → `MarketStateSnapshot`
- [x] Mid-price reference: `S_mid = (Bid + Ask) / 2`
- [x] Fallback chain: mid → last → close → carry-forward (each labeled)
- [x] `UnderlyingState` and `OptionRow` dataclasses with quality flags
- [x] Snapshot completeness metrics per underlying and maturity
- [x] `_build_option_rows()` fully implemented
- [x] `_derive_state_flags()` fully implemented
- [x] Snapshot covers Euro Stoxx 50 index + constituent rows (not just SPY)
- **Acceptance:** Same raw events + params → identical snapshots on re-run; NotImplementedError resolved

### Day 4–5 — Step 6: Forward and implied carry engine
**Files:** `src/forwards/engine.py`, `src/forwards/models.py`
- [x] `estimate_forward(snapshot, maturity, rate, config)` → `ForwardResult`
- [x] Put-call parity: `F(T) ≈ K + e^(rT) * (C(K,T) - P(K,T))`
- [x] Carry identity: `q(T) = r(T) - (1/T) * ln(F(T)/S0)`
- [x] Liquidity weights: `ω_i = 1 / (SpreadPct_i + ε)`
- [x] Outlier rejection via Median Absolute Deviation (MAD)
- [x] `ForwardDiagnostics`: candidate list, weights, residuals, confidence score
- [x] Fallback policy: interpolate neighbors → borrow prior snapshot → mark unusable
- **Acceptance:** Forward stable across small strike-set perturbations

### Day 5 — Step 7: Quote normalization and QC
**Files:** `src/qc/quote_filter.py`, `src/qc/checks.py`
- [x] Named QC checks (not monolithic if-statement):
  - `check_spread_pct` — spread / mid ≤ threshold
  - `check_bid_positive` — bid > 0
  - `check_quote_age` — age ≤ max_quote_age_seconds
  - `check_open_interest` — OI ≥ min_open_interest
  - `check_crossed_market` — bid ≤ ask
  - `check_intrinsic_value` — price ≥ intrinsic
  - `check_parity_residual` — robust z-score on parity residuals
- [x] Each check returns `{status: pass/caution/reject, reason_code, value, threshold}`
- [x] Store rejected quotes with reason codes (auditable)
- **Acceptance:** Same quote consistently accepted/rejected under fixed threshold version

---

## Week 1–2 Bridge: Core Analytics (Steps 8–10)

### Day 6 — Step 8: Implied volatility solver ✅
**Files:** `src/iv/solver.py`, `src/iv/models.py`
- [x] Bracketed root solver (Brentq) — scalar first, then vectorized batch
- [x] `solve_iv(market_price, S, K, T, r, q, option_type)` → `IvSolveResult`
- [x] `IvSolveResult`: `{implied_vol, converged, iterations, residual, lower_bound, upper_bound, failure_reason}`
- [x] Pre-checks: intrinsic bounds, no-arbitrage bounds before entering solver
- [x] American proxy IV via documented convention (`solve_iv_american_proxy`, model_name="bs_american_proxy")
- [x] Failed solves return structured diagnostics, never silent NaN
- **Acceptance:** Reference contracts converge; bad quotes return structured failures ✅ (101 tests)

### Day 6–7 — Step 9: Surface engine ✅
**Files:** `src/surfaces/calibration.py`, `src/surfaces/models.py`, `src/surfaces/interpolation.py`
- [x] Transform IV points → log-moneyness + total variance space
- [x] `fit_slice(points, expiry_str, config)` → SVI params `{a, b, rho, m, sigma}`
- [x] SVI: `w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))`
- [x] Fallback: monotone cubic spline in total variance space (PCHIP)
- [x] Cross-maturity: linear interpolation in total variance space (`interpolate_surface_at`)
- [x] No-arbitrage diagnostics: calendar monotonicity check (`check_calendar_monotonicity`)
- [x] Store: raw IV points, accepted points, rejected points, params, grid, RMSE
- [x] Plotting helper for slice visualization (`plot_slice`)
- **Acceptance:** Reproducible params; calendar diagnostic computed; fit error metrics exposed ✅ (69 tests)

### Day 7 — Step 10: Pricing engine ✅
**Files:** `src/pricing/european.py`, `src/pricing/american.py`, `src/pricing/models.py`
- [x] `price_european(inputs: EuropeanInputs)` → `PricingResult` (Black-Scholes with carry)
- [x] Black-Scholes: `d1`, `d2`, call/put formulas with carry, stored as optional fields
- [x] `price_american(inputs: AmericanInputs)` → `PricingResult` (CRR binomial tree, n_steps=200)
- [x] Analytic Greeks for European; finite-difference (bump-and-reprice) for American
- [x] `PricingResult`: `{price, delta, gamma, vega, theta, dollar_gamma, dollar_vega, model_name, rho, d1, d2, n_steps}`
- [x] Benchmark fixtures: put-call parity, deep ITM/OTM, American=European when q=0
- **Acceptance:** Reference cases match; American converges to European in degenerate cases ✅ (88 new tests)

---

## Week 2: Risk, Infra, QC (Steps 11–16)

### Day 8 — Step 11: Greeks and per-position risk ✅
**Files:** `src/risk/aggregation.py`, `src/risk/models.py`, `src/risk/uam.py`
- [x] `compute_position_risk(position, snapshot, pricer, config)` → `PositionRisk`
- [x] `aggregate_risk(positions_risk, group_keys)` → `list[RiskAggregates]`
- [x] Dollar Greeks: `dollar_delta = Δ×S×Q×M`, `dollar_gamma = Γ×S²×Q×M`, `dollar_vega = ν×Q×M`
- [x] Local PnL approx: `ΔV ≈ Δ·dS + ½·Γ·dS² + ν·dσ + Θ·dt` (`compute_local_pnl_attribution`)
- [x] Reconciliation diagnostics vs broker-returned Greeks (`reconcile_with_broker_greeks`)
- [x] Store both line-level and aggregate outputs
- [x] **UAM (Utilisation des Actifs Margés):** shock ±5% spot / ±20% vol (4 scenarios), `UAMResult` with `uam_ratio`
- [x] `src/risk/uam.py`: `compute_uam(position_risks, config)` → `UAMResult`
- **Acceptance:** Aggregates reconcile to line-level sums; UAM metric computed and logged ✅ (81 tests)

### Day 8–9 — Step 12: Scenario engine
**Files:** `src/risk/scenarios.py`
- [x] `Scenario` dataclass: `{scenario_id, spot_shift_pct, vol_shift_abs, time_roll_days, version}`
- [x] `run_scenario(scenario, positions, snapshot, surface, pricer)` → `ScenarioResult`
- [x] Full repricing path (reference) + local Greek approximation (speed)
- [x] Contributor analysis: top N positions by scenario PnL impact
- [x] Version-controlled scenario grids in `configs/scenarios.yaml`
- **Acceptance:** Reports deterministic given positions + snapshot + scenario version

### Day 9 — Step 13: Historical reconstruction and replay
**Files:** `src/orchestration/replay.py`
- [x] `replay_day(date, code_version, config_version)` → runs full pipeline on stored raw data
- [x] Calls **same library functions** as live processing (no separate historical code path)
- [x] Writes to versioned partitions: `analytics/v=X.Y.Z/dt=YYYY-MM-DD/`
- [x] Missing-data detection and partial-data flags
- [x] Replay manifest with code version, config hashes, status
- **Acceptance:** Replay == live on overlapping dates with same code version

### Day 10 — Step 14: Validation framework
**Files:** `src/qc/validation.py`, `src/qc/anomaly.py`
- [x] Named validation checks (returns status + severity + value + context):
  - Collector continuity, underlying quote health, option chain coverage
  - Forward stability, parity residual, IV solver convergence
  - Surface fit error, calendar sanity, Greek sanity, scenario completeness
- [x] Robust z-score: `z_i = (x_i - median(x)) / (1.4826 * MAD(x))`
- [x] Daily QC report: pass/warn/fail per check per underlying
- [x] Anomaly detection vs rolling baseline
- [x] Triage table: every failure with reason code and context
- **Acceptance:** Failing underlyings identifiable within minutes from QC report

### Day 10–11 — Step 15: Orchestration, logging, observability
**Files:** `src/orchestration/jobs.py`, `src/orchestration/scheduler.py`, `src/utils/logging_utils.py`
- [x] Job entry points: universe_refresh, live_collect, incremental_analytics, eod_reconciliation, replay, qc_run
- [x] Structured logging with correlation IDs (session → analytics job)
- [x] Metrics catalog: event rates, stale ratios, solver failures, scenario runtime
- [x] Restart idempotency: re-run produces same output or new versioned partition
- [x] Dry-run mode for all batch jobs
- **Acceptance:** Simulated failure detected within documented interval; no duplicate outputs on restart

### Day 11 — Step 16: ATR Straddle strategy and paper execution
**Files:** `src/strategy/straddle.py`, `src/execution/order_manager.py`
- [x] `ATRStraddle`: buy 1 ATM call + 1 ATM put on Euro Stoxx 50 — professor requirement
- [x] Roll logic: open new straddle when existing position reaches 9-month maturity
- [x] Strike selection: closest to ATM using delta surface (|Δ| ≈ 0.50)
- [x] `order_manager.py`: paper-trading order placement via IBKR (read_only=False)
- [x] Position sizing: fixed notional or vol-adjusted — configurable in `configs/strategy.yaml`
- [x] Position reconciliation: compare expected vs broker-reported positions
- **Acceptance:** Straddle opens/rolls without errors in paper account; positions reconcile ✓

### Day 12 — Step 17: Dashboard
**Files:** `src/dashboard/app.py`, `src/dashboard/plots.py`
- [x] Dash or Streamlit app showing: vol surface heatmap, Greeks by position, scenario PnL bar chart
- [x] Live refresh from storage layer (reads Parquet / PostgreSQL)
- [x] ATR Straddle position status panel (open legs, DTE, PnL)
- [x] UAM gauge — professor requirement
- **Acceptance:** Dashboard loads from stored data without live IBKR connection ✓

### Day 12 — Step 18: Production hardening and docs
**Files:** `docs/`, `scripts/`, `RUNBOOKS.md`
- [x] Runbooks: start-of-day, intraday, end-of-day, replay, incident response
- [x] `docs/schemas.md` — all table definitions
- [x] `docs/architecture_overview.md` — data flow, service boundaries (keep current, update)
- [x] `docs/environment.md` — PostgreSQL + InfluxDB setup, smoke test
- [x] `release_checklist.md`
- [x] Known limitations doc (esp. Yahoo Finance = EOD only, no intraday)
- **Acceptance:** New engineer can set up env, run smoke test, trigger replay, read QC report independently ✓

---

## Data Model (12 Table Families)

| Table | Primary Keys | Description |
|-------|-------------|-------------|
| `instrument_master` | instrument_key, as_of_date | Canonical contracts |
| `raw_market_events` | session_id, event_id | Immutable tick observations |
| `market_state_snapshots` | snapshot_ts, instrument_key | Time-aligned analytics inputs |
| `forward_curve` | snapshot_ts, underlying, maturity | Forward + carry diagnostics |
| `iv_points` | snapshot_ts, contract_key | Solved IV observations |
| `surface_parameters` | snapshot_ts, underlying, maturity, model_version | Fitted params |
| `surface_grid` | snapshot_ts, underlying, maturity, moneyness_bucket | Regularized grid |
| `pricing_results` | snapshot_ts, contract_key, pricer_version | Model price + Greeks |
| `positions` | valuation_ts, portfolio_id, contract_key | Position records |
| `risk_aggregates` | valuation_ts, portfolio_id, group_key | Grouped risk |
| `scenario_results` | valuation_ts, portfolio_id, scenario_id, contract_key | Stress PnL |
| `qc_results` | run_id, check_name, target_key | Validation outcomes |

---

## Key Mathematical Identities

- **Reference spot:** `S_mid = (B_t + A_t) / 2`
- **Put-call parity forward:** `F(T) ≈ K + e^(rT) * (C - P)`
- **Carry identity:** `q(T) = r(T) - (1/T) * ln(F(T)/S0)`
- **Log-moneyness:** `k = ln(K / F(T))`
- **Total variance:** `w(k,T) = σ_imp(k,T)² * T`
- **BS d1:** `d1 = [ln(S/K) + (r - q + σ²/2)*T] / (σ√T)`
- **BS d2:** `d2 = d1 - σ√T`
- **Call:** `C = S*e^(-qT)*N(d1) - K*e^(-rT)*N(d2)`
- **Put:** `P = K*e^(-rT)*N(-d2) - S*e^(-qT)*N(-d1)`
- **SVI:** `w(k) = a + b*(ρ*(k-m) + sqrt((k-m)² + σ²))`
- **Calendar check:** `∂w(k,T)/∂T ≥ 0`
- **Dollar gamma:** `ΓS² × multiplier`
- **Local PnL:** `ΔV ≈ Δ*dS + 0.5*Γ*dS² + ν*dσ + Θ*dt`
- **Robust z-score:** `z_i = (x_i - median(x)) / (1.4826 * MAD(x))`

---

## Critical Implementation Rules (from spec)
1. **Never compute analytics inside the broker callback** — callbacks only normalize, stamp, persist
2. **Never hide a fallback** — reference_type field must always be stored
3. **Never store rejected quotes silently** — reason_code required
4. **Replay must use the same code path as live** — no separate historical implementation
5. **Never overwrite historical partitions** — always write new version identifier
6. **All configs are versioned** — thresholds, solver bounds, scenario grids in YAML files
7. **Forward is first-class output** — store diagnostics alongside chosen forward
8. **Store both fitted parameters AND grid values** — parameters alone insufficient for operations

---

## Configuration Files to Create
- `configs/environment.yaml` — storage paths, log levels, scheduler settings
- `configs/broker.yaml` — client IDs, reconnect policy, session windows
- `configs/universe.yaml` — monitored underlyings, exchanges, maturity windows
- `configs/qc.yaml` — quote filters, stale limits, solver thresholds
- `configs/scenarios.yaml` — named stress scenarios, shifts
- `configs/pricing.yaml` — solver bounds, finite-difference bumps

## End-to-End Daily Sequence
1. Start connectivity → verify session health
2. Refresh instrument master for session date
3. Launch collectors → write immutable raw events
4. Build normalized market-state snapshots
5. Build forward curves by maturity
6. Filter quotes (QC)
7. Solve implied volatilities
8. Fit surface slices + cross-maturity grid
9. Compute model prices and Greeks
10. Join positions → compute line-level and aggregate risk
11. Run scenario engine → publish stress summaries
12. Run QC suite → generate operator dashboard
13. Archive artifacts, close day, prepare replay partitions
