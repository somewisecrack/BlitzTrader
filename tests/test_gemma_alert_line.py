"""
tests/test_gemma_alert_line.py
--------------------------------
Tests for Part B: Gemma status must appear in EVERY trade entry alert.

Invariants tested:
  - Valid opinion → alignment and confidence in message
  - Gemma disabled (_enabled=False), no opinion → UNAVAILABLE in message
  - Gemma enabled but opinion not yet arrived → PENDING in message
  - Opinion with gemma_error → UNAVAILABLE in message
"""
import os
import sys
import types as module_types
import unittest
from unittest.mock import MagicMock, patch

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


def _make_bot():
    """Create a BlitzTrader instance wired for _notify_entry() testing."""
    bot = BlitzTrader()
    bot._telegram = MagicMock()
    bot._gemma_opinions = {}
    bot._gemma = MagicMock()
    bot._gemma._enabled = True
    return bot


def _signal():
    return {
        "symbol": "NIFTY",
        "strategy": "VP-05 3EMA Trend",
        "direction": "BUY",
        "stop_loss": 23900.0,
        "target": 24200.0,
    }


def _gate_result():
    return {
        "confidence": 0.85,
        "reason": "EMA stacked bullish",
        "conditions_checked": ["EMA bull stack"],
        "risk_notes": "",
    }


class TestGemmaAlertLine(unittest.TestCase):
    """Gemma line must always appear in the Telegram entry notification."""

    def _get_sent_message(self, bot):
        """Return the message string passed to send_telegram."""
        self.assertTrue(
            bot._telegram.send_telegram.called,
            "send_telegram was not called at all",
        )
        return bot._telegram.send_telegram.call_args[0][0]

    def test_alert_includes_gemma_valid_opinion(self):
        """A valid opinion with alignment=STRONG and confidence=0.8 must appear."""
        bot = _make_bot()
        signal_id = "TEST_SIG_001"
        bot._gemma_opinions[signal_id] = {
            "alignment": "STRONG",
            "confidence": 0.8,
            "key_observation": "EMA stack confirmed bullish momentum",
            "gemma_error": None,
        }

        bot._notify_entry(
            signal=_signal(),
            fill_price=24000.0,
            gate_result=_gate_result(),
            python_reason="Python approved",
            signal_id=signal_id,
        )

        msg = self._get_sent_message(bot)
        self.assertIn("STRONG", msg)
        self.assertIn("80%", msg)
        self.assertIn("EMA stack confirmed", msg)

    def test_alert_includes_gemma_unavailable_when_disabled(self):
        """
        When Gemma observer is disabled and no opinion in dict,
        the message must contain 'UNAVAILABLE'.
        """
        bot = _make_bot()
        bot._gemma._enabled = False  # observer is disabled
        signal_id = "TEST_SIG_002"
        # No opinion in _gemma_opinions

        bot._notify_entry(
            signal=_signal(),
            fill_price=24000.0,
            gate_result=_gate_result(),
            python_reason="Python approved",
            signal_id=signal_id,
        )

        msg = self._get_sent_message(bot)
        self.assertIn("UNAVAILABLE", msg)
        self.assertIn("GEMMA_OBSERVER_ENABLED=false", msg)

    def test_alert_includes_gemma_pending_when_async_not_arrived(self):
        """
        When Gemma is enabled but no opinion has arrived yet,
        the message must contain 'PENDING'.
        """
        bot = _make_bot()
        bot._gemma._enabled = True
        signal_id = "TEST_SIG_003"
        # No opinion in _gemma_opinions

        bot._notify_entry(
            signal=_signal(),
            fill_price=24000.0,
            gate_result=_gate_result(),
            python_reason="Python approved",
            signal_id=signal_id,
        )

        msg = self._get_sent_message(bot)
        self.assertIn("PENDING", msg)

    def test_alert_includes_gemma_error_line(self):
        """
        When the opinion dict contains gemma_error, the message must
        contain 'UNAVAILABLE' with the error text.
        """
        bot = _make_bot()
        signal_id = "TEST_SIG_004"
        bot._gemma_opinions[signal_id] = {
            "alignment": None,
            "confidence": 0,
            "key_observation": None,
            "gemma_error": "connection refused",
        }

        bot._notify_entry(
            signal=_signal(),
            fill_price=24000.0,
            gate_result=_gate_result(),
            python_reason="Python approved",
            signal_id=signal_id,
        )

        msg = self._get_sent_message(bot)
        self.assertIn("UNAVAILABLE", msg)
        self.assertIn("connection refused", msg)

    def test_gemma_line_present_even_when_gemma_is_none(self):
        """
        If self._gemma is None (not configured at all),
        we fall through to the PENDING branch (not a crash).
        """
        bot = _make_bot()
        bot._gemma = None  # no observer configured
        signal_id = "TEST_SIG_005"
        # No opinion in _gemma_opinions

        bot._notify_entry(
            signal=_signal(),
            fill_price=24000.0,
            gate_result=_gate_result(),
            python_reason="Python approved",
            signal_id=signal_id,
        )

        msg = self._get_sent_message(bot)
        # With gemma=None and no opinion, condition `self._gemma and not self._gemma._enabled`
        # is False (gemma is None → falsy), so we get PENDING
        self.assertIn("PENDING", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
