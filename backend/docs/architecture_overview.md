# Architecture Overview

> Last reviewed: Step 18 — all 18 implementation steps complete. See `docs/limitations.md`
> for known gaps and `docs/schemas.md` for full table definitions.

## System Purpose
Strategy-agnostic volatility infrastructure platform built on IBKR (Interactive Brokers).
Provides: data capture, pricing, surface calibration, Greeks, scenario risk, and UAM.

**Primary underlying:** Euro Stoxx 50 index (`^STOXX50E` / ESTX50 on EUREX)
**Demo strategy:** ATR Straddle — buy 1 ATM call + 1 ATM put, roll at 9-month maturity
**Key risk metric:** UAM (Utilisation des Actifs Margés) — margin shock ±5% spot / ±20% vol

## Seven-Layer Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Layer 7: Dashboard                                      │
│  dashboard/ → vol surface, Greeks, UAM, straddle status  │
├─────────────────────────────────────────────────────────┤
│  Layer 6: Strategy & Execution                           │
│  strategy/ (ATR Straddle) → execution/ (paper orders)    │
├─────────────────────────────────────────────────────────┤
│  Layer 5: Portfolio / Risk Analytics                     │
│  risk/ → Greeks, UAM, scenario PnL, aggregation          │
├─────────────────────────────────────────────────────────┤
│  Layer 4: Derived Analytics                              │
│  forwards/ → qc/ → iv/ → surfaces/ → pricing/           │
├─────────────────────────────────────────────────────────┤
│  Layer 3: Normalized Market State                        │
│  snapshots/ → market_state_snapshots table               │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Raw Capture (IMMUTABLE)                        │
│  collectors/ (IBKR live) + historical/ (Yahoo Finance)   │
├─────────────────────────────────────────────────────────┤
│  Layer 1: Connectivity & Storage                         │
│  connectivity/ (IBKR adapter) + storage/ (DB + files)    │
└─────────────────────────────────────────────────────────┘
```

**Rule: No downstream layer may silently overwrite an upstream observation.**

## Services

| Service | Module | Responsibility |
|---------|--------|---------------|
| Connectivity | `connectivity/session.py` | IBKR session, state machine, reconnect, heartbeat |
| Universe | `universe/discovery.py` | Euro Stoxx 50 chain discovery, instrument master |
| Collector (live) | `collectors/raw_collector.py` | Lightweight tick capture → raw store (IBKR) |
| Collector (historical) | `historical/yfinance_loader.py` | EOD bars → backtest store (Yahoo Finance) |
| Storage | `storage/writer.py`, `storage/reader.py` | Parquet analytics + PostgreSQL metadata + InfluxDB ticks |
| Snapshot Builder | `snapshots/builder.py` | Raw events → time-aligned snapshots |
| Forward Engine | `forwards/engine.py` | Parity-based forward + carry diagnostics |
| QC Filter | `qc/quote_filter.py` | Named per-quote validation checks |
| IV Solver | `iv/solver.py` | Bracketed root solver (Brent), structured diagnostics |
| Surface Engine | `surfaces/calibration.py` | SVI fit + PCHIP spline fallback, grid output |
| Pricing | `pricing/european.py`, `american.py` | Black-Scholes (European), CRR binomial (American) |
| Risk | `risk/aggregation.py`, `scenarios.py` | Greeks, scenario PnL, line + aggregate outputs |
| UAM | `risk/uam.py` | Margin shock: ±5% spot / ±20% vol → UAM ratio |
| Strategy | `strategy/straddle.py` | ATR Straddle signal, roll logic (9-month DTE) |
| Execution | `execution/order_manager.py` | Paper-trading order placement via IBKR |
| Orchestration | `orchestration/jobs.py`, `replay.py` | Job entry points, manifests, EOD replay |
| QC Framework | `qc/validation.py` | Named validation checks, daily QC report |
| Dashboard | `dashboard/app.py` | Dash/Streamlit UI: surface, Greeks, UAM, straddle |

## Data Sources

| Data type | Source | Module |
|-----------|--------|--------|
| Live option quotes + index | IBKR API (EUREX) | `collectors/raw_collector.py` |
| Historical EOD bars (Euro Stoxx 50) | Yahoo Finance (`yfinance`) | `historical/yfinance_loader.py` |
| Historical EOD bars (50 constituents) | Yahoo Finance (`yfinance`) | `historical/yfinance_loader.py` |
| Options / futures history | IBKR `reqHistoricalData` | `collectors/raw_collector.py` |

Yahoo Finance is EOD only — **never** used for intraday or options pricing.

## Data Flow (one trading day)

```
IBKR (EUREX) ──→ RawCollector ──────→ raw_market_events (immutable JSONL / InfluxDB)
Yahoo Finance ──→ yfinance_loader ──→ historical_bars (Parquet backtest store)
                                                    │
                                        Snapshot Builder
                                                    │
                                           Forward Engine
                                                    │
                                            QC Filter
                                                    │
                                            IV Solver
                                                    │
                                          Surface Engine
                                                    │
                                          Pricing Engine
                                                    │
                              ┌─────────────────────┤
                              ▼                     ▼
                    Risk Aggregation + UAM     ATR Straddle
                              │                     │
                    Scenario Engine          Order Manager
                              │                     │
                          QC Report            paper orders
                              │
                          Dashboard
