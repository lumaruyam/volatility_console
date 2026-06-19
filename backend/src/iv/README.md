# iv

Implied volatility solving via Brent's method with Newton-Raphson warm-start.
Handles European options directly and American options via a binomial-tree proxy.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `solve_iv_batch(records, config)` | `solver.py` | Vectorised entry point — list of input dicts → `list[IvSolveResult]` |
| `solve_iv(market_price, inputs, config)` | `solver.py` | Single-contract solve; returns `IvSolveResult` with `converged` flag |
| `bs_price(S, K, T, r, q, sigma, option_type)` | `solver.py` | Black-Scholes price |
| `bs_vega(S, K, T, r, q, sigma)` | `solver.py` | Analytic vega (used internally for Newton step) |
| `log_moneyness(K, forward)` | `solver.py` | `log(K/F)` — standard SVI x-axis |
| `total_variance(iv, T)` | `solver.py` | `iv² × T` — standard SVI y-axis |
| `IvSolveResult` | `models.py` | `implied_vol`, `converged`, `iterations`, `residual`, `qc_status` |

## Input record keys for `solve_iv_batch`

`market_price`, `S`, `K`, `T`, `r`, `q`, `option_type` (`"C"`/`"P"`), `contract_key`, `snapshot_ts`

## Failure modes

- `converged=False` on deep ITM or deep OTM options where vega collapses; treat `implied_vol=None` from these as unusable.
- `T <= 0` (expired options) raises immediately — filter expiries before calling.
- Bid-ask midpoint below intrinsic value returns `converged=False`; the `qc_status` field will be `"caution"` or `"reject"`.
