# orchestration

EOD pipeline execution, incremental analytics, job scheduling,
and historical replay. All work is expressed as pure `job_*` functions
that receive `JobRunContext`, a reader, and a writer — no global state.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `job_eod_pipeline(run, reader, writer, metrics)` | `jobs.py` | Runs the full 8-step EOD sequence (snapshots → forwards → IV → surface → Greeks → scenarios → QC) |
| `job_build_snapshots` / `job_build_forwards` / `job_solve_iv` / `job_fit_surfaces` / `job_compute_greeks` / `job_run_scenarios` / `job_run_qc` | `jobs.py` | Individual pipeline steps; each is idempotent via `check_idempotency` |
| `job_live_collect` | `jobs.py` | Blocking data collection loop; streams raw events via `RawWriter` |
| `job_universe_refresh` | `jobs.py` | Refreshes option universe from broker and writes `InstrumentMasterRow` records |
| `JobRunContext` | `jobs.py` | Carries `session_id`, `trade_date`, `run_id`, `config` |
| `replay_day(trade_date, config)` | `replay.py` | Re-runs the EOD pipeline on stored raw events for a historical date |
| `replay_date_range(start, end, config)` | `replay.py` | Loops `replay_day` over a date range |
| `should_run` / `record_job_result` | `scheduler.py` | Pure scheduling logic; no threads — the caller drives the tick loop |

## Failure modes

- `check_idempotency` returns `True` (already done) when a matching key exists in storage; call at the top of each job to prevent double-writes.
- Jobs do not retry on failure — the caller (scheduler or CI) is responsible for retry/backoff.
- `job_live_collect` must be cancelled via `KeyboardInterrupt` or the `duration_seconds` config key; it does not self-terminate otherwise.
