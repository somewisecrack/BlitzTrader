"""
tests/test_gatekeeper_entry_flow.py
-------------------------------------
Integration-style tests for the two-stage spread entry gate:
  1. Python hard review  (deterministic)
  2. SpreadBuilder       (build spread candidate)
  3. Gemini gatekeeper   (5s timeout, APPROVE/REJECT)
  4. SpreadExecutionEngine.place_spread()

These tests exercise _process_tradeable_signals_python() with mocked
spread components and gatekeeper to verify invariants without making
real API calls or live orders.

Invariants tested:
  - Python-rejected signal never reaches SpreadBuilder or gatekeeper
  - SpreadBuilder returning None → gatekeeper NOT called, spread NOT placed
  - Gatekeeper REJECT → spread NOT placed
  - Gatekeeper APPROVE + Python approved → place_spread called exactly once
  - Gatekeeper timeout/error → spread NOT placed
  - No API key → all signals auto-rejected (gatekeeper=None)
  - Entry Telegram message contains both leg tsyms, max profit, max loss
  - Entry Telegram message does NOT contain "SL" or "Target"
"""
import os
import sys
import types as module_types
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

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
from tools.options_spread_builder import SpreadBuilder, SpreadCandidate, SpreadLeg
from tools.options_spread_execution import SpreadExecutionEngine, OpenSpread
from tools.options_spread_portfolio import SpreadPortfolio

IST = pytz.timezone("Asia/Kolkata")


# ── Test fixtures ──────────────────────────────────────────────────────────────

def _fake_candidate() -> SpreadCandidate:
    expiry = date(2026, 5, 29)
    long_leg = SpreadLeg("BUY", "CE", 24500, expiry, "T1", "NIFTY29MAY26C24500", "NFO", 75, {}, 51.0)
    short_leg = SpreadLeg("SELL", "CE", 24600, expiry, "T2", "NIFTY29MAY26C24600", "NFO", 75, {}, 20.0)
    return SpreadCandidate(
        symbol="NIFTY",
        spread_type="BULL_CALL",
        direction="BULLISH",
        expiry=expiry,
        expiry_str="29-MAY-2026",
        legs=[long_leg, short_leg],
        lot_size=75,
        lots=1,
        net_debit_or_credit=31.0,
        max_profit=5175.0,
        max_loss=2325.0,
        breakeven=24531.0,
        risk_reward=2.2,
        signal_id="",
        strategy="VP-05 3EMA Trend",
        underlying_price=24000.0,
    )


def _fake_open_spread() -> OpenSpread:
    return OpenSpread(
        spread_id="SPR-20260529-123456-ABCDEF",
        symbol="NIFTY",
        spread_type="BULL_CALL",
        direction="BULLISH",
        expiry="29-MAY-2026",
        lot_size=75,
        lots=1,
        long_tsym="NIFTY29MAY26C24500",
        long_token="T1",
        long_action="BUY",
        long_strike=24500,
        long_option_type="CE",
        long_fill_price=51.0,
        long_order_id="ORD001",
        short_tsym="NIFTY29MAY26C24600",
        short_token="T2",
        short_action="SELL",
        short_strike=24600,
        short_option_type="CE",
        short_fill_price=20.0,
        short_order_id="ORD002",
        net_debit_or_credit=31.0,
        max_profit=5175.0,
        max_loss=2325.0,
        breakeven=24531.0,
        opened_at="2026-05-29T09:30:00+05:30",
        signal_id="sig_001",
        strategy="VP-05 3EMA Trend",
        underlying_at_entry=24000.0,
    )


