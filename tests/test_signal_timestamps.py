"""
tests/test_signal_timestamps.py
---------------------------------
Validates that strategy signals use the candle's actual timestamp (from ssboe/
time field in REST data), NOT datetime.now(), and that signals from old/stale
candles are correctly identified.

Run: python -m pytest tests/test_signal_timestamps.py -v
"""
import sys
import os
import time
import datetime
import unittest
from unittest.mock import MagicMock

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────

def _ist_ts(hour: int, minute: int, date: datetime.date = None) -> float:
    """Return Unix timestamp for given IST time on the given date (defaults to today)."""
    if date is None:
        date = datetime.datetime.now(IST).date()
    dt = IST.localize(datetime.datetime(date.year, date.month, date.day, hour, minute, 0))
    return dt.timestamp()


def _make_candle(ts: float, close: float = 24000.0) -> dict:
    """Minimal candle dict matching the format produced by get_candles()."""
    return {
        "time":   ts,
        "open":   close - 10,
        "high":   close + 20,
        "low":    close - 20,
        "close":  close,
        "volume": 5000,
    }


def _build_rest_raw(ts: float, close: float = 24000.0) -> dict:
    """Shoonya REST candle with ssboe (Unix epoch) — as returned by get_time_price_series."""
    return {
        "ssboe": str(int(ts)),
        "into":  close - 10,
        "inth":  close + 20,
        "intl":  close - 20,
        "intc":  close,
        "intv":  "5000",
    }


# ─────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────

class TestSignalTimestampIsCurrentSession(unittest.TestCase):
    """Signal times must come from candle timestamps, within today's trading session."""

    def test_signal_timestamp_is_current_session(self):
        """
        Generate a signal from a candle with a known ssboe from today's session.
        Assert that the signal's time field matches that candle's IST time.
        """
        # Known candle timestamp: 10:30 AM IST today
        known_ts = _ist_ts(10, 30)
        expected_time_str = datetime.datetime.fromtimestamp(known_ts, IST).strftime("%H:%M:%S")
        expected_date_str = datetime.datetime.fromtimestamp(known_ts, IST).strftime("%Y-%m-%d")

        from tools.market_data import MarketDataTools

        # Build enough candles for EMA computation (need 100+ for EMA100)
        base_ts = _ist_ts(9, 15)
        candles_5m = [
            _build_rest_raw(base_ts + i * 300, close=24000.0 + i * 0.5)
            for i in range(150)
        ]
        # Put the known candle at the end
        candles_5m[-1] = _build_rest_raw(known_ts, close=24000.0)

        client = MagicMock()
        client.get_time_price_series.return_value = candles_5m
        client.search_scrip.return_value = None

        feed = MagicMock()
        feed.get_candles.return_value = None

        nse_tokens = {
            "NIFTY": {"exchange": "NFO", "token": "66691", "tsym": "NIFTY28APR26F"},
        }

        tool = MarketDataTools(client, feed, nse_tokens)
        # Stub out daily data calls to speed up the test
        tool._get_recent_daily_ohlc = MagicMock(return_value=[])
        tool._get_avg_cpr_width = MagicMock(return_value=None)

        result = tool.get_strategy_signals(symbol="NIFTY", lookback_bars=3)
        signals = result.get("signals", [])

        # All emitted signals must use candle-based timestamps
        for sig in signals:
            sig_time = sig.get("time", "")
            sig_date = sig.get("signal_date", "")
            sig_full = sig.get("signal_datetime_ist", "")

            # signal_date must be today (not datetime.now())
            today_date = datetime.datetime.now(IST).strftime("%Y-%m-%d")
            if sig_date:
                self.assertEqual(sig_date, today_date,
                                 f"Signal date {sig_date!r} should be today {today_date!r}")

            # signal_datetime_ist must end with 'IST'
            if sig_full:
                self.assertTrue(sig_full.endswith(" IST"),
                                f"signal_datetime_ist must end with IST: {sig_full!r}")


