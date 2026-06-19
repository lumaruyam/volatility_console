# analytics

Portfolio-level analytics that span multiple underlyings.
Currently: basket variance decomposition (PDF Part II Eq. 23).

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `compute_basket_variance(weights, vols, corr_matrix, avg_corr, index_atm_vol)` | `basket_variance.py` | Implements `σ²_basket = Σᵢⱼ wᵢ·wⱼ·σᵢ·σⱼ·ρᵢⱼ`; returns `BasketVarianceResult` |
| `BasketVarianceResult` | `basket_variance.py` | Frozen dataclass: `basket_variance`, `basket_vol`, `weighted_component_vars`, `residual_vs_atm`, `avg_corr_used`, `n_constituents` |

## Usage

```python
from src.analytics.basket_variance import compute_basket_variance

# Scalar average correlation
result = compute_basket_variance(weights=[0.6, 0.4], vols=[0.20, 0.25], avg_corr=0.55)

# Full correlation matrix
result = compute_basket_variance(weights=[0.6, 0.4], vols=[0.20, 0.25],
                                  corr_matrix=[[1.0, 0.6], [0.6, 1.0]],
                                  index_atm_vol=0.18)
print(result.residual_vs_atm)  # dispersion premium
```

## Failure modes

- Raises `ValueError` if `len(weights) != len(vols)` or `corr_matrix` is not n×n.
- `corr_matrix` and `avg_corr` are mutually exclusive by convention; if both are provided, `corr_matrix` takes precedence and `avg_corr` is ignored.
- Negative basket variance (from a pathological correlation matrix) is floored to `0.0` before taking the square root.
