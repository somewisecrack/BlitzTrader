"""
tests/test_execution_enforcement.py
-------------------------------------
Proves that place_virtual_order() enforces futures-only execution and that
the live agent surface does not expose get_option_chain for trade entry.

Run: python -m pytest tests/test_execution_enforcement.py -v
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

# Ensure project root is on path when run directly or via pytest from project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_executor():
    """Build an OrderExecutionTools with all dependencies mocked out."""
    from tools.order_execution import OrderExecutionTools

    state = MagicMock()
    state.get_state.return_value = {
        "is_stopped": False,
        "is_paused": False,
        "positions": [],
        "pending_orders": [],
        "trades": [],
        "daily_pnl": 0.0,
        "virtual_capital": 500_000,
        "margin_used": 0.0,
    }

    ledger = MagicMock()
    def lot_size_for(symbol):
        sym = symbol.upper()
        if "BANKNIFTY" in sym:
            return 15
        if "FINNIFTY" in sym:
            return 60
        if "NIFTY" in sym:
            return 25
        return None

    ledger.get_lot_size.side_effect = lot_size_for
    ledger.estimate_margin.return_value = 100_000
    ledger.validate_position_size.return_value = (True, "")
    ledger.execute_market_fill.return_value = {
        "symbol": "NIFTY28APR26F",
        "direction": "BUY",
        "quantity": 25,
        "fill_price": 24000.0,
        "cost": 600000.0,
        "margin_used": 60000.0,
        "fill_time": 1700000000.0,
        "order_id": "test-001",
    }

    feed = MagicMock()
    feed.get_best_bid_ask.return_value = (23995.0, 24005.0)

    client = MagicMock()
    client.get_best_bid_ask_rest.return_value = (23995.0, 24005.0)
    client.search_scrip.return_value = [{"token": "66691"}]
    client.get_order_margin.return_value = {
        "stat": "Ok",
        "remarks": "Order Success",
        "cash": "500000.00",
        "ordermargin": "100000.00",
        "marginused": "100000.00",
        "marginusedprev": "0.00",
    }

    active_tokens = {
        "NIFTY": {
            "exchange": "NFO",
            "token": "66691",
            "tsym": "NIFTY28APR26F",
            "expiry": "28-APR-2026",
        },
        "BANKNIFTY": {
            "exchange": "NFO",
            "token": "66688",
            "tsym": "BANKNIFTY28APR26F",
            "expiry": "28-APR-2026",
            "lot_size": 15,
        },
        "FINNIFTY": {
            "exchange": "NFO",
            "token": "66689",
            "tsym": "FINNIFTY28APR26F",
            "expiry": "28-APR-2026",
            "lot_size": 60,
        },
    }

    executor = OrderExecutionTools(
        state_manager=state,
        virtual_ledger=ledger,
        live_feed=feed,
        shoonya_client=client,
        active_tokens=active_tokens,
        no_entry_after="23:59",
    )
    return executor


class TestExecutionEnforcement(unittest.TestCase):

    def setUp(self):
        self.executor = _make_executor()

    # ── Option symbol rejection ─────────────────────────────────────────────

    def test_ce_symbol_rejected(self):
        """CE option symbols must be hard-blocked."""
        result = self.executor.place_virtual_order(
            symbol="NIFTY24500CE",
            direction="BUY",
            quantity=25,
            stop_loss=24400.0,
        )
        self.assertIn("BLOCKED", result.get("error", ""),
                      f"Expected BLOCKED in error, got: {result}")
        self.assertEqual(result.get("status"), "REJECTED")

    def test_pe_symbol_rejected(self):
        """PE option symbols must be hard-blocked."""
        result = self.executor.place_virtual_order(
            symbol="BANKNIFTY52000PE",
            direction="BUY",
            quantity=15,
            stop_loss=51500.0,
        )
        self.assertIn("BLOCKED", result.get("error", ""),
                      f"Expected BLOCKED in error, got: {result}")
        self.assertEqual(result.get("status"), "REJECTED")

    # ── Bare logical name rejection ─────────────────────────────────────────

    def test_bare_nifty_rejected(self):
        """Bare 'NIFTY' must be rejected — agent must use the resolved tsym."""
        result = self.executor.place_virtual_order(
            symbol="NIFTY",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        self.assertIn("BLOCKED", result.get("error", ""),
                      f"Expected BLOCKED in error, got: {result}")
        self.assertEqual(result.get("status"), "REJECTED")

    def test_bare_banknifty_rejected(self):
        """Bare 'BANKNIFTY' must be rejected — agent must use the resolved tsym."""
        result = self.executor.place_virtual_order(
            symbol="BANKNIFTY",
            direction="SELL",
            quantity=15,
            stop_loss=52000.0,
        )
        self.assertIn("BLOCKED", result.get("error", ""),
                      f"Expected BLOCKED in error, got: {result}")
        self.assertEqual(result.get("status"), "REJECTED")

    def test_internal_token_resolution_does_not_accept_bare_logical_name(self):
        """Helper should not silently resolve bare logical names for new-order semantics."""
        token = self.executor._resolve_token("NIFTY")
        self.assertIsNone(
            token,
            "Bare logical names must not resolve via _resolve_token(); caller must pass futures tsym.",
        )

    def test_bare_sensex_rejected(self):
        """Bare 'SENSEX' must also be rejected."""
        result = self.executor.place_virtual_order(
            symbol="SENSEX",
            direction="BUY",
            quantity=10,
            stop_loss=70000.0,
        )
        self.assertIn("BLOCKED", result.get("error", ""),
                      f"Expected BLOCKED in error, got: {result}")
        self.assertEqual(result.get("status"), "REJECTED")

    def test_bare_finnifty_rejected(self):
        """Bare 'FINNIFTY' must also be rejected."""
        result = self.executor.place_virtual_order(
            symbol="FINNIFTY",
            direction="BUY",
            quantity=40,
            stop_loss=22000.0,
        )
        self.assertIn("BLOCKED", result.get("error", ""),
                      f"Expected BLOCKED in error, got: {result}")
        self.assertEqual(result.get("status"), "REJECTED")

    # ── Valid futures tsym accepted ─────────────────────────────────────────

    def test_valid_nifty_futures_tsym_accepted(self):
        """A valid futures tsym like NIFTY28APR26F must NOT be blocked at the symbol-check level."""
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        # Must not contain "BLOCKED" in the error — may succeed or fail for other reasons
        # (e.g. time cutoff, market data) but must NOT be rejected by symbol guardrail.
        error_msg = result.get("error", "")
        self.assertNotIn("BLOCKED: bare logical", error_msg,
                         f"Futures tsym should not be blocked by bare-name check: {result}")
        self.assertNotIn("BLOCKED: Options", error_msg,
                         f"Futures tsym should not be blocked by CE/PE check: {result}")

    def test_valid_banknifty_futures_tsym_accepted(self):
        """BANKNIFTY28APR26F must not be blocked at the symbol-check level."""
        # Reconfigure mock to return BANKNIFTY fill data
        self.executor._feed.get_best_bid_ask.return_value = (53995.0, 54005.0)
        result = self.executor.place_virtual_order(
            symbol="BANKNIFTY28APR26F",
            direction="SELL",
            quantity=15,
            stop_loss=54500.0,
        )
        error_msg = result.get("error", "")
        self.assertNotIn("BLOCKED: bare logical", error_msg,
                         f"Futures tsym should not be blocked by bare-name check: {result}")
        self.assertNotIn("BLOCKED: Options", error_msg,
                         f"Futures tsym should not be blocked by CE/PE check: {result}")

    def test_valid_finnifty_futures_tsym_accepted(self):
        """FINNIFTY28APR26F must be a valid futures execution symbol."""
        self.executor._feed.get_best_bid_ask.return_value = (23995.0, 24005.0)
        result = self.executor.place_virtual_order(
            symbol="FINNIFTY28APR26F",
            direction="BUY",
            quantity=60,
            stop_loss=23850.0,
        )
        error_msg = result.get("error", "")
        self.assertNotIn("BLOCKED: bare logical", error_msg,
                         f"FINNIFTY futures tsym should not be blocked by bare-name check: {result}")
        self.assertNotIn("Only NIFTY", error_msg,
                         f"FINNIFTY should be in the allowed futures set: {result}")

    def test_rejects_more_than_one_nifty_lot_even_with_tight_stop(self):
        """Tight SL must not permit oversized futures quantity."""
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="SELL",
            quantity=1125,
            stop_loss=24239.1,
            target=24196.8,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("Only 1 lot", result.get("error", ""))

    def test_rejects_more_than_one_banknifty_lot_even_with_tight_stop(self):
        """Tight SL must not permit oversized BANKNIFTY futures quantity."""
        result = self.executor.place_virtual_order(
            symbol="BANKNIFTY28APR26F",
            direction="SELL",
            quantity=1665,
            stop_loss=56460.0,
            target=56415.0,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("Only 1 lot", result.get("error", ""))

    def test_rejects_non_lot_partial_quantity(self):
        """Futures must trade exactly one whole lot, not arbitrary units."""
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=10,
            stop_loss=23900.0,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("Required quantity is exactly 25", result.get("error", ""))

    def test_margin_guard_blocks_when_available_capital_insufficient(self):
        """Capital/margin must be a hard gate, independent of SL risk."""
        self.executor._client.get_order_margin.return_value = {
            "stat": "Ok",
            "remarks": "Order Success",
            "cash": "500000.00",
            "ordermargin": "600000.00",
            "marginused": "600000.00",
            "marginusedprev": "0.00",
        }
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("Margin required", result.get("error", ""))

    def test_shoonya_order_margin_is_used_for_position_margin(self):
        """The virtual position should store Shoonya's returned ordermargin."""
        self.executor._client.get_order_margin.return_value = {
            "stat": "Ok",
            "remarks": "Order Success",
            "cash": "500000.00",
            "ordermargin": "206280.00",
            "marginused": "206280.00",
            "marginusedprev": "0.00",
        }
        result = self.executor.place_virtual_order(
            symbol="BANKNIFTY28APR26F",
            direction="BUY",
            quantity=15,
            stop_loss=23000.0,
        )
        self.assertEqual(result.get("status"), "FILLED", result)
        added_position = self.executor._state.add_position.call_args.args[0]
        self.assertEqual(added_position["margin_used"], 206280.0)
        self.executor._client.get_order_margin.assert_called()

    def test_shoonya_margin_failure_blocks_entry(self):
        """If Shoonya RMS cannot price the order margin, fail closed."""
        self.executor._client.get_order_margin.return_value = {
            "stat": "Not_Ok",
            "emsg": "RMS unavailable",
        }
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("Shoonya margin check failed", result.get("error", ""))

    def test_rejects_sell_target_above_entry(self):
        """A SELL target above entry would create a losing 'target hit' and must be blocked."""
        self.executor._feed.get_best_bid_ask.return_value = (23995.0, 24005.0)
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="SELL",
            quantity=25,
            stop_loss=24100.0,
            target=24010.0,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("SELL target must be below entry", result.get("error", ""))

    def test_rejects_buy_target_below_entry(self):
        """A BUY target below entry would create a losing 'target hit' and must be blocked."""
        self.executor._feed.get_best_bid_ask.return_value = (23995.0, 24005.0)
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
            target=23990.0,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("BUY target must be above entry", result.get("error", ""))

    def test_rejects_stop_loss_on_wrong_side(self):
        """Stop-loss must be on the loss side of the entry for both directions."""
        self.executor._feed.get_best_bid_ask.return_value = (23995.0, 24005.0)
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="SELL",
            quantity=25,
            stop_loss=23990.0,
            target=23800.0,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("SELL stop_loss must be above entry", result.get("error", ""))

    def test_one_each_nifty_banknifty_finnifty_allowed_but_fourth_blocked(self):
        """With three enabled instruments, no-pyramiding caps live positions at 3."""
        self.executor._max_positions = 3
        self.executor._state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [
                {"symbol": "NIFTY28APR26F"},
            ],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 100_000,
        }
        self.executor._feed.get_best_bid_ask.return_value = (56010.0, 56020.0)
        second = self.executor.place_virtual_order(
            symbol="BANKNIFTY28APR26F",
            direction="BUY",
            quantity=15,
            stop_loss=56000.0,
        )
        self.assertNotEqual(second.get("status"), "REJECTED", second)

        self.executor._state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [
                {"symbol": "NIFTY28APR26F"},
                {"symbol": "BANKNIFTY28APR26F"},
            ],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 200_000,
        }
        self.executor._feed.get_best_bid_ask.return_value = (23995.0, 24005.0)
        third = self.executor.place_virtual_order(
            symbol="FINNIFTY28APR26F",
            direction="BUY",
            quantity=60,
            stop_loss=23850.0,
        )
        self.assertNotEqual(third.get("status"), "REJECTED", third)

        self.executor._state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [
                {"symbol": "NIFTY28APR26F"},
                {"symbol": "BANKNIFTY28APR26F"},
                {"symbol": "FINNIFTY28APR26F"},
            ],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 300_000,
        }
        fourth = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        self.assertEqual(fourth.get("status"), "REJECTED")
        self.assertIn("maximum 3 open positions", fourth.get("error", ""))

    def test_no_pyramiding_blocks_finnifty_not_as_nifty(self):
        """FINNIFTY exposure is tracked separately from plain NIFTY."""
        self.executor._max_positions = 3
        self.executor._state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [
                {"symbol": "NIFTY28APR26F", "direction": "BUY", "quantity": 25},
            ],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 100_000,
        }
        finnifty = self.executor.place_virtual_order(
            symbol="FINNIFTY28APR26F",
            direction="BUY",
            quantity=60,
            stop_loss=23850.0,
        )
        self.assertNotEqual(finnifty.get("status"), "REJECTED", finnifty)

        self.executor._state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [
                {"symbol": "FINNIFTY28APR26F", "direction": "BUY", "quantity": 60},
            ],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 100_000,
        }
        duplicate = self.executor.place_virtual_order(
            symbol="FINNIFTY28APR26F",
            direction="SELL",
            quantity=60,
            stop_loss=24050.0,
        )
        self.assertEqual(duplicate.get("status"), "REJECTED")
        self.assertIn("No pyramiding", duplicate.get("error", ""))

    def test_no_pyramiding_blocks_second_position_same_instrument(self):
        """Cannot open another NIFTY position while any NIFTY position is open."""
        self.executor._state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [
                {"symbol": "NIFTY28APR26F", "direction": "SELL", "quantity": 25},
            ],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 100_000,
        }
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("No pyramiding", result.get("error", ""))

    def test_no_pyramiding_allows_same_instrument_after_position_closed(self):
        """Once no NIFTY position exists in state, a fresh NIFTY setup is allowed."""
        self.executor._state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [
                {"symbol": "BANKNIFTY28APR26F", "direction": "SELL", "quantity": 15},
            ],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 100_000,
        }
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        self.assertNotEqual(result.get("status"), "REJECTED", result)

    def test_no_pyramiding_blocks_duplicate_pending_order(self):
        """A pending order counts as instrument exposure for new-entry purposes."""
        self.executor._state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [],
            "pending_orders": [
                {"symbol": "NIFTY28APR26F", "order_id": "pending-1"},
            ],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 0.0,
        }
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("Pending NIFTY order", result.get("error", ""))

    # ── Context builder source check ────────────────────────────────────────

    def test_live_prompt_does_not_mention_get_option_chain_for_entry(self):
        """
        context_builder.py must NOT instruct the agent to call get_option_chain
        as a positive step before placing an entry.  DO NOT / BLOCKED references
        are acceptable because they are prohibitions, not instructions.
        The live tool list (Available tools: ...) must not list get_option_chain.
        """
        cb_path = os.path.join(os.path.dirname(__file__), "..", "context_builder.py")
        with open(cb_path) as f:
            src = f.read()

        # The live tool list must not include get_option_chain
        available_tools_line = next(
            (line for line in src.splitlines() if line.startswith("Available tools:")),
            ""
        )
        self.assertNotIn(
            "get_option_chain",
            available_tools_line,
            "get_option_chain must not appear in the 'Available tools:' live tool list",
        )

        # No POSITIVE instruction to call get_option_chain before entry.
        # We check for affirmative patterns; "DO NOT call" references are fine.
        positive_patterns = [
            "call get_option_chain() to get",        # e.g. "call get_option_chain() to get the chain"
            "call get_option_chain() first",          # e.g. "call get_option_chain() first"
            "use get_option_chain() before",          # e.g. "use get_option_chain() before placing"
            "1. get_option_chain",                    # numbered step listing it as step 1
        ]
        src_lower = src.lower()
        for pat in positive_patterns:
            self.assertNotIn(
                pat.lower(),
                src_lower,
                f"context_builder must not positively instruct the agent to use get_option_chain: found '{pat}'",
            )

    def test_system_prompt_does_not_list_get_option_chain_as_live_tool(self):
        """SYSTEM_PROMPT Available tools line must not include get_option_chain."""
        from context_builder import SYSTEM_PROMPT
        # Find the Available tools line
        tools_line = next(
            (line for line in SYSTEM_PROMPT.splitlines() if line.startswith("Available tools:")),
            ""
        )
        self.assertNotIn(
            "get_option_chain",
            tools_line,
            "SYSTEM_PROMPT Available tools must not include get_option_chain",
        )

    def test_place_virtual_order_error_message_mentions_tsym(self):
        """When a bare logical name is used, error should mention resolved tsym format."""
        result = self.executor.place_virtual_order(
            symbol="NIFTY",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        error = result.get("error", "")
        # Error should guide the caller to use the correct tsym format
        self.assertTrue(
            "tsym" in error.lower() or "28APR26F" in error or "NIFTY28APR" in error
            or "futures tsym" in error.lower(),
            f"Error message should reference the futures tsym format: {error}",
        )

    def test_registry_live_tools_exclude_get_option_chain(self):
        """Live tool definitions must exclude get_option_chain even if legacy tools exist."""
        from tools.registry import ToolRegistry

        registry = ToolRegistry(
            market_data=MagicMock(),
            order_execution=MagicMock(),
            telegram=MagicMock(),
            journal=MagicMock(),
            strategy_reader=MagicMock(),
            memory_reader=MagicMock(),
            goal_manager=MagicMock(),
        )
        live_names = {tool["name"] for tool in registry.get_tool_definitions()}
        self.assertNotIn("get_option_chain", live_names)

    def test_daily_trade_cap_blocks_eleventh_entry(self):
        """Completed + open + pending entries must not exceed daily cap."""
        self.executor._state.get_state.return_value.update({
            "trades": [{"symbol": "NIFTY28APR26F"} for _ in range(10)],
            "positions": [],
            "pending_orders": [],
        })

        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )

        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("Daily trade cap reached", result.get("error", ""))

    def test_daily_trade_cap_counts_open_positions(self):
        """Nine closed plus one open position leaves no room for another entry."""
        self.executor._state.get_state.return_value.update({
            "trades": [{"symbol": "NIFTY28APR26F"} for _ in range(9)],
            "positions": [
                {
                    "symbol": "BANKNIFTY28APR26F",
                    "direction": "BUY",
                    "quantity": 15,
                }
            ],
            "pending_orders": [],
        })

        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )

        self.assertEqual(result.get("status"), "REJECTED")
        self.assertIn("10/10", result.get("error", ""))

    def test_daily_trade_cap_allows_tenth_entry(self):
        """Nine prior entries still allows exactly one more."""
        self.executor._state.get_state.return_value.update({
            "trades": [{"symbol": "NIFTY28APR26F"} for _ in range(9)],
            "positions": [],
            "pending_orders": [],
        })

        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )

        self.assertEqual(result.get("status"), "FILLED")


