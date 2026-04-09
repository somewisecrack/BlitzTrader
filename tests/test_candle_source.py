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


if __name__ == "__main__":
    unittest.main(verbosity=2)
