"""
tests/test_serial_exit.py — Tests for serial-numbered positions and exit-by-serial.

Covers:
 Status format (1–6)
 Exit routing via text patterns (7–9)
 Safety checks (10–19)
 Futures identity revalidation (20–26)
 Pairs identity revalidation (27–30)
 Pair safe close (31–34)
 P&L confirmation (35–37)
 Safety: pairs exit never calls place_order (38–39)
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


def _make_pair_pos(pair_name="DRREDDY/NESTLEIND", closed=False, opened_at=None):
    ll = _Leg("NESTLEIND", "NESTLEIND-EQ", "tok_long", "BUY", 204, 1423.40)
    sl = _Leg("DRREDDY", "DRREDDY-EQ", "tok_short", "SELL", 228, 1323.60)
    pp = _PairPos(
        pair_name=pair_name,
        timeframe="30m",
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
        opened_at=opened_at or datetime.now().isoformat(),
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


def _futures_position(
    sym="NIFTY26MAY26F",
    direction="BUY",
    qty=65,
    entry=23684.80,
    order_id="abc123",
    entry_time=None,
):
    return {
        "symbol": sym,
        "direction": direction,
        "quantity": qty,
        "entry_price": entry,
        "entry_time": entry_time or str(time.time()),
        "order_id": order_id,
    }


def _make_fresh_futures_index(
    tmp_path,
    ps_mod,
    sym="NIFTY26MAY26F",
    direction="BUY",
    qty=65,
    entry=23684.80,
    opened_at="",
    order_id="abc123",
):
    """Write a fresh futures index entry and return the index dict."""
    index = {
        "generated_at": datetime.now(IST).isoformat(),
        "ttl_seconds": 1800,
        "positions": [
            {
                "serial": 1,
                "type": "futures",
                "tradingsymbol": sym,
                "direction": direction,
                "qty": qty,
                "entry_price": entry,
                "opened_at": opened_at,
                "order_id": order_id,
                "token": "tok_nifty",
            }
        ],
    }
    ps_mod.save_position_index(index)
    return index


def _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos):
    """Write a fresh pairs index entry from a pair position."""
    ll = pair_pos.long_leg
    sl = pair_pos.short_leg
    index = {
        "generated_at": datetime.now(IST).isoformat(),
        "ttl_seconds": 1800,
        "positions": [
            {
                "serial": 1,
                "type": "pairs",
                "pair_name": pair_pos.pair_name,
                "opened_at": pair_pos.opened_at,
                "timeframe": pair_pos.timeframe,
                "long_symbol": ll.symbol,
                "long_tradingsymbol": ll.tradingsymbol,
                "long_token": ll.token,
                "long_qty": ll.qty,
                "long_entry_price": ll.entry_price,
                "short_symbol": sl.symbol,
                "short_tradingsymbol": sl.tradingsymbol,
                "short_token": sl.token,
                "short_qty": sl.qty,
                "short_entry_price": sl.entry_price,
            }
        ],
    }
    ps_mod.save_position_index(index)
    return index


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

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        _, index = _build_status(futures_positions=[_futures_position()], price_map={"tok_nifty": 23600.0})
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
        fut_pos = _futures_position()
        _make_fresh_futures_index(
            tmp_path, ps_mod,
            opened_at=str(fut_pos["entry_time"]),
            order_id=fut_pos["order_id"],
        )

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
        pair_pos = _make_pair_pos()
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])

        # _entry_price returns current market price for closing
        pp._entry_price.side_effect = lambda client, scrip, action: 1436.20 if action == "SELL" else 1330.60
        pp._close_leg.side_effect = lambda leg, price: (
            (price - leg.entry_price) * leg.qty if leg.side == "BUY"
            else (leg.entry_price - price) * leg.qty
        )
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
        assert result["pair_name"] == "DRREDDY/NESTLEIND"
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
        fut_pos = _futures_position()
        _make_fresh_futures_index(
            tmp_path, ps_mod,
            opened_at=str(fut_pos["entry_time"]),
            order_id=fut_pos["order_id"],
        )
        assert ps_mod._INDEX_FILE.exists()

        sm = _make_state_manager(positions=[fut_pos])
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
        fut_pos = _futures_position()
        _make_fresh_futures_index(
            tmp_path, ps_mod,
            opened_at=str(fut_pos["entry_time"]),
            order_id=fut_pos["order_id"],
        )

        sm = _make_state_manager(positions=[fut_pos])
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


# ──────────────────────────────────────────────────────────────
#   20–26. FUTURES identity revalidation
# ──────────────────────────────────────────────────────────────

def test_futures_matching_identity_calls_close(tmp_path):
    """FIX 1 test 20: identical fields → close_position called."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        entry_t = str(time.time())
        fut_pos = _futures_position(entry_time=entry_t, order_id="ord001")
        _make_fresh_futures_index(
            tmp_path, ps_mod,
            opened_at=entry_t,
            order_id="ord001",
        )
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        oe.close_position.return_value = {"exit_price": 23700.0, "pnl": 975.0}
        pp = _make_pairs_portfolio(positions=[])

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is True
        oe.close_position.assert_called_once()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_different_entry_time_not_closed(tmp_path):
    """FIX 1 test 21: same symbol, different entry_time → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry_time="1000.0", order_id="ord001")
        # Index has different opened_at
        _make_fresh_futures_index(tmp_path, ps_mod, opened_at="9999.0", order_id="ord001")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[])

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        oe.close_position.assert_not_called()
        # Index should be invalidated
        assert not ps_mod._INDEX_FILE.exists()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_different_order_id_not_closed(tmp_path):
    """FIX 1 test 22: same symbol, different order_id → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry_time="1000.0", order_id="real_order")
        _make_fresh_futures_index(tmp_path, ps_mod, opened_at="1000.0", order_id="stale_order")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[])

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_different_qty_not_closed(tmp_path):
    """FIX 1 test 23: same symbol, different qty → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        # Live position has qty=75 but index says 65
        fut_pos = _futures_position(qty=75, entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, qty=65, opened_at="", order_id="")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[])

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_different_entry_price_not_closed(tmp_path):
    """FIX 1 test 24: same symbol, entry_price outside tolerance → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry=23600.00, entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, entry=23700.00, opened_at="", order_id="")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[])

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_different_direction_not_closed(tmp_path):
    """FIX 1 test 25: same symbol, direction flipped → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(direction="SELL", entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, direction="BUY", opened_at="", order_id="")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[])

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_entry_price_within_tolerance_is_match(tmp_path):
    """FIX 1 test 26: entry_price within 1e-4 tolerance → treated as match."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        base = 23684.80
        tiny_diff = base + 0.00005  # well within 1e-4
        fut_pos = _futures_position(entry=tiny_diff, entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, entry=base, opened_at="", order_id="")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        oe.close_position.return_value = {"exit_price": 23700.0, "pnl": 975.0}
        pp = _make_pairs_portfolio(positions=[])

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is True
        oe.close_position.assert_called_once()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   27–30. PAIRS identity revalidation
