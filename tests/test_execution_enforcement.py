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
        if "NIFTY" in sym:
            return 25
        return None

    ledger.get_lot_size.side_effect = lot_size_for
    ledger.estimate_margin.return_value = 100_000
    ledger.validate_position_size.return_value = (True, "")
    def preview_market_fill(**kwargs):
        direction = kwargs["direction"]
        quantity = kwargs["quantity"]
        if direction == "BUY":
            price = float(kwargs["best_ask"])
            side = "asks"
        else:
            price = float(kwargs["best_bid"])
            side = "bids"
        return {
            "fill_price": price,
            "levels_consumed": [{"price": price, "qty": quantity}],
            "unfilled_qty": 0,
            "book_side": side,
            "tick_size": 0.05,
        }
    ledger.preview_market_fill.side_effect = preview_market_fill
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
    def get_quotes(exchange, token):
        if token == "66688":
            return {
                "stat": "Ok",
                "bp1": "56010",
                "bq1": "200",
                "bp2": "56009",
                "bq2": "300",
                "bp3": "56008",
                "bq3": "500",
                "sp1": "56020",
                "sq1": "150",
                "sp2": "56021",
                "sq2": "300",
                "sp3": "56022",
                "sq3": "500",
            }
        if token == "66689":
            return {
                "stat": "Ok",
                "bp1": "24010",
                "bq1": "200",
                "bp2": "24009.5",
                "bq2": "300",
                "bp3": "24009",
                "bq3": "500",
                "sp1": "24020",
                "sq1": "150",
                "sp2": "24020.5",
                "sq2": "300",
                "sp3": "24021",
                "sq3": "500",
            }
        return {
            "stat": "Ok",
            "bp1": "23995",
            "bq1": "200",
            "bp2": "23994.5",
            "bq2": "300",
            "bp3": "23994",
            "bq3": "500",
            "sp1": "24005",
            "sq1": "150",
            "sp2": "24005.5",
            "sq2": "300",
            "sp3": "24006",
            "sq3": "500",
        }
    client.get_quotes.side_effect = get_quotes
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

    def test_market_entry_uses_orderbook_preview_for_risk(self):
        self.executor._ledger.preview_market_fill.side_effect = None
        self.executor._ledger.preview_market_fill.return_value = {
            "fill_price": 24012.0,
            "levels_consumed": [{"price": 24005.0, "qty": 10}, {"price": 24012.0, "qty": 15}],
            "unfilled_qty": 0,
            "book_side": "asks",
            "tick_size": 0.5,
        }
        self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        kwargs = self.executor._ledger.validate_position_size.call_args.kwargs
        self.assertEqual(kwargs["entry_price"], 24012.0)

    def test_market_entry_passes_depth_to_fill_engine(self):
        self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        kwargs = self.executor._ledger.execute_market_fill.call_args.kwargs
        self.assertIn("bids", kwargs)
        self.assertIn("asks", kwargs)
        self.assertGreaterEqual(len(kwargs["asks"]), 1)

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

    def test_finnifty_futures_tsym_rejected(self):
        """FINNIFTY28APR26F must be BLOCKED — FINNIFTY removed from active futures universe."""
        self.executor._feed.get_best_bid_ask.return_value = (23995.0, 24005.0)
        result = self.executor.place_virtual_order(
            symbol="FINNIFTY28APR26F",
            direction="BUY",
            quantity=60,
            stop_loss=23850.0,
        )
        self.assertIn("BLOCKED", result.get("error", ""),
                      f"FINNIFTY futures tsym should now be blocked: {result}")
        self.assertEqual(result.get("status"), "REJECTED")

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

    def test_shoonya_ok_margin_is_accepted_even_if_remarks_say_insufficient(self):
        """
        Some Shoonya responses return stat=Ok and numeric ordermargin while also
        setting remarks='Insufficient Balance' because account cash is 0 in the
        broker account. The simulator must use the numeric ordermargin and apply
        its own virtual-capital check instead of hard-blocking on remarks text.
        """
        self.executor._state.get_state.return_value.update({
            "virtual_capital": 1_000_000,
            "daily_pnl": 0.0,
            "margin_used": 0.0,
        })
        self.executor._client.get_order_margin.return_value = {
            "stat": "Ok",
            "remarks": "Insufficient Balance",
            "cash": "0.00",
            "ordermargin": "188473.52",
            "marginused": "188473.52",
            "marginusedprev": "0.00",
        }
        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        self.assertEqual(result.get("status"), "FILLED", result)
        added_position = self.executor._state.add_position.call_args.args[0]
        self.assertEqual(added_position["margin_used"], 188473.52)

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

    def test_one_each_nifty_banknifty_allowed_but_third_blocked(self):
        """With two enabled instruments, no-pyramiding caps live positions at 2."""
        self.executor._max_positions = 2
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
        third = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )
        # The third attempt is blocked either by max positions OR by no-pyramiding
        self.assertEqual(third.get("status"), "REJECTED")

    def test_no_pyramiding_blocks_banknifty_not_as_nifty(self):
        """BANKNIFTY exposure is tracked separately from plain NIFTY."""
        self.executor._max_positions = 2
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
        self.executor._feed.get_best_bid_ask.return_value = (56010.0, 56020.0)
        banknifty = self.executor.place_virtual_order(
            symbol="BANKNIFTY28APR26F",
            direction="BUY",
            quantity=15,
            stop_loss=55900.0,
        )
        self.assertNotEqual(banknifty.get("status"), "REJECTED", banknifty)

        self.executor._state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [
                {"symbol": "BANKNIFTY28APR26F", "direction": "BUY", "quantity": 15},
            ],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 100_000,
        }
        duplicate = self.executor.place_virtual_order(
            symbol="BANKNIFTY28APR26F",
            direction="SELL",
            quantity=15,
            stop_loss=56200.0,
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

    def test_registry_suppresses_fake_enter_after_rejected_order(self):
        """ENTER logs must not be written if the order tool just rejected the trade."""
        from tools.registry import ToolRegistry

        order_exec = MagicMock()
        order_exec.place_virtual_order.return_value = {
            "status": "REJECTED",
            "error": "BLOCKED: Shoonya margin check says Insufficient Balance",
            "symbol": "NIFTY28APR26F",
            "direction": "SELL",
        }
        journal = MagicMock()
        journal.log_decision.return_value = {"status": "logged"}
        registry = ToolRegistry(
            market_data=MagicMock(),
            order_execution=order_exec,
            telegram=MagicMock(),
            journal=journal,
            strategy_reader=MagicMock(),
            memory_reader=MagicMock(),
            goal_manager=MagicMock(),
        )

        place = registry.execute("place_virtual_order", {
            "symbol": "NIFTY28APR26F",
            "direction": "SELL",
            "quantity": 25,
            "stop_loss": 24300.0,
            "target": 24100.0,
        })
        self.assertEqual(place["status"], "REJECTED")

        registry.execute("log_decision", {
            "action": "ENTER_SHORT",
            "symbol": "NIFTY28APR26F",
            "strategy_applied": "VP-05 3EMA Trend",
            "market_context_summary": "Bearish setup",
            "reason": "Approved by model",
        })

        journal.log_decision.assert_called_once()
        kwargs = journal.log_decision.call_args.kwargs
        self.assertEqual(kwargs["action"], "REJECT")
        self.assertIn("Latest order result", kwargs["reason"])

    def test_no_daily_trade_cap_with_many_completed_trades(self):
        """Daily trade count is no longer capped; 15 completed trades must not block a new entry."""
        self.executor._state.get_state.return_value.update({
            "trades": [{"symbol": "NIFTY28APR26F"} for _ in range(15)],
            "positions": [],
            "pending_orders": [],
        })

        result = self.executor.place_virtual_order(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            stop_loss=23900.0,
        )

        self.assertEqual(result.get("status"), "FILLED",
                         f"Expected FILLED but got: {result}")


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

            def update_position_by_order_id(self, order_id, **kwargs):
                for pos in self.positions:
                    if pos["order_id"] == order_id:
                        pos.update(kwargs)
                        return pos
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
        feed.get_live_quote.return_value = None
        client = MagicMock()
        client.get_quotes.return_value = None

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
        self.assertEqual(ledger.get_lot_size("NIFTY28APR26F"), 25)

    def test_dynamic_lot_size_override_from_shoonya_metadata(self):
        from tools.virtual_ledger import VirtualLedger

        ledger = VirtualLedger(lot_sizes={"NIFTY": 65, "BANKNIFTY": 30})
        self.assertEqual(ledger.get_lot_size("NIFTY28APR26F"), 65)
        self.assertEqual(ledger.get_lot_size("BANKNIFTY28APR26F"), 30)

    def test_banknifty_margin_uses_banknifty_lot_size(self):
        from tools.virtual_ledger import VirtualLedger

        ledger = VirtualLedger()
        # Backward-compatible fallback when price is unavailable.
        self.assertEqual(ledger.estimate_margin("BANKNIFTY28APR26F", 15), 100_000)
        self.assertEqual(ledger.estimate_margin("BANKNIFTY28APR26F", 30), 200_000)

    def test_market_fill_sweeps_ask_side_for_buy(self):
        from tools.virtual_ledger import VirtualLedger

        ledger = VirtualLedger()
        fill = ledger.execute_market_fill(
            symbol="NIFTY28APR26F",
            direction="BUY",
            quantity=25,
            best_bid=23995.0,
            best_ask=24000.0,
            bids=[{"price": 23995.0, "qty": 100}],
            asks=[{"price": 24000.0, "qty": 10}, {"price": 24001.0, "qty": 15}],
        )
        self.assertEqual(fill["fill_price"], 24000.6)
        self.assertEqual(fill["levels_consumed"][0]["qty"], 10)
        self.assertEqual(fill["levels_consumed"][1]["qty"], 15)

    def test_market_fill_sweeps_bid_side_for_sell(self):
        from tools.virtual_ledger import VirtualLedger

        ledger = VirtualLedger()
        fill = ledger.execute_market_fill(
            symbol="BANKNIFTY28APR26F",
            direction="SELL",
            quantity=15,
            best_bid=54820.0,
            best_ask=54840.0,
            bids=[{"price": 54820.0, "qty": 5}, {"price": 54819.0, "qty": 10}],
            asks=[{"price": 54840.0, "qty": 100}],
        )
        self.assertEqual(fill["fill_price"], 54819.33)
        self.assertEqual(fill["levels_consumed"][0]["qty"], 5)
        self.assertEqual(fill["levels_consumed"][1]["qty"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
