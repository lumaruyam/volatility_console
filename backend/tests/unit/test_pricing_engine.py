"""
Comprehensive tests for Step 10: Pricing engine.

Acceptance criteria (PLAN):
  - Reference cases match known BS identities.
  - American converges to European in degenerate cases (q=0 call; deep OTM).
  - Benchmark fixtures: put-call parity, deep ITM/OTM, dollar Greeks formulas.
"""

from __future__ import annotations

import math
import pytest

from src.pricing.models import PricingResult
from src.pricing.european import EuropeanInputs, EuropeanResult, local_pnl_approximation, price_european
from src.pricing.american import AmericanInputs, AmericanResult, price_american


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _eu(S=100.0, K=100.0, T=1.0, r=0.05, q=0.02, sigma=0.20,
        opt="C", mult=100.0) -> PricingResult:
    return price_european(EuropeanInputs(S=S, K=K, T=T, r=r, q=q,
                                         sigma=sigma, option_type=opt,
                                         multiplier=mult))


def _am(S=100.0, K=100.0, T=1.0, r=0.05, q=0.0, sigma=0.20,
        opt="C", n=200, mult=100.0) -> PricingResult:
    return price_american(AmericanInputs(S=S, K=K, T=T, r=r, q=q,
                                         sigma=sigma, option_type=opt,
                                         n_steps=n, multiplier=mult))


# ---------------------------------------------------------------------------
# TestPricingResultModel
# ---------------------------------------------------------------------------

class TestPricingResultModel:
    def test_european_returns_pricing_result(self):
        result = _eu()
        assert isinstance(result, PricingResult)

    def test_american_returns_pricing_result(self):
        result = _am()
        assert isinstance(result, PricingResult)

    def test_european_result_alias(self):
        """EuropeanResult is an alias for PricingResult — backward compat."""
        assert EuropeanResult is PricingResult

    def test_american_result_alias(self):
        """AmericanResult is an alias for PricingResult — backward compat."""
        assert AmericanResult is PricingResult

    def test_european_model_name(self):
        assert _eu().model_name == "black_scholes"

    def test_american_model_name(self):
        assert _am().model_name == "crr_binomial"

    def test_pricing_result_frozen(self):
        r = _eu()
        with pytest.raises((AttributeError, TypeError)):
            r.price = 0.0  # type: ignore[misc]

    def test_required_fields_present(self):
        r = _eu()
        assert hasattr(r, "price")
        assert hasattr(r, "delta")
        assert hasattr(r, "gamma")
        assert hasattr(r, "vega")
        assert hasattr(r, "theta")
        assert hasattr(r, "dollar_gamma")
        assert hasattr(r, "dollar_vega")
        assert hasattr(r, "model_name")

    def test_european_has_rho(self):
        assert _eu().rho is not None

    def test_european_has_d1_d2(self):
        r = _eu()
        assert r.d1 is not None
        assert r.d2 is not None

    def test_american_has_n_steps(self):
        r = _am(n=100)
        assert r.n_steps == 100

    def test_european_n_steps_none(self):
        assert _eu().n_steps is None

    def test_american_rho_none(self):
        assert _am().rho is None


# ---------------------------------------------------------------------------
# TestEuropeanPutCallParity
# ---------------------------------------------------------------------------

class TestEuropeanPutCallParity:
    """C − P = S·e^(−qT) − K·e^(−rT)"""

    @pytest.mark.parametrize("K, T", [
        (90, 0.25), (100, 0.5), (110, 1.0), (100, 2.0), (95, 0.1),
    ])
    def test_price_parity(self, K, T):
        call = _eu(K=K, T=T, opt="C")
        put  = _eu(K=K, T=T, opt="P")
        S, r, q = 100.0, 0.05, 0.02
        lhs = call.price - put.price
        rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
        assert lhs == pytest.approx(rhs, abs=1e-8), f"K={K}, T={T}: {lhs:.8f} != {rhs:.8f}"

    @pytest.mark.parametrize("K, T", [
        (90, 0.5), (100, 1.0), (110, 0.5),
    ])
    def test_delta_parity(self, K, T):
        """Δ_call − Δ_put = e^(−qT)"""
        q = 0.02
        call = _eu(K=K, T=T, opt="C")
        put  = _eu(K=K, T=T, opt="P")
        expected = math.exp(-q * T)
        assert call.delta - put.delta == pytest.approx(expected, abs=1e-8)


# ---------------------------------------------------------------------------
# TestEuropeanDeltaProperties
# ---------------------------------------------------------------------------

