"""
tests/test_serial_exit.py — Tests for serial-numbered positions and exit-by-serial.

Covers:
 Status format (1–6)
 Exit routing via text patterns (7–9)
 Safety checks (10–19)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
import tempfile
import re

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ──────────────────────────────────────────────────────────────
#   STUBS
# ──────────────────────────────────────────────────────────────

@dataclass
class _Leg:
    symbol: str
    tradingsymbol: str
    token: str
    side: str
    qty: int
    entry_price: float
    exit_price: float | None = None
    stop_price: float | None = None
    stop_armed_at_profit_pct: float | None = None
    realized_pnl: float | None = None
    closed_at: str | None = None


@dataclass
class _PairPos:
    pair_name: str
    timeframe: str
    method: str
    z_score: float
    beta: float
    prob_profit: float
    prob_profit_low: float
    prob_profit_high: float
    long_leg: _Leg
    short_leg: _Leg
    margin_used: float
    capital_reserved: float
    opened_at: str
    closed_at: str | None = None
    pnl: float | None = None
    matched_timeframes: list = field(default_factory=list)
    half_life: int = 0


def _make_pair_pos(pair_name="DRREDDY/NESTLEIND", closed=False):
    ll = _Leg("NESTLEIND", "NESTLEIND", "tok_long", "BUY", 204, 1423.40)
    sl = _Leg("DRREDDY", "DRREDDY", "tok_short", "SELL", 228, 1323.60)
    pp = _PairPos(
        pair_name=pair_name,
        timeframe="5m",
        method="EG",
        z_score=2.1,
        beta=0.95,
        prob_profit=62.0,
        prob_profit_low=55.0,
        prob_profit_high=70.0,
        long_leg=ll,
        short_leg=sl,
        margin_used=12000,
        capital_reserved=50000,
        opened_at=datetime.now().isoformat(),
    )
    if closed:
        pp.closed_at = datetime.now().isoformat()
        pp.pnl = -500.0
    return pp


def _make_state_manager(positions=None, daily_pnl=1234.0):
    sm = MagicMock()
    sm.get_state.return_value = {
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": 0.5,
        "positions": positions or [],
        "virtual_capital": 1_000_000,
        "available_balance": 950_000,
        "margin_used": 50_000,
    }
    sm.get_open_positions.return_value = positions or []
    return sm


def _make_pairs_portfolio(positions=None):
    pp = MagicMock()
    pp.positions = positions or []
    pp.get_status.return_value = {
        "realized_pnl": 500.0,
        "unrealized_pnl": 200.0,
        "net_pnl": 700.0,
        "open_pairs": sum(1 for p in (positions or []) if not p.closed_at),
        "positions": [
            {
                "pair": p.pair_name,
                "timeframe": p.timeframe,
                "realized_pnl": p.long_leg.realized_pnl or 0.0,
                "unrealized_pnl": 0.0,
                "closed": bool(p.closed_at),
            }
            for p in (positions or [])
        ],
    }
    return pp


def _futures_position(sym="NIFTY26MAY26F", direction="BUY", qty=65, entry=23684.80):
    return {
        "symbol": sym,
        "direction": direction,
        "quantity": qty,
        "entry_price": entry,
        "entry_time": time.time(),
        "order_id": "abc123",
    }


# ──────────────────────────────────────────────────────────────
#   HELPERS
# ──────────────────────────────────────────────────────────────

def _build_status(
    futures_positions=None,
    pair_positions=None,
    price_map: dict | None = None,
):
    """
    Call build_status_message with stubbed feed / state / portfolio.
    price_map: {token: price} used by the fake live_feed.get_ltp.
    """
    from tools.position_serial import build_status_message

    sm = _make_state_manager(positions=futures_positions or [])
    pair_pos = pair_positions or []
    pp = _make_pairs_portfolio(positions=pair_pos)

    feed = MagicMock()
    price_map = price_map or {}
    feed.get_ltp.side_effect = lambda token: price_map.get(token)

    msg, index = build_status_message(
        state_manager=sm,
        pairs_portfolio=pp,
        live_feed=feed,
        shoonya_client=None,
        active_tokens={
            "NIFTY": {
                "tsym": "NIFTY26MAY26F",
                "token": "tok_nifty",
                "exchange": "NFO",
                "lot_size": 65,
            }
        },
    )
    return msg, index


# ──────────────────────────────────────────────────────────────
#   1. Status format: sections present
# ──────────────────────────────────────────────────────────────

def test_status_message_has_required_sections():
    msg, _ = _build_status()
    assert "Overall" in msg
    assert "Futures" in msg
    assert "Pairs" in msg
    assert "Open Positions" in msg


# ──────────────────────────────────────────────────────────────
#   2. Open futures position gets serial number
# ──────────────────────────────────────────────────────────────

def test_open_futures_position_has_serial():
    pos = _futures_position()
    price_map = {"tok_nifty": 23631.0}
    msg, index = _build_status(futures_positions=[pos], price_map=price_map)
    # Serial line should appear
    assert "1. [Futures]" in msg
    assert "NIFTY26MAY26F" in msg
    assert len(index["positions"]) == 1
    assert index["positions"][0]["serial"] == 1
    assert index["positions"][0]["type"] == "futures"


# ──────────────────────────────────────────────────────────────
#   3. Open pair position gets single serial (both legs)
# ──────────────────────────────────────────────────────────────

def test_open_pair_position_has_serial():
    pp = _make_pair_pos()
    price_map = {"tok_long": 1436.20, "tok_short": 1330.60}
    msg, index = _build_status(pair_positions=[pp], price_map=price_map)
    assert "1. [Pairs]" in msg
    assert "DRREDDY/NESTLEIND" in msg
    assert len(index["positions"]) == 1
    assert index["positions"][0]["serial"] == 1
    assert index["positions"][0]["type"] == "pairs"


# ──────────────────────────────────────────────────────────────
#   4. Closed positions do not appear in Open Positions
# ──────────────────────────────────────────────────────────────

def test_closed_pair_not_in_open_positions():
    closed = _make_pair_pos(closed=True)
    msg, index = _build_status(pair_positions=[closed])
    # Closed pair should NOT produce a serial
    assert "[Pairs]" not in msg
    assert index["positions"] == []


# ──────────────────────────────────────────────────────────────
#   5. No open positions → "Open Positions\n- None"
# ──────────────────────────────────────────────────────────────

def test_no_open_positions_shows_none():
    msg, index = _build_status()
    assert "Open Positions" in msg
    assert "- None" in msg
    assert index["positions"] == []


# ──────────────────────────────────────────────────────────────
#   6. Serial mapping is written to runtime after status generation
# ──────────────────────────────────────────────────────────────

def test_index_persisted_after_status(tmp_path):
    from tools import position_serial as ps_mod

    pos = _futures_position()
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        _, index = _build_status(futures_positions=[pos], price_map={"tok_nifty": 23600.0})
        ps_mod.save_position_index(index)
        assert ps_mod._INDEX_FILE.exists()
        loaded = json.loads(ps_mod._INDEX_FILE.read_text())
        assert len(loaded["positions"]) == 1
        assert "generated_at" in loaded
        assert loaded["ttl_seconds"] == 1800
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   7–9. Exit routing text patterns
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected_serial", [
    ("exit 1", 1),
    ("close position 2", 2),
    ("square off #3", 3),
    ("close serial 2", 2),
    ("close #2", 2),
])
def test_exit_routing_patterns(text, expected_serial):
    exit_patterns = [
        r'\bexit\s+#?(\d+)\b',
        r'\bclose\s+(?:position\s+|serial\s+)?#?(\d+)\b',
        r'\bsquare\s+off\s+#?(\d+)\b',
    ]
    found = None
    for pattern in exit_patterns:
        m = re.search(pattern, text.lower())
        if m:
            found = int(m.group(1))
            break
    assert found == expected_serial, f"Pattern not matched for '{text}'"


# ──────────────────────────────────────────────────────────────
#   10. Invalid serial → no orders, clear error
# ──────────────────────────────────────────────────────────────

def test_invalid_serial_no_orders(tmp_path):
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        # Write a fresh index with serial 1 only
        index = {
            "generated_at": datetime.now(IST).isoformat(),
            "ttl_seconds": 1800,
            "positions": [
                {"serial": 1, "type": "futures", "tradingsymbol": "NIFTY26MAY26F",
                 "direction": "BUY", "qty": 65, "entry_price": 23684.0, "opened_at": "", "token": ""}
            ],
        }
        ps_mod.save_position_index(index)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio()
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=99,
            state_manager=sm,
            pairs_portfolio=pp,
            order_execution=oe,
            shoonya_client=MagicMock(),
            telegram_handler=tg,
        )
        assert result["success"] is False
        assert "not found" in result["error"].lower() or "99" in result["error"]
        oe.close_position.assert_not_called()
        oe.place_virtual_order.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   11. Stale mapping → no orders, asks for fresh status
# ──────────────────────────────────────────────────────────────

def test_stale_index_no_orders(tmp_path):
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        # Write an index that's 2 hours old
        from datetime import timedelta
        old_time = (datetime.now(IST) - timedelta(hours=2)).isoformat()
        index = {
            "generated_at": old_time,
            "ttl_seconds": 1800,
            "positions": [
                {"serial": 1, "type": "futures", "tradingsymbol": "NIFTY26MAY26F",
                 "direction": "BUY", "qty": 65, "entry_price": 23684.0, "opened_at": "", "token": ""}
            ],
        }
        ps_mod.save_position_index(index)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio()
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
            pairs_portfolio=pp,
            order_execution=oe,
            shoonya_client=MagicMock(),
            telegram_handler=tg,
        )
        assert result["success"] is False
        assert "stale" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   12. Already-closed position → no orders, clear error
# ──────────────────────────────────────────────────────────────

def test_already_closed_futures_no_orders(tmp_path):
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        index = {
            "generated_at": datetime.now(IST).isoformat(),
            "ttl_seconds": 1800,
            "positions": [
                {"serial": 1, "type": "futures", "tradingsymbol": "NIFTY26MAY26F",
                 "direction": "BUY", "qty": 65, "entry_price": 23684.0, "opened_at": "", "token": ""}
            ],
        }
        ps_mod.save_position_index(index)

        # State has NO open positions (already closed)
        sm = _make_state_manager(positions=[])
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[])
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
            pairs_portfolio=pp,
            order_execution=oe,
            shoonya_client=MagicMock(),
            telegram_handler=tg,
        )
        assert result["success"] is False
        assert "no longer open" in result["error"].lower() or "already closed" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   13. Valid futures serial → correct close method called
# ──────────────────────────────────────────────────────────────

def test_valid_futures_serial_calls_close(tmp_path):
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        index = {
            "generated_at": datetime.now(IST).isoformat(),
            "ttl_seconds": 1800,
            "positions": [
                {"serial": 1, "type": "futures", "tradingsymbol": "NIFTY26MAY26F",
                 "direction": "BUY", "qty": 65, "entry_price": 23684.0, "opened_at": "", "token": "tok_nifty"}
            ],
        }
        ps_mod.save_position_index(index)

        fut_pos = _futures_position()
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        oe.close_position.return_value = {
            "symbol": "NIFTY26MAY26F",
            "exit_price": 23700.0,
            "pnl": 975.0,
            "status": "CLOSED",
        }
        pp = _make_pairs_portfolio(positions=[])
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
            pairs_portfolio=pp,
            order_execution=oe,
            shoonya_client=MagicMock(),
            telegram_handler=tg,
        )
        assert result["success"] is True
        assert result["type"] == "futures"
        assert result["pnl"] == 975.0
        oe.close_position.assert_called_once_with("NIFTY26MAY26F")
        # Verify Telegram confirmation was sent
        tg.send_telegram.assert_called_once()
        msg = tg.send_telegram.call_args[0][0]
        assert "serial #1" in msg.lower() or "#1" in msg
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   14. Valid pairs serial → both legs closed
# ──────────────────────────────────────────────────────────────

def test_valid_pairs_serial_closes_both_legs(tmp_path):
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_name = "DRREDDY/NESTLEIND"
        index = {
            "generated_at": datetime.now(IST).isoformat(),
            "ttl_seconds": 1800,
            "positions": [
                {
                    "serial": 1, "type": "pairs", "pair_name": pair_name,
                    "opened_at": datetime.now().isoformat(),
                    "long_symbol": "NESTLEIND", "long_tradingsymbol": "NESTLEIND", "long_token": "tok_l",
                    "short_symbol": "DRREDDY", "short_tradingsymbol": "DRREDDY", "short_token": "tok_s",
                }
            ],
        }
        ps_mod.save_position_index(index)

        pair_pos = _make_pair_pos(pair_name=pair_name)
        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])

        # _entry_price returns current market price for closing
        pp._entry_price.side_effect = lambda client, scrip, action: 1436.20 if action == "SELL" else 1330.60
        pp._close_leg.side_effect = lambda leg, price: (price - leg.entry_price) * leg.qty if leg.side == "BUY" else (leg.entry_price - price) * leg.qty
        pp._persist = MagicMock()

        tg = MagicMock()
        client = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
            pairs_portfolio=pp,
            order_execution=oe,
            shoonya_client=client,
            telegram_handler=tg,
        )
        assert result["success"] is True
        assert result["type"] == "pairs"
        assert result["pair_name"] == pair_name
        # Both legs should have been closed (entry_price called for both)
        assert pp._entry_price.call_count == 2
        pp._persist.assert_called()
        tg.send_telegram.assert_called_once()
        msg = tg.send_telegram.call_args[0][0]
        assert "DRREDDY" in msg or "NESTLEIND" in msg
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   15. After exit → mapping invalidated
# ──────────────────────────────────────────────────────────────

def test_index_invalidated_after_futures_exit(tmp_path):
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        index = {
            "generated_at": datetime.now(IST).isoformat(),
            "ttl_seconds": 1800,
            "positions": [
                {"serial": 1, "type": "futures", "tradingsymbol": "NIFTY26MAY26F",
                 "direction": "BUY", "qty": 65, "entry_price": 23684.0, "opened_at": "", "token": ""}
            ],
        }
        ps_mod.save_position_index(index)
        assert ps_mod._INDEX_FILE.exists()

        sm = _make_state_manager(positions=[_futures_position()])
        oe = MagicMock()
        oe.close_position.return_value = {"exit_price": 23700.0, "pnl": 975.0}
        pp = _make_pairs_portfolio(positions=[])
        tg = MagicMock()

        ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
            pairs_portfolio=pp,
            order_execution=oe,
            shoonya_client=MagicMock(),
            telegram_handler=tg,
        )
        # Index file should be deleted after successful exit
        assert not ps_mod._INDEX_FILE.exists()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   16. Tool never opens a new position
# ──────────────────────────────────────────────────────────────

def test_exit_serial_never_calls_place_order(tmp_path):
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        index = {
            "generated_at": datetime.now(IST).isoformat(),
            "ttl_seconds": 1800,
            "positions": [
                {"serial": 1, "type": "futures", "tradingsymbol": "NIFTY26MAY26F",
                 "direction": "BUY", "qty": 65, "entry_price": 23684.0, "opened_at": "", "token": ""}
            ],
        }
        ps_mod.save_position_index(index)

        sm = _make_state_manager(positions=[_futures_position()])
        oe = MagicMock()
        oe.close_position.return_value = {"exit_price": 23700.0, "pnl": 975.0}
        pp = _make_pairs_portfolio(positions=[])

        ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
            pairs_portfolio=pp,
            order_execution=oe,
            shoonya_client=MagicMock(),
            telegram_handler=MagicMock(),
        )
        # place_virtual_order must NEVER be called
        oe.place_virtual_order.assert_not_called()
        oe.close_all_positions.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   17. Existing futures order safety checks unchanged
#       (guardrails in order_execution not touched)
# ──────────────────────────────────────────────────────────────

def test_order_execution_guardrails_module_unchanged():
    """Verify order_execution.py still has all safety checks."""
    import tools.order_execution as oe_mod
    import inspect
    src = inspect.getsource(oe_mod)
    assert "_check_guardrails" in src
    assert "BLOCKED" in src
    assert "_BARE_LOGICAL_NAMES" in src
    # Ensure place_virtual_order is still present
    assert "def place_virtual_order" in src


# ──────────────────────────────────────────────────────────────
#   18. No hardcoded market data introduced
# ──────────────────────────────────────────────────────────────

def test_no_hardcoded_prices_in_serial_module():
    import tools.position_serial as ps_mod
    import inspect
    src = inspect.getsource(ps_mod)
    # There should be no hardcoded numeric literals used as prices
    # (no magic numbers like 23684.80 hardcoded)
    assert "23684" not in src
    assert "23631" not in src
    assert "1423.40" not in src


# ──────────────────────────────────────────────────────────────
#   19. Full existing test suite compatibility
#       (just ensure the new module imports cleanly)
# ──────────────────────────────────────────────────────────────

def test_new_module_imports_cleanly():
    """No import errors from the new position_serial module."""
    from tools import position_serial
    assert hasattr(position_serial, "build_status_message")
    assert hasattr(position_serial, "save_position_index")
    assert hasattr(position_serial, "load_position_index")
    assert hasattr(position_serial, "invalidate_position_index")
    assert hasattr(position_serial, "exit_position_by_serial")


def test_registry_imports_new_tools():
    """ToolRegistry accepts new keyword args without breaking."""
    from tools.registry import ToolRegistry, LIVE_TOOLS
    assert "exit_position_by_serial" in LIVE_TOOLS
    assert "get_status_with_serials" in LIVE_TOOLS
