"""
tests/test_session_guard.py — Tests for tools/expiry_calendar.py (session guard).

Run from /home/user/BlitzTrader/GammaBlast/:
    pytest tests/test_session_guard.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import os
from datetime import date
from unittest.mock import patch

import pytest

from tools.expiry_calendar import (
    active_symbol_for_day,
    is_gammablast_day,
    holiday_name,
    _EXCHANGE_HOLIDAYS_2026,
)
import config


# ---------------------------------------------------------------------------
# active_symbol_for_day
# ---------------------------------------------------------------------------

def test_tuesday_nifty():
    """9 Jun 2026 is a Tuesday — should return NIFTY."""
    assert active_symbol_for_day(date(2026, 6, 9)) == "NIFTY"


def test_thursday_sensex():
    """11 Jun 2026 is a Thursday — should return SENSEX."""
    assert active_symbol_for_day(date(2026, 6, 11)) == "SENSEX"


def test_monday_skips():
    """8 Jun 2026 is a Monday — GammaBlast does not run."""
    assert active_symbol_for_day(date(2026, 6, 8)) is None


def test_wednesday_skips():
    """10 Jun 2026 is a Wednesday — GammaBlast does not run."""
    assert active_symbol_for_day(date(2026, 6, 10)) is None


def test_friday_skips():
    """12 Jun 2026 is a Friday — GammaBlast does not run."""
    assert active_symbol_for_day(date(2026, 6, 12)) is None


def test_holiday_tuesday_skips():
    """
    A Tuesday that is a known holiday should return None.

    2026-03-03 (Holi) is a Tuesday and is in _EXCHANGE_HOLIDAYS_2026.
    Verify via patch to make the test self-documenting and independent of
    calendar drift.
    """
    holi_tuesday = date(2026, 3, 3)
    assert holi_tuesday.weekday() == 1, "Sanity: 2026-03-03 must be a Tuesday"

    # Confirm the holiday is already in the source dict
    assert holi_tuesday in _EXCHANGE_HOLIDAYS_2026

    # active_symbol_for_day should return None despite it being Tuesday
    assert active_symbol_for_day(holi_tuesday) is None


def test_holiday_tuesday_skips_via_mock():
    """
    Patch in a synthetic holiday on the known Tuesday 2026-06-09 and verify
    that active_symbol_for_day returns None even though it is a Tuesday.
    """
    target_tuesday = date(2026, 6, 9)
    assert target_tuesday.weekday() == 1, "Sanity: 2026-06-09 must be a Tuesday"

    extra_env = f"{target_tuesday.isoformat()}:Mock Exchange Holiday"
    with patch.dict(os.environ, {"GAMMABLAST_EXTRA_HOLIDAYS": extra_env}):
        result = active_symbol_for_day(target_tuesday)
    assert result is None, (
        "A Tuesday that is a holiday must return None, not NIFTY"
    )


# ---------------------------------------------------------------------------
# is_gammablast_day
# ---------------------------------------------------------------------------

def test_is_gammablast_day_true():
    """9 Jun 2026 (Tuesday, non-holiday) => GammaBlast day."""
    assert is_gammablast_day(date(2026, 6, 9)) is True


def test_is_gammablast_day_false():
    """10 Jun 2026 (Wednesday) => not a GammaBlast day."""
    assert is_gammablast_day(date(2026, 6, 10)) is False


# ---------------------------------------------------------------------------
# Config guard: verify NO_NEW_ENTRY_AFTER is NOT in config
# (GammaBlast uses 15:12 via ENTRY_CUTOFF_IST, never 15:05)
# ---------------------------------------------------------------------------

def test_no_new_entry_after_15_05():
    """
    config.py must NOT define a NO_NEW_ENTRY_AFTER constant.
    GammaBlast uses ENTRY_CUTOFF_IST = "15:12" instead.
    """
    assert not hasattr(config, "NO_NEW_ENTRY_AFTER"), (
        "config.NO_NEW_ENTRY_AFTER must not exist; use ENTRY_CUTOFF_IST = '15:12'"
    )
