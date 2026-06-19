# risk

Position-level risk aggregation, scenario analysis, UAM stress testing,
historical VaR, and PnL attribution.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `compute_position_risk(pos, analytics_snapshot, pricer, config)` | `aggregation.py` | Full-reprice Greeks for one position; returns `PositionRisk` |
| `aggregate_risk(position_risks, group_keys)` | `aggregation.py` | Sums `PositionRisk` rows into `RiskAggregates` by group (portfolio, underlying, …) |
| `reconcile_with_broker_greeks(position_risks, broker_rows)` | `aggregation.py` | Delta diff check between model and broker-reported Greeks |
| `run_scenario_grid(scenarios, positions, analytics_snapshots, pricer, config, method)` | `scenarios.py` | Runs all `Scenario` objects; returns `list[ScenarioResult]` |
| `load_scenarios_from_config(config)` | `scenarios.py` | Deserialises scenario definitions from `run.config["scenarios"]` |
| `compute_uam(position_risks, config)` | `uam.py` | UAM ratio + worst-case PnL across ±5% spot / ±30% vol grid |
| `compute_historical_var(ticker, portfolio_value, window_days)` | `var.py` | Historical-simulation VaR at 95% and 99%; fetches returns via yfinance |
| `compute_correlation(tickers)` | `correlation.py` | Pearson return correlation matrix (252-day window) |

## Analytics snapshot dict keys

`S`, `K`, `T`, `r`, `q`, `sigma`, `option_type`, `multiplier`, `forward`, `snapshot_ts`

## Failure modes

- `compute_position_risk` propagates the pricer's `ValueError` for non-positive vol — ensure all `sigma` values in the analytics snapshot are positive before calling.
- `run_scenario_grid` with `method="full_reprice"` can be slow for large books (O(positions × scenarios)); use `method="greek_approx"` for interactive endpoints.
- `compute_historical_var` returns a fallback result when yfinance fetch fails (no internet or ticker not found).
