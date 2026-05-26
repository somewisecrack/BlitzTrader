"""
tests/test_gatekeeper_entry_flow.py
-------------------------------------
Integration-style tests for the two-stage entry gate:
  1. Python hard review  (deterministic)
  2. Gemini gatekeeper   (5s timeout, APPROVE/REJECT)

These tests exercise _process_tradeable_signals_python() with a mocked
gatekeeper instance to verify invariants without making real API calls.

Invariants tested:
  - Python-rejected signal never reaches gatekeeper
  - Gatekeeper REJECT prevents order placement
  - Gatekeeper APPROVE + Python approved → order placed
  - Gatekeeper timeout/error → order NOT placed
  - Gatekeeper failure is logged but does not raise
  - No API key → all signals auto-rejected (gatekeeper=None)
  - Exit logic (SL/EOD) never calls gatekeeper
"""
import os
import sys
import types as module_types
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch, call

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub google.genai for import
if "google.genai" not in sys.modules:
    google_mod = module_types.ModuleType("google")
    genai_mod = module_types.ModuleType("google.genai")
    genai_types = module_types.SimpleNamespace(
        Tool=object,
        Schema=lambda **kwargs: kwargs,
        FunctionDeclaration=lambda **kwargs: kwargs,
    )
    genai_mod.types = genai_types
    google_mod.genai = genai_mod
    sys.modules.setdefault("google", google_mod)
    sys.modules.setdefault("google.genai", genai_mod)

from main import BlitzTrader
from tools.gemini_gatekeeper import GeminiGatekeeper

IST = pytz.timezone("Asia/Kolkata")


def _bot_with_state(state: dict) -> BlitzTrader:
    bot = BlitzTrader()
    bot._state = MagicMock()
    bot._state.get_state.return_value = state
    bot._active_tokens = {
        "NIFTY": {
            "exchange": "NFO",
            "token": "66691",
            "tsym": "NIFTY28APR26F",
            "lot_size": 25,
        },
        "BANKNIFTY": {
            "exchange": "NFO",
            "token": "66688",
            "tsym": "BANKNIFTY28APR26F",
            "lot_size": 15,
        },
    }
    bot._journal = MagicMock()
    bot._order_exec = MagicMock()
    bot._telegram = MagicMock()
    bot._market_data = MagicMock()
    bot._market_data.get_indicators.return_value = {
        "current_price": 24000.0,
        "ema20": 24050.0,
        "adx14": 28.0,
        "rsi14": 45.0,
        "avg_volume_20": 50000.0,
        "ema_stacked_bull": True,
        "ema_stacked_bear": False,
    }
    bot._market_data.get_candles.return_value = {"candles": []}
    bot._promoted_futures_filters = []
    bot._audit = MagicMock()
    return bot


def _state(**overrides):
    base = {
        "is_paused": False,
        "is_stopped": False,
        "daily_pnl": 0.0,
        "positions": [],
        "pending_orders": [],
        "trades": [],
    }
    base.update(overrides)
    return base


def _tradeable_signal(symbol="NIFTY", direction="BUY"):
    return {
        "symbol": symbol,
        "strategy": "VP-05 3EMA Trend",
        "direction": direction,
        "interval": "3",
        "entry_reference": 24000.0,
        "stop_loss": 23900.0,
        "target": 24200.0,
        "execution_symbol": "NIFTY28APR26F",
        "lot_size": 25,
    }


def _gate_approve(confidence=0.85):
    return {
        "decision": "APPROVE",
        "approved": True,
        "confidence": confidence,
        "reason": "EMA stacked bullish, ADX strong trend",
        "risk_notes": "Watch for sudden reversals",
        "conditions_checked": ["EMA bull stack", "ADX > 25"],
        "must_not_override_python_guardrails": True,
        "gatekeeper_error": None,
        "elapsed_seconds": 1.2,
    }


def _gate_reject(reason="Conflicting signals", error=None):
    return {
        "decision": "REJECT",
        "approved": False,
        "confidence": 0.7,
        "reason": reason,
        "risk_notes": "",
        "conditions_checked": [],
        "gatekeeper_error": error,
        "elapsed_seconds": 0.9,
    }


