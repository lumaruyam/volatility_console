# Data Schemas — All Table Families

Canonical reference for all 12 table families in the Volatility Infrastructure Platform.
Python dataclass definitions live in `src/storage/schemas.py`.

**Storage conventions:**
- All timestamps are UTC epoch seconds (float), never local time
- Monetary values in native currency (EUR for ESTX50)
- Partition paths: `analytics/<table>/dt=<YYYY-MM-DD>/underlying=<sym>/v=<version>/data.parquet`
- Raw events: `data/raw/dt=<YYYY-MM-DD>/session=<id>/events.jsonl` (append-only)
- All derived tables reference source `snapshot_ts` for full lineage

---

## 1. `instrument_master`

**Primary key:** `instrument_key`, `as_of_date`  
**Layer:** analytics  
**Purpose:** Canonical contract registry — single source of truth for all contract metadata.

| Column | Type | Description |
|--------|------|-------------|
| `instrument_key` | str | Unique key: `{symbol}\|{sec_type}\|{exchange}\|{expiry}\|{strike}\|{right}` |
| `as_of_date` | str (ISO date) | Date this record is valid for |
| `underlying_symbol` | str | E.g. `ESTX50` |
| `sec_type` | str | `OPT`, `IND`, `STK`, `FUT` |
| `exchange` | str | E.g. `EUREX` |
| `currency` | str | E.g. `EUR` |
| `expiry` | str? | ISO date; `None` for non-expiring instruments |
| `strike` | float? | Option strike; `None` for non-options |
| `option_right` | str? | `C` (call), `P` (put), `None` |
| `multiplier` | float? | Contract multiplier (10.0 for ESTX50 options) |
| `contract_id_broker` | str? | IBKR `conid` |
| `trading_class` | str? | IBKR trading class |
| `universe_version` | str | Config version used during discovery |

---

## 2. `raw_market_events`

**Primary key:** `session_id`, `event_id`  
**Layer:** raw (immutable, append-only)  
**Purpose:** Verbatim tick observations from IBKR. Never modified after write.

| Column | Type | Description |
|--------|------|-------------|
| `session_id` | str | Collector session UUID |
| `event_id` | str | Monotone event counter within session |
| `instrument_key` | str | Identifies the instrument |
| `field_name` | str | E.g. `bid`, `ask`, `last`, `volume` |
| `field_value` | float | Raw numeric value |
| `exchange_ts` | float? | Exchange timestamp (UTC epoch); `None` if not provided |
| `receipt_ts` | float | System receipt timestamp (UTC epoch) |
| `source` | str | `live` or `replay` |

**Rule:** No analytics code may write to this table. Only `RawCollector` may append.

---

## 3. `market_state_snapshots`

**Primary key:** `snapshot_ts`, `instrument_key`  
**Layer:** analytics  
**Purpose:** Time-aligned, cleaned market state — input to all derived analytics.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_ts` | float | UTC epoch — defines the analytics timeline |
| `instrument_key` | str | Contract identifier |
| `underlying_symbol` | str | Parent underlying |
| `bid` | float? | Best bid (None if unavailable) |
| `ask` | float? | Best ask |
| `last` | float? | Last traded price |
| `mid` | float? | `(bid + ask) / 2` |
| `volume` | float? | Cumulative day volume |
| `open_interest` | float? | Open interest |
| `spread_pct` | float? | `(ask - bid) / mid` |
| `reference_spot` | float? | Underlying spot used for this snapshot |
| `reference_type` | str? | How spot was obtained: `mid`, `last`, `fallback_prev_close` |
| `quote_age_seconds` | float? | Age of most recent constituent tick |
| `is_stale` | bool | True if `quote_age_seconds` exceeds threshold |
| `is_market_open` | bool | True during exchange hours |
| `maturity_years` | float? | ACT/365 time to expiry |
| `session_id` | str | Source collector session |
| `snapshot_version` | str | Default `"1.0"` |

---

## 4. `forward_curve`

**Primary key:** `snapshot_ts`, `underlying`, `expiry_str`  
**Layer:** analytics  
**Purpose:** Put-call parity derived forward rates and carry diagnostics.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_ts` | float | UTC epoch |
| `underlying` | str | E.g. `ESTX50` |
| `expiry_str` | str | ISO date of the expiry slice |
| `maturity_years` | float | ACT/365 time to expiry |
| `chosen_forward` | float | Forward used downstream: `F(T) ≈ K + e^(rT)*(C-P)` |
| `weighted_mean_forward` | float | Inverse-spread-weighted mean across candidates |
| `median_forward` | float | Median across candidates |
| `confidence_score` | float | `[0,1]`: proportion of candidates within 1% of median |
| `candidates_count` | int | Number of strike pairs used |
| `fallback_used` | str | `none`, `previous_close`, `index_level` |
| `implied_carry` | float? | `q(T) = r - (1/T)*ln(F/S0)` |
| `diagnostics_version` | str | Config version |

