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
        "daily_pnl": 0.0,
    }

    ledger = MagicMock()
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
        },
    }

    executor = OrderExecutionTools(
        state_manager=state,
        virtual_ledger=ledger,
        live_feed=feed,
        shoonya_client=client,
        active_tokens=active_tokens,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
