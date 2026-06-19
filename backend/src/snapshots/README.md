# snapshots

Converts a stream of raw tick events into a single consistent `MarketStateSnapshot`
at a chosen timestamp, one per underlying per session window.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `build_snapshot(events, snapshot_ts, config)` | `builder.py` | Entry point — takes `list[RawEvent]`, returns `MarketStateSnapshot` |
| `MarketStateSnapshot` | `models.py` | Top-level result: `underlying_state`, `option_rows`, `snapshot_ts`, `snapshot_version` |
| `UnderlyingState` | `models.py` | Bid/ask/last/mid, reference spot, staleness flags |
| `OptionRow` | `models.py` | Per-contract: strike, expiry, bid/ask/mid, `maturity_years`, `spread_pct` |

## Configuration keys (`config` dict)

- `stale_quote_threshold_seconds` — marks a quote stale if its age exceeds this (default 60 s).
- `min_spread_pct` — lower bound on spread; quotes below this are flagged suspicious.

## Failure modes

- Returns a snapshot with `option_rows=[]` when no option events are present for an underlying — always check `len(snapshot.option_rows)` before downstream IV solving.
- `build_snapshot` requires `list[RawEvent]` objects, not raw dicts; deserialise with `RawEvent.from_dict()` first.
- Reference spot falls back to last trade price if both bid and ask are `None`; `reference_type` on `UnderlyingState` records which source was used.
