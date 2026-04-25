"""
tests/test_event_driven_gatekeeper.py
-------------------------------------
Proves Python is the live decision engine for signal execution.
"""
import os
import sys
import unittest
import types as module_types
from datetime import datetime
from unittest.mock import MagicMock

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The local test environment used for quick guardrail tests may not have the
# Gemini SDK installed.  These tests only exercise BlitzTrader's Python
# prefilter, so stub the import surface needed while importing main.py.
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
        "FINNIFTY": {
            "exchange": "NFO",
            "token": "66689",
            "tsym": "FINNIFTY28APR26F",
            "lot_size": 60,
        },
    }
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


def _signal(symbol="NIFTY", strategy="VP-05 3EMA Trend", direction="BUY"):
    return {
        "symbol": symbol,
        "interval": "3",
        "strategy": strategy,
        "direction": direction,
        "entry_reference": 24000.0,
        "stop_loss": 23900.0,
    }


class TestEventDrivenGatekeeper(unittest.TestCase):

    def test_tradeable_signal_is_enriched_without_scoring(self):
        bot = _bot_with_state(_state())
        tradeable, blocked = bot._filter_tradeable_signals(
            [_signal()],
            IST.localize(datetime(2026, 4, 22, 10, 0)),
        )

        self.assertEqual(blocked, [])
        self.assertEqual(len(tradeable), 1)
        self.assertEqual(tradeable[0]["execution_symbol"], "NIFTY28APR26F")
        self.assertEqual(tradeable[0]["lot_size"], 25)
        self.assertNotIn("score", tradeable[0])
        self.assertNotIn("approved", tradeable[0])

    def test_same_instrument_open_position_blocks_duplicate_entry(self):
        bot = _bot_with_state(_state(
            positions=[{"symbol": "NIFTY28APR26F", "direction": "BUY"}],
        ))
        tradeable, blocked = bot._filter_tradeable_signals(
            [_signal()],
            IST.localize(datetime(2026, 4, 22, 10, 0)),
        )

        self.assertEqual(tradeable, [])
        self.assertEqual(len(blocked), 1)
        self.assertIn("No pyramiding", blocked[0]["blocked_reason"])

    def test_daily_trade_cap_blocks_all_candidates(self):
        bot = _bot_with_state(_state(trades=[{} for _ in range(10)]))
        tradeable, blocked = bot._filter_tradeable_signals(
            [_signal("NIFTY"), _signal("BANKNIFTY")],
            IST.localize(datetime(2026, 4, 22, 10, 0)),
        )

        self.assertEqual(tradeable, [])
        self.assertEqual(len(blocked), 2)
        self.assertIn("Daily trade cap reached", blocked[0]["blocked_reason"])

    def test_existing_pending_review_blocks_duplicate_instrument(self):
        bot = _bot_with_state(_state())
        tradeable, blocked = bot._filter_tradeable_signals(
            [_signal("NIFTY"), _signal("BANKNIFTY")],
            IST.localize(datetime(2026, 4, 22, 10, 0)),
            existing_pending=[_signal("NIFTY")],
        )

        self.assertEqual([s["symbol"] for s in tradeable], ["BANKNIFTY"])
        self.assertEqual([s["symbol"] for s in blocked], ["NIFTY"])

    def test_after_no_entry_cutoff_blocks_without_gemini(self):
        bot = _bot_with_state(_state())
        tradeable, blocked = bot._filter_tradeable_signals(
            [_signal()],
            IST.localize(datetime(2026, 4, 22, 15, 6)),
        )

        self.assertEqual(tradeable, [])
        self.assertIn("No new entries after", blocked[0]["blocked_reason"])

    def test_python_review_allows_finnifty_vp01_when_indicators_align(self):
        bot = _bot_with_state(_state())
        bot._market_data = MagicMock()
        bot._market_data.get_indicators.return_value = {
            "current_price": 26200.0,
            "ema20": 26220.0,
            "adx14": 25.0,
            "rsi14": 42.0,
            "avg_volume_20": 1000.0,
            "ema_stacked_bull": False,
            "ema_stacked_bear": True,
        }

        approved, _context, reason = bot._review_signal_python({
            "symbol": "FINNIFTY",
            "interval": "5",
            "strategy": "VP-01 Counter Bull Trap",
            "direction": "SELL",
            "stop_loss": 26250.0,
            "target": 26100.0,
        })

        self.assertTrue(approved)
        self.assertIn("Python approved", reason)

    def test_python_review_accepts_valid_nifty_signal(self):
        bot = _bot_with_state(_state())
        bot._market_data = MagicMock()
        bot._market_data.get_indicators.return_value = {
            "current_price": 24000.0,
            "ema20": 24020.0,
            "adx14": 28.0,
            "rsi14": 39.0,
            "avg_volume_20": 50000.0,
            "ema_stacked_bull": False,
            "ema_stacked_bear": True,
        }

        approved, context, reason = bot._review_signal_python({
            "symbol": "NIFTY",
            "interval": "5",
            "strategy": "VP-01 Counter Bull Trap",
            "direction": "SELL",
            "stop_loss": 24040.0,
            "target": 23920.0,
        })

        self.assertTrue(approved)
        self.assertIn("NIFTY 5m", context)
        self.assertIn("Python approved", reason)

    def test_python_review_enforces_volume_confirmation(self):
        bot = _bot_with_state(_state())
        bot._market_data = MagicMock()
        bot._market_data.get_indicators.return_value = {
            "current_price": 56000.0,
            "ema20": 56100.0,
            "adx14": 24.0,
            "rsi14": 33.0,
            "avg_volume_20": 12000.0,
            "ema_stacked_bull": False,
            "ema_stacked_bear": True,
        }
        bot._market_data.get_candles.return_value = {
            "candles": [{"volume": 5000}, {"volume": 7000}],
        }

        approved, _context, reason = bot._review_signal_python({
            "symbol": "BANKNIFTY",
            "interval": "3",
            "strategy": "VP-07 Wicks Pullback",
            "direction": "SELL",
            "stop_loss": 56150.0,
            "target": 55850.0,
            "requires_volume_confirmation": True,
        })

        self.assertFalse(approved)
        self.assertIn("below avg_volume_20", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
