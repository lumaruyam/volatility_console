"""
Unit tests for src/historical/ibkr_loader.py.

All tests use a MockAdapter — no real IBKR connection is required.
IbkrAdapter.get_historical_bars() is mocked to return synthetic bar dicts.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

from src.historical.ibkr_loader import (
    _bars_to_df,
    _dates_to_duration,
    _yf_ticker_to_ibkr_symbol,
    fetch_constituents_history,
    fetch_euro_stoxx_50,
    fetch_index_history,
    to_historical_bars,
    validate_history,
)
from src.historical.yfinance_loader import HistoricalBar


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_bars(n: int = 5, start_price: float = 100.0) -> list[dict]:
    """Synthetic bar dicts matching IbkrAdapter.get_historical_bars() output."""
    bars = []
    for i in range(n):
        dt = date(2023, 1, 3 + i).isoformat()          # Mon 2023-01-09 onwards
        p  = start_price + i
        bars.append({
            "date":      dt,
            "open":      p,
            "high":      p + 1.0,
            "low":       p - 1.0,
            "close":     p + 0.5,
            "volume":    1_000_000 + i * 1_000,
            "bar_count": 100,
            "average":   p + 0.25,
        })
    return bars


def _make_adapter(bars: list[dict] | None = None, raises: bool = False) -> MagicMock:
    """Build a MagicMock that satisfies the IbkrAdapter.get_historical_bars() contract."""
    adapter = MagicMock()
    if raises:
        adapter.get_historical_bars.side_effect = RuntimeError("simulated connection error")
    else:
        adapter.get_historical_bars.return_value = bars if bars is not None else _make_bars()
    return adapter


# ---------------------------------------------------------------------------
# 1. _dates_to_duration
# ---------------------------------------------------------------------------

class TestDatesToDuration:
    def test_over_two_years_returns_3Y(self):
        # 2020-01-01 → 2022-06-01 = 882 days > 730
        assert _dates_to_duration("2020-01-01", "2022-06-01") == "3 Y"

    def test_over_one_year_returns_2Y(self):
        # 2021-07-01 → 2022-12-01 = 518 days  (>365, ≤730)
        assert _dates_to_duration("2021-07-01", "2022-12-01") == "2 Y"

    def test_over_six_months_returns_1Y(self):
        # 2022-07-01 → 2023-01-31 = 214 days  (>182, ≤365)
        assert _dates_to_duration("2022-07-01", "2023-01-31") == "1 Y"

    def test_under_six_months_returns_6M(self):
        # 2023-07-01 → 2023-11-01 = 123 days  (≤182)
        assert _dates_to_duration("2023-07-01", "2023-11-01") == "6 M"

    def test_defaults_end_to_today(self):
        # Regardless of computed result, must not raise
        result = _dates_to_duration("2010-01-01")
        assert result in ("6 M", "1 Y", "2 Y", "3 Y")


# ---------------------------------------------------------------------------
# 2. _yf_ticker_to_ibkr_symbol
# ---------------------------------------------------------------------------

class TestYfTickerToIbkrSymbol:
    def test_strips_german_suffix(self):
        assert _yf_ticker_to_ibkr_symbol("ADS.DE") == "ADS"

    def test_strips_french_suffix(self):
        assert _yf_ticker_to_ibkr_symbol("MC.PA") == "MC"

    def test_strips_amsterdam_suffix(self):
        assert _yf_ticker_to_ibkr_symbol("ASML.AS") == "ASML"

    def test_no_suffix_unchanged(self):
        assert _yf_ticker_to_ibkr_symbol("AAPL") == "AAPL"


# ---------------------------------------------------------------------------
# 3. fetch_index_history — columns and index
# ---------------------------------------------------------------------------

class TestFetchIndexHistoryColumns:
    def test_returns_dataframe_not_empty(self):
        adapter = _make_adapter()
        df = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_required_columns_present(self):
        adapter = _make_adapter()
        df = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        for col in ("Open", "High", "Low", "Close", "Adj Close", "Volume"):
            assert col in df.columns, f"Missing column: {col}"

    def test_index_is_utc_datetimeindex(self):
        adapter = _make_adapter()
        df = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is not None
        assert str(df.index.tz) == "UTC"

    def test_adj_close_equals_close(self):
        adapter = _make_adapter()
        df = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        pd.testing.assert_series_equal(df["Close"], df["Adj Close"],
                                       check_names=False)

    def test_rows_match_bar_count(self):
        adapter = _make_adapter(_make_bars(7))
        df = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        assert len(df) == 7


# ---------------------------------------------------------------------------
# 4. fetch_index_history — adapter contract for ESTX50
# ---------------------------------------------------------------------------

class TestFetchIndexHistoryAdapterContract:
    def test_calls_adapter_with_estx50_symbol(self):
        adapter = _make_adapter()
        fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        adapter.get_historical_bars.assert_called_once()
        kwargs = adapter.get_historical_bars.call_args.kwargs
        assert kwargs["symbol"] == "ESTX50"

    def test_calls_adapter_with_ind_sec_type(self):
        adapter = _make_adapter()
        fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        kwargs = adapter.get_historical_bars.call_args.kwargs
        assert kwargs["sec_type"] == "IND"

    def test_calls_adapter_with_eurex_exchange(self):
        adapter = _make_adapter()
        fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        kwargs = adapter.get_historical_bars.call_args.kwargs
        assert kwargs["exchange"] == "EUREX"

    def test_calls_adapter_with_eur_currency(self):
        adapter = _make_adapter()
        fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        kwargs = adapter.get_historical_bars.call_args.kwargs
        assert kwargs["currency"] == "EUR"

    def test_daily_interval_maps_to_1_day_bar_size(self):
        adapter = _make_adapter()
        fetch_index_history(adapter, "^STOXX50E", start="2022-01-01", interval="1d")
        kwargs = adapter.get_historical_bars.call_args.kwargs
        assert kwargs["bar_size"] == "1 day"

    def test_hourly_interval_maps_to_1_hour_bar_size(self):
        adapter = _make_adapter()
        fetch_index_history(adapter, "^STOXX50E", start="2022-01-01", interval="1h")
        kwargs = adapter.get_historical_bars.call_args.kwargs
        assert kwargs["bar_size"] == "1 hour"

    def test_5m_interval_maps_to_5_mins_bar_size(self):
        adapter = _make_adapter()
        fetch_index_history(adapter, "^STOXX50E", start="2022-01-01", interval="5m")
        kwargs = adapter.get_historical_bars.call_args.kwargs
        assert kwargs["bar_size"] == "5 mins"

    def test_constituent_ticker_uses_stk_smart(self):
        adapter = _make_adapter()
        fetch_index_history(adapter, "ADS.DE", start="2022-01-01")
        kwargs = adapter.get_historical_bars.call_args.kwargs
        assert kwargs["symbol"]   == "ADS"
        assert kwargs["sec_type"] == "STK"
        assert kwargs["exchange"] == "SMART"
        assert kwargs["currency"] == "EUR"


# ---------------------------------------------------------------------------
# 5. fetch_index_history — error handling
# ---------------------------------------------------------------------------

class TestFetchIndexHistoryErrors:
    def test_returns_empty_df_on_adapter_error(self):
        adapter = _make_adapter(raises=True)
        df = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_returns_empty_df_when_adapter_returns_no_bars(self):
        adapter = _make_adapter(bars=[])
        df = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        assert df.empty

    def test_never_raises(self):
        adapter = _make_adapter(raises=True)
        # Must not propagate the RuntimeError
        result = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        assert isinstance(result, pd.DataFrame)


# ---------------------------------------------------------------------------
# 6. fetch_constituents_history
# ---------------------------------------------------------------------------

class TestFetchConstituentsHistory:
    def test_returns_multiindex_columns(self):
        adapter = _make_adapter()
        df = fetch_constituents_history(adapter, ["ADS.DE", "MC.PA"],
                                        start="2022-01-01")
        assert isinstance(df.columns, pd.MultiIndex), (
            "Expected MultiIndex columns like (field, ticker)"
        )

    def test_close_column_present_at_level0(self):
        adapter = _make_adapter()
        df = fetch_constituents_history(adapter, ["ADS.DE", "MC.PA"],
                                        start="2022-01-01")
        assert "Close" in df.columns.get_level_values(0)

    def test_ticker_names_appear_at_level1(self):
        adapter = _make_adapter()
        df = fetch_constituents_history(adapter, ["ADS.DE", "MC.PA"],
                                        start="2022-01-01")
        level1 = set(df.columns.get_level_values(1))
        assert {"ADS.DE", "MC.PA"}.issubset(level1)

    def test_pacing_delay_between_requests(self):
        adapter = _make_adapter()
        with patch("src.historical.ibkr_loader.time") as mock_time:
            fetch_constituents_history(adapter, ["ADS.DE", "MC.PA", "SAP.DE"],
                                       start="2022-01-01")
            # Sleep called once before the 2nd ticker and once before the 3rd
            assert mock_time.sleep.call_count == 2
            mock_time.sleep.assert_called_with(0.5)

    def test_no_sleep_before_first_ticker(self):
        adapter = _make_adapter()
        with patch("src.historical.ibkr_loader.time") as mock_time:
            fetch_constituents_history(adapter, ["ADS.DE"],
                                       start="2022-01-01")
            mock_time.sleep.assert_not_called()

    def test_continues_when_one_ticker_fails(self):
        adapter = MagicMock()
        adapter.get_historical_bars.side_effect = [
            RuntimeError("ADS failed"),
            _make_bars(),                              # MC.PA succeeds
        ]
        with patch("src.historical.ibkr_loader.time"):
            df = fetch_constituents_history(adapter, ["ADS.DE", "MC.PA"],
                                            start="2022-01-01")
        assert not df.empty
        level1 = set(df.columns.get_level_values(1))
        assert "MC.PA" in level1
        assert "ADS.DE" not in level1

    def test_returns_empty_if_all_tickers_fail(self):
        adapter = _make_adapter(raises=True)
        with patch("src.historical.ibkr_loader.time"):
            df = fetch_constituents_history(adapter, ["ADS.DE", "MC.PA"],
                                            start="2022-01-01")
        assert df.empty

    def test_constituent_adapter_calls_use_stk_smart(self):
        adapter = _make_adapter()
        with patch("src.historical.ibkr_loader.time"):
            fetch_constituents_history(adapter, ["ADS.DE"],
                                       start="2022-01-01")
        kwargs = adapter.get_historical_bars.call_args.kwargs
        assert kwargs["sec_type"] == "STK"
        assert kwargs["exchange"] == "SMART"
        assert kwargs["currency"] == "EUR"
        assert kwargs["symbol"]   == "ADS"


# ---------------------------------------------------------------------------
# 7. to_historical_bars
# ---------------------------------------------------------------------------

class TestToHistoricalBars:
    def _get_df(self, n: int = 3) -> pd.DataFrame:
        adapter = _make_adapter(_make_bars(n))
        return fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")

    def test_source_is_ibkr(self):
        bars = to_historical_bars(self._get_df(), "ESTX50")
        assert all(b.source == "ibkr" for b in bars)

    def test_length_matches_df_rows(self):
        bars = to_historical_bars(self._get_df(5), "ESTX50")
        assert len(bars) == 5

    def test_ticker_field_set_correctly(self):
        bars = to_historical_bars(self._get_df(1), "ESTX50")
        assert bars[0].ticker == "ESTX50"

    def test_ohlcv_values_match_synthetic_bars(self):
        synthetic = _make_bars(1)          # close = 100.5, volume = 1_000_000
        adapter   = _make_adapter(synthetic)
        df        = fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")
        bars      = to_historical_bars(df, "ESTX50")
        assert bars[0].close  == pytest.approx(synthetic[0]["close"])
        assert bars[0].volume == pytest.approx(float(synthetic[0]["volume"]))

    def test_adj_close_equals_close(self):
        bars = to_historical_bars(self._get_df(3), "ESTX50")
        for bar in bars:
            assert bar.adj_close == pytest.approx(bar.close)

    def test_returns_historicalbar_instances(self):
        bars = to_historical_bars(self._get_df(1), "ESTX50")
        assert isinstance(bars[0], HistoricalBar)

    def test_empty_df_yields_empty_list(self):
        bars = to_historical_bars(pd.DataFrame(), "ESTX50")
        assert bars == []


# ---------------------------------------------------------------------------
# 8. validate_history (re-exported from yfinance_loader — same checks)
# ---------------------------------------------------------------------------

class TestValidateHistory:
    def _valid_df(self, n: int = 10) -> pd.DataFrame:
        adapter = _make_adapter(_make_bars(n, start_price=50.0))
        return fetch_index_history(adapter, "^STOXX50E", start="2022-01-01")

    def test_no_warnings_for_valid_data(self):
        warnings = validate_history(self._valid_df(10), "ESTX50")
        assert warnings == []

    def test_warns_on_empty_dataframe(self):
        warnings = validate_history(pd.DataFrame(), "ESTX50")
        assert len(warnings) > 0
        assert any("empty" in w.lower() for w in warnings)

    def test_warns_when_too_few_rows(self):
        warnings = validate_history(self._valid_df(2), "ESTX50", min_rows=5)
        assert any("row" in w.lower() for w in warnings)

    def test_warns_on_zero_close_prices(self):
        df = self._valid_df(5)
        df["Close"] = 0.0
        warnings = validate_history(df, "ESTX50")
        assert any("non-positive" in w or "positive" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# 9. fetch_euro_stoxx_50
# ---------------------------------------------------------------------------

class TestFetchEuroStoxx50:
    def test_returns_dict_with_index_and_constituents_keys(self):
        adapter = _make_adapter()
        with patch("src.historical.ibkr_loader.time"):
            result = fetch_euro_stoxx_50(adapter, start="2022-01-01")
        assert set(result.keys()) == {"index", "constituents"}

    def test_index_df_not_empty(self):
        adapter = _make_adapter()
        with patch("src.historical.ibkr_loader.time"):
            result = fetch_euro_stoxx_50(adapter, start="2022-01-01")
        assert not result["index"].empty