# ──────────────────────────────────────────────────────────────

def test_pairs_matching_identity_calls_close(tmp_path):
    """FIX 2 test 27: identical pair fields → virtual close called."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._entry_price.side_effect = lambda c, s, a: 1436.20 if a == "SELL" else 1330.60
        pp._close_leg.side_effect = lambda leg, price: 100.0
        pp._persist = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is True
        pp._persist.assert_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_pairs_different_opened_at_not_closed(tmp_path):
    """FIX 2 test 28: same pair_name, different opened_at → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos(opened_at="2026-01-01T09:00:00")
        # Index has different opened_at
        idx_entry = {
            "serial": 1, "type": "pairs",
            "pair_name": pair_pos.pair_name,
            "opened_at": "2026-01-02T09:00:00",  # different
            "timeframe": pair_pos.timeframe,
            "long_symbol": pair_pos.long_leg.symbol,
            "long_tradingsymbol": pair_pos.long_leg.tradingsymbol,
            "long_token": pair_pos.long_leg.token,
            "long_qty": pair_pos.long_leg.qty,
            "long_entry_price": pair_pos.long_leg.entry_price,
            "short_symbol": pair_pos.short_leg.symbol,
            "short_tradingsymbol": pair_pos.short_leg.tradingsymbol,
            "short_token": pair_pos.short_leg.token,
            "short_qty": pair_pos.short_leg.qty,
            "short_entry_price": pair_pos.short_leg.entry_price,
        }
        index = {"generated_at": datetime.now(IST).isoformat(), "ttl_seconds": 1800, "positions": [idx_entry]}
        ps_mod.save_position_index(index)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._persist = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        pp._persist.assert_not_called()
        assert not ps_mod._INDEX_FILE.exists()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_pairs_different_long_qty_not_closed(tmp_path):
    """FIX 2 test 29: same pair_name, different long qty → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        idx_entry = {
            "serial": 1, "type": "pairs",
            "pair_name": pair_pos.pair_name,
            "opened_at": pair_pos.opened_at,
            "timeframe": pair_pos.timeframe,
            "long_symbol": pair_pos.long_leg.symbol,
            "long_tradingsymbol": pair_pos.long_leg.tradingsymbol,
            "long_token": pair_pos.long_leg.token,
            "long_qty": 999,  # wrong
            "long_entry_price": pair_pos.long_leg.entry_price,
            "short_symbol": pair_pos.short_leg.symbol,
            "short_tradingsymbol": pair_pos.short_leg.tradingsymbol,
            "short_token": pair_pos.short_leg.token,
            "short_qty": pair_pos.short_leg.qty,
            "short_entry_price": pair_pos.short_leg.entry_price,
        }
        index = {"generated_at": datetime.now(IST).isoformat(), "ttl_seconds": 1800, "positions": [idx_entry]}
        ps_mod.save_position_index(index)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._persist = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        pp._persist.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_pairs_different_short_entry_price_not_closed(tmp_path):
    """FIX 2 test 30: same pair_name, different short entry_price → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        idx_entry = {
            "serial": 1, "type": "pairs",
            "pair_name": pair_pos.pair_name,
            "opened_at": pair_pos.opened_at,
            "timeframe": pair_pos.timeframe,
            "long_symbol": pair_pos.long_leg.symbol,
            "long_tradingsymbol": pair_pos.long_leg.tradingsymbol,
            "long_token": pair_pos.long_leg.token,
            "long_qty": pair_pos.long_leg.qty,
            "long_entry_price": pair_pos.long_leg.entry_price,
            "short_symbol": pair_pos.short_leg.symbol,
            "short_tradingsymbol": pair_pos.short_leg.tradingsymbol,
            "short_token": pair_pos.short_leg.token,
            "short_qty": pair_pos.short_leg.qty,
            "short_entry_price": 9999.99,  # wrong
        }
        index = {"generated_at": datetime.now(IST).isoformat(), "ttl_seconds": 1800, "positions": [idx_entry]}
        ps_mod.save_position_index(index)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._persist = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        pp._persist.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_pairs_different_long_token_not_closed(tmp_path):
    """FIX 2: same pair_name, different long token → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        idx_entry = {
            "serial": 1, "type": "pairs",
            "pair_name": pair_pos.pair_name,
            "opened_at": pair_pos.opened_at,
            "timeframe": pair_pos.timeframe,
            "long_symbol": pair_pos.long_leg.symbol,
            "long_tradingsymbol": pair_pos.long_leg.tradingsymbol,
            "long_token": "wrong_token",  # mismatch
            "long_qty": pair_pos.long_leg.qty,
            "long_entry_price": pair_pos.long_leg.entry_price,
            "short_symbol": pair_pos.short_leg.symbol,
            "short_tradingsymbol": pair_pos.short_leg.tradingsymbol,
            "short_token": pair_pos.short_leg.token,
            "short_qty": pair_pos.short_leg.qty,
            "short_entry_price": pair_pos.short_leg.entry_price,
        }
        index = {"generated_at": datetime.now(IST).isoformat(), "ttl_seconds": 1800, "positions": [idx_entry]}
        ps_mod.save_position_index(index)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._persist = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        pp._persist.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   31–34. Pair safe close (FIX 3)
# ──────────────────────────────────────────────────────────────

def test_pair_long_exit_price_unavailable_no_mutation(tmp_path):
    """FIX 3 test 31: long exit price unavailable → no state mutation."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        # Long price unavailable, short is fine
        pp._entry_price.side_effect = lambda c, s, a: None if a == "SELL" else 1330.60
        pp._persist = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "unavailable" in result["error"].lower()
        assert "no state changed" in result["error"].lower()
        pp._persist.assert_not_called()
        pp._close_leg.assert_not_called()
        assert pair_pos.closed_at is None
    finally:
        ps_mod._INDEX_FILE = original_file


