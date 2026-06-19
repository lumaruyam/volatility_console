# pricing

Closed-form and lattice option pricers with full Greek output.
All pricers are stateless pure functions — safe to call from any thread.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `price_european(inputs: EuropeanInputs)` | `european.py` | Black-Scholes-Merton; returns `PricingResult` with price + full Greeks |
| `price_black76(inputs: Black76Inputs)` | `european.py` | Black-76 for futures options (no dividend carry) |
| `local_pnl_approximation(pos_risk, dS, d_sigma)` | `european.py` | Taylor expansion PnL: Δ·dS + ½Γ·dS² + ν·dσ |
| `price_american(inputs: AmericanInputs)` | `american.py` | CRR binomial tree; early-exercise premium over Black-Scholes |
| `EuropeanInputs` / `AmericanInputs` / `Black76Inputs` | `european.py`, `american.py` | Typed input dataclasses: `S, K, T, r, q, sigma, option_type` |
| `PricingResult` | `models.py` | `price`, `delta`, `gamma`, `vega`, `theta`, `rho` |

## Failure modes

- `T = 0` (expiry today) is handled by returning intrinsic value with `delta = ±1` and all other Greeks `= 0`.
- `sigma <= 0` raises `ValueError` — IV solver should never pass a non-positive vol; guard at call site.
- `price_american` runtime scales as O(steps²); keep `steps` ≤ 200 for latency-sensitive paths.
