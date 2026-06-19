# qc

Two-layer quality control: real-time quote filtering during collection
and end-of-day pipeline validation across all analytics outputs.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `run_quote_qc(quote, config)` | `quote_filter.py` | Real-time check on a single quote; returns `QuoteQCOutcome` with `accepted` flag and reason list |
| `filter_chain(quotes, config)` | `quote_filter.py` | Batch filter; returns `(accepted, rejected)` lists |
| `run_daily_qc(trade_date, underlying, run_id, all_data, config, expected_scenarios)` | `validation.py` | Runs all 8 daily checks; returns `DailyQCReport` |
| `build_triage_table(reports)` | `validation.py` | Flattens a list of `DailyQCReport` into a list of dicts for the API / storage |
| `detect_anomaly(series, config)` | `anomaly.py` | Z-score based spike detection on a numeric time series |
| `run_anomaly_detection(all_series, config)` | `anomaly.py` | Runs `detect_anomaly` across all series in a dict |
| `DailyQCReport` / `ValidationCheckResult` | `validation.py` | Result types; `check.status` is `"pass"`, `"warn"`, or `"fail"` |

## Daily checks (in order)

`collector_continuity` → `underlying_quote_health` → `iv_solver_convergence` →
`forward_stability` → `calendar_sanity` → `surface_fit_error` → `greek_sanity` → `scenario_completeness`

## Failure modes

- `run_daily_qc` never raises; all check failures are captured in `DailyQCReport.checks` — callers do not need a try/except.
- `detect_anomaly` requires at least 5 data points for a meaningful z-score; returns an empty list for shorter series.
- `store_rejected_outcomes` in `quote_filter.py` is a side-effect write — do not call during replay (read-only mode).