class TestTrailingStops(unittest.TestCase):

    def test_buy_trailing_stop_moves_to_breakeven_after_half_percent_move(self):
        executor = _make_executor()
        pos = {
            "symbol": "NIFTY28APR26F",
            "direction": "BUY",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "order_id": "buy-half",
        }
        executor._state.update_position_by_order_id.return_value = pos

        changed = executor._apply_trailing_stop(pos, 100.6)

        self.assertTrue(changed)
        self.assertEqual(pos["stop_loss"], 100.0)
        self.assertEqual(pos["trailing_locked_pct"], 0.0)

    def test_buy_trailing_stop_locks_half_percent_after_one_percent_move(self):
        executor = _make_executor()
        pos = {
            "symbol": "NIFTY28APR26F",
            "direction": "BUY",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "order_id": "buy-one",
        }
        executor._state.update_position_by_order_id.return_value = pos

        changed = executor._apply_trailing_stop(pos, 101.2)

        self.assertTrue(changed)
        self.assertEqual(pos["stop_loss"], 100.5)
        self.assertEqual(pos["trailing_locked_pct"], 0.5)

    def test_buy_trailing_stop_locks_one_percent_after_two_percent_move(self):
        executor = _make_executor()
        pos = {
            "symbol": "BANKNIFTY28APR26F",
            "direction": "BUY",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "order_id": "buy-1",
        }
        executor._state.update_position_by_order_id.return_value = pos

        changed = executor._apply_trailing_stop(pos, 102.4)

        self.assertTrue(changed)
        self.assertEqual(pos["stop_loss"], 101.0)
        self.assertEqual(pos["trailing_locked_pct"], 1.0)
        executor._state.update_position_by_order_id.assert_called_once()

    def test_buy_trailing_stop_ratcheting_does_not_move_backwards(self):
        executor = _make_executor()
        pos = {
            "symbol": "BANKNIFTY28APR26F",
            "direction": "BUY",
            "entry_price": 100.0,
            "stop_loss": 102.0,
            "order_id": "buy-2",
        }

        changed = executor._apply_trailing_stop(pos, 102.4)

        self.assertFalse(changed)
        self.assertEqual(pos["stop_loss"], 102.0)

    def test_sell_trailing_stop_locks_one_percent_after_two_percent_move(self):
        executor = _make_executor()
        pos = {
            "symbol": "BANKNIFTY28APR26F",
            "direction": "SELL",
            "entry_price": 100.0,
            "stop_loss": 105.0,
            "order_id": "sell-1",
        }
        executor._state.update_position_by_order_id.return_value = pos

        changed = executor._apply_trailing_stop(pos, 97.8)

        self.assertTrue(changed)
        self.assertEqual(pos["stop_loss"], 99.0)
        self.assertEqual(pos["trailing_locked_pct"], 1.0)

    def test_sell_trailing_stop_moves_to_breakeven_after_half_percent_move(self):
        executor = _make_executor()
        pos = {
            "symbol": "BANKNIFTY28APR26F",
            "direction": "SELL",
            "entry_price": 100.0,
            "stop_loss": 105.0,
            "order_id": "sell-half",
        }
        executor._state.update_position_by_order_id.return_value = pos

        changed = executor._apply_trailing_stop(pos, 99.4)

        self.assertTrue(changed)
        self.assertEqual(pos["stop_loss"], 100.0)
        self.assertEqual(pos["trailing_locked_pct"], 0.0)

    def test_check_sl_target_closes_exact_order_id_for_same_symbol_positions(self):
        from tools.order_execution import OrderExecutionTools

        class FakeState:
            def __init__(self):
                self.positions = [
                    {
                        "symbol": "BANKNIFTY28APR26F",
                        "direction": "SELL",
                        "quantity": 15,
                        "entry_price": 54705.92,
                        "stop_loss": 55967.0,
                        "target": 52168.4,
                        "order_id": "wide-stop",
                    },
                    {
                        "symbol": "BANKNIFTY28APR26F",
                        "direction": "SELL",
                        "quantity": 15,
                        "entry_price": 54757.24,
                        "stop_loss": 54834.0,
                        "target": 54640.8,
                        "order_id": "tight-stop",
                    },
                ]
                self.removed = []
                self.trades = []

            def get_open_positions(self):
                return self.positions

            def remove_position_by_order_id(self, order_id):
                for i, pos in enumerate(self.positions):
                    if pos["order_id"] == order_id:
                        self.removed.append(order_id)
                        return self.positions.pop(i)
                return None

            def remove_position(self, symbol):
                raise AssertionError("remove_position(symbol) should not be used for auto-close")

            def update_daily_pnl(self, pnl):
                pass

            def add_trade(self, trade):
                self.trades.append(trade)

        state = FakeState()
        ledger = MagicMock()
        ledger.execute_market_fill.return_value = {
            "symbol": "BANKNIFTY28APR26F",
            "direction": "BUY",
            "quantity": 15,
            "fill_price": 54830.0,
            "cost": 822450.0,
            "margin_used": 82245.0,
            "fill_time": 1700000000.0,
            "order_id": "exit-1",
        }
        ledger.calculate_realized_pnl.return_value = -1091.4

        feed = MagicMock()
        feed.get_ltp.return_value = 54835.8
        feed.get_best_bid_ask.return_value = (54820.0, 54840.0)
        client = MagicMock()

        executor = OrderExecutionTools(
            state_manager=state,
            virtual_ledger=ledger,
            live_feed=feed,
            shoonya_client=client,
            active_tokens={
                "BANKNIFTY": {
                    "exchange": "NFO",
                    "token": "66688",
                    "tsym": "BANKNIFTY28APR26F",
                }
            },
        )

        result = executor.check_sl_target()

        self.assertEqual(len(result), 1)
        self.assertEqual(state.removed, ["tight-stop"])
        self.assertEqual([p["order_id"] for p in state.positions], ["wide-stop"])