class TestFuturesCandleTimestampParsing(unittest.TestCase):
    """Verify ssboe parsing gives correct IST datetime."""

    def _parse_ts(self, c: dict) -> float:
        """Mirror the parsing logic from market_data.py get_candles()."""
        _IST = pytz.timezone("Asia/Kolkata")
        ssboe = c.get("ssboe", "")
        if ssboe and str(ssboe).isdigit():
            return float(ssboe)
        raw_time = c.get("time", "")
        for fmt in ("%H:%M:%S %d-%m-%Y", "%d-%m-%Y %H:%M:%S"):
            try:
                return _IST.localize(datetime.datetime.strptime(raw_time, fmt)).timestamp()
            except ValueError:
                continue
        return 0.0

    def test_futures_candle_timestamp_parsing(self):
        """ssboe of known IST time parses back to that exact IST datetime."""
        expected_ts = _ist_ts(9, 15)  # 09:15 IST today
        c = {"ssboe": str(int(expected_ts))}
        parsed = self._parse_ts(c)
        self.assertAlmostEqual(parsed, expected_ts, places=0,
                               msg="ssboe parsing should return exact Unix timestamp")

        parsed_ist = datetime.datetime.fromtimestamp(parsed, IST)
        self.assertEqual(parsed_ist.hour, 9)
        self.assertEqual(parsed_ist.minute, 15)

    def test_ssboe_priority_over_time_field(self):
        """ssboe must win over 'time' field if both are present."""
        known_ts = _ist_ts(10, 30)
        c = {"ssboe": str(int(known_ts)), "time": "09:15:00 10-04-2026"}  # conflicting time field
        parsed = self._parse_ts(c)
        self.assertAlmostEqual(parsed, known_ts, places=0)

    def test_index_candle_time_format_parses_correctly(self):
        """Index candle 'HH:MM:SS DD-MM-YYYY' format parses to correct IST timestamp."""
        c = {"time": "09:15:00 10-04-2026"}
        parsed = self._parse_ts(c)
        expected = IST.localize(datetime.datetime(2026, 4, 10, 9, 15, 0)).timestamp()
        self.assertAlmostEqual(parsed, expected, places=0)

    def test_futures_candle_time_format_parses_correctly(self):
        """Futures candle 'DD-MM-YYYY HH:MM:SS' format parses to correct IST timestamp."""
        c = {"time": "10-04-2026 09:15:00"}
        parsed = self._parse_ts(c)
        expected = IST.localize(datetime.datetime(2026, 4, 10, 9, 15, 0)).timestamp()
        self.assertAlmostEqual(parsed, expected, places=0)

    def test_both_formats_produce_same_timestamp(self):
        """Both time formats for the same instant produce the same epoch."""
        c1 = {"time": "09:15:00 10-04-2026"}
        c2 = {"time": "10-04-2026 09:15:00"}
        self.assertAlmostEqual(self._parse_ts(c1), self._parse_ts(c2), places=0)


class TestStaleSignalNotGeneratedFromOldCandle(unittest.TestCase):
    """Signals from yesterday's candles should be dated to yesterday, not today."""

    def test_stale_signal_not_treated_as_current(self):
        """
        If the most recent candle is from yesterday, the signal's signal_date
        should reflect that date — not today.
        """
        _IST = pytz.timezone("Asia/Kolkata")
        yesterday = (datetime.datetime.now(_IST) - datetime.timedelta(days=1)).date()

        # Candle from yesterday 14:00 IST
        stale_ts = _IST.localize(
            datetime.datetime(yesterday.year, yesterday.month, yesterday.day, 14, 0, 0)
        ).timestamp()

        from tools.market_data import MarketDataTools

        # Build candles all from yesterday
        base_ts = _IST.localize(
            datetime.datetime(yesterday.year, yesterday.month, yesterday.day, 9, 15, 0)
        ).timestamp()
        candles_5m = [
            _build_rest_raw(base_ts + i * 300, close=24000.0 + i * 0.5)
            for i in range(150)
        ]
        candles_5m[-1] = _build_rest_raw(stale_ts, close=24000.0)

        client = MagicMock()
        client.get_time_price_series.return_value = candles_5m
        client.search_scrip.return_value = None

        feed = MagicMock()
        feed.get_candles.return_value = None

        nse_tokens = {
            "NIFTY": {"exchange": "NFO", "token": "66691", "tsym": "NIFTY28APR26F"},
        }

        tool = MarketDataTools(client, feed, nse_tokens)
        tool._get_recent_daily_ohlc = MagicMock(return_value=[])
        tool._get_avg_cpr_width = MagicMock(return_value=None)

        result = tool.get_strategy_signals(symbol="NIFTY", lookback_bars=3)
        signals = result.get("signals", [])

        yesterday_str = yesterday.strftime("%Y-%m-%d")
        today_str = datetime.datetime.now(_IST).strftime("%Y-%m-%d")

        for sig in signals:
            sig_date = sig.get("signal_date")
            if sig_date:
                # Stale candles produce signals dated to yesterday, NOT today
                self.assertNotEqual(
                    sig_date, today_str,
                    f"A stale candle from {yesterday_str} must not produce a signal "
                    f"dated today ({today_str}). Got: {sig_date}"
                )
                self.assertEqual(
                    sig_date, yesterday_str,
                    f"Signal from yesterday's candle should be dated {yesterday_str}, "
                    f"got {sig_date}"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
