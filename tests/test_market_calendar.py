"""
tests/test_market_calendar.py - NSE holiday guard tests.
"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.market_calendar import (
    _parse_extra_holidays,
    get_market_holiday_name,
    is_nse_trading_day,
)


class TestMarketCalendar(unittest.TestCase):

    def test_april_14_2026_is_nse_holiday(self):
        day = date(2026, 4, 14)
        self.assertFalse(is_nse_trading_day(day))
        self.assertEqual(
            get_market_holiday_name(day),
            "Dr. Baba Saheb Ambedkar Jayanti",
        )

    def test_regular_weekday_is_trading_day(self):
        self.assertTrue(is_nse_trading_day(date(2026, 4, 15)))

    def test_future_2026_dates_match_nse_equity_derivatives_table(self):
        self.assertFalse(is_nse_trading_day(date(2026, 9, 14)))
        self.assertEqual(get_market_holiday_name(date(2026, 9, 14)), "Ganesh Chaturthi")
        self.assertTrue(is_nse_trading_day(date(2026, 9, 4)))
        self.assertFalse(is_nse_trading_day(date(2026, 11, 24)))

    def test_weekend_is_not_trading_day(self):
        self.assertFalse(is_nse_trading_day(date(2026, 4, 18)))

    def test_extra_holidays_parser(self):
        parsed = _parse_extra_holidays("2026-07-01:Special Holiday,2026-07-02")
        self.assertEqual(parsed[date(2026, 7, 1)], "Special Holiday")
        self.assertEqual(parsed[date(2026, 7, 2)], "Exchange holiday")


if __name__ == "__main__":
    unittest.main(verbosity=2)