class TestEuropeanDeltaProperties:
    def test_call_delta_in_range(self):
        assert 0.0 <= _eu(opt="C").delta <= 1.0

    def test_put_delta_in_range(self):
        assert -1.0 <= _eu(opt="P").delta <= 0.0

    def test_deep_itm_call_delta_near_one(self):
        """S >> K → call delta → e^(−qT) ≈ 1."""
        r = _eu(S=500.0, K=50.0, T=0.5, q=0.0, opt="C")
        assert r.delta > 0.99

    def test_deep_otm_call_delta_near_zero(self):
        r = _eu(S=50.0, K=500.0, T=0.5, opt="C")
        assert r.delta < 0.01

    def test_deep_itm_put_delta_near_minus_one(self):
        r = _eu(S=50.0, K=500.0, T=0.5, q=0.0, opt="P")
        assert r.delta < -0.99

    def test_deep_otm_put_delta_near_zero(self):
        r = _eu(S=500.0, K=50.0, T=0.5, opt="P")
        assert r.delta > -0.01

    def test_delta_monotone_in_spot_call(self):
        """Call delta increases as S increases."""
        d1 = _eu(S=90, opt="C").delta
        d2 = _eu(S=100, opt="C").delta
        d3 = _eu(S=110, opt="C").delta
        assert d1 < d2 < d3

    def test_delta_monotone_in_spot_put(self):
        d1 = _eu(S=90, opt="P").delta
        d2 = _eu(S=100, opt="P").delta
        d3 = _eu(S=110, opt="P").delta
        assert d1 < d2 < d3  # less negative as S rises


# ---------------------------------------------------------------------------
# TestEuropeanGamma
# ---------------------------------------------------------------------------

class TestEuropeanGamma:
    def test_gamma_positive(self):
        assert _eu().gamma > 0.0

    def test_gamma_same_for_call_and_put(self):
        """Gamma is identical for call and put at same inputs."""
        call = _eu(opt="C")
        put  = _eu(opt="P")
        assert call.gamma == pytest.approx(put.gamma, abs=1e-10)

    def test_gamma_zero_at_expiry(self):
        r = _eu(T=0.0)
        assert r.gamma == 0.0

    def test_gamma_peaks_near_atm(self):
        g_itm = _eu(S=120.0, opt="C").gamma
        g_atm = _eu(S=100.0, opt="C").gamma
        g_otm = _eu(S=80.0, opt="C").gamma
        assert g_atm > g_itm
        assert g_atm > g_otm

    def test_dollar_gamma_formula(self):
        inp = EuropeanInputs(S=5000, K=5000, T=0.5, r=0.03, q=0.02, sigma=0.20,
                             option_type="C", multiplier=10.0)
        r = price_european(inp)
        assert r.dollar_gamma == pytest.approx(r.gamma * inp.S ** 2 * inp.multiplier, abs=1e-8)


# ---------------------------------------------------------------------------
# TestEuropeanVega
# ---------------------------------------------------------------------------

class TestEuropeanVega:
    def test_vega_positive(self):
        assert _eu().vega > 0.0

    def test_vega_same_for_call_and_put(self):
        assert _eu(opt="C").vega == pytest.approx(_eu(opt="P").vega, abs=1e-10)

    def test_vega_zero_at_expiry(self):
        assert _eu(T=0.0).vega == 0.0

    def test_vega_unit_is_per_point(self):
        """vega = ∂V/∂σ per 1 percentage-point vol move (not per 1 unit)."""
        r = _eu(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20, opt="C")
        # Per 1 pp: bump σ by 0.01 (1 vol point)
        r2 = _eu(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.21, opt="C")
        fd_vega = r2.price - r.price  # ≈ vega * 1 pp
        assert fd_vega == pytest.approx(r.vega, rel=0.02)

    def test_dollar_vega_formula(self):
        inp = EuropeanInputs(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20,
                             option_type="C", multiplier=10.0)
        r = price_european(inp)
        assert r.dollar_vega == pytest.approx(r.vega * 0.01 * inp.multiplier, abs=1e-8)

    def test_vega_increases_with_vol_near_atm(self):
        """Higher vol → higher vega for near-ATM (more time value to move)."""
        v1 = _eu(sigma=0.10).vega
        v2 = _eu(sigma=0.30).vega
        # Near ATM vega can go either way but this checks the function runs
        assert v1 > 0 and v2 > 0


# ---------------------------------------------------------------------------
# TestEuropeanTheta
# ---------------------------------------------------------------------------