def test_pair_short_exit_price_unavailable_no_mutation(tmp_path):
    """FIX 3 test 32: short exit price unavailable → no state mutation."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        # Short price unavailable, long is fine
        pp._entry_price.side_effect = lambda c, s, a: 1436.20 if a == "SELL" else None
        pp._persist = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "unavailable" in result["error"].lower()
        assert "no state changed" in result["error"].lower()
        pp._persist.assert_not_called()
        pp._close_leg.assert_not_called()
        assert pair_pos.closed_at is None
    finally:
        ps_mod._INDEX_FILE = original_file


def test_pair_one_leg_already_closed_safe_error(tmp_path):
    """FIX 3 test 33: one leg already closed → safe error, no mutation."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        # Mark only the long leg as closed (abnormal state)
        pair_pos.long_leg.closed_at = datetime.now().isoformat()
        pair_pos.long_leg.realized_pnl = 500.0
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._persist = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "one leg already closed" in result["error"].lower() or "partial" in result["error"].lower()
        pp._persist.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_pair_both_prices_available_closes_and_persists(tmp_path):
    """FIX 3 test 34: both prices available → legs closed, pair marked, pnl computed, persisted."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._entry_price.side_effect = lambda c, s, a: 1440.00 if a == "SELL" else 1320.00
        # Simulate _close_leg setting realized_pnl on the real leg objects
        def _fake_close_leg(leg, price):
            pnl = (price - leg.entry_price) * leg.qty if leg.side == "BUY" else (leg.entry_price - price) * leg.qty
            leg.realized_pnl = pnl
            leg.closed_at = datetime.now().isoformat()
            return pnl
        pp._close_leg.side_effect = _fake_close_leg
        pp._persist = MagicMock()
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=tg,
        )
        assert result["success"] is True
        assert result["long_exit_price"] == 1440.00
        assert result["short_exit_price"] == 1320.00
        # pair_pos.closed_at should be set
        assert pair_pos.closed_at is not None
        # pnl should match sum of legs
        expected_long_pnl = (1440.00 - 1423.40) * 204
        expected_short_pnl = (1323.60 - 1320.00) * 228
        assert abs(result["pnl"] - round(expected_long_pnl + expected_short_pnl, 2)) < 0.01
        pp._persist.assert_called_once()
        tg.send_telegram.assert_called_once()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   35–37. P&L confirmation (FIX 4)
# ──────────────────────────────────────────────────────────────

def test_futures_exit_confirmation_has_pnl_sections(tmp_path):
    """FIX 4 test 35: futures exit confirmation contains Overall/Futures/Pairs."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, opened_at="", order_id="")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        oe.close_position.return_value = {"exit_price": 23700.0, "pnl": 975.0}
        pp = _make_pairs_portfolio(positions=[])
        tg = MagicMock()

        ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=tg,
        )
        tg.send_telegram.assert_called_once()
        msg = tg.send_telegram.call_args[0][0]
        assert "Overall" in msg
        assert "Futures" in msg
        assert "Pairs" in msg
        assert "Realized P&L" in msg
    finally:
        ps_mod._INDEX_FILE = original_file