```

## Key Design Rules

1. **Never compute analytics inside broker callbacks** — callbacks: normalize → stamp → persist only
2. **Never hide fallbacks** — `reference_type` field always set; all fallbacks logged
3. **Replay uses identical code path** — no separate historical implementation
4. **Configs are versioned** — thresholds, solver bounds, scenario grids in YAML
5. **Forward is first-class output** — diagnostics stored alongside chosen forward
6. **Store both SVI params AND grid** — params for archival, grid for operations
7. **All derived records reference source `snapshot_ts`** — full data lineage
8. **Pacing limiter enforced** — ≤ 40 msg/s to IBKR (configured in `broker.yaml`)
9. **Yahoo Finance = stocks/indices only** — options and futures always via IBKR

## Storage Partitioning

```
data/
  raw/
    dt=2026-01-15/session=abc123/events.jsonl         ← append-only ticks
  analytics/
    instrument_master/dt=2026-01-15/v=1.0/data.parquet
    market_state_snapshots/dt=2026-01-15/underlying=ESTX50/data.parquet
    forward_curve/dt=2026-01-15/underlying=ESTX50/v=1.0/data.parquet
    iv_points/dt=2026-01-15/underlying=ESTX50/v=1.0/data.parquet
    surface_parameters/dt=2026-01-15/underlying=ESTX50/v=1.0/data.parquet
    surface_grid/dt=2026-01-15/underlying=ESTX50/v=1.0/data.parquet
    pricing_results/dt=2026-01-15/v=1.0/data.parquet
    risk_aggregates/dt=2026-01-15/v=1.0/data.parquet
    scenario_results/dt=2026-01-15/v=1.0/data.parquet
    qc_results/dt=2026-01-15/v=run_001/data.parquet
  manifests/
    run_2026-01-15_eod_001.json
metadata.db   ← SQLite (dev baseline) → PostgreSQL (production)
```

**InfluxDB** stores raw ticks for time-series queries (production). The JSONL files remain as
the replay-safe immutable archive — InfluxDB is additive, not a replacement.

## Universe

- **Index:** `^STOXX50E` (IBKR: `ESTX50`, EUREX, EUR)
- **Constituents:** 50 equities (ADS.DE, AI.PA, … VOW3.DE) — sourced from `EURO_STOXX_50_TICKERS`
- **Strike selection:** delta-based, −30Δ to +30Δ, steps [10, 15, 20, 25, 30]Δ
- **Maturity ladder (days):** 10, 30, 90, 180, 270, 365, 548, 730, 1095
- **Day-count convention:** ACT/365