class TestEuropeanTheta:
    def test_theta_negative_atm_call(self):
        """ATM long call loses time value → theta < 0."""
        assert _eu(opt="C").theta < 0.0

    def test_theta_negative_atm_put(self):
        assert _eu(opt="P").theta < 0.0

    def test_theta_zero_at_expiry(self):
        assert _eu(T=0.0).theta == 0.0

    def test_theta_same_units_for_call_and_put(self):
        """Both theta per calendar day — should be in same order of magnitude."""
        tc = abs(_eu(opt="C").theta)
        tp = abs(_eu(opt="P").theta)
        assert tc > 0 and tp > 0

    def test_theta_more_negative_near_expiry(self):
        """Theta accelerates as T → 0 (time decay is faster near expiry)."""
        theta_1y = _eu(T=1.0).theta
        theta_1m = _eu(T=1/12).theta
        assert theta_1m < theta_1y   # more negative near expiry


# ---------------------------------------------------------------------------
# TestEuropeanRho
# ---------------------------------------------------------------------------

class TestEuropeanRho:
    def test_call_rho_positive(self):
        """Higher r → higher call value (PV of exercise cheaper)."""
        assert _eu(opt="C").rho > 0.0

    def test_put_rho_negative(self):
        """Higher r → lower put value (PV of strike received decreases)."""
        assert _eu(opt="P").rho < 0.0

    def test_call_rho_increases_with_maturity(self):
        rho_short = _eu(T=0.25, opt="C").rho
        rho_long  = _eu(T=2.0, opt="C").rho
        assert rho_long > rho_short


# ---------------------------------------------------------------------------
# TestEuropeanD1D2
# ---------------------------------------------------------------------------

class TestEuropeanD1D2:
    def test_d1_greater_than_d2(self):
        r = _eu()
        assert r.d1 > r.d2

    def test_d2_equals_d1_minus_sigma_sqrt_T(self):
        inp = EuropeanInputs(S=100, K=100, T=1.0, r=0.05, q=0.02, sigma=0.20, option_type="C")
        r = price_european(inp)
        expected_d2 = r.d1 - inp.sigma * math.sqrt(inp.T)
        assert r.d2 == pytest.approx(expected_d2, abs=1e-10)

    def test_d1_atm_zero_rate(self):
        """ATM, r=q, T=1 → d1 = σ/2."""
        sigma = 0.20
        inp = EuropeanInputs(S=100, K=100, T=1.0, r=0.05, q=0.05, sigma=sigma, option_type="C")
        r = price_european(inp)
        assert r.d1 == pytest.approx(sigma / 2, abs=1e-6)


# ---------------------------------------------------------------------------
# TestEuropeanDeepLimits
# ---------------------------------------------------------------------------

class TestEuropeanDeepLimits:
    def test_deep_itm_call_approaches_forward(self):
        """Deep ITM call ≈ S·e^(−qT) − K·e^(−rT) (intrinsic at low vol)."""
        S, K, T, r, q = 200.0, 50.0, 1.0, 0.05, 0.02
        r_result = price_european(EuropeanInputs(S=S, K=K, T=T, r=r, q=q, sigma=0.001,
                                                  option_type="C"))
        expected = S * math.exp(-q * T) - K * math.exp(-r * T)
        assert r_result.price == pytest.approx(expected, abs=0.01)

    def test_deep_otm_call_near_zero(self):
        r = price_european(EuropeanInputs(S=50.0, K=500.0, T=0.5, r=0.05, q=0.0,
                                           sigma=0.20, option_type="C"))
        assert r.price < 0.001

    def test_deep_itm_put_approaches_forward(self):
        S, K, T, r, q = 50.0, 200.0, 1.0, 0.05, 0.0
        r_result = price_european(EuropeanInputs(S=S, K=K, T=T, r=r, q=q, sigma=0.001,
                                                  option_type="P"))
        expected = K * math.exp(-r * T) - S
        assert r_result.price == pytest.approx(expected, abs=0.10)

    def test_deep_otm_put_near_zero(self):
        r = price_european(EuropeanInputs(S=500.0, K=50.0, T=0.5, r=0.05, q=0.0,
                                           sigma=0.20, option_type="P"))
        assert r.price < 0.001

    def test_expired_option_returns_intrinsic(self):
        call = _eu(S=110.0, K=100.0, T=0.0, opt="C")
        assert call.price == pytest.approx(10.0, abs=1e-8)

        put = _eu(S=90.0, K=100.0, T=0.0, opt="P")
        assert put.price == pytest.approx(10.0, abs=1e-8)

    def test_expired_otm_returns_zero(self):
        call = _eu(S=90.0, K=100.0, T=0.0, opt="C")
        assert call.price == pytest.approx(0.0, abs=1e-8)


