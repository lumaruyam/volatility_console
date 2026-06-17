"""
Unit tests for pricing engine.

Covers: BS identities, Greeks signs, put-call parity, limiting cases,
American converges to European when no early exercise.
"""

import math
import pytest

from src.pricing.european import EuropeanInputs, price_european
from src.pricing.american import AmericanInputs, price_american
from src.iv.solver import bs_price, solve_iv, PricingInputs


class TestBlackScholes:

    def test_put_call_parity(self):
        """C - P = S*e^(-qT) - K*e^(-rT)"""
        S, K, T, r, q, sigma = 100.0, 100.0, 1.0, 0.05, 0.02, 0.20
        call = bs_price(S, K, T, r, q, sigma, "C")
        put = bs_price(S, K, T, r, q, sigma, "P")
        lhs = call - put
        rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
        assert abs(lhs - rhs) < 1e-8, f"Put-call parity violated: {lhs} != {rhs}"

    def test_call_above_intrinsic(self):
        """Call price >= max(S*e^(-qT) - K*e^(-rT), 0)"""
        S, K, T, r, q, sigma = 100.0, 90.0, 0.5, 0.05, 0.0, 0.25
        call = bs_price(S, K, T, r, q, sigma, "C")
        intrinsic = max(S - K * math.exp(-r * T), 0.0)
        assert call >= intrinsic - 1e-8

    def test_put_above_intrinsic(self):
        S, K, T, r, q, sigma = 100.0, 110.0, 0.5, 0.05, 0.0, 0.25
        put = bs_price(S, K, T, r, q, sigma, "P")
        intrinsic = max(K * math.exp(-r * T) - S, 0.0)
        assert put >= intrinsic - 1e-8

    def test_deep_itm_call_approaches_forward(self):
        """Deep ITM call approaches S*e^(-qT) - K*e^(-rT) as sigma → 0"""
        S, K, T, r, q = 150.0, 50.0, 1.0, 0.05, 0.0
        call = bs_price(S, K, T, r, q, 0.001, "C")
        expected = S - K * math.exp(-r * T)
        assert abs(call - expected) < 0.01

    def test_zero_time_returns_intrinsic_call(self):
        S, K = 105.0, 100.0
        price = bs_price(S, K, 0.0, 0.05, 0.0, 0.20, "C")
        assert price == pytest.approx(max(S - K, 0.0), abs=1e-8)

    def test_zero_time_returns_intrinsic_put(self):
        S, K = 95.0, 100.0
        price = bs_price(S, K, 0.0, 0.05, 0.0, 0.20, "P")
        assert price == pytest.approx(max(K - S, 0.0), abs=1e-8)

    def test_invalid_option_type(self):
        with pytest.raises(ValueError, match="option_type"):
            bs_price(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, "X")


class TestEuropeanGreeks:

    def setup_method(self):
        self.base = EuropeanInputs(S=100.0, K=100.0, T=1.0, r=0.05, q=0.02,
                                    sigma=0.20, option_type="C")

    def test_call_delta_in_range(self):
        result = price_european(self.base)
        assert 0.0 <= result.delta <= 1.0

    def test_put_delta_in_range(self):
        put_inputs = EuropeanInputs(**{**self.base.__dict__, "option_type": "P"})
        result = price_european(put_inputs)
        assert -1.0 <= result.delta <= 0.0

    def test_gamma_positive(self):
        result = price_european(self.base)
        assert result.gamma > 0.0

    def test_vega_positive(self):
        result = price_european(self.base)
        assert result.vega > 0.0

    def test_dollar_gamma_formula(self):
        """DollarGamma = Γ * S² * multiplier"""
        result = price_european(self.base)
        expected = result.gamma * self.base.S ** 2 * self.base.multiplier
        assert abs(result.dollar_gamma - expected) < 1e-8

    def test_finite_difference_delta(self):
        """Analytic delta should match central-difference estimate within tolerance."""
        dS = 0.01
        up = EuropeanInputs(**{**self.base.__dict__, "S": self.base.S + dS})
        dn = EuropeanInputs(**{**self.base.__dict__, "S": self.base.S - dS})
        fd_delta = (price_european(up).price - price_european(dn).price) / (2 * dS)
        analytic_delta = price_european(self.base).delta
        assert abs(fd_delta - analytic_delta) < 0.001


