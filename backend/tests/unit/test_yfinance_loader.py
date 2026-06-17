"""
Unit tests for Yahoo Finance historical loader.

Uses unittest.mock to avoid real network calls in CI/unit tests.
Integration tests (with real network) live in tests/integration/.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from src.historical.yfinance_loader import (
    EURO_STOXX_50_TICKERS,
    INDEX_TICKERS,
    fetch_index_history,
    get_close_series,
    to_historical_bars,
    validate_history,
)


def _mock_ohlcv(n: int = 10, start: str = "2024-01-01") -> pd.DataFrame:
    """Build a fake yfinance-style OHLCV DataFrame."""
    idx = pd.date_range(start, periods=n, freq="B", tz="UTC")
    return pd.DataFrame({
        "Open":      [100.0 + i for i in range(n)],
        "High":      [101.0 + i for i in range(n)],
        "Low":       [99.0  + i for i in range(n)],
        "Close":     [100.5 + i for i in range(n)],
        "Adj Close": [100.5 + i for i in range(n)],
        "Volume":    [1_000_000.0] * n,
    }, index=idx)


class TestFetchIndexHistory:

    @patch("src.historical.yfinance_loader.yf.download")
    def test_returns_dataframe_on_success(self, mock_dl):
        mock_dl.return_value = _mock_ohlcv(20)
        df = fetch_index_history("^STOXX50E", "2024-01-01", "2024-06-01")
        assert not df.empty
        assert "Close" in df.columns
        assert len(df) == 20

    @patch("src.historical.yfinance_loader.yf.download")
    def test_returns_empty_on_exception(self, mock_dl):
        mock_dl.side_effect = Exception("network error")
        df = fetch_index_history("^STOXX50E", "2024-01-01")
        assert df.empty  # Never raises — caller decides

    @patch("src.historical.yfinance_loader.yf.download")
    def test_index_is_utc(self, mock_dl):
        mock_dl.return_value = _mock_ohlcv(5)
        df = fetch_index_history("^GSPC", "2024-01-01")
        assert df.index.tz is not None


class TestGetCloseSeries:

    def test_adj_close_returned_by_default(self):
        df = _mock_ohlcv(10)
        series = get_close_series(df, adjusted=True)
        assert len(series) == 10
        assert series.iloc[0] == pytest.approx(100.5)

    def test_unadjusted_close(self):
        df = _mock_ohlcv(10)
        series = get_close_series(df, adjusted=False)
        assert series.name == "Close"

    def test_raises_on_missing_column(self):
        df = pd.DataFrame({"Open": [1.0, 2.0]})
        with pytest.raises(KeyError):
            get_close_series(df)


class TestToHistoricalBars:

    def test_converts_all_rows(self):
        df = _mock_ohlcv(5)
        bars = to_historical_bars(df, "^STOXX50E")
        assert len(bars) == 5
        assert bars[0].ticker == "^STOXX50E"
        assert bars[0].close == pytest.approx(100.5)
        assert bars[0].source == "yfinance"

    def test_bar_dates_are_date_objects(self):
        df = _mock_ohlcv(3)
        bars = to_historical_bars(df, "TEST")
        assert isinstance(bars[0].date, date)


class TestValidateHistory:

    def test_valid_df_returns_no_warnings(self):
        df = _mock_ohlcv(20)
        warnings = validate_history(df, "^STOXX50E", min_rows=5)
        assert warnings == []

    def test_empty_df_flagged(self):
        warnings = validate_history(pd.DataFrame(), "BAD", min_rows=1)
        assert any("empty" in w for w in warnings)

    def test_too_few_rows_flagged(self):
        df = _mock_ohlcv(3)
        warnings = validate_history(df, "SHORT", min_rows=10)
        assert any("3 rows" in w for w in warnings)

    def test_nan_close_flagged(self):
        df = _mock_ohlcv(5)
        df.loc[df.index[2], "Close"] = float("nan")
        warnings = validate_history(df, "NAN_TEST")
        assert any("NaN" in w for w in warnings)


class TestConstants:

    def test_euro_stoxx_50_has_50_tickers(self):
        assert len(EURO_STOXX_50_TICKERS) == 50

    def test_index_tickers_has_euro_stoxx(self):
        assert "EURO_STOXX_50" in INDEX_TICKERS
        assert INDEX_TICKERS["EURO_STOXX_50"] == "^STOXX50E"
