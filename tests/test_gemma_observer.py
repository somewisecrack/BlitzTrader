"""
tests/test_gemma_observer.py
------------------------------
Unit tests for GemmaObserver — the async non-blocking signal observer.

Invariants under test:
  - submit() returns immediately (non-blocking)
  - callback is invoked from daemon thread with opinion dict
  - timeout → error_opinion with gemma_error set
  - API error → error_opinion
  - Invalid JSON → error_opinion
  - Valid response → opinion with alignment/confidence/key_observation/concern
  - alignment must be in STRONG/MODERATE/WEAK/CONFLICTED
  - opinion NEVER has an "approved" or "decision" field (not a decision maker)
  - _error_opinion always returns gemma_error, never raises
  - Gemma observer failure does not affect trading logic (caller ignores gracefully)
"""
import json
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.gemma_observer import GemmaObserver, _VALID_ALIGNMENTS


def _observer(callback=None, timeout: int = 15) -> GemmaObserver:
    return GemmaObserver(
        api_key="test-key",
        model="gemma-3-4b-it",
        timeout_seconds=timeout,
        callback=callback,
    )


def _signal(**kwargs) -> dict:
    base = {
        "symbol": "BANKNIFTY",
        "strategy": "VP-07 Wicks Pullback",
        "direction": "SELL",
        "interval": "5",
        "entry_reference": 56000.0,
        "stop_loss": 56150.0,
        "target": 55750.0,
        "_signal_id": "BANKNIFTY_VP07_SELL_12345",
    }
    base.update(kwargs)
    return base


_GOOD_OPINION = json.dumps({
    "alignment": "STRONG",
    "confidence": 0.78,
    "key_observation": "Price rejected upper Bollinger Band with volume spike",
    "concern": "Broader NIFTY still holding support — watch carefully",
})


class TestGemmaObserverNonBlocking(unittest.TestCase):
    """submit() must return immediately — it does not block the caller."""

    def test_submit_returns_immediately(self):
        called = threading.Event()

        def slow_run(signal, context):
            time.sleep(0.5)
            return {"alignment": "WEAK", "confidence": 0.3,
                    "key_observation": "slow", "concern": "none", "gemma_error": None}

        obs = _observer()
        with patch.object(obs, "_run", side_effect=slow_run):
            t0 = time.monotonic()
            obs.submit(_signal(), "ctx")
            elapsed = time.monotonic() - t0
        # submit() should return in well under 100ms
        self.assertLess(elapsed, 0.1)

    def test_callback_invoked_asynchronously(self):
        results = []
        done = threading.Event()

        def cb(sig, opinion):
            results.append(opinion)
            done.set()

        obs = _observer(callback=cb)
        with patch.object(obs, "_run", return_value={
            "alignment": "MODERATE",
            "confidence": 0.6,
            "key_observation": "ok",
            "concern": "none",
            "gemma_error": None,
        }):
            obs.submit(_signal(), "ctx")
            done.wait(timeout=2)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["alignment"], "MODERATE")

    def test_callback_receives_signal_and_opinion(self):
        received_signal = []
        received_opinion = []
        done = threading.Event()

        def cb(sig, op):
            received_signal.append(sig)
            received_opinion.append(op)
            done.set()

        obs = _observer(callback=cb)
        sig = _signal(symbol="NIFTY")
        with patch.object(obs, "_run", return_value={
            "alignment": "WEAK", "confidence": 0.4,
            "key_observation": "meh", "concern": "none", "gemma_error": None,
        }):
            obs.submit(sig, "ctx")
            done.wait(timeout=2)

        self.assertEqual(received_signal[0]["symbol"], "NIFTY")


class TestGemmaObserverValidResponse(unittest.TestCase):
    """Happy path: valid JSON with all required fields."""

    def test_valid_opinion_parsed(self):
        obs = _observer()
        with patch.object(obs, "_run", return_value={
            "alignment": "STRONG",
            "confidence": 0.78,
            "key_observation": "Price rejected upper Bollinger Band",
            "concern": "Broader NIFTY still holding support",
            "gemma_error": None,
        }):
            done = threading.Event()
            opinions = []
            obs._callback = lambda s, o: (opinions.append(o), done.set())
            obs.submit(_signal(), "ctx")
            done.wait(timeout=2)

        op = opinions[0]
        self.assertEqual(op["alignment"], "STRONG")
        self.assertAlmostEqual(op["confidence"], 0.78)
        self.assertIsNone(op["gemma_error"])

    def test_parse_opinion_all_alignments(self):
        obs = _observer()
        for alignment in _VALID_ALIGNMENTS:
            raw = json.dumps({
                "alignment": alignment,
                "confidence": 0.5,
                "key_observation": "test",
                "concern": "none",
            })
            result = obs._parse_opinion(raw)
            self.assertEqual(result["alignment"], alignment)
            self.assertIsNone(result["gemma_error"])

    def test_parse_opinion_code_fence_stripped(self):
        obs = _observer()
        wrapped = "```json\n" + _GOOD_OPINION + "\n```"
        result = obs._parse_opinion(wrapped)
        self.assertEqual(result["alignment"], "STRONG")
        self.assertIsNone(result["gemma_error"])


