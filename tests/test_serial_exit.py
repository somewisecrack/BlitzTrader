"""
tests/test_serial_exit.py — Tests for serial-numbered positions and exit-by-serial.

Futures-only runtime. Pairs tests removed in Part A architecture overhaul.

Covers:
 Status format (1–2)
 Exit routing via text patterns (3)
 Safety checks (4–10)
 Futures identity revalidation (11–17)
 P&L confirmation (18–20)
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


# ──────────────────────────────────────────────────────────────
#   HELPERS
# ──────────────────────────────────────────────────────────────

def _build_status(
    futures_positions=None,
    price_map: dict | None = None,
):
    """
    Call build_status_message with stubbed feed / state / portfolio.
    price_map: {token: price} used by the fake live_feed.get_ltp.
    """
    from tools.position_serial import build_status_message

    sm = _make_state_manager(positions=futures_positions or [])

    feed = MagicMock()
    price_map = price_map or {}
    feed.get_ltp.side_effect = lambda token: price_map.get(token)

    msg, index = build_status_message(
        state_manager=sm,
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
    assert "Futures" in msg
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
#   3. Exit routing text patterns
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
#   4. No open positions → "Open Positions\n- None"
# ──────────────────────────────────────────────────────────────

def test_no_open_positions_shows_none():
    msg, index = _build_status()
    assert "Open Positions" in msg
    assert "- None" in msg
    assert index["positions"] == []


# ──────────────────────────────────────────────────────────────
#   5. Serial mapping is written to runtime after status generation
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
#   6. Invalid serial → no orders, clear error
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
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=99,
            state_manager=sm,
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
#   7. Stale mapping → no orders, asks for fresh status
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
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
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
#   8. Already-closed position → no orders, clear error
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
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
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
#   9. Valid futures serial → correct close method called
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
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
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
#   10. After exit → mapping invalidated
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
        tg = MagicMock()

        ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
            order_execution=oe,
            shoonya_client=MagicMock(),
            telegram_handler=tg,
        )
        # Index file should be deleted after successful exit
        assert not ps_mod._INDEX_FILE.exists()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   11. Tool never opens a new position
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

        ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
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
#   12. Existing futures order safety checks unchanged
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
#   13. No hardcoded market data introduced
# ──────────────────────────────────────────────────────────────

def test_no_hardcoded_prices_in_serial_module():
    import tools.position_serial as ps_mod
    import inspect
    src = inspect.getsource(ps_mod)
    # There should be no hardcoded numeric literals used as prices
    assert "23684" not in src
    assert "23631" not in src


# ──────────────────────────────────────────────────────────────
#   14. Module imports cleanly
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
#   15–21. FUTURES identity revalidation
# ──────────────────────────────────────────────────────────────

def test_futures_matching_identity_calls_close(tmp_path):
    """Identical fields → close_position called."""
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

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is True
        oe.close_position.assert_called_once()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_different_entry_time_not_closed(tmp_path):
    """Same symbol, different entry_time → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry_time="1000.0", order_id="ord001")
        # Index has different opened_at
        _make_fresh_futures_index(tmp_path, ps_mod, opened_at="9999.0", order_id="ord001")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm,
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
    """Same symbol, different order_id → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry_time="1000.0", order_id="real_order")
        _make_fresh_futures_index(tmp_path, ps_mod, opened_at="1000.0", order_id="stale_order")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_different_qty_not_closed(tmp_path):
    """Same symbol, different qty → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        # Live position has qty=75 but index says 65
        fut_pos = _futures_position(qty=75, entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, qty=65, opened_at="", order_id="")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_different_entry_price_not_closed(tmp_path):
    """Same symbol, entry_price outside tolerance → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry=23600.00, entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, entry=23700.00, opened_at="", order_id="")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_different_direction_not_closed(tmp_path):
    """Same symbol, direction flipped → not closed."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(direction="SELL", entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, direction="BUY", opened_at="", order_id="")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is False
        assert "no longer matches" in result["error"].lower()
        oe.close_position.assert_not_called()
    finally:
        ps_mod._INDEX_FILE = original_file