class TestIVSolver:

    def _make_config(self):
        return {"lower_vol": 0.0001, "upper_vol": 5.0, "price_tolerance": 1e-6, "max_iterations": 100}

    def test_round_trip_atm_call(self):
        """solve_iv(bs_price(sigma)) should return sigma."""
        S, K, T, r, q, sigma = 100.0, 100.0, 1.0, 0.05, 0.02, 0.25
        price = bs_price(S, K, T, r, q, sigma, "C")
        inputs = PricingInputs(S=S, K=K, T=T, r=r, q=q, option_type="C")
        result = solve_iv(price, inputs, self._make_config(), contract_key="test")
        assert result.converged
        assert abs(result.implied_vol - sigma) < 1e-4

    def test_round_trip_otm_put(self):
        S, K, T, r, q, sigma = 100.0, 90.0, 0.5, 0.05, 0.0, 0.30
        price = bs_price(S, K, T, r, q, sigma, "P")
        inputs = PricingInputs(S=S, K=K, T=T, r=r, q=q, option_type="P")
        result = solve_iv(price, inputs, self._make_config())
        assert result.converged
        assert abs(result.implied_vol - sigma) < 1e-4

    def test_below_intrinsic_returns_failure(self):
        """Price below intrinsic should return structured failure, not raise."""
        inputs = PricingInputs(S=110.0, K=100.0, T=1.0, r=0.05, q=0.0, option_type="C")
        result = solve_iv(0.5, inputs, self._make_config())  # price < intrinsic of 10
        assert not result.converged
        assert result.failure_reason == "BELOW_INTRINSIC"
        assert result.implied_vol is None


class TestRho:

    def setup_method(self):
        self.call_inputs = EuropeanInputs(S=100.0, K=100.0, T=1.0, r=0.05, q=0.02,
                                          sigma=0.20, option_type="C", multiplier=100.0)
        self.put_inputs = EuropeanInputs(S=100.0, K=100.0, T=1.0, r=0.05, q=0.02,
                                         sigma=0.20, option_type="P", multiplier=100.0)

    def test_call_rho_positive(self):
        """Call rho > 0: higher rates increase call value."""
        result = price_european(self.call_inputs)
        assert result.rho > 0.0

    def test_put_rho_negative(self):
        """Put rho < 0: higher rates decrease put value."""
        result = price_european(self.put_inputs)
        assert result.rho < 0.0

    def test_rho_matches_bs_formula(self):
        """rho = K·T·e^(−rT)·N(d2) for calls."""
        import math
        from scipy.stats import norm
        inp = self.call_inputs
        sqrt_T = math.sqrt(inp.T)
        d1 = (math.log(inp.S / inp.K) + (inp.r - inp.q + 0.5 * inp.sigma ** 2) * inp.T) / (inp.sigma * sqrt_T)
        d2 = d1 - inp.sigma * sqrt_T
        expected = inp.K * inp.T * math.exp(-inp.r * inp.T) * norm.cdf(d2)
        result = price_european(inp)
        assert abs(result.rho - expected) < 1e-10

    def test_dollar_rho_formula(self):
        """dollar_rho = rho * 0.0001 * multiplier"""
        result = price_european(self.call_inputs)
        expected = result.rho * 0.0001 * self.call_inputs.multiplier
        assert abs(result.dollar_rho - expected) < 1e-10

    def test_rho_zero_at_expiry(self):
        """At T=0, rho should be 0 (no sensitivity to rates at expiry)."""
        for otype in ("C", "P"):
            inp = EuropeanInputs(S=100.0, K=100.0, T=0.0, r=0.05, q=0.02,
                                 sigma=0.20, option_type=otype)
            result = price_european(inp)
            assert result.rho == 0.0
            assert result.dollar_rho == 0.0

    def test_call_rho_finite_difference(self):
        """Analytic rho should match bump-and-reprice within 1e-4."""
        dr = 1e-4
        inp = self.call_inputs
        up = EuropeanInputs(**{**inp.__dict__, "r": inp.r + dr})
        dn = EuropeanInputs(**{**inp.__dict__, "r": inp.r - dr})
        fd_rho = (price_european(up).price - price_european(dn).price) / (2 * dr)
        analytic_rho = price_european(inp).rho
        assert abs(fd_rho - analytic_rho) < 1e-4


class TestAmericanPricer:

    def test_american_call_no_dividend_equals_european(self):
        """
        American call on non-dividend-paying stock = European call.
        (Never optimal to exercise early when q=0.)
        """
        S, K, T, r, q, sigma = 100.0, 100.0, 1.0, 0.05, 0.0, 0.20
        american = price_american(AmericanInputs(S=S, K=K, T=T, r=r, q=q,
                                                  sigma=sigma, option_type="C"))
        european = price_european(EuropeanInputs(S=S, K=K, T=T, r=r, q=q,
                                                  sigma=sigma, option_type="C"))
        # Should be very close (within numerical tolerance of the tree)
        assert abs(american.price - european.price) < 0.05

    def test_american_put_ge_european_put(self):
        """American put >= European put (early exercise premium is non-negative)."""
        S, K, T, r, q, sigma = 100.0, 110.0, 1.0, 0.05, 0.0, 0.20
        american = price_american(AmericanInputs(S=S, K=K, T=T, r=r, q=q,
                                                  sigma=sigma, option_type="P"))
        european = price_european(EuropeanInputs(S=S, K=K, T=T, r=r, q=q,
                                                  sigma=sigma, option_type="P"))
        assert american.price >= european.price - 1e-4