class TestVirtualLedgerSizing(unittest.TestCase):

    def test_banknifty_lot_size_not_misclassified_as_nifty(self):
        from tools.virtual_ledger import VirtualLedger

        ledger = VirtualLedger()
        self.assertEqual(ledger.get_lot_size("BANKNIFTY28APR26F"), 15)
        self.assertEqual(ledger.get_lot_size("FINNIFTY28APR26F"), 60)
        self.assertEqual(ledger.get_lot_size("NIFTY28APR26F"), 25)

    def test_dynamic_lot_size_override_from_shoonya_metadata(self):
        from tools.virtual_ledger import VirtualLedger

        ledger = VirtualLedger(lot_sizes={"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60})
        self.assertEqual(ledger.get_lot_size("NIFTY28APR26F"), 65)
        self.assertEqual(ledger.get_lot_size("BANKNIFTY28APR26F"), 30)
        self.assertEqual(ledger.get_lot_size("FINNIFTY28APR26F"), 60)

    def test_banknifty_margin_uses_banknifty_lot_size(self):
        from tools.virtual_ledger import VirtualLedger

        ledger = VirtualLedger()
        # Backward-compatible fallback when price is unavailable.
        self.assertEqual(ledger.estimate_margin("BANKNIFTY28APR26F", 15), 100_000)
        self.assertEqual(ledger.estimate_margin("BANKNIFTY28APR26F", 30), 200_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
