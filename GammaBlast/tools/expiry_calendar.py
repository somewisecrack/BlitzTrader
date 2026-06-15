"""
tools/expiry_calendar.py — GammaBlast expiry-day session guard.

NIFTY expires on Tuesdays; SENSEX expires on Thursdays.
GammaBlast only runs on those specific weekdays, and only when the exchange
is open (not a holiday).

active_symbol_for_day(date) → "NIFTY" | "SENSEX" | None
is_gammablast_day(date) → bool
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional


# NSE/BSE trading holidays 2026 (same exchange, same holiday list)
_EXCHANGE_HOLIDAYS_2026: dict[date, str] = {
    date(2026, 1, 15): "Maharashtra municipal elections",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3):  "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3):  "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1):  "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti/Dussehra",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali - Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}

# weekday constants
_TUESDAY  = 1
_THURSDAY = 3


def _extra_holidays() -> dict[date, str]:
    """Parse GAMMABLAST_EXTRA_HOLIDAYS env var (comma-sep YYYY-MM-DD[:Name])."""
    raw = os.environ.get("GAMMABLAST_EXTRA_HOLIDAYS", "")
    extra: dict[date, str] = {}
    for item in raw.split(","):
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


def all_holidays() -> dict[date, str]:
    h = dict(_EXCHANGE_HOLIDAYS_2026)
    h.update(_extra_holidays())
    return h


def is_exchange_open(day: date) -> bool:
    return day.weekday() < 5 and day not in all_holidays()


def is_nifty_expiry_day(day: date) -> bool:
    """True when NIFTY weekly expiry falls on this day (Tuesdays, exchange open)."""
    return day.weekday() == _TUESDAY and is_exchange_open(day)


def is_sensex_expiry_day(day: date) -> bool:
    """True when SENSEX weekly expiry falls on this day (Thursdays, exchange open)."""
    return day.weekday() == _THURSDAY and is_exchange_open(day)


def active_symbol_for_day(day: Optional[date] = None) -> Optional[str]:
    """
    Return "NIFTY" on Tuesdays, "SENSEX" on Thursdays (exchange open days only).
    Returns None on all other days — GammaBlast should not run.
    """
    if day is None:
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if is_nifty_expiry_day(day):
        return "NIFTY"
    if is_sensex_expiry_day(day):
        return "SENSEX"
    return None


def is_gammablast_day(day: Optional[date] = None) -> bool:
    return active_symbol_for_day(day) is not None


def holiday_name(day: date) -> Optional[str]:
    return all_holidays().get(day)