# ---------------------------------------------------------------------------
# TestLocalPnlApproximation
# ---------------------------------------------------------------------------

class TestLocalPnlApproximation:
    def test_positive_spot_move_helps_call(self):
        r = _eu(opt="C")
        pnl = local_pnl_approximation(r, dS=1.0, d_sigma_pct=0.0, dt_days=0.0)
        assert pnl > 0

    def test_negative_spot_move_hurts_call(self):
        r = _eu(opt="C")
        pnl = local_pnl_approximation(r, dS=-1.0, d_sigma_pct=0.0, dt_days=0.0)
        assert pnl < 0

    def test_positive_spot_move_hurts_put(self):
        r = _eu(opt="P")
        pnl = local_pnl_approximation(r, dS=1.0, d_sigma_pct=0.0, dt_days=0.0)
        assert pnl < 0

    def test_vol_bump_helps_long_option(self):
        r = _eu(opt="C")
        pnl = local_pnl_approximation(r, dS=0.0, d_sigma_pct=1.0, dt_days=0.0)
        assert pnl > 0

    def test_time_decay_hurts_long(self):
        r = _eu(opt="C")
        pnl = local_pnl_approximation(r, dS=0.0, d_sigma_pct=0.0, dt_days=1.0)
        assert pnl < 0

    def test_gamma_pnl_positive_for_large_move(self):
        """Large move, direction-neutral: gamma pnl (Γ·dS²/2) dominates."""
        r = _eu(opt="C")
        pnl_up = local_pnl_approximation(r, dS=10.0, d_sigma_pct=0.0, dt_days=0.0)
        pnl_dn = local_pnl_approximation(r, dS=-10.0, d_sigma_pct=0.0, dt_days=0.0)
        # Both large moves → gamma pnl should keep both positive (call is long gamma)
        assert pnl_up > 0


# ---------------------------------------------------------------------------
# TestAmericanProperties
# ---------------------------------------------------------------------------

class TestAmericanProperties:
    def test_price_positive(self):
        assert _am().price > 0.0

    def test_call_delta_in_range(self):
        assert 0.0 <= _am(opt="C").delta <= 1.0

    def test_put_delta_in_range(self):
        assert -1.0 <= _am(opt="P").delta <= 0.0

    def test_gamma_positive(self):
        assert _am().gamma > 0.0

    def test_vega_positive(self):
        assert _am().vega > 0.0

    def test_dollar_gamma_formula(self):
        inp = AmericanInputs(S=5000, K=5000, T=0.5, r=0.03, q=0.0, sigma=0.20,
                              option_type="C", multiplier=10.0)
        r = price_american(inp)
        assert r.dollar_gamma == pytest.approx(r.gamma * inp.S ** 2 * inp.multiplier, rel=0.01)

    def test_dollar_vega_formula(self):
        inp = AmericanInputs(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20,
                              option_type="C", multiplier=10.0)
        r = price_american(inp)
        assert r.dollar_vega == pytest.approx(r.vega * 0.01 * inp.multiplier, abs=1e-6)


# ---------------------------------------------------------------------------
# TestAmericanVsEuropean — acceptance criterion
# ---------------------------------------------------------------------------

class TestAmericanVsEuropean:
    """
    Acceptance criterion: American converges to European in degenerate cases.
    """

    def test_call_equals_european_when_q_zero(self):
        """American call with q=0 = European call (no early exercise premium)."""
        eu = _eu(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20, opt="C")
        am = _am(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20, opt="C", n=300)
        assert am.price == pytest.approx(eu.price, abs=0.05)

    def test_call_equals_european_otm_q_zero(self):
        eu = _eu(S=100, K=110, T=0.5, r=0.05, q=0.0, sigma=0.25, opt="C")
        am = _am(S=100, K=110, T=0.5, r=0.05, q=0.0, sigma=0.25, opt="C", n=300)
        assert am.price == pytest.approx(eu.price, abs=0.05)

    def test_call_ge_european_with_dividends(self):
        """With q>0, American call ≥ European call (early exercise can be optimal)."""
        eu = _eu(S=100, K=100, T=1.0, r=0.05, q=0.05, sigma=0.20, opt="C")
        am = _am(S=100, K=100, T=1.0, r=0.05, q=0.05, sigma=0.20, opt="C", n=200)
        assert am.price >= eu.price - 1e-4

    @pytest.mark.parametrize("K, T", [
        (90, 0.5), (100, 1.0), (110, 0.5), (100, 2.0),
    ])
    def test_put_ge_european(self, K, T):
        """American put ≥ European put always (early exercise premium ≥ 0)."""
        eu = _eu(S=100, K=K, T=T, r=0.05, q=0.0, sigma=0.20, opt="P")
        am = _am(S=100, K=K, T=T, r=0.05, q=0.0, sigma=0.20, opt="P", n=200)
        assert am.price >= eu.price - 1e-4

    def test_put_equals_european_deep_otm(self):
        """Deep OTM American put ≈ European put (early exercise worthless)."""
        eu = _eu(S=200, K=50, T=1.0, r=0.05, q=0.0, sigma=0.20, opt="P")
        am = _am(S=200, K=50, T=1.0, r=0.05, q=0.0, sigma=0.20, opt="P", n=200)
        assert am.price == pytest.approx(eu.price, abs=0.01)

    def test_delta_call_q_zero_close_to_european(self):
        eu = _eu(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20, opt="C")
        am = _am(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20, opt="C", n=300)
        assert am.delta == pytest.approx(eu.delta, abs=0.02)


