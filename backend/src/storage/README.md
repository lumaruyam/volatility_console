# storage

Parquet-backed read/write layer for all analytics artefacts.
`StorageWriter` is append-safe and validates each row before write.
`StorageReader` is read-only and returns plain dicts (not dataclasses).

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `StorageWriter` | `writer.py` | Batch writer; key methods: `write_snapshots`, `write_forward_curve`, `write_iv_points`, `write_surface_parameters`, `write_surface_grid`, `write_pricing_results`, `write_risk_aggregates`, `write_scenario_results`, `write_qc_results`, `write_manifest`, `write_lineage` |
| `StorageReader` | `reader.py` | Batch reader; key methods: `read_raw_events`, `read_snapshots`, `read_forward_curve`, `read_iv_points`, `read_surface_parameters`, `read_pricing_results`, `read_positions`, `read_scenario_results`, `list_partitions` |
| Row dataclasses | `schemas.py` | `MarketStateSnapshotRow`, `ForwardCurveRow`, `IVPointRow`, `SurfaceParametersRow`, `SurfaceGridRow`, `PricingResultRow`, `RiskAggregateRow`, `ScenarioResultRow`, `QCResultRow` — documentation of column names and types |

## Partition layout

`{storage_root}/{table}/{trade_date}/{underlying}/data.parquet`

## Failure modes

- `StorageWriter` raises `ValueError` on the first invalid row (e.g. `NaN` in a non-nullable field) before writing anything in the batch.
- `StorageReader` returns an empty list (not `None`) when a partition does not exist — always guard with `if not rows:`.
- Row dataclasses in `schemas.py` are documentation only; they are not validated at runtime. The writer's internal `_validate_*` functions enforce the constraints.
- Concurrent writes to the same partition from multiple processes are not safe; the EOD pipeline is designed to run as a single process per trade date.
