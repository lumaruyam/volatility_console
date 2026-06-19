"""Unit tests for basket variance identity — PDF Part II Eq. 23."""

from __future__ import annotations

import math
import pytest

from src.analytics.basket_variance import BasketVarianceResult, compute_basket_variance


class TestBasketVarianceIdentity:
    def test_returns_dataclass(self):
        r = compute_basket_variance([0.5, 0.5], [0.2, 0.3])
        assert isinstance(r, BasketVarianceResult)

    def test_perfect_correlation_equals_weighted_average_vol(self):
        # rho=1 everywhere → basket_vol = sum(w_i * sigma_i)
        r = compute_basket_variance([0.5, 0.5], [0.2, 0.3], avg_corr=1.0)
        assert r.basket_vol == pytest.approx(0.25, rel=1e-9)

    def test_zero_correlation_sum_of_squares(self):
        # rho=0 → basket_var = sum(w_i^2 * sigma_i^2)
        r = compute_basket_variance([0.5, 0.5], [0.2, 0.3], avg_corr=0.0)
        expected_var = 0.25 * 0.04 + 0.25 * 0.09  # = 0.0325
        assert r.basket_variance == pytest.approx(expected_var, rel=1e-9)
        assert r.basket_vol == pytest.approx(math.sqrt(expected_var), rel=1e-9)

    def test_default_avg_corr_is_05(self):
        r1 = compute_basket_variance([0.6, 0.4], [0.20, 0.25])
        r2 = compute_basket_variance([0.6, 0.4], [0.20, 0.25], avg_corr=0.5)
        assert r1.basket_variance == pytest.approx(r2.basket_variance, rel=1e-12)
        assert r1.avg_corr_used == pytest.approx(0.5)

    def test_single_constituent(self):
        # Trivially basket_vol == sigma_1 for a single-stock basket
        r = compute_basket_variance([1.0], [0.30])
        assert r.basket_vol == pytest.approx(0.30, rel=1e-9)
        assert r.n_constituents == 1

    def test_full_corr_matrix(self):
        # [[1, 0.7], [0.7, 1]], w=[0.4, 0.6], sigma=[0.25, 0.20]
        corr = [[1.0, 0.7], [0.7, 1.0]]
        r = compute_basket_variance([0.4, 0.6], [0.25, 0.20], corr_matrix=corr)
        expected_var = (
            0.4 * 0.4 * 0.25 * 0.25 * 1.0
            + 0.4 * 0.6 * 0.25 * 0.20 * 0.7
            + 0.6 * 0.4 * 0.20 * 0.25 * 0.7
            + 0.6 * 0.6 * 0.20 * 0.20 * 1.0
        )
        assert r.basket_variance == pytest.approx(expected_var, rel=1e-9)
        assert r.avg_corr_used == pytest.approx(0.7, rel=1e-9)

    def test_avg_corr_computed_from_matrix(self):
        # Off-diagonal values are 0.3 and 0.9 → avg = 0.6
        corr = [[1.0, 0.3, 0.9], [0.3, 1.0, 0.9], [0.9, 0.9, 1.0]]
        r = compute_basket_variance([1/3, 1/3, 1/3], [0.2, 0.2, 0.2], corr_matrix=corr)
        assert r.avg_corr_used == pytest.approx((0.3 + 0.9 + 0.3 + 0.9 + 0.9 + 0.9) / 6, rel=1e-9)

    def test_residual_vs_atm_with_index_vol(self):
        r = compute_basket_variance([0.5, 0.5], [0.3, 0.3], avg_corr=1.0, index_atm_vol=0.25)
        assert r.residual_vs_atm == pytest.approx(0.30 - 0.25, rel=1e-9)

    def test_residual_vs_atm_zero_when_not_supplied(self):
        r = compute_basket_variance([0.5, 0.5], [0.3, 0.3])
        assert r.residual_vs_atm == pytest.approx(0.0)

    def test_weighted_component_vars_shape(self):
        r = compute_basket_variance([0.4, 0.6], [0.20, 0.25])
        assert len(r.weighted_component_vars) == 2
        assert r.weighted_component_vars[0] == pytest.approx(0.4**2 * 0.20**2, rel=1e-9)
        assert r.weighted_component_vars[1] == pytest.approx(0.6**2 * 0.25**2, rel=1e-9)

    def test_n_constituents(self):
        r = compute_basket_variance([0.2, 0.3, 0.5], [0.2, 0.2, 0.2])
        assert r.n_constituents == 3

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError, match="same length"):
            compute_basket_variance([0.5, 0.5], [0.2])

    def test_wrong_corr_matrix_size_raises(self):
        with pytest.raises(ValueError, match="n×n"):
            compute_basket_variance([0.5, 0.5], [0.2, 0.3], corr_matrix=[[1.0]])

    def test_basket_vol_nonnegative_when_variance_floored(self):
        # Pathological: negative variance should floor to 0
        # Force by passing a corr_matrix that yields near-zero variance
        r = compute_basket_variance([1.0, -1.0], [0.2, 0.2], avg_corr=1.0)
        assert r.basket_vol >= 0.0
