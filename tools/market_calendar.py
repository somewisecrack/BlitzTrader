"""
tools/market_calendar.py - NSE trading-day guardrails.

The systemd timer can only express "weekdays at 09:00"; this module blocks
actual agent startup on exchange holidays before Shoonya/Gemini/feed init.
"""
from __future__ import annotations

import os
from datetime import date, datetime


NSE_TRADING_HOLIDAYS_2026: dict[date, str] = {
    date(2026, 1, 15): "Maharashtra municipal elections",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti/Dussehra",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali - Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}


def _parse_extra_holidays(raw: str | None = None) -> dict[date, str]:
    """
    Parse comma-separated YYYY-MM-DD[:Name] entries from NSE_EXTRA_HOLIDAYS.

    Example:
        NSE_EXTRA_HOLIDAYS=2026-02-01:Special Holiday,2026-07-01
    """
    value = raw if raw is not None else os.environ.get("NSE_EXTRA_HOLIDAYS", "")
    extra: dict[date, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        day_raw, _, name = item.partition(":")
        try:
            day = datetime.strptime(day_raw.strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        extra[day] = name.strip() or "Exchange holiday"
    return extra


def nse_holidays() -> dict[date, str]:
    """Return built-in NSE holidays plus any env-configured additions."""
    holidays = dict(NSE_TRADING_HOLIDAYS_2026)
    holidays.update(_parse_extra_holidays())
    return holidays


def get_market_holiday_name(day: date) -> str | None:
    """Return the holiday name for day, or None if it is not a known holiday."""
    return nse_holidays().get(day)


def is_nse_trading_day(day: date) -> bool:
    """True only for weekdays that are not known NSE trading holidays."""
    return day.weekday() < 5 and get_market_holiday_name(day) is None