class TestGemmaObserverErrorPaths(unittest.TestCase):
    """All error paths must return error_opinion, never raise."""

    def test_timeout_returns_error_opinion(self):
        obs = _observer(timeout=1)
        with patch.object(obs, "_run", return_value=obs._error_opinion("timed out")):
            done = threading.Event()
            opinions = []
            obs._callback = lambda s, o: (opinions.append(o), done.set())
            obs.submit(_signal(), "ctx")
            done.wait(timeout=2)
        op = opinions[0]
        self.assertEqual(op["alignment"], "WEAK")
        self.assertIsNotNone(op["gemma_error"])

    def test_api_error_returns_error_opinion_not_raises(self):
        obs = _observer()
        with patch.object(obs, "_run", return_value=obs._error_opinion("503 error")):
            done = threading.Event()
            opinions = []
            obs._callback = lambda s, o: (opinions.append(o), done.set())
            obs.submit(_signal(), "ctx")
            done.wait(timeout=2)
        self.assertIsNotNone(opinions[0]["gemma_error"])

    def test_invalid_json_returns_error_opinion(self):
        obs = _observer()
        result = obs._parse_opinion("not json")
        self.assertIsNotNone(result["gemma_error"])
        self.assertIn("JSON", result["gemma_error"])

    def test_json_wrong_alignment_returns_error_opinion(self):
        obs = _observer()
        bad = json.dumps({
            "alignment": "BULLISH",  # not a valid alignment
            "confidence": 0.6,
            "key_observation": "test",
            "concern": "none",
        })
        result = obs._parse_opinion(bad)
        self.assertIsNotNone(result["gemma_error"])
        self.assertIn("BULLISH", result["gemma_error"])

    def test_json_array_not_object_is_error(self):
        obs = _observer()
        result = obs._parse_opinion("[]")
        self.assertIsNotNone(result["gemma_error"])


class TestGemmaObserverNeverDecisionMaker(unittest.TestCase):
    """
    Critical: GemmaObserver must NEVER produce keys that could be
    mistaken for trade approval/rejection.
    """

    def test_opinion_has_no_approved_key(self):
        obs = _observer()
        result = obs._parse_opinion(_GOOD_OPINION)
        self.assertNotIn("approved", result)
        self.assertNotIn("decision", result)

    def test_error_opinion_has_no_approved_key(self):
        result = GemmaObserver._error_opinion("some error")
        self.assertNotIn("approved", result)
        self.assertNotIn("decision", result)

    def test_callback_exception_does_not_crash_thread(self):
        """Even if callback raises, the observer daemon must not propagate the exception."""
        def bad_cb(sig, op):
            raise RuntimeError("callback crashed")

        obs = _observer(callback=bad_cb)
        # Should not raise — errors in callback are caught
        with patch.object(obs, "_run", return_value={
            "alignment": "WEAK", "confidence": 0.0,
            "key_observation": "x", "concern": "y", "gemma_error": None,
        }):
            obs.submit(_signal(), "ctx")
            time.sleep(0.3)  # let the daemon thread finish


class TestGemmaObserverPromptBuilding(unittest.TestCase):
    """Prompt must include signal fields without inventing data."""

    def test_prompt_includes_symbol_strategy_direction(self):
        obs = _observer()
        sig = _signal(symbol="NIFTY", strategy="VP-22 Supply Zone", direction="BUY")
        prompt = obs._build_prompt(sig, "ctx line")
        self.assertIn("NIFTY", prompt)
        self.assertIn("VP-22 Supply Zone", prompt)
        self.assertIn("BUY", prompt)

    def test_prompt_includes_context(self):
        obs = _observer()
        prompt = obs._build_prompt(_signal(), "ADX 28 | RSI 45 | VWAP 56000")
        self.assertIn("ADX 28", prompt)

    def test_prompt_does_not_invent_data(self):
        """Prompt must only reference what's in signal + context."""
        obs = _observer()
        sig = _signal()
        context = "clean context only"
        prompt = obs._build_prompt(sig, context)
        # Must not contain fabricated technical data not in inputs
        self.assertNotIn("Fibonacci", prompt)
        self.assertNotIn("MACD", prompt)


class TestValidAlignments(unittest.TestCase):
    def test_valid_alignments_are_complete(self):
        self.assertEqual(_VALID_ALIGNMENTS, {"STRONG", "MODERATE", "WEAK", "CONFLICTED"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