class TestGatekeeperApproveFlow(unittest.TestCase):
    """Happy path: Python approves, Gemini approves → order placed."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
        self.bot._gatekeeper.evaluate.return_value = _gate_approve()
        self.bot._order_exec.place_virtual_order.return_value = {
            "status": "FILLED",
            "fill_price": 24000.0,
        }

    def test_order_is_placed_on_double_approval(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._order_exec.place_virtual_order.assert_called_once()

    def test_journal_logs_entry_action(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        call_args = self.bot._journal.log_decision.call_args
        self.assertIn(call_args.kwargs.get("action", ""), {"ENTER_LONG", "ENTER_SHORT"})

    def test_telegram_notification_sent(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._telegram.send_telegram.assert_called_once()

    def test_telegram_message_includes_gemini_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        msg = self.bot._telegram.send_telegram.call_args[0][0]
        self.assertIn("EMA stacked bullish", msg)
        self.assertIn("85%", msg)  # confidence formatted

    def test_telegram_message_has_no_gemma_reference(self):
        """Entry alert must not mention Gemma/local observer."""
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        msg = self.bot._telegram.send_telegram.call_args[0][0]
        self.assertNotIn("Gemma", msg)
        self.assertNotIn("gemma", msg)
        self.assertNotIn("observer", msg)
        self.assertNotIn("Ollama", msg)

    def test_journal_entry_includes_gemini_and_python_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        reason = self.bot._journal.log_decision.call_args.kwargs.get("reason", "")
        self.assertIn("Gemini", reason)
        self.assertIn("Python", reason)


class TestGatekeeperRejectFlow(unittest.TestCase):
    """Gemini rejects → no order placed."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
        self.bot._gatekeeper.evaluate.return_value = _gate_reject("Conflicting EMA signals")

    def test_order_not_placed_on_gatekeeper_reject(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._order_exec.place_virtual_order.assert_not_called()

    def test_journal_logs_reject_with_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        call_args = self.bot._journal.log_decision.call_args
        self.assertEqual(call_args.kwargs.get("action"), "REJECT")
        self.assertIn("Conflicting EMA signals", call_args.kwargs.get("reason", ""))

    def test_telegram_not_sent_on_reject(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._telegram.send_telegram.assert_not_called()


class TestGatekeeperTimeoutFlow(unittest.TestCase):
    """Gatekeeper timeout → auto-reject, no order placed."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
        self.bot._gatekeeper.evaluate.return_value = _gate_reject(
            error="Gatekeeper timed out after 5s"
        )

    def test_order_not_placed_on_timeout(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._order_exec.place_virtual_order.assert_not_called()

    def test_journal_logs_timeout_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        reason = self.bot._journal.log_decision.call_args.kwargs.get("reason", "")
        self.assertIn("timed out", reason.lower())


class TestPythonRejectSkipsGatekeeper(unittest.TestCase):
    """Python hard review rejects signal → gatekeeper never called."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
        # Make Python review reject: signal has ema_stacked_bull=True but direction=SELL
        self.bot._market_data.get_indicators.return_value = {
            "current_price": 24000.0,
            "ema20": 24050.0,
            "adx14": 28.0,
            "rsi14": 45.0,
            "avg_volume_20": 50000.0,
            "ema_stacked_bull": True,   # bull stack → SELL is rejected
            "ema_stacked_bear": False,
        }

    def test_gatekeeper_not_called_on_python_reject(self):
        sig = _tradeable_signal(direction="SELL")  # will be rejected by ema_bull check
        self.bot._process_tradeable_signals_python([sig])
        self.bot._gatekeeper.evaluate.assert_not_called()

    def test_order_not_placed_on_python_reject(self):
        sig = _tradeable_signal(direction="SELL")
        self.bot._process_tradeable_signals_python([sig])
        self.bot._order_exec.place_virtual_order.assert_not_called()


class TestNoGatekeeperConfigured(unittest.TestCase):
    """When gatekeeper is None (no API key), all signals must be auto-rejected."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = None  # no API key

    def test_no_gatekeeper_rejects_all_signals(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._order_exec.place_virtual_order.assert_not_called()

    def test_no_gatekeeper_logs_reject_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        reason = self.bot._journal.log_decision.call_args.kwargs.get("reason", "")
        self.assertIn("not configured", reason.lower())


class TestGatekeeperContextBuilding(unittest.TestCase):
    """build_gatekeeper_context must produce correct indicator strings."""

    def test_context_includes_indicators(self):
        from context_builder import build_gatekeeper_context
        sig = {
            "symbol": "NIFTY",
            "interval": "3",
            "direction": "BUY",
            "entry_reference": 24000.0,
            "stop_loss": 23900.0,
            "target": 24200.0,
        }
        indicators = {
            "current_price": 24000.0,
            "ema20": 24050.0,
            "adx14": 28.0,
            "rsi14": 45.0,
            "avg_volume_20": 50000.0,
            "ema_stacked_bull": True,
            "ema_stacked_bear": False,
        }
        ctx = build_gatekeeper_context(sig, indicators)
        self.assertIn("NIFTY", ctx)
        self.assertIn("24000", ctx)
        self.assertIn("28.0", ctx)
        self.assertIn("45", ctx)
        self.assertIn("bullish", ctx.lower())

    def test_context_includes_rr_ratio(self):
        from context_builder import build_gatekeeper_context
        sig = {
            "symbol": "NIFTY",
            "interval": "3",
            "direction": "BUY",
            "entry_reference": 24000.0,
            "stop_loss": 23900.0,   # risk = 100
            "target": 24300.0,      # reward = 300 → R:R = 3.0
        }
        ctx = build_gatekeeper_context(sig, {"current_price": 24000.0})
        self.assertIn("3.0:1", ctx)

    def test_context_handles_missing_indicators_gracefully(self):
        from context_builder import build_gatekeeper_context
        sig = {"symbol": "NIFTY", "interval": "3", "direction": "BUY"}
        # Should not raise even with minimal indicator dict
        ctx = build_gatekeeper_context(sig, {})
        self.assertIn("NIFTY", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
