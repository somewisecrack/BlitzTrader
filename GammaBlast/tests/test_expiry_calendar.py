"""
tests/test_expiry_calendar.py — Additional expiry calendar and config tests.

Run from /home/user/BlitzTrader/GammaBlast/:
    pytest tests/test_expiry_calendar.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from datetime import date

import pytest

import config
from tools.expiry_calendar import (
    holiday_name,
    _EXCHANGE_HOLIDAYS_2026,
)


# ---------------------------------------------------------------------------
# Lot size configuration
# ---------------------------------------------------------------------------

def test_nifty_lot_size_configured():
    """NIFTY lot size must be 25 (per NSE contract specification)."""
    assert config.LOT_SIZE["NIFTY"] == 25, (
        f"Expected LOT_SIZE['NIFTY']=25, got {config.LOT_SIZE['NIFTY']}"
    )


def test_sensex_lot_size_configured():
    """SENSEX lot size must be 10 (per BSE contract specification)."""
    assert config.LOT_SIZE["SENSEX"] == 10, (
        f"Expected LOT_SIZE['SENSEX']=10, got {config.LOT_SIZE['SENSEX']}"
    )


# ---------------------------------------------------------------------------
# Strike step configuration
# ---------------------------------------------------------------------------

def test_nifty_strike_step():
    """NIFTY strike step must be 50 points."""
    assert config.STRIKE_STEP["NIFTY"] == 50, (
        f"Expected STRIKE_STEP['NIFTY']=50, got {config.STRIKE_STEP['NIFTY']}"
    )


def test_sensex_strike_step():
    """SENSEX strike step must be 100 points."""
    assert config.STRIKE_STEP["SENSEX"] == 100, (
        f"Expected STRIKE_STEP['SENSEX']=100, got {config.STRIKE_STEP['SENSEX']}"
    )


# ---------------------------------------------------------------------------
# Session timing
# ---------------------------------------------------------------------------

def test_entry_cutoff_is_1512():
    """ENTRY_CUTOFF_IST must be '15:12' — no new virtual entries after this."""
    assert config.ENTRY_CUTOFF_IST == "15:12", (
        f"Expected ENTRY_CUTOFF_IST='15:12', got '{config.ENTRY_CUTOFF_IST}'"
    )


def test_eod_force_close_is_1515():
    """EOD_FORCE_CLOSE_IST must be '15:15' — all positions force-closed at this time."""
    assert config.EOD_FORCE_CLOSE_IST == "15:15", (
        f"Expected EOD_FORCE_CLOSE_IST='15:15', got '{config.EOD_FORCE_CLOSE_IST}'"
    )


# ---------------------------------------------------------------------------
# Holiday data completeness
# ---------------------------------------------------------------------------

def test_holiday_returns_none():
    """
    Republic Day 2026-01-26 is a known holiday — holiday_name() must return
    its name (non-empty string), and the date must be in the holidays dict.
    """
    republic_day = date(2026, 1, 26)
    assert republic_day in _EXCHANGE_HOLIDAYS_2026, (
        "Republic Day 2026-01-26 must be in _EXCHANGE_HOLIDAYS_2026"
    )
    name = holiday_name(republic_day)
    assert name is not None, "holiday_name() must return a string for Republic Day"
    assert len(name) > 0, "holiday_name() must return a non-empty string"
    assert "Republic" in name, (
        f"Expected holiday name to contain 'Republic', got '{name}'"
    )


def test_holi_is_holiday():
    """Holi 2026-03-03 (a Tuesday) must be in the holiday dict."""
    holi = date(2026, 3, 3)
    assert holi in _EXCHANGE_HOLIDAYS_2026
    name = holiday_name(holi)
    assert name is not None
    assert "Holi" in name


def test_unknown_date_returns_none():
    """A regular trading day must not appear in the holiday dict."""
    regular_day = date(2026, 6, 9)  # Tuesday, non-holiday
    assert holiday_name(regular_day) is None


def test_all_2026_holidays_are_in_dict():
    """Spot-check: the dict should have at least 10 holidays for 2026."""
    assert len(_EXCHANGE_HOLIDAYS_2026) >= 10, (
        f"Expected at least 10 holidays in _EXCHANGE_HOLIDAYS_2026, "
        f"got {len(_EXCHANGE_HOLIDAYS_2026)}"
    )


def test_holidays_are_weekdays():
    """
    All entries in _EXCHANGE_HOLIDAYS_2026 must be weekdays (Mon–Fri).
    Exchanges never list Saturday/Sunday as trading holidays.
    """
    for day, name in _EXCHANGE_HOLIDAYS_2026.items():
        assert day.weekday() < 5, (
            f"Holiday '{name}' on {day} falls on a weekend "
            f"({day.strftime('%A')}) — that's unexpected"
        )
