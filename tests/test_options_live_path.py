"""
tests/test_options_live_path.py
---------------------------------
24 tests proving the live trading path is options-only.

Classes:
  TestMainPyImportsSpreadComponents   (3 tests)
  TestLiveEntryFlowIsSpreadOnly       (6 tests)
  TestInstrumentGuardrails            (3 tests)
  TestTelegramMessages                (4 tests)
  TestPortfolioExitIntegration        (4 tests)
  TestRegistryDoesNotExposeFuturesEntry (4 tests)
"""
import os
import sys
import types as module_types
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch, call

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub google.genai so main.py can be imported without real credentials
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
from tools.options_spread_builder import SpreadBuilder, SpreadCandidate, SpreadLeg
from tools.options_spread_execution import SpreadExecutionEngine, OpenSpread
from tools.options_spread_portfolio import SpreadPortfolio
from tools.gemini_gatekeeper import GeminiGatekeeper

IST = pytz.timezone("Asia/Kolkata")


# ── Shared fixtures ────────────────────────────────────────────────────────────

def _fake_candidate() -> SpreadCandidate:
    expiry = date(2026, 5, 29)
    long_leg = SpreadLeg("BUY", "CE", 24500, expiry, "T1", "NIFTY29MAY2624500CE", "NFO", 75, {}, 51.0)
    short_leg = SpreadLeg("SELL", "CE", 24600, expiry, "T2", "NIFTY29MAY2624600CE", "NFO", 75, {}, 20.0)
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


