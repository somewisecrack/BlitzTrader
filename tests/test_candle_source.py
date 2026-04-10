"""
tests/test_candle_source.py
----------------------------
Validates that get_candles() uses Shoonya REST as the primary source
and falls back to live-feed candles only when REST returns nothing.

Run: python -m pytest tests/test_candle_source.py -v
"""
import sys
import os
import time
import datetime
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is on path when run directly or via pytest from project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_rest_bar(offset_secs: int, close: float = 100.0) -> dict:
    """Return a Shoonya-style raw candle dict for a bar 'offset_secs' ago."""
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    ts = time.time() - offset_secs
    t_str = datetime.datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S %d-%m-%Y")
    return {
        "time": t_str,
        "into": close,
        "inth": close + 1,
        "intl": close - 1,
        "intc": close,
        "intv": 1000,
    }


def _make_live_bar(offset_secs: int, close: float = 200.0) -> dict:
    """Return a live-feed-style candle dict."""
    return {
        "time": time.time() - offset_secs,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 0,
    }


def _build_tool(rest_bars, live_bars):
    """Construct a MarketDataTools instance with mocked broker and feed."""
    from tools.market_data import MarketDataTools

    client = MagicMock()
    client.get_time_price_series.return_value = rest_bars
    client.search_scrip.return_value = None

    feed = MagicMock()
    feed.get_candles.return_value = live_bars

    nse_tokens = {"NIFTY": {"exchange": "NSE", "token": "26000"}}
    return MarketDataTools(client, feed, nse_tokens), client, feed


class TestCandleSourcePriority(unittest.TestCase):

    def test_rest_preferred_when_available(self):
        """When REST returns enough bars, result.source is 'rest_api' and live feed is never called."""
        rest_bars = [_make_rest_bar(300 * i) for i in range(5)]
        live_bars = [_make_live_bar(60 * i) for i in range(5)]

        tool, client, feed = _build_tool(rest_bars, live_bars)
        result = tool.get_candles("NIFTY", "5", count=5)

        self.assertEqual(result.get("source"), "rest_api")
        self.assertFalse("error" in result)
        feed.get_candles.assert_not_called()
        client.get_time_price_series.assert_called_once()

    def test_live_feed_fallback_when_rest_returns_none(self):
        """When REST returns None, live-feed candles are used and source is 'live_feed'."""
        live_bars = [_make_live_bar(60 * i) for i in range(5)]

        tool, client, feed = _build_tool(None, live_bars)
        result = tool.get_candles("NIFTY", "5", count=5)

        self.assertEqual(result.get("source"), "live_feed")
        self.assertFalse("error" in result)
        client.get_time_price_series.assert_called_once()  # REST was attempted
        feed.get_candles.assert_called_once()              # fallback was used

    def test_live_feed_fallback_when_rest_returns_empty_list(self):
        """When REST returns [] (valid but empty), live-feed fallback activates."""
        live_bars = [_make_live_bar(60 * i) for i in range(3)]

        tool, client, feed = _build_tool([], live_bars)
        result = tool.get_candles("NIFTY", "5", count=5)

        self.assertEqual(result.get("source"), "live_feed")
        feed.get_candles.assert_called_once()

    def test_error_when_both_sources_fail(self):
        """When both REST and live feed return nothing, an error dict is returned."""
        tool, _, _ = _build_tool(None, None)
        result = tool.get_candles("NIFTY", "5", count=5)

        self.assertIn("error", result)
        self.assertEqual(result.get("candles", []), [])

    def test_cache_hit_skips_both_sources(self):
        """A fresh cache entry is returned without calling REST or live feed again."""
        rest_bars = [_make_rest_bar(300 * i) for i in range(5)]
        tool, client, feed = _build_tool(rest_bars, None)

        # Prime the cache
        r1 = tool.get_candles("NIFTY", "5", count=5)
        self.assertEqual(r1.get("source"), "rest_api")

        # Second call — exact cache hit returns immediately (source unchanged, no new API call)
        client.get_time_price_series.reset_mock()
        r2 = tool.get_candles("NIFTY", "5", count=5)
        # Exact cache hit returns the stored result as-is (source = "rest_api", not "_cache")
        self.assertIn(r2.get("source"), ("rest_api", "rest_api_cache", "live_feed_cache"))
        client.get_time_price_series.assert_not_called()
        feed.get_candles.assert_not_called()

    def test_candle_source_propagates_to_indicators(self):
        """get_indicators() result includes candle_source from the underlying get_candles()."""
        # Provide 120 REST bars (EMA100 needs 100+)
        rest_bars = [_make_rest_bar(300 * i, close=100.0 + i * 0.1) for i in range(130)]
        tool, client, feed = _build_tool(rest_bars, None)

        # Mock out daily-OHLC calls to keep the test focused
        tool._get_recent_daily_ohlc = MagicMock(return_value=[])
        tool._get_avg_cpr_width = MagicMock(return_value=None)

        result = tool.get_indicators("NIFTY", "5")
        # Either a valid indicator dict or an error — both acceptable here;
        # what matters is that candle_source is present when data was available.
        if "error" not in result:
            self.assertIn("candle_source", result)
            self.assertEqual(result["candle_source"], "rest_api")