**Rule:** `fallback_used` is never empty — always records whether a fallback was applied.

---

## 5. `iv_points`

**Primary key:** `snapshot_ts`, `contract_key`  
**Layer:** analytics  
**Purpose:** Per-option implied volatility from the Brent root solver.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_ts` | float | UTC epoch |
| `contract_key` | str | Same as `instrument_key` |
| `underlying` | str | Parent underlying |
| `expiry_str` | str | ISO expiry date |
| `maturity_years` | float | ACT/365 |
| `strike` | float | Option strike |
| `option_right` | str | `C` or `P` |
| `forward` | float | Forward rate used |
| `log_moneyness` | float | `k = ln(K/F)` |
| `market_price` | float | Mid-price input to solver |
| `implied_vol` | float | Solved IV (annualised, ACT/365) |
| `total_variance` | float | `σ² * T` |
| `converged` | bool | True if solver converged within tolerance |
| `solver_residual` | float | Absolute pricing error at solution |
| `iterations` | int | Number of Brent iterations |
| `failure_reason` | str? | `None` if converged; error code otherwise |
| `model_name` | str | E.g. `black_scholes` |
| `solver_version` | str | Config version |

---

## 6. `surface_parameters`

**Primary key:** `snapshot_ts`, `underlying`, `expiry_str`, `model_version`  
**Layer:** analytics  
**Purpose:** Fitted SVI or spline parameters for one expiry slice.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_ts` | float | UTC epoch |
| `underlying` | str | E.g. `ESTX50` |
| `expiry_str` | str | ISO expiry date |
| `maturity_years` | float | ACT/365 |
| `model_name` | str | `svi` or `spline` |
| `model_version` | str | Config version |
| `svi_a` | float? | SVI level parameter; `None` for spline |
| `svi_b` | float? | SVI angle parameter |
| `svi_rho` | float? | SVI skew correlation |
| `svi_m` | float? | SVI ATM log-moneyness |
| `svi_sigma` | float? | SVI curvature |
| `fit_rmse` | float | RMS fit error across accepted IV points |
| `fit_max_error` | float | Maximum absolute fit error |
| `n_accepted_points` | int | Number of IV points used in fit |
| `quality_flag` | str | `ok`, `caution`, `failed` |

**Rule:** Both SVI params AND grid are stored — params for archival, grid for operations.

---

## 7. `surface_grid`

**Primary key:** `snapshot_ts`, `underlying`, `expiry_str`, `log_moneyness`  
**Layer:** analytics  
**Purpose:** Regularised IV grid evaluated on a fixed moneyness ladder — operations input.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_ts` | float | UTC epoch |
| `underlying` | str | E.g. `ESTX50` |
| `expiry_str` | str | ISO expiry date |
| `maturity_years` | float | ACT/365 |
| `log_moneyness` | float | `k = ln(K/F)` on standardised grid |
| `total_variance` | float | `w(k,T) = σ²(k)*T`; must be non-decreasing in T |
| `implied_vol` | float | `σ_imp = sqrt(w/T)` |
| `model_name` | str | E.g. `svi` or `spline` |
| `model_version` | str | Config version |

---

## 8. `pricing_results`

**Primary key:** `snapshot_ts`, `contract_key`, `pricer_version`  
**Layer:** analytics  
**Purpose:** Model prices and Greeks for all live option positions.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_ts` | float | UTC epoch |
| `contract_key` | str | Identifies the option |
| `underlying` | str | Parent underlying |
| `pricer_name` | str | `black_scholes` (European) or `crr_binomial` (American) |
| `pricer_version` | str | Config version |
| `model_price` | float | Theoretical price |
| `delta` | float | `∂V/∂S` (raw, per-option) |
| `gamma` | float | `∂²V/∂S²` |
| `vega_per_point` | float | `∂V/∂σ` per 1 vol-point |
| `theta_per_day` | float | `∂V/∂t` per calendar day |
| `dollar_gamma` | float | `gamma * S² * quantity * multiplier` |
| `dollar_vega` | float | `vega_per_point * quantity * multiplier` |
| `forward_used` | float | Forward rate input |
| `sigma_used` | float | IV input from surface grid |

---

## 9. `positions`

**Primary key:** `valuation_ts`, `portfolio_id`, `contract_key`  
**Layer:** analytics  
**Purpose:** Signed position quantities at a point in time.