def test_futures_entry_price_within_tolerance_is_match(tmp_path):
    """Entry_price within 1e-4 tolerance → treated as match."""
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

        result = ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=MagicMock(),
        )
        assert result["success"] is True
        oe.close_position.assert_called_once()
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   22. P&L confirmation: futures exit has P&L sections
# ──────────────────────────────────────────────────────────────

def test_futures_exit_confirmation_has_pnl_sections(tmp_path):
    """Futures exit confirmation contains Futures P&L info."""
    from tools import position_serial as ps_mod
    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, opened_at="", order_id="")
        sm = _make_state_manager(positions=[fut_pos])
        oe = MagicMock()
        oe.close_position.return_value = {"exit_price": 23700.0, "pnl": 975.0}
        tg = MagicMock()

        ps_mod.exit_position_by_serial(
            serial=1, state_manager=sm,
            order_execution=oe, shoonya_client=MagicMock(), telegram_handler=tg,
        )
        tg.send_telegram.assert_called_once()
        msg = tg.send_telegram.call_args[0][0]
        assert "Futures" in msg
        assert "Realized P&L" in msg
    finally:
        ps_mod._INDEX_FILE = original_file


# ──────────────────────────────────────────────────────────────
#   23. P&L confirmation: updated realized P&L reflected
# ──────────────────────────────────────────────────────────────

def test_futures_exit_uses_updated_post_exit_realized_pnl(tmp_path):
    """Futures exit confirmation reflects updated realized P&L after close."""
    from tools import position_serial as ps_mod

    original_file = ps_mod._INDEX_FILE
    ps_mod._INDEX_FILE = tmp_path / "telegram_position_index.json"
    try:
        fut_pos = _futures_position(entry_time="", order_id="")
        _make_fresh_futures_index(tmp_path, ps_mod, opened_at="", order_id="")

        call_count = [0]
        def _get_state():
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "daily_pnl": 1234.0,
                    "daily_pnl_pct": 0.5,
                    "positions": [fut_pos],
                    "virtual_capital": 1_000_000,
                    "available_balance": 950_000,
                    "margin_used": 50_000,
                }
            else:
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
        tg = MagicMock()

        result = ps_mod.exit_position_by_serial(
            serial=1,
            state_manager=sm,
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


# ──────────────────────────────────────────────────────────────
#   24. Safety composite smoke test
# ──────────────────────────────────────────────────────────────

def test_all_safety_checks_present():
    """All original safety checks remain intact."""
    from tools import position_serial as ps_mod
    import inspect
    src = inspect.getsource(ps_mod)

    # NEVER opens a new position
    assert "place_order" not in src or "_entry_price" not in src
    # Index TTL check present
    assert "_is_index_fresh" in src
    # Invalidation present
    assert "invalidate_position_index" in src
    # Identity helpers present
    assert "_match_futures_identity" in src


# ──────────────────────────────────────────────────────────────
#   25. Pairs removed: no pairs references in module
# ──────────────────────────────────────────────────────────────

def test_no_pairs_in_status_message():
    """build_status_message must not contain pairs sections."""
    msg, index = _build_status()
    # Futures-only runtime: no Pairs section
    assert "Open Pairs" not in msg
    assert "[Pairs]" not in msg


def test_build_pnl_summary_not_in_module():
    """_build_pnl_summary has been replaced by _build_updated_pnl_summary."""
    import tools.position_serial as ps_mod
    assert not hasattr(ps_mod, "_build_pnl_summary"), (
        "_build_pnl_summary should have been removed; use _build_updated_pnl_summary"
    )
    assert hasattr(ps_mod, "_build_updated_pnl_summary"), (
        "_build_updated_pnl_summary must exist"
    )