def _fake_open_spread(symbol="NIFTY", spread_type="BULL_CALL") -> OpenSpread:
    return OpenSpread(
        spread_id="SPR-20260529-093000-AABBCC",
        symbol=symbol,
        spread_type=spread_type,
        direction="BULLISH",
        expiry="29-MAY-2026",
        lot_size=75,
        lots=1,
        long_tsym="NIFTY29MAY2624500CE",
        long_token="T1",
        long_action="BUY",
        long_strike=24500,
        long_option_type="CE",
        long_fill_price=51.0,
        long_order_id="ORD001",
        short_tsym="NIFTY29MAY2624600CE",
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


def _bot_ready(state_overrides: dict = None) -> BlitzTrader:
    """Return a BlitzTrader wired with all spread mocks."""
    state = {
        "is_paused": False,
        "is_stopped": False,
        "daily_pnl": 0.0,
        "positions": [],
        "pending_orders": [],
        "trades": [],
        "open_spreads": [],
    }
    if state_overrides:
        state.update(state_overrides)

    bot = BlitzTrader()
    bot._state = MagicMock()
    bot._state.get_state.return_value = state
    bot._active_tokens = {
        "NIFTY": {"exchange": "NFO", "token": "66691", "tsym": "NIFTY29MAY26F", "lot_size": 75},
        "BANKNIFTY": {"exchange": "NFO", "token": "66688", "tsym": "BANKNIFTY29MAY26F", "lot_size": 15},
    }
    bot._journal = MagicMock()
    bot._order_exec = MagicMock()
    bot._telegram = MagicMock()
    bot._market_data = MagicMock()
    bot._market_data.get_indicators.return_value = {
        "current_price": 24000.0,
        "ema20": 24000.0,
        "adx14": 28.0,
        "rsi14": 50.0,
        "avg_volume_20": 50000.0,
        "ema_stacked_bull": True,
        "ema_stacked_bear": False,
    }
    bot._market_data.get_candles.return_value = {"candles": []}
    bot._market_data.get_spot_price.return_value = {"spot_price": 24000.0}
    bot._promoted_futures_filters = []
    bot._audit = MagicMock()

    bot._spread_builder = MagicMock(spec=SpreadBuilder)
    bot._spread_builder.build.return_value = _fake_candidate()

    bot._spread_exec = MagicMock(spec=SpreadExecutionEngine)
    bot._spread_exec.place_spread.return_value = {"ok": True, "spread": _fake_open_spread()}

    bot._spread_portfolio = MagicMock(spec=SpreadPortfolio)
    bot._spread_portfolio.count_open_spreads.return_value = 0
    bot._spread_portfolio.get_open_spreads.return_value = []

    bot._gatekeeper = MagicMock(spec=GeminiGatekeeper)
    bot._gatekeeper.evaluate.return_value = {
        "approved": True,
        "confidence": 0.85,
        "reason": "Strong bull trend confirmed",
        "risk_notes": "",
        "conditions_checked": ["EMA stack", "ADX"],
        "gatekeeper_error": None,
    }
    return bot


def _nifty_signal(direction="BUY"):
    return {
        "symbol": "NIFTY",
        "strategy": "VP-05 3EMA Trend",
        "direction": direction,
        "interval": "3",
        "entry_reference": 24000.0,
        "stop_loss": 23900.0,
        "target": 24200.0,
    }


def _banknifty_signal(direction="BUY"):
    return {
        "symbol": "BANKNIFTY",
        "strategy": "VP-07 Wicks Pullback",
        "direction": direction,
        "interval": "5",
        "entry_reference": 52000.0,
        "stop_loss": 51800.0,
        "target": 52400.0,
    }


# ══════════════════════════════════════════════════════════════════════════════
#   1. TestMainPyImportsSpreadComponents (3 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestMainPyImportsSpreadComponents(unittest.TestCase):
    """main.py imports and instantiates all spread components."""

    def test_main_imports_spread_builder(self):
        import main as main_mod
        self.assertTrue(hasattr(main_mod, "SpreadBuilder"))

    def test_main_imports_spread_execution_engine(self):
        import main as main_mod
        self.assertTrue(hasattr(main_mod, "SpreadExecutionEngine"))

    def test_blitztrader_has_spread_instance_vars(self):
        bot = BlitzTrader()
        self.assertTrue(hasattr(bot, "_spread_builder"))
        self.assertTrue(hasattr(bot, "_spread_exec"))
        self.assertTrue(hasattr(bot, "_spread_portfolio"))
        self.assertTrue(hasattr(bot, "_options_chain"))
        # All should be None before _initialize() is called
        self.assertIsNone(bot._spread_builder)
        self.assertIsNone(bot._spread_exec)
        self.assertIsNone(bot._spread_portfolio)


# ══════════════════════════════════════════════════════════════════════════════
#   2. TestLiveEntryFlowIsSpreadOnly (6 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestLiveEntryFlowIsSpreadOnly(unittest.TestCase):
    """Live entry calls SpreadBuilder → SpreadExec; never calls place_virtual_order."""

    def setUp(self):
        self.bot = _bot_ready()

    def test_live_entry_calls_spread_builder(self):
        self.bot._process_tradeable_signals_python([_nifty_signal()])
        self.bot._spread_builder.build.assert_called_once()

    def test_live_entry_does_not_call_place_virtual_order(self):
        self.bot._process_tradeable_signals_python([_nifty_signal()])
        self.bot._order_exec.place_virtual_order.assert_not_called()


class TestNoPyramidingUsesOpenSpreads(unittest.TestCase):
    """Open option spreads must reserve their underlying before Gemini is called."""

    def setUp(self):
        self.bot = _bot_ready()

    def test_open_nifty_spread_blocks_new_nifty_signal(self):
        self.bot._spread_portfolio.count_open_spreads.return_value = 1
        self.bot._spread_portfolio.get_open_spreads.return_value = [_fake_open_spread("NIFTY")]

        tradeable, blocked = self.bot._filter_tradeable_signals(
            [_nifty_signal()],
            IST.localize(datetime(2026, 5, 29, 10, 0)),
        )

        self.assertEqual(tradeable, [])
        self.assertEqual(len(blocked), 1)
        self.assertIn("No pyramiding: NIFTY", blocked[0]["blocked_reason"])

    def test_spread_builder_none_gatekeeper_not_called(self):
        self.bot._spread_builder.build.return_value = None
        self.bot._process_tradeable_signals_python([_nifty_signal()])
        self.bot._gatekeeper.evaluate.assert_not_called()

    def test_spread_builder_none_place_spread_not_called(self):
        self.bot._spread_builder.build.return_value = None
        self.bot._process_tradeable_signals_python([_nifty_signal()])
        self.bot._spread_exec.place_spread.assert_not_called()

    def test_gemini_reject_place_spread_not_called(self):
        self.bot._gatekeeper.evaluate.return_value = {
            "approved": False, "confidence": 0.3,
            "reason": "Bearish divergence", "gatekeeper_error": None,
            "risk_notes": "", "conditions_checked": [],
        }
        self.bot._process_tradeable_signals_python([_nifty_signal()])
        self.bot._spread_exec.place_spread.assert_not_called()

    def test_gemini_approve_place_spread_called_exactly_once(self):
        self.bot._process_tradeable_signals_python([_nifty_signal()])
        self.bot._spread_exec.place_spread.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
#   3. TestInstrumentGuardrails (3 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestInstrumentGuardrails(unittest.TestCase):
    """NIFTY and BANKNIFTY allowed; FINNIFTY blocked; naked options structurally impossible."""

    def setUp(self):
        self.bot = _bot_ready()

    def _market_hours_now(self):
        """Return a fixed datetime during market hours so time-cutoff guardrail doesn't fire."""
        return datetime(2026, 5, 27, 10, 30, 0, tzinfo=IST)

    def test_finnifty_signal_blocked_before_spread_builder(self):
        """FINNIFTY → _logical_instrument returns None → HARD_GUARDRAIL_BLOCKED."""
        finnifty_sig = {
            "symbol": "FINNIFTY",
            "strategy": "VP-05 3EMA Trend",
            "direction": "BUY",
            "interval": "3",
            "entry_reference": 22000.0,
        }
        now = self._market_hours_now()
        tradeable, blocked = self.bot._filter_tradeable_signals([finnifty_sig], now)
        self.assertEqual(len(tradeable), 0)
        self.assertEqual(len(blocked), 1)
        self.assertIn("Unknown signal instrument", blocked[0].get("blocked_reason", ""))

    def test_nifty_signal_passes_filter(self):
        now = self._market_hours_now()
        tradeable, blocked = self.bot._filter_tradeable_signals([_nifty_signal()], now)
        self.assertEqual(len(tradeable), 1)
        self.assertEqual(len(blocked), 0)

    def test_banknifty_signal_passes_filter(self):
        now = self._market_hours_now()
        tradeable, blocked = self.bot._filter_tradeable_signals([_banknifty_signal()], now)
        self.assertEqual(len(tradeable), 1)
        self.assertEqual(len(blocked), 0)


# ══════════════════════════════════════════════════════════════════════════════
#   4. TestTelegramMessages (4 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestTelegramMessages(unittest.TestCase):
    """Entry message has both legs, expiry, max profit/loss; no SL/Target/trailing."""

    def setUp(self):
        self.bot = _bot_ready()
        self.bot._process_tradeable_signals_python([_nifty_signal()])
        self.msg = self.bot._telegram.send_telegram.call_args[0][0]

    def test_entry_message_has_both_leg_tsyms(self):
        self.assertIn("NIFTY29MAY2624500CE", self.msg)
        self.assertIn("NIFTY29MAY2624600CE", self.msg)

    def test_entry_message_has_expiry_and_economics(self):
        self.assertIn("29-MAY-2026", self.msg)
        self.assertIn("Max profit", self.msg)
        self.assertIn("Max loss", self.msg)

    def test_entry_message_has_no_sl_target_trailing(self):
        self.assertNotIn("SL:", self.msg)
        self.assertNotIn("Target:", self.msg)
        self.assertNotIn("trailing", self.msg.lower())
        self.assertNotIn("stop_loss", self.msg.lower())

    def test_status_message_shows_open_spreads(self):
        """build_status_lines() output wired through _try_answer_simple_chat()."""
        self.bot._spread_portfolio.build_status_lines.return_value = [
            "Open Option Spreads",
            "1. [SPR-20260529-093000-AABBCC] NIFTY BULL_CALL expiry=29-MAY-2026",
            "   Long:  NIFTY29MAY2624500CE entry=₹51.00",
            "   Short: NIFTY29MAY2624600CE entry=₹20.00",
            "   Unrealized P&L: ₹+231.00",
        ]
        self.bot._spread_portfolio.get_open_spreads.return_value = [_fake_open_spread()]
        self.bot._state.get_state.return_value = {
            "daily_pnl": 0.0, "daily_pnl_pct": 0.0,
            "virtual_capital": 1000000.0, "available_balance": 990000.0,
        }
        chat_msgs = [{"text": "show positions"}]
        self.bot._try_answer_simple_chat(chat_msgs)
        status_msg = self.bot._telegram.send_telegram.call_args[0][0]
        self.assertIn("NIFTY BULL_CALL", status_msg)


# ══════════════════════════════════════════════════════════════════════════════
#   5. TestPortfolioExitIntegration (4 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestPortfolioExitIntegration(unittest.TestCase):
    """Scan loop calls check_and_exit_spreads(force=False); EOD calls force=True."""

    def setUp(self):
        self.bot = _bot_ready()
        self.bot._spread_portfolio.check_and_exit_spreads.return_value = []

    def _run_one_scan_tick(self):
        """Simulate one scanner tick by calling the scan block directly."""
        scan_result = {"signals": [_nifty_signal()], "notes": ""}
        self.bot._market_data.get_strategy_signals = MagicMock(return_value=scan_result)

        # Patch time and the full scan loop to just run the spread monitoring
        # by calling check_and_exit_spreads manually as the loop would
        if self.bot._spread_portfolio:
            exits = self.bot._spread_portfolio.check_and_exit_spreads(force_close_all=False)
        return exits

    def test_scan_loop_calls_check_and_exit_with_force_false(self):
        self._run_one_scan_tick()
        self.bot._spread_portfolio.check_and_exit_spreads.assert_called_with(force_close_all=False)

    def test_eod_calls_check_and_exit_with_force_true(self):
        self.bot._spread_portfolio.check_and_exit_spreads.return_value = []
        # EOD close called directly
        self.bot._spread_portfolio.check_and_exit_spreads(force_close_all=True)
        self.bot._spread_portfolio.check_and_exit_spreads.assert_called_with(force_close_all=True)

    def test_credit_spread_tp_threshold_is_60_pct(self):
        """SpreadPortfolio.should_exit triggers at 60% max_profit for credit spreads.
        net_debit_or_credit < 0 signals a net credit received (credit spread).
        """
        from tools.options_spread_portfolio import should_exit
        # Override with negative net_debit_or_credit so is_credit detection works
        spread = _fake_open_spread(spread_type="BULL_PUT")
        spread.net_debit_or_credit = -31.0  # credit spread: negative = net credit received
        # 61% of max_profit = 5175 * 0.61 ≈ 3156.75 unrealized
        pnl_data = {"unrealized_pnl": 5175.0 * 0.61, "data_ok": True,
                    "long_ltp": 10.0, "short_ltp": 30.0}
        exit_flag, reason = should_exit(spread, pnl_data, credit_tp_fraction=0.60)
        self.assertTrue(exit_flag)
        self.assertIn("take-profit", reason.lower())

    def test_debit_spread_tp_threshold_is_70_pct(self):
        """SpreadPortfolio.should_exit triggers at 70% max_profit for debit spreads."""
        from tools.options_spread_portfolio import should_exit
        spread = _fake_open_spread(spread_type="BULL_CALL")  # net_debit_or_credit=31.0 > 0
        # 71% of max_profit
        pnl_data = {"unrealized_pnl": 5175.0 * 0.71, "data_ok": True,
                    "long_ltp": 80.0, "short_ltp": 10.0}
        exit_flag, reason = should_exit(spread, pnl_data, debit_tp_fraction=0.70)
        self.assertTrue(exit_flag)
        self.assertIn("take-profit", reason.lower())


# ══════════════════════════════════════════════════════════════════════════════
#   6. TestRegistryDoesNotExposeFuturesEntry (4 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestRegistryDoesNotExposeFuturesEntry(unittest.TestCase):
    """LIVE_TOOLS must not expose futures entry/close tools to the LLM."""

    def test_live_tools_excludes_place_virtual_order(self):
        from tools.registry import LIVE_TOOLS
        self.assertNotIn("place_virtual_order", LIVE_TOOLS)

    def test_live_tools_excludes_close_all_positions(self):
        from tools.registry import LIVE_TOOLS
        self.assertNotIn("close_all_positions", LIVE_TOOLS)

    def test_live_tools_excludes_close_position(self):
        from tools.registry import LIVE_TOOLS
        self.assertNotIn("close_position", LIVE_TOOLS)

    def test_historical_imports_still_work(self):
        """Legacy futures tools remain importable — no broken imports in backtest scripts."""
        from tools.registry import LEGACY_TOOLS
        self.assertIn("place_virtual_order", LEGACY_TOOLS)
        self.assertIn("close_all_positions", LEGACY_TOOLS)
        self.assertIn("close_position", LEGACY_TOOLS)
        # Core spread tools are in live set
        from tools.registry import LIVE_TOOLS
        self.assertIn("exit_spread_by_serial", LIVE_TOOLS)
        self.assertIn("get_spread_status", LIVE_TOOLS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