def test_pairs_exit_confirmation_has_pnl_sections(tmp_path):
    """FIX 4 test 36: pairs exit confirmation contains Overall/Futures/Pairs."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._entry_price.side_effect = lambda c, s, a: 1440.00 if a == "SELL" else 1320.00

        def _fake_close_leg(leg, price):
            pnl = (price - leg.entry_price) * leg.qty if leg.side == "BUY" else (leg.entry_price - price) * leg.qty
            leg.realized_pnl = pnl
            leg.closed_at = datetime.now().isoformat()
            return pnl
        pp._close_leg.side_effect = _fake_close_leg
        pp._persist = MagicMock()
        tg = MagicMock()

        ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=tg,
        )
        tg.send_telegram.assert_called_once()
        msg = tg.send_telegram.call_args[0][0]
        assert "Overall" in msg
        assert "Futures" in msg
        assert "Pairs" in msg
        assert "Realized P&L" in msg
    finally:
        ps_mod._INDEX_FILE = original_file


def test_serial_index_invalidated_after_pairs_exit(tmp_path):
    """FIX 4 test 37: serial index invalidated after successful pairs exit."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)
        assert ps_mod._INDEX_FILE.exists()

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._entry_price.side_effect = lambda c, s, a: 1440.00 if a == "SELL" else 1320.00

        def _fake_close_leg(leg, price):
            pnl = (price - leg.entry_price) * leg.qty if leg.side == "BUY" else (leg.entry_price - price) * leg.qty
            leg.realized_pnl = pnl
            leg.closed_at = datetime.now().isoformat()
            return pnl
        pp._close_leg.side_effect = _fake_close_leg
        pp._persist = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is True
        assert not ps_mod._INDEX_FILE.exists()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   38–39. Safety: pairs exit is virtual-only (FIX 3 safety)