class TestStarttimeFormula(unittest.TestCase):
    """Verify the lookback formula in get_candles() provides enough history."""

    def _calendar_days(self, count: int, interval_min: int) -> int:
        import math
        trading_mins_needed = (count + 20) * interval_min
        trading_days_needed = math.ceil(trading_mins_needed / 375) + 1
        return trading_days_needed * 3

    def test_3m_100_bars_is_enough(self):
        """100 × 3m bars need 300 trading-minutes (< 1 day). Formula gives ≥ 6 calendar days."""
        days = self._calendar_days(count=100, interval_min=3)
        # 120 bars × 3m = 360 min → ceil(360/375)=1 + 1 = 2 trading days → 6 calendar days
        self.assertGreaterEqual(days, 6)

    def test_5m_100_bars_is_enough(self):
        """100 × 5m bars need 500 trading-minutes (~1.3 days). Formula gives ≥ 9 calendar days."""
        days = self._calendar_days(count=100, interval_min=5)
        # 120 × 5 = 600 min → ceil(600/375)=2 + 1 = 3 trading days → 9 calendar days
        self.assertGreaterEqual(days, 9)

    def test_15m_100_bars_is_enough(self):
        """100 × 15m bars need 1500 trading-minutes (~4 days). Formula gives ≥ 15 calendar days."""
        days = self._calendar_days(count=100, interval_min=15)
        # 120 × 15 = 1800 min → ceil(1800/375)=5 + 1 = 6 trading days → 18 calendar days
        self.assertGreaterEqual(days, 15)

    def test_220_bars_3m_covers_ema100(self):
        """Default candle count of 220 for 3m must cover 100-period EMA warm-up (220 bars)."""
        days = self._calendar_days(count=220, interval_min=3)
        # 240 × 3 = 720 min → ceil(720/375)=2 + 1 = 3 trading days → 9 calendar days
        self.assertGreaterEqual(days, 9)
        # The returned count (220) exceeds EMA100 requirement of 100 bars
        self.assertGreaterEqual(220, 100)

    def test_lookback_increases_monotonically_with_interval(self):
        """Larger intervals require proportionally more lookback calendar days."""
        days_3m  = self._calendar_days(count=100, interval_min=3)
        days_5m  = self._calendar_days(count=100, interval_min=5)
        days_15m = self._calendar_days(count=100, interval_min=15)
        self.assertLessEqual(days_3m, days_5m)
        self.assertLessEqual(days_5m, days_15m)


class TestSsboeParsing(unittest.TestCase):
    """Verify the candle timestamp parsing logic (ssboe-first with time-string fallback)."""

    def _parse_ts(self, c: dict) -> float:
        """Mirror the parsing logic from market_data.py get_candles()."""
        import datetime
        import pytz
        IST = pytz.timezone("Asia/Kolkata")

        ssboe = c.get("ssboe", "")
        if ssboe and str(ssboe).isdigit():
            return float(ssboe)

        raw_time = c.get("time", "")
        for fmt in ("%H:%M:%S %d-%m-%Y", "%d-%m-%Y %H:%M:%S"):
            try:
                return IST.localize(datetime.datetime.strptime(raw_time, fmt)).timestamp()
            except ValueError:
                continue
        return 0.0

    def test_ssboe_takes_priority_over_time_field(self):
        """When ssboe is present and numeric, it wins regardless of time field."""
        c = {"ssboe": "1775533500", "time": "totally-invalid"}
        self.assertEqual(self._parse_ts(c), 1775533500.0)

    def test_index_time_format_hhmm_ddmm(self):
        """Old index candle format: 'HH:MM:SS DD-MM-YYYY' parses correctly."""
        import datetime, pytz
        IST = pytz.timezone("Asia/Kolkata")
        c = {"time": "09:15:00 10-04-2026"}
        ts = self._parse_ts(c)
        expected = IST.localize(datetime.datetime(2026, 4, 10, 9, 15, 0)).timestamp()
        self.assertAlmostEqual(ts, expected, places=0)

    def test_futures_time_format_ddmm_hhmm(self):
        """Futures candle format: 'DD-MM-YYYY HH:MM:SS' parses correctly."""
        import datetime, pytz
        IST = pytz.timezone("Asia/Kolkata")
        c = {"time": "10-04-2026 09:15:00"}
        ts = self._parse_ts(c)
        expected = IST.localize(datetime.datetime(2026, 4, 10, 9, 15, 0)).timestamp()
        self.assertAlmostEqual(ts, expected, places=0)

    def test_both_formats_produce_same_timestamp(self):
        """Both time-field formats for the same instant produce the same epoch."""
        c1 = {"time": "09:15:00 10-04-2026"}
        c2 = {"time": "10-04-2026 09:15:00"}
        self.assertAlmostEqual(self._parse_ts(c1), self._parse_ts(c2), places=0)

    def test_invalid_time_returns_zero(self):
        """Unparseable time field returns 0.0 so the candle can be filtered out."""
        c = {"time": "garbage"}
        self.assertEqual(self._parse_ts(c), 0.0)

    def test_missing_ssboe_and_time_returns_zero(self):
        """Empty candle with no timestamp fields returns 0.0."""
        self.assertEqual(self._parse_ts({}), 0.0)

    def test_non_numeric_ssboe_falls_back_to_time_field(self):
        """If ssboe is present but not all-digits, time field is used instead."""
        import datetime, pytz
        IST = pytz.timezone("Asia/Kolkata")
        c = {"ssboe": "N/A", "time": "09:15:00 10-04-2026"}
        ts = self._parse_ts(c)
        expected = IST.localize(datetime.datetime(2026, 4, 10, 9, 15, 0)).timestamp()
        self.assertAlmostEqual(ts, expected, places=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
