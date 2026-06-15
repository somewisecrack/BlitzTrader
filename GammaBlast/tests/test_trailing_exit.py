"""
tests/test_trailing_exit.py — Tests for tools/trailing_exit.py.

Run from /home/user/BlitzTrader/GammaBlast/:
    pytest tests/test_trailing_exit.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import time

import pytest

from tools.trailing_exit import TrailingExitEngine


# ---------------------------------------------------------------------------
# Fixture: a default engine matching config defaults
# ---------------------------------------------------------------------------

@pytest.fixture
def engine() -> TrailingExitEngine:
    return TrailingExitEngine(
        trail_activation_mult=2.0,
        trail_initial_fraction=0.40,
        trail_tight_mult=4.0,
        trail_tight_fraction=0.25,
        hard_stop_fraction=0.50,
        stale_data_seconds=300,
    )


def _pos(entry_price: float, trail_stop=None, peak_ltp: float = None) -> dict:
    """Build a minimal position dict for testing."""
    if peak_ltp is None:
        peak_ltp = entry_price
    return {
        "position_id": "TEST_POS",
        "entry_price": entry_price,
        "peak_ltp": peak_ltp,
        "trail_stop": trail_stop,
    }


# ---------------------------------------------------------------------------
# evaluate() — exit conditions
# ---------------------------------------------------------------------------

def test_hard_stop(engine: TrailingExitEngine):
    """
    entry=100, current_ltp=49.
    Hard stop fires at <= 100 * 0.50 = 50.
    49 <= 50 => (True, 'HARD_STOP').
    """
    pos = _pos(entry_price=100.0, trail_stop=None)
    should_exit, reason = engine.evaluate(pos, 49.0, time.time())
    assert should_exit is True
    assert reason == "HARD_STOP"


def test_no_exit_normal(engine: TrailingExitEngine):
    """entry=100, current_ltp=110 with fresh quote — no exit."""
    pos = _pos(entry_price=100.0, trail_stop=None)
    should_exit, reason = engine.evaluate(pos, 110.0, time.time())
    assert should_exit is False
    assert reason == ""


def test_trail_stop_hit(engine: TrailingExitEngine):
    """
    Position has trail_stop=150, current_ltp=140.
    140 <= 150 => (True, 'TRAIL_STOP_HIT').
    Hard stop is 100*0.5=50 — not triggered.
    """
    pos = _pos(entry_price=100.0, trail_stop=150.0, peak_ltp=300.0)
    should_exit, reason = engine.evaluate(pos, 140.0, time.time())
    assert should_exit is True
    assert reason == "TRAIL_STOP_HIT"


def test_stale_data_exit(engine: TrailingExitEngine):
    """
    last_quote_time = now - 400s (> 300s threshold) => (True, 'STALE_DATA').
    LTP is well above hard stop so only STALE_DATA triggers.
    """
    pos = _pos(entry_price=100.0, trail_stop=None)
    stale_time = time.time() - 400.0
    should_exit, reason = engine.evaluate(pos, 110.0, stale_time)
    assert should_exit is True
    assert reason == "STALE_DATA"


# ---------------------------------------------------------------------------
# compute_trail_stop()
# ---------------------------------------------------------------------------

def test_trail_activation(engine: TrailingExitEngine):
    """
    entry=100, peak=100, current=190.
    Activation requires current >= 100*2 = 200.
    190 < 200 => compute_trail_stop returns None (not yet active).
    """
    pos = _pos(entry_price=100.0, trail_stop=None, peak_ltp=100.0)
    result = engine.compute_trail_stop(pos, 190.0)
    assert result is None


def test_trail_activates_at_2x(engine: TrailingExitEngine):
    """
    entry=100, peak=210, current=210.
    current >= 200 (activation) but < 400 (tight zone).
    trail = peak * (1 - 0.40) = 210 * 0.60 = 126.
    """
    pos = _pos(entry_price=100.0, trail_stop=None, peak_ltp=210.0)
    result = engine.compute_trail_stop(pos, 210.0)
    assert result == pytest.approx(126.0)


def test_trail_tightens_above_4x(engine: TrailingExitEngine):
    """
    entry=100, peak=420, current=420.
    current >= 400 (tight zone).
    trail = peak * (1 - 0.25) = 420 * 0.75 = 315.
    """
    pos = _pos(entry_price=100.0, trail_stop=None, peak_ltp=420.0)
    result = engine.compute_trail_stop(pos, 420.0)
    assert result == pytest.approx(315.0)


def test_trail_never_decreases(engine: TrailingExitEngine):
    """
    Existing trail_stop=130; newly computed trail would be 125.
    compute_trail_stop must return max(125, 130) = 130 — never loosen.

    Setup: entry=100, peak=208.33... so 208.33*(1-0.40)=125 (approx).
    We use peak=208.34 to get a computed trail just under 125.
    Actually: use peak=208.0 => 208*0.60=124.8, then existing=130 => max=130.
    """
    # peak=208 => trail_initial = 208 * 0.60 = 124.8 < 130
    pos = _pos(entry_price=100.0, trail_stop=130.0, peak_ltp=208.0)
    # current must be >= 200 (activation) but < 400 (tight zone)
    result = engine.compute_trail_stop(pos, 208.0)
    assert result == pytest.approx(130.0), (
        "Trail stop must never move down; existing 130 beats computed 124.8"
    )


# ---------------------------------------------------------------------------
# check_eod_close()
# ---------------------------------------------------------------------------

def test_eod_close(engine: TrailingExitEngine):
    """15:15 is at the EOD threshold => True; 15:14 is before => False."""
    assert engine.check_eod_close("15:15") is True
    assert engine.check_eod_close("15:14") is False


def test_eod_close_after_threshold(engine: TrailingExitEngine):
    """Times past 15:15 also trigger EOD close."""
    assert engine.check_eod_close("15:20") is True
    assert engine.check_eod_close("16:00") is True


def test_eod_close_before_market_close(engine: TrailingExitEngine):
    """Well before 15:15 must return False."""
    assert engine.check_eod_close("09:15") is False
    assert engine.check_eod_close("14:59") is False
