# forwards

Put-call parity forward estimation from live option chains.
Uses liquidity-weighted aggregation across strikes, with outlier rejection
and PCHIP interpolation for maturities that lack sufficient quotes.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `estimate_forward_curve(snapshot, maturities, rate, config)` | `engine.py` | Main entry — returns `list[ForwardResult]`, one per maturity |
| `estimate_forward(calls, puts, spot, rate, maturity, config)` | `engine.py` | Single-maturity forward from matched call/put pairs |
| `compute_carry_diagnostics(fwd_result, spot, rate)` | `engine.py` | Returns `CarryDiagnostics` with implied carry, dividend yield, basis |
| `ForwardResult` | `models.py` | Forward price, confidence score, number of contributing pairs, fallback flag |
| `CarryDiagnostics` | `models.py` | Implied carry and dividend yield derived from the forward |

## Failure modes

- `ForwardResult.is_fallback=True` means fewer than `min_pairs` (default 3) put-call pairs were usable; the forward was extrapolated from neighbouring maturities.
- Confidence score below 0.4 indicates high spread or few pairs — downstream IV solve will weight these points lower.
- Returns an empty list when the snapshot has no option rows; callers must handle this before writing `ForwardCurveRow` records.
