# Known Limitations

This document lists confirmed limitations, known gaps, and intentional design
trade-offs in the Volatility Infrastructure Platform. It is updated with every
release. Items marked **[Professor req]** reflect explicit assignment constraints.

---

## Data Sources

### Yahoo Finance — EOD only, no options
- `yfinance` returns **end-of-day bars** for equity indices and stocks only.
- No intraday data, no options data, no futures history via Yahoo Finance.
- `^STOXX50E` ticker returns the index level; ESTX50 options are IBKR-only.
- **Impact:** The Yahoo Finance loader (`historical/yfinance_loader.py`) is limited
  to backtesting underlying spot prices. All options analytics require a live or
  recorded IBKR session.
- **Workaround:** Use `replay_day()` on raw data captured via the live IBKR collector.

### IBKR Paper Account Entitlements
- Paper accounts may not have the same market-data entitlements as live accounts.
- EUREX real-time data requires explicit subscription in IBKR Account Management.
- Without the subscription, `request_snapshot()` returns 15-minute delayed quotes.
- **Mitigation:** `bootstrap_smoke_test.py` reports `is_delayed = True` in the
  snapshot stage when delayed data is detected.

### Exchange Hours
- EUREX option market data is only available **09:00–17:30 CET** on business days.
- Outside these hours, the collector returns stale or no data.
- The `is_market_open` field in `market_state_snapshots` tracks this condition.

---

## IBKR Pacing and Rate Limits

- IBKR enforces a hard limit of **50 simultaneous requests** and a soft limit of
  **~40 messages/second**.
- The system's `max_messages_per_second: 40` setting in `configs/broker.yaml`
  respects this; pacing violations cause a mandatory 10-second pause.
- **Impact:** Full Euro Stoxx 50 option chain discovery (50 underlyings × multiple
  expiries) can take several minutes. Do not lower the pacing setting.
- IBKR also limits `reqHistoricalData` to **60 historical data requests per
  10 minutes** — relevant for bulk backfills.

---

## ATR Straddle Strategy

**[Professor req]** The ATR Straddle is a demonstration strategy, not a production signal:

- **Single underlying:** Only ESTX50 is supported. Adding a second underlying
  requires extending `configs/universe.yaml` and testing the option chain
  discovery for that exchange.
- **Roll threshold fixed at 270 days** — this is the professor requirement and is
  not dynamically calibrated to term structure shape or vol regime.
- **No gamma scalping or delta hedging:** The strategy holds the straddle to roll
  without intraday hedging. Net delta will drift.
- **Paper trading only:** `order_manager.py` defaults to `paper_trading=True`.
  Setting `paper_trading=False` routes real orders — requires separate sign-off
  and a change to `configs/strategy.yaml`.

---

## IV Solver

- The Brent root-solver is calibrated for **standard liquid options** (0.05–5.00
  annualised IV). Deep in-the-money, very short-dated, or very long-dated options
  may not converge.
- Failed convergence is recorded with `converged=False` and a non-null `failure_reason`.
  These points are **excluded from the surface fit** but never silently dropped.
- The minimum accepted IV is configurable via `configs/qc.yaml → min_iv`.

---

## Vol Surface Calibration

- **SVI fit** requires ≥ 4 accepted IV points per expiry slice. Slices with fewer
  points fall back to a **PCHIP spline**. The `quality_flag` field records which
  model was used.
- **Calendar arbitrage detection** (`check_calendar_sanity`) flags violations but
  does **not** enforce no-arbitrage in the calibration step. A future version may
  add a constrained SVI fit.
- **Butterfly arbitrage** is not explicitly checked in the current QC framework.

---

## Storage

### SQLite as Dev Baseline
- `metadata.db` (SQLite) is the development lineage store. It is **not suitable for
  production concurrent writes** (multiple collectors or parallel job runs).
- Production deployment should use PostgreSQL 15+ — see `docs/environment.md`.

### No Data Expiry / TTL
- The platform writes versioned Parquet partitions indefinitely.
- There is no automated cleanup of old `v=<version>` partitions.
- Manual cleanup: `find data/analytics -name "data.parquet" -mtime +365 | xargs ls -lh`
  — review before deleting; older versions may be needed for replay comparisons.

### InfluxDB is Additive
- InfluxDB stores raw ticks for time-series queries in production.
- The JSONL files remain the **canonical replay-safe archive** — InfluxDB is additive,
  not a replacement.
- If InfluxDB is unavailable, the platform falls back to JSONL with no data loss.

---

## Replay

- Replay is only possible for dates where **raw JSONL events exist**.
- If the collector was not running on a given date, there is no raw data and
  `detect_data_completeness` returns `is_empty=True` → replay is skipped.
- Replay idempotency guarantee: **same code version + same raw data → identical output**.
  A different code version legitimately produces different output; this is expected.

---

## Dashboard

- The Streamlit dashboard (`src/dashboard/app.py`) has no authentication.
  Do not expose it to untrusted networks without adding auth middleware.
- The dashboard is **read-only** — it reads from the storage layer and does not
  modify any data or place orders.
- All charts are Matplotlib (`Agg` backend) — interactive zooming is not supported.
  A future version could switch to Plotly for interactive charts.

---

## Observability

- Structured logging writes to `logs/` with session/job/step correlation IDs.
  There is no centralised log aggregation (e.g., ELK, Datadog) in the current setup.
- `MetricsCatalog` records event_rate, stale_ratio, solver_failures,
  scenario_runtime. It is **in-memory only** — metrics are not persisted across
  process restarts and are not exported to Prometheus or InfluxDB.

---

## Out of Scope (by Design)

The following are explicitly **not** implemented in this platform:

| Feature | Reason |
|---------|--------|
| American options with early exercise on index | ESTX50 index options are European-style |
| Real-time Greek hedging / delta hedger | Out of scope for the professor's assignment |
| Multi-asset portfolio (equities + options) | Single-underlying demonstration platform |
| High-frequency tick capture (< 1s) | IBKR API is not a HFT feed |
| Regulatory reporting (MiFID II, EMIR) | Not required for paper-trading demonstration |
| Live options history via Yahoo Finance | Technically impossible — EOD indices only |