| Column | Type | Description |
|--------|------|-------------|
| `valuation_ts` | float | UTC epoch |
| `portfolio_id` | str | E.g. `STRADDLE_PAPER` |
| `contract_key` | str | Option or underlying identifier |
| `quantity` | float | Signed: positive = long, negative = short |
| `avg_cost` | float? | Average fill price (None if not tracked) |
| `currency` | str | E.g. `EUR` |
| `position_source` | str | `broker`, `manual`, or `hypothetical` |

---

## 10. `risk_aggregates`

**Primary key:** `valuation_ts`, `portfolio_id`, `group_key`  
**Layer:** analytics  
**Purpose:** Summed dollar Greeks by portfolio or underlying.

| Column | Type | Description |
|--------|------|-------------|
| `valuation_ts` | float | UTC epoch |
| `portfolio_id` | str | Portfolio identifier |
| `group_key` | str | Grouping dimension (e.g. `underlying_symbol`) |
| `net_delta` | float | Sum of raw deltas across positions |
| `net_gamma` | float | Sum of raw gammas |
| `net_vega` | float | Sum of raw vegas |
| `net_theta` | float | Sum of raw thetas |
| `net_dollar_delta` | float | `Σ delta * S * qty * mult` |
| `net_dollar_gamma` | float | `Σ gamma * S² * qty * mult` |
| `net_dollar_vega` | float | `Σ vega_per_point * qty * mult` |
| `net_market_value` | float | `Σ model_price * qty * mult` (signed) |
| `position_count` | int | Number of positions aggregated |
| `analytics_version` | str | Config version |
| `snapshot_ts_used` | float | Source snapshot timestamp |

---

## 11. `scenario_results`

**Primary key:** `valuation_ts`, `portfolio_id`, `scenario_id`, `contract_key`  
**Layer:** analytics  
**Purpose:** Per-position stress PnL for each named scenario.

| Column | Type | Description |
|--------|------|-------------|
| `valuation_ts` | float | UTC epoch |
| `portfolio_id` | str | Portfolio identifier |
| `scenario_id` | str | E.g. `spot_dn_10pct_vol_up_5pts` |
| `scenario_version` | str | Scenario YAML version |
| `contract_key` | str | Option identifier |
| `base_price` | float | Model price before scenario |
| `stressed_price` | float | Model price after scenario |
| `pnl` | float | `(stressed - base) * qty * mult` |
| `method` | str | `full_reprice` or `greek_approx` |
| `analytics_version` | str | Config version |
| `snapshot_ts_used` | float | Source snapshot timestamp |

**UAM scenarios** (prefix `uam_`): spot ±5% × vol ±20%, evaluated via Greek approximation.

---

## 12. `qc_results`

**Primary key:** `run_id`, `check_name`, `target_key`  
**Layer:** analytics  
**Purpose:** Named validation check outcomes from the daily QC report.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | str | Job run UUID |
| `check_name` | str | E.g. `check_underlying_quote_health` |
| `target_key` | str | Underlying, contract_key, or expiry label |
| `qc_status` | str | `pass`, `warn`, or `fail` |
| `reason_code` | str | Non-empty for non-pass rows (e.g. `HIGH_SPREAD_PCT`) |
| `measured_value` | float? | The metric that was checked |
| `threshold` | float? | The threshold it was checked against |
| `severity` | str | `info`, `warn`, or `critical` |
| `run_ts` | float | UTC epoch of the QC run |
| `threshold_version` | str | `configs/qc.yaml` version |
| `context_json` | str | JSON blob of additional diagnostics |

**Rule:** `reason_code` is never empty when `qc_status != "pass"`.

---

## Appendix: Partition Path Conventions

```
data/
  raw/
    dt=2025-06-01/
      session=<uuid>/events.jsonl        ← raw ticks (immutable, append-only)

  analytics/
    instrument_master/dt=2025-06-01/v=1.0/data.parquet
    market_state_snapshots/dt=2025-06-01/underlying=ESTX50/data.parquet
    forward_curve/dt=2025-06-01/underlying=ESTX50/v=1.0/data.parquet
    iv_points/dt=2025-06-01/underlying=ESTX50/v=1.0/data.parquet
    surface_parameters/dt=2025-06-01/underlying=ESTX50/v=1.0/data.parquet
    surface_grid/dt=2025-06-01/underlying=ESTX50/v=1.0/data.parquet
    pricing_results/dt=2025-06-01/v=1.0/data.parquet
    risk_aggregates/dt=2025-06-01/v=1.0/data.parquet
    scenario_results/dt=2025-06-01/v=1.0/data.parquet
    qc_results/dt=2025-06-01/v=<run_id>/data.parquet

  manifests/
    run_2025-06-01_eod_001.json          ← job run manifest

metadata.db                              ← SQLite lineage + write log (dev)
```

**Replay versioning:** `analytics/v=<code_version>/dt=<YYYY-MM-DD>/` — new partition per
code version; prior versions never overwritten.