def _bot_with_state(state: dict) -> BlitzTrader:
    bot = BlitzTrader()
    bot._state = MagicMock()
    bot._state.get_state.return_value = state
    bot._active_tokens = {
        "NIFTY": {
            "exchange": "NFO",
            "token": "66691",
            "tsym": "NIFTY29MAY26F",
            "lot_size": 75,
        },
        "BANKNIFTY": {
            "exchange": "NFO",
            "token": "66688",
            "tsym": "BANKNIFTY29MAY26F",
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
    bot._market_data.get_spot_price.return_value = {"spot_price": 24000.0}
    bot._promoted_futures_filters = []
    bot._audit = MagicMock()

    # Options spread components
    bot._spread_builder = MagicMock(spec=SpreadBuilder)
    bot._spread_builder.build.return_value = _fake_candidate()

    bot._spread_exec = MagicMock(spec=SpreadExecutionEngine)
    bot._spread_exec.place_spread.return_value = {"ok": True, "spread": _fake_open_spread()}

    bot._spread_portfolio = MagicMock(spec=SpreadPortfolio)
    bot._spread_portfolio.count_open_spreads.return_value = 0
    bot._spread_portfolio.get_open_spreads.return_value = []

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
    """Signal as passed by _filter_tradeable_signals — no execution_symbol or lot_size."""
    return {
        "symbol": symbol,
        "strategy": "VP-05 3EMA Trend",
        "direction": direction,
        "interval": "3",
        "entry_reference": 24000.0,
        "stop_loss": 23900.0,
        "target": 24200.0,
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


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestGatekeeperApproveFlow(unittest.TestCase):
    """Happy path: Python approves, SpreadBuilder succeeds, Gemini approves → spread placed."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
        self.bot._gatekeeper.evaluate.return_value = _gate_approve()

    def test_place_spread_called_on_double_approval(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._spread_exec.place_spread.assert_called_once()

    def test_place_virtual_order_never_called(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._order_exec.place_virtual_order.assert_not_called()

    def test_journal_logs_entry_action(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        call_args = self.bot._journal.log_decision.call_args
        self.assertIn(call_args.kwargs.get("action", ""), {"ENTER_LONG", "ENTER_SHORT"})

    def test_telegram_notification_sent(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._telegram.send_telegram.assert_called_once()

    def test_telegram_message_includes_both_legs(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        msg = self.bot._telegram.send_telegram.call_args[0][0]
        self.assertIn("NIFTY29MAY26C24500", msg)
        self.assertIn("NIFTY29MAY26C24600", msg)

    def test_telegram_message_includes_max_profit_and_max_loss(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        msg = self.bot._telegram.send_telegram.call_args[0][0]
        self.assertIn("Max profit", msg)
        self.assertIn("Max loss", msg)

    def test_telegram_message_has_no_sl_or_target(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        msg = self.bot._telegram.send_telegram.call_args[0][0]
        self.assertNotIn("SL:", msg)
        self.assertNotIn("Target:", msg)
        self.assertNotIn("stop_loss", msg.lower())
        self.assertNotIn("trailing", msg.lower())

    def test_telegram_message_includes_gemini_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        msg = self.bot._telegram.send_telegram.call_args[0][0]
        self.assertIn("EMA stacked bullish", msg)
        self.assertIn("85%", msg)

    def test_telegram_message_has_no_gemma_reference(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        msg = self.bot._telegram.send_telegram.call_args[0][0]
        self.assertNotIn("Gemma", msg)
        self.assertNotIn("gemma", msg)
        self.assertNotIn("observer", msg)
        self.assertNotIn("Ollama", msg)

    def test_journal_entry_includes_gatekeeper_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        reason = self.bot._journal.log_decision.call_args.kwargs.get("reason", "")
        self.assertIn("Gatekeeper", reason)
        self.assertIn("EMA stacked bullish", reason)


class TestSpreadBuilderNoneRejectsEntry(unittest.TestCase):
    """SpreadBuilder returning None → gatekeeper NOT called, spread NOT placed."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
        self.bot._spread_builder.build.return_value = None  # SpreadBuilder failed

    def test_gatekeeper_not_called_when_spread_build_fails(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._gatekeeper.evaluate.assert_not_called()

    def test_place_spread_not_called_when_build_fails(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._spread_exec.place_spread.assert_not_called()

    def test_journal_logs_spread_build_reject(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        reason = self.bot._journal.log_decision.call_args.kwargs.get("reason", "")
        self.assertIn("Spread build failed", reason)


class TestGatekeeperRejectFlow(unittest.TestCase):
    """Gemini rejects → spread NOT placed."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
        self.bot._gatekeeper.evaluate.return_value = _gate_reject("Conflicting EMA signals")

    def test_place_spread_not_called_on_gatekeeper_reject(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._spread_exec.place_spread.assert_not_called()

    def test_journal_logs_reject_with_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        call_args = self.bot._journal.log_decision.call_args
        self.assertEqual(call_args.kwargs.get("action"), "REJECT")
        self.assertIn("Conflicting EMA signals", call_args.kwargs.get("reason", ""))

    def test_telegram_not_sent_on_reject(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._telegram.send_telegram.assert_not_called()


class TestGatekeeperTimeoutFlow(unittest.TestCase):
    """Gatekeeper timeout → auto-reject, spread NOT placed."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
        self.bot._gatekeeper.evaluate.return_value = _gate_reject(
            error="Gatekeeper timed out after 5s"
        )

    def test_place_spread_not_called_on_timeout(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._spread_exec.place_spread.assert_not_called()

    def test_journal_logs_timeout_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        reason = self.bot._journal.log_decision.call_args.kwargs.get("reason", "")
        self.assertIn("timed out", reason.lower())


class TestPythonRejectSkipsGatekeeperAndSpreadBuilder(unittest.TestCase):
    """Python hard review rejects signal → SpreadBuilder and gatekeeper never called."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
        # ema_stacked_bull=True but direction=SELL → Python rejects
        self.bot._market_data.get_indicators.return_value = {
            "current_price": 24000.0,
            "ema20": 24050.0,
            "adx14": 28.0,
            "rsi14": 45.0,
            "avg_volume_20": 50000.0,
            "ema_stacked_bull": True,
            "ema_stacked_bear": False,
        }

    def test_gatekeeper_not_called_on_python_reject(self):
        sig = _tradeable_signal(direction="SELL")
        self.bot._process_tradeable_signals_python([sig])
        self.bot._gatekeeper.evaluate.assert_not_called()

    def test_spread_builder_not_called_on_python_reject(self):
        sig = _tradeable_signal(direction="SELL")
        self.bot._process_tradeable_signals_python([sig])
        self.bot._spread_builder.build.assert_not_called()

    def test_place_spread_not_called_on_python_reject(self):
        sig = _tradeable_signal(direction="SELL")
        self.bot._process_tradeable_signals_python([sig])
        self.bot._spread_exec.place_spread.assert_not_called()


class TestNoGatekeeperConfigured(unittest.TestCase):
    """When gatekeeper is None (no API key), all signals must be auto-rejected."""

    def setUp(self):
        self.bot = _bot_with_state(_state())
        self.bot._gatekeeper = None  # no API key

    def test_no_gatekeeper_rejects_all_signals(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        self.bot._spread_exec.place_spread.assert_not_called()

    def test_no_gatekeeper_logs_reject_reason(self):
        self.bot._process_tradeable_signals_python([_tradeable_signal()])
        reason = self.bot._journal.log_decision.call_args.kwargs.get("reason", "")
        self.assertIn("not configured", reason.lower())


class TestGatekeeperContextBuilding(unittest.TestCase):
    """build_gatekeeper_context and build_spread_gatekeeper_context produce correct output."""

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
        ctx = build_gatekeeper_context(sig, {})
        self.assertIn("NIFTY", ctx)

    def test_spread_gatekeeper_context_includes_both_legs(self):
        from context_builder import build_spread_gatekeeper_context
        candidate = _fake_candidate()
        indicators = {
            "current_price": 24000.0,
            "ema20": 24050.0,
            "adx14": 28.0,
            "rsi14": 55.0,
            "ema_stacked_bull": True,
            "ema_stacked_bear": False,
        }
        ctx = build_spread_gatekeeper_context(candidate, indicators)
        self.assertIn("NIFTY29MAY26C24500", ctx)
        self.assertIn("NIFTY29MAY26C24600", ctx)
        self.assertIn("BULL_CALL", ctx)
        self.assertIn("APPROVE or REJECT", ctx)

    def test_spread_gatekeeper_context_has_no_sl_target(self):
        from context_builder import build_spread_gatekeeper_context
        candidate = _fake_candidate()
        ctx = build_spread_gatekeeper_context(candidate, {})
        self.assertNotIn("stop_loss", ctx.lower())
        self.assertNotIn("target", ctx.lower())
        self.assertIn("Max-loss exit", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
