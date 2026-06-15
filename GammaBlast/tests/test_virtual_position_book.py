"""
tests/test_virtual_position_book.py — Tests for tools/virtual_position_book.py.

Run from /home/user/BlitzTrader/GammaBlast/:
    pytest tests/test_virtual_position_book.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import json
import tempfile
from pathlib import Path

import pytest

import config
from tools.virtual_position_book import VirtualPositionBook


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

LOT_SIZES = {"NIFTY": 25, "SENSEX": 10}

EXPIRY = "10-JUN-2026"


def _make_book(tmp_dir: str) -> VirtualPositionBook:
    """Return a fresh VirtualPositionBook backed by a temp state file."""
    state_file = Path(tmp_dir) / "live_state.json"
    return VirtualPositionBook(state_file=state_file, lot_sizes=LOT_SIZES)


def _open_nifty_position(book: VirtualPositionBook, strike: int = 24500,
                          option_type: str = "CE", entry_price: float = 100.0) -> str:
    """Helper: open one NIFTY position and return the position_id."""
    return book.open_position(
        symbol="NIFTY",
        expiry=EXPIRY,
        strike=strike,
        option_type=option_type,
        tsym=f"NIFTY10JUN26{option_type[0]}{strike}",
        token="12345",
        exchange="NFO",
        lots=1,
        lot_size=25,
        entry_price=entry_price,
        entry_time="2026-06-09T10:30:00",
    )


# ---------------------------------------------------------------------------
# Position lifecycle
# ---------------------------------------------------------------------------

def test_open_position():
    """Open a position; it must appear in open_positions()."""
    with tempfile.TemporaryDirectory() as tmp:
        book = _make_book(tmp)
        pos_id = _open_nifty_position(book)

        open_pos = book.open_positions()
        assert len(open_pos) == 1
        assert open_pos[0]["position_id"] == pos_id
        assert open_pos[0]["status"] == "OPEN"
        assert open_pos[0]["symbol"] == "NIFTY"
        assert open_pos[0]["strike"] == 24500
        assert open_pos[0]["option_type"] == "CE"


def test_one_lot_per_strike():
    """
    After opening NIFTY 24500 CE, can_open() must return False for the
    same contract (symbol, expiry, strike, option_type).
    """
    with tempfile.TemporaryDirectory() as tmp:
        book = _make_book(tmp)
        _open_nifty_position(book, strike=24500, option_type="CE")

        can = book.can_open("NIFTY", EXPIRY, 24500, "CE")
        assert can is False


def test_no_duplicate_open_position():
    """
    open_position() must raise ValueError if the same
    (symbol, expiry, strike, option_type) already exists.
    """
    with tempfile.TemporaryDirectory() as tmp:
        book = _make_book(tmp)
        _open_nifty_position(book, strike=24500, option_type="CE")

        with pytest.raises(ValueError, match="already exists"):
            _open_nifty_position(book, strike=24500, option_type="CE")


def test_close_position_pnl():
    """
    Open at 100, close at 200.
    PnL = (200 - 100) * 1 lot * 25 lot_size = 2500.
    """
    with tempfile.TemporaryDirectory() as tmp:
        book = _make_book(tmp)
        pos_id = _open_nifty_position(book, entry_price=100.0)

        pnl = book.close_position(
            position_id=pos_id,
            exit_price=200.0,
            exit_time="2026-06-09T14:00:00",
            exit_reason="TRAIL_STOP_HIT",
        )

        assert pnl == pytest.approx(2500.0)
        # Position should now be CLOSED
        all_pos = book.all_positions()
        assert len(all_pos) == 1
        assert all_pos[0]["status"] == "CLOSED"
        assert all_pos[0]["pnl"] == pytest.approx(2500.0)
        # No longer appears in open_positions
        assert book.open_positions() == []


def test_peak_ltp_monotonic():
    """
    update_ltp(500) then update_ltp(300) — peak_ltp must stay at 500.
    """
    with tempfile.TemporaryDirectory() as tmp:
        book = _make_book(tmp)
        pos_id = _open_nifty_position(book, entry_price=100.0)

        book.update_ltp(pos_id, 500.0)
        book.update_ltp(pos_id, 300.0)

        pos = book.positions[pos_id]
        assert pos["peak_ltp"] == pytest.approx(500.0)
        assert pos["current_ltp"] == pytest.approx(300.0)


def test_state_persistence():
    """
    Open a position, save state; create a new VirtualPositionBook from
    the same state_file; the position must be present in the new instance.
    """
    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "live_state.json"

        book1 = VirtualPositionBook(state_file=state_file, lot_sizes=LOT_SIZES)
        pos_id = _open_nifty_position(book1)

        # State is saved automatically after open_position.
        # Create a fresh book from the same file.
        book2 = VirtualPositionBook(state_file=state_file, lot_sizes=LOT_SIZES)

        assert pos_id in book2.positions
        loaded = book2.positions[pos_id]
        assert loaded["symbol"] == "NIFTY"
        assert loaded["strike"] == 24500
        assert loaded["option_type"] == "CE"
        assert loaded["status"] == "OPEN"


# ---------------------------------------------------------------------------
# Config guard tests
# ---------------------------------------------------------------------------

def test_no_global_max_position_cap():
    """config.py must NOT define MAX_OPEN_POSITIONS."""
    assert not hasattr(config, "MAX_OPEN_POSITIONS"), (
        "config.MAX_OPEN_POSITIONS must not exist in GammaBlast"
    )


def test_no_max_risk_per_trade():
    """config.py must NOT define MAX_RISK_PER_TRADE_PCT."""
    assert not hasattr(config, "MAX_RISK_PER_TRADE_PCT"), (
        "config.MAX_RISK_PER_TRADE_PCT must not exist in GammaBlast"
    )


def test_no_max_daily_loss():
    """config.py must NOT define MAX_DAILY_LOSS_PCT."""
    assert not hasattr(config, "MAX_DAILY_LOSS_PCT"), (
        "config.MAX_DAILY_LOSS_PCT must not exist in GammaBlast"
    )


def test_entry_cutoff_not_1505():
    """
    config.ENTRY_CUTOFF_IST must be '15:12' — never '15:05'.
    GammaBlast uses 15:12 to allow the 15:10 candle to confirm before cutoff.
    """
    assert config.ENTRY_CUTOFF_IST == "15:12", (
        f"Expected ENTRY_CUTOFF_IST='15:12', got '{config.ENTRY_CUTOFF_IST}'"
    )