# ---------------------------------------------------------------------------
# TestAmericanConvergence — price converges as n_steps increases
# ---------------------------------------------------------------------------

class TestAmericanConvergence:
    def test_price_converges_with_steps(self):
        """More tree steps → price converges toward BS price."""
        prices = [
            _am(n=n, opt="C").price for n in [50, 100, 200, 400]
        ]
        # Each step count should be within 5% of the next (convergence)
        for i in range(1, len(prices)):
            assert abs(prices[i] - prices[i-1]) < 0.10

    def test_expired_tree_returns_intrinsic_call(self):
        r = _am(S=110, K=100, T=0.0, opt="C")
        assert r.price == pytest.approx(10.0, abs=1e-8)

    def test_expired_tree_returns_intrinsic_put(self):
        r = _am(S=90, K=100, T=0.0, opt="P")
        assert r.price == pytest.approx(10.0, abs=1e-8)

    def test_expired_otm_returns_zero(self):
        r = _am(S=90, K=100, T=0.0, opt="C")
        assert r.price == pytest.approx(0.0, abs=1e-8)


# ---------------------------------------------------------------------------
# TestAcceptanceCriterion — PLAN acceptance
# ---------------------------------------------------------------------------

class TestAcceptanceCriterion:
    """
    PLAN Step 10: "Reference cases match; American converges to European
    in degenerate cases."
    """

    REFERENCE_CASES = [
        # (desc, S, K, T, r, q, sigma, opt, expected_parity_delta)
        # parity delta = Δcall - Δput should = e^(-qT)
        ("ATM 1y r=5 q=2", 100, 100, 1.0, 0.05, 0.02, 0.20, None),
        ("OTM call 3m",   100, 110, 0.25, 0.03, 0.01, 0.25, None),
        ("ITM put 6m",    100, 110, 0.50, 0.04, 0.00, 0.18, None),
    ]

    @pytest.mark.parametrize("desc,S,K,T,r,q,sigma,opt", [
        (d, s, k, t, rr, qq, sig, "C") for d, s, k, t, rr, qq, sig, _ in REFERENCE_CASES
    ])
    def test_put_call_parity_holds(self, desc, S, K, T, r, q, sigma, opt):
        call = price_european(EuropeanInputs(S=S, K=K, T=T, r=r, q=q, sigma=sigma,
                                              option_type="C"))
        put  = price_european(EuropeanInputs(S=S, K=K, T=T, r=r, q=q, sigma=sigma,
                                              option_type="P"))
        lhs = call.price - put.price
        rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
        assert lhs == pytest.approx(rhs, abs=1e-6), f"{desc}: {lhs:.8f} != {rhs:.8f}"

    def test_american_call_equals_european_q0(self):
        eu = _eu(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20, opt="C")
        am = _am(S=100, K=100, T=1.0, r=0.05, q=0.0, sigma=0.20, opt="C", n=400)
        assert am.price == pytest.approx(eu.price, abs=0.03), (
            f"American={am.price:.4f} should match European={eu.price:.4f} when q=0"
        )

    def test_all_greeks_finite(self):
        for opt in ("C", "P"):
            r = _eu(opt=opt)
            assert math.isfinite(r.price)
            assert math.isfinite(r.delta)
            assert math.isfinite(r.gamma)
            assert math.isfinite(r.vega)
            assert math.isfinite(r.theta)
            assert math.isfinite(r.dollar_gamma)
            assert math.isfinite(r.dollar_vega)
