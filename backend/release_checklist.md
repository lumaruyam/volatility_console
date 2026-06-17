# Release Checklist

Use this checklist before merging to `main` or tagging a production release.
All items must be checked; document any exceptions in the PR description.

---

## 1. Code Quality

- [ ] All unit tests pass with zero failures
  ```bash
  python -m pytest tests/ -v
  ```
- [ ] No new `# noqa`, `# type: ignore`, or `pass` stubs left in src/
- [ ] No hardcoded secrets, credentials, or host addresses in source or config YAML
- [ ] `.env` is in `.gitignore` and not staged for commit
- [ ] All new config thresholds are in versioned YAML (not hardcoded in Python)

## 2. Config Versioning

- [ ] Any changed `configs/*.yaml` file has its `version:` field incremented
- [ ] `src/__version__` (or `src/__init__.py`) reflects the new release version
- [ ] `configs/strategy.yaml` — `roll_dte_days`, `atm_delta_target`, `sizing_mode` reviewed
- [ ] `configs/scenarios.yaml` — scenario grid matches professor requirements (UAM ±5% spot / ±20% vol)
- [ ] `configs/qc.yaml` — thresholds reviewed; `threshold_version` updated if changed

## 3. Security

- [ ] No `VOL_INFRA_IBKR__*` secrets in any committed file
- [ ] `read_only: false` only when intentional paper-trading execution is desired
- [ ] New broker callbacks do **not** compute analytics (callbacks: normalize → stamp → persist only)
- [ ] All rejected quotes have a non-empty `reason_code` (never silently discarded)
- [ ] All fallback paths set `reference_type` / `fallback_used` (never hidden)

## 4. Data Integrity

- [ ] Replay uses the same code path as live (`job_eod_pipeline` called from both)
- [ ] New analytics tables reference `snapshot_ts` for lineage
- [ ] No partition-overwrite — new versions write to new `v=<version>` paths
- [ ] `idempotency_key` computed as `SHA-256(run_id:job_name:trade_date:code_version)[:16]`
- [ ] `partition_path(base, version, date)` is the single canonical path builder

## 5. Test Coverage

- [ ] New public functions have unit tests (happy path + at least one edge case)
- [ ] New QC check functions have pass / warn / fail test cases
- [ ] Acceptance criterion tests pass for each affected step
- [ ] No tests use constant baselines for anomaly detection (MAD=0 defeats the z-score)

## 6. Operational Readiness

- [ ] Bootstrap smoke test passes in mock mode
  ```bash
  python scripts/bootstrap_smoke_test.py --mock
  ```
- [ ] `RUNBOOKS.md` updated if any operational procedure changed
- [ ] `docs/schemas.md` updated if any table schema changed
- [ ] `docs/environment.md` updated if new env vars or setup steps are required
- [ ] `docs/limitations.md` updated if new known limitations are introduced

## 7. Dashboard

- [ ] `streamlit run src/dashboard/app.py` loads without error from mock storage
- [ ] All six dashboard tabs render without exceptions
- [ ] UAM gauge displays at ratios 0.0, 0.5, 1.0, 1.5 without layout breakage

## 8. Replay Validation (required for any analytics change)

- [ ] Replay of a recent trading day produces `status = "ok"`
- [ ] `compare_replay_vs_live` shows zero `differing_keys` when code version is unchanged
- [ ] Intentional divergence (model fix) is documented in the commit message

## 9. Final Sign-off

- [ ] PR reviewed and approved
- [ ] All CI checks green
- [ ] Release notes updated (or PR description is the release note)
- [ ] Tagged: `git tag -a vX.Y.Z -m "Release vX.Y.Z — <summary>"`

---

## Quick Smoke Sequence (copy-paste)

```bash
# 1. Tests
python -m pytest tests/ -q

# 2. Bootstrap (no IBKR needed)
python scripts/bootstrap_smoke_test.py --mock

# 3. Config sanity
python -c "from src.utils.config import load_config; load_config()"

# 4. Dashboard import
python -c "import src.dashboard.plots; import src.dashboard.app; print('dashboard OK')"

# 5. Strategy sanity
python -c "
from src.strategy.straddle import compute_position_size
qty = compute_position_size(5000.0, 0.20, {'notional': 100000, 'multiplier': 10})
print('qty:', qty)  # expected: 2.0
"
```

All five commands must exit 0 before tagging a release.