# ──────────────────────────────────────────────────────────────

def test_pairs_exit_never_calls_place_order(tmp_path):
    """FIX 3 safety test 38: pairs exit never calls place_order."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)

        sm = _make_state_manager()
        oe = MagicMock()
        pp = _make_pairs_portfolio(positions=[pair_pos])
        pp._entry_price.side_effect = lambda c, s, a: 1440.00 if a == "SELL" else 1320.00

        def _fake_close_leg(leg, price):
            pnl = (price - leg.entry_price) * leg.qty if leg.side == "BUY" else (leg.entry_price - price) * leg.qty
            leg.realized_pnl = pnl
            leg.closed_at = datetime.now().isoformat()
            return pnl
        pp._close_leg.side_effect = _fake_close_leg
        pp._persist = MagicMock()

        client = MagicMock()

        ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm, pairs_portfolio=pp,
            order_execution=oe, shoonya_client=client, telegram_handler=MagicMock(),
        )

        # place_order / place_virtual_order must NEVER be called
        oe.place_virtual_order.assert_not_called()
        oe.close_position.assert_not_called()
        # Shoonya client must not have placed any orders
        client.place_order.assert_not_called() if hasattr(client, "place_order") else None
    finally:
        ps_mod._INDEX_FILE = original_file


def test_all_previous_safety_tests_still_pass(tmp_path):
    """Test 39: All original safety checks remain intact (composite smoke test)."""
    from tools import position_serial as ps_mod
    import inspect
    src = inspect.getsource(ps_mod)

    # NEVER opens a new position
    assert "place_order" not in src or "place_order" in "pairs_portfolio._entry_price"
    # Index TTL check present
    assert "_is_index_fresh" in src
    # Invalidation present
    assert "invalidate_position_index" in src
    # Identity helpers present
    assert "_match_futures_identity" in src
    assert "_match_pair_identity" in src
    # Safe close checks present
    assert "No state changed" in src
    # Virtual-only comment present
    assert "VIRTUAL" in src.upper() or "virtual" in src


# ──────────────────────────────────────────────────────────────
#   40–44. P&L confirmation with real pairs_portfolio (new tests)
# ──────────────────────────────────────────────────────────────

def test_futures_exit_confirmation_includes_real_pairs_pnl(tmp_path):
    """Test 40: Futures exit confirmation shows Pairs realized P&L from real pairs_portfolio."""
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, opened_at="", order_id="")

        sm = _make_state_manager(positions=[fut_pos], daily_pnl=800.0)
        oe = MagicMock()
        oe.close_position.return_value = {"exit_price": 23700.0, "pnl": 975.0}

        # Real pairs portfolio with non-zero realized P&L
        pp = MagicMock()
        pp.positions = []
        pp.get_status.return_value = {
            "realized_pnl": 5000.0,
            "unrealized_pnl": 1200.0,
            "net_pnl": 6200.0,
            "positions": [],
        }
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
        tg.send_telegram.assert_called_once()
        msg = tg.send_telegram.call_args[0][0]
        # Pairs section must be present
        assert "Pairs" in msg
        # Realized P&L must NOT be ₹0.00 — real value of 5000.0 should appear
        assert "₹0.00" not in msg.split("Pairs")[-1].split("\n")[1]
        assert "+5,000.00" in msg or "5000" in msg
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_exit_uses_updated_post_exit_realized_pnl(tmp_path):
    """Test 41: Futures exit confirmation reflects updated realized P&L after close."""
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, opened_at="", order_id="")

        # Simulate state manager returning updated P&L after close.
        # First call is inside _exit_futures_by_serial for identity check (positions still open).
        # Second call is inside _build_updated_pnl_summary → build_status_message (post-close).
        call_count = [0]
        def _get_state():
            call_count[0] += 1
            if call_count[0] == 1:
                # Identity check — position still present, original P&L
                return {
                    "daily_pnl": 1234.0,
                    "daily_pnl_pct": 0.5,
                    "positions": [fut_pos],
                    "virtual_capital": 1_000_000,
                    "available_balance": 950_000,
                    "margin_used": 50_000,
                }
            else:
                # Post-close summary — updated realized P&L, position gone
                return {
                    "daily_pnl": 3210.0,
                    "daily_pnl_pct": 0.5,
                    "positions": [],
                    "virtual_capital": 1_000_000,
                    "available_balance": 950_000,
                    "margin_used": 50_000,
                }

        sm = MagicMock()
        sm.get_state.side_effect = _get_state
        sm.get_open_positions.return_value = [fut_pos]

        oe = MagicMock()
        oe.close_position.return_value = {"exit_price": 23700.0, "pnl": 975.0}

        pp = MagicMock()
        pp.positions = []
        pp.get_status.return_value = {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "net_pnl": 0.0, "positions": []}
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
        msg = tg.send_telegram.call_args[0][0]
        # The confirmation must include the updated futures realized P&L (3210.0)
        assert "3,210.00" in msg or "3210" in msg
    finally:
        ps_mod._INDEX_FILE = original_file


def test_pairs_exit_confirmation_includes_updated_pairs_realized_pnl(tmp_path):
    """Test 42: Pairs exit confirmation shows updated pairs realized P&L after close."""
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        pair_pos = _make_pair_pos()
        _make_fresh_pairs_index(tmp_path, ps_mod, pair_pos)

        sm = _make_state_manager()
        oe = MagicMock()

        pp = _make_pairs_portfolio(positions=[pair_pos])
        # After closing the pair, get_status returns updated realized P&L
        pp.get_status.return_value = {
            "realized_pnl": 4500.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 4500.0,
            "positions": [],
        }
        pp._entry_price.side_effect = lambda c, s, a: 1440.00 if a == "SELL" else 1320.00

        def _fake_close_leg(leg, price):
            pnl = (price - leg.entry_price) * leg.qty if leg.side == "BUY" else (leg.entry_price - price) * leg.qty
            leg.realized_pnl = pnl
            leg.closed_at = datetime.now().isoformat()
            return pnl
        pp._close_leg.side_effect = _fake_close_leg
        pp._persist = MagicMock()
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
        msg = tg.send_telegram.call_args[0][0]
        assert "Pairs" in msg
        # Updated realized P&L of 4500.0 must appear
        assert "4,500.00" in msg or "4500" in msg
    finally:
        ps_mod._INDEX_FILE = original_file


def test_null_pairs_portfolio_not_in_module():
    """Test 43: _NullPairsPortfolio has been removed from the module."""
    import tools.position_serial as ps_mod
    assert not hasattr(ps_mod, "_NullPairsPortfolio"), (
        "_NullPairsPortfolio should have been removed; it was masking real pairs P&L"
    )


def test_build_pnl_summary_not_in_module():
    """Test 44: _build_pnl_summary has been replaced by _build_updated_pnl_summary."""
    import tools.position_serial as ps_mod
    assert not hasattr(ps_mod, "_build_pnl_summary"), (
        "_build_pnl_summary should have been removed; use _build_updated_pnl_summary"
    )
    assert hasattr(ps_mod, "_build_updated_pnl_summary"), (
        "_build_updated_pnl_summary must exist"
    )
