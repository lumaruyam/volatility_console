# surfaces

SVI (Stochastic Volatility Inspired) surface calibration per maturity slice,
with PCHIP spline fallback for illiquid expiries. Produces a `SurfaceFitResult`
that can be queried at arbitrary log-moneyness and maturity.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `fit_surface(iv_points, config, underlying, snapshot_ts)` | `calibration.py` | Entry point — groups points by expiry, fits each slice, returns `SurfaceFitResult` |
| `fit_slice(points, expiry_str, config)` | `calibration.py` | Single-expiry SVI fit; falls back to PCHIP when fewer than `min_points_per_slice` |
| `check_calendar_monotonicity(slices)` | `calibration.py` | Verifies total variance is non-decreasing across maturities; used by QC |
| `evaluate_slice_variance(slice_result, k)` | `interpolation.py` | Query fitted variance at log-moneyness `k` |
| `interpolate_surface_at(surface, K, T, S)` | `interpolation.py` | Bilinear interpolation across slices for arbitrary (K, T) |
| `get_atm_vol(ticker, expiry)` | `atm_vol.py` | Live ATM vol via yfinance (falls back to realised vol on fetch failure) |
| `SurfaceFitResult` | `models.py` | List of `SliceFitResult`; `SliceFitResult` carries `SVIParameters` + RMSE + fallback flag |

## Failure modes

- `SliceFitResult.is_fallback=True` means SVI L-BFGS-B did not converge; PCHIP spline was used instead — the slice cannot be used for calendar-arbitrage checks.
- `fit_surface` skips slices with fewer than `config["min_points_per_slice"]` (default 5) points; check `len(surface.slices)` vs expected maturities.
- `interpolate_surface_at` returns `None` for maturities outside the fitted range — extrapolation is deliberately disabled.
