"""
tests/test_gemma_observer.py
------------------------------
Unit tests for GemmaObserver — the async non-blocking local signal observer.

Architecture:
  - Uses Ollama HTTP API (NOT google-genai)
  - Default: DISABLED (VM resources insufficient)
  - Observer-only: no "approved"/"decision" fields ever

Invariants:
  - No google.genai import in tools/gemma_observer.py
  - submit() is always non-blocking
  - Any error/timeout/disabled → UNAVAILABLE or WEAK opinion, never raises
  - opinion dict never has "approved" or "decision" keys
"""
import json
import threading
import time
import unittest
from io import BytesIO
from unittest.mock import patch, MagicMock, call
import urllib.error
import socket

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.gemma_observer import GemmaObserver, _VALID_OPINIONS, _VALID_ALIGNMENTS


def _observer_enabled(**kwargs) -> GemmaObserver:
    defaults = dict(
        enabled=True, url="http://localhost:11434",
        model="gemma3:1b", timeout_seconds=3,
    )
    defaults.update(kwargs)
    return GemmaObserver(**defaults)


def _observer_disabled(**kwargs) -> GemmaObserver:
    defaults = dict(
        enabled=False, url="http://localhost:11434",
        model="gemma3:1b", timeout_seconds=3,
    )
    defaults.update(kwargs)
    return GemmaObserver(**defaults)


def _signal(**kwargs) -> dict:
    base = {
        "symbol": "BANKNIFTY", "strategy": "VP-07 Wicks Pullback",
        "direction": "SELL", "interval": "5",
        "entry_reference": 56000.0, "stop_loss": 56150.0, "target": 55750.0,
        "_signal_id": "BANKNIFTY_VP07_SELL_12345",
    }
    base.update(kwargs)
    return base


def _good_ollama_response(opinion="GOOD", confidence=0.78):
    """Build what Ollama's /api/generate returns."""
    inner = json.dumps({
        "opinion": opinion,
        "confidence": confidence,
        "reason": "Price rejected upper Bollinger Band with volume spike",
        "concerns": ["Broader NIFTY still holding support"],
        "hallucination_guardrail_ack": True,
    })
    return json.dumps({"model": "gemma3:1b", "response": inner}).encode("utf-8")


class TestNoGoogleGenaiImport(unittest.TestCase):
    """Critical: gemma_observer.py must not import google.genai."""

    def test_no_google_genai_in_source(self):
        src_path = os.path.join(os.path.dirname(__file__), "..", "tools", "gemma_observer.py")
        src = open(src_path).read()
        self.assertNotIn("google", src.lower(),
            "tools/gemma_observer.py must not contain 'google' — use Ollama HTTP only")
        self.assertNotIn("genai", src.lower(),
            "tools/gemma_observer.py must not reference 'genai'")


class TestGemmaDisabledMode(unittest.TestCase):
    """When disabled, submit() returns immediately with UNAVAILABLE opinion."""

    def test_disabled_returns_unavailable_immediately(self):
        opinions = []
        done = threading.Event()

        def cb(sig, op):
            opinions.append(op)
            done.set()

        obs = _observer_disabled(callback=cb)
        obs.submit(_signal(), "ctx")
        done.wait(timeout=1)

        self.assertEqual(len(opinions), 1)
        self.assertEqual(opinions[0]["alignment"], "UNAVAILABLE")
        self.assertIsNotNone(opinions[0]["gemma_error"])
        self.assertIn("disabled", opinions[0]["gemma_error"].lower())

    def test_disabled_makes_no_http_calls(self):
        with patch("urllib.request.urlopen") as mock_open:
            obs = _observer_disabled()
            obs.submit(_signal(), "ctx")
            time.sleep(0.1)
            mock_open.assert_not_called()

    def test_disabled_submit_returns_immediately(self):
        obs = _observer_disabled()
        t0 = time.monotonic()
        obs.submit(_signal(), "ctx")
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.05)


class TestGemmaEnabledHappyPath(unittest.TestCase):
    """When enabled and Ollama returns valid JSON."""

    def _mock_urlopen(self, resp_bytes: bytes):
        """Returns a context manager mock that reads resp_bytes."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_bytes
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def test_good_opinion_maps_to_strong(self):
        opinions = []
        done = threading.Event()

        def cb(sig, op):
            opinions.append(op)
            done.set()

        obs = _observer_enabled(callback=cb)
        with self._mock_urlopen(_good_ollama_response("GOOD", 0.78)):
            obs.submit(_signal(), "ctx")
            done.wait(timeout=3)

        self.assertEqual(opinions[0]["alignment"], "STRONG")
        self.assertAlmostEqual(opinions[0]["confidence"], 0.78)
        self.assertIsNone(opinions[0]["gemma_error"])

    def test_bad_opinion_maps_to_weak(self):
        opinions = []
        done = threading.Event()
        obs = _observer_enabled(callback=lambda s, o: (opinions.append(o), done.set()))
        with self._mock_urlopen(_good_ollama_response("BAD", 0.6)):
            obs.submit(_signal(), "ctx")
            done.wait(timeout=3)
        self.assertEqual(opinions[0]["alignment"], "WEAK")

    def test_unclear_opinion_maps_to_conflicted(self):
        opinions = []
        done = threading.Event()
        obs = _observer_enabled(callback=lambda s, o: (opinions.append(o), done.set()))
        with self._mock_urlopen(_good_ollama_response("UNCLEAR", 0.4)):
            obs.submit(_signal(), "ctx")
            done.wait(timeout=3)
        self.assertEqual(opinions[0]["alignment"], "CONFLICTED")

    def test_concerns_joined_into_concern_string(self):
        inner = json.dumps({
            "opinion": "BAD", "confidence": 0.7,
            "reason": "Momentum weakening",
            "concerns": ["RSI divergence", "EMA crossdown"],
            "hallucination_guardrail_ack": True,
        })
        resp = json.dumps({"response": inner}).encode()
        opinions = []
        done = threading.Event()
        obs = _observer_enabled(callback=lambda s, o: (opinions.append(o), done.set()))
        with self._mock_urlopen(resp):
            obs.submit(_signal(), "ctx")
            done.wait(timeout=3)
        self.assertIn("RSI divergence", opinions[0]["concern"])


class TestGemmaErrorPaths(unittest.TestCase):
    """All error paths must return error_opinion, never raise."""

    def _run_and_collect(self, obs, signal, ctx, timeout=3):
        opinions = []
        done = threading.Event()
        obs._callback = lambda s, o: (opinions.append(o), done.set())
        obs.submit(signal, ctx)
        done.wait(timeout=timeout)
        return opinions

    def test_connection_refused_returns_error_opinion(self):
        obs = _observer_enabled()
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
            ops = self._run_and_collect(obs, _signal(), "ctx")
        self.assertIsNotNone(ops[0]["gemma_error"])
        self.assertIn("connection", ops[0]["gemma_error"].lower())

    def test_url_error_returns_error_opinion(self):
        obs = _observer_enabled()
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Name or service not known")):
            ops = self._run_and_collect(obs, _signal(), "ctx")
        self.assertIsNotNone(ops[0]["gemma_error"])

    def test_socket_timeout_returns_error_opinion(self):
        obs = _observer_enabled(timeout_seconds=1)
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            ops = self._run_and_collect(obs, _signal(), "ctx")
        self.assertIsNotNone(ops[0]["gemma_error"])
        self.assertIn("timed out", ops[0]["gemma_error"].lower())

    def test_malformed_outer_json_returns_error(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        obs = _observer_enabled()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ops = self._run_and_collect(obs, _signal(), "ctx")
        self.assertIsNotNone(ops[0]["gemma_error"])

    def test_malformed_inner_json_returns_error(self):
        resp = json.dumps({"response": "not valid json {{"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        obs = _observer_enabled()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ops = self._run_and_collect(obs, _signal(), "ctx")
        self.assertIsNotNone(ops[0]["gemma_error"])

    def test_hallucination_ack_false_returns_error(self):
        inner = json.dumps({
            "opinion": "GOOD", "confidence": 0.8,
            "reason": "strong", "concerns": [],
            "hallucination_guardrail_ack": False,
        })
        resp = json.dumps({"response": inner}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        obs = _observer_enabled()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ops = self._run_and_collect(obs, _signal(), "ctx")
        self.assertIsNotNone(ops[0]["gemma_error"])
        self.assertIn("hallucination_guardrail_ack", ops[0]["gemma_error"])

    def test_hallucination_ack_missing_returns_error(self):
        inner = json.dumps({
            "opinion": "GOOD", "confidence": 0.8,
            "reason": "strong", "concerns": [],
            # missing hallucination_guardrail_ack
        })
        resp = json.dumps({"response": inner}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        obs = _observer_enabled()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ops = self._run_and_collect(obs, _signal(), "ctx")
        self.assertIsNotNone(ops[0]["gemma_error"])

    def test_invalid_opinion_value_returns_error(self):
        inner = json.dumps({
            "opinion": "BULLISH", "confidence": 0.8,
            "reason": "strong", "concerns": [],
            "hallucination_guardrail_ack": True,
        })
        resp = json.dumps({"response": inner}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        obs = _observer_enabled()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ops = self._run_and_collect(obs, _signal(), "ctx")
        self.assertIsNotNone(ops[0]["gemma_error"])
        self.assertIn("BULLISH", ops[0]["gemma_error"])

    def test_confidence_out_of_range_returns_error(self):
        obs = _observer_enabled()
        result = obs._parse_opinion(json.dumps({
            "opinion": "GOOD", "confidence": 1.5,
            "reason": "ok", "concerns": [],
            "hallucination_guardrail_ack": True,
        }))
        self.assertIsNotNone(result["gemma_error"])

    def test_confidence_as_string_returns_error(self):
        obs = _observer_enabled()
        result = obs._parse_opinion(json.dumps({
            "opinion": "GOOD", "confidence": "high",
            "reason": "ok", "concerns": [],
            "hallucination_guardrail_ack": True,
        }))
        self.assertIsNotNone(result["gemma_error"])


class TestGemmaInvariantsNeverDecisionMaker(unittest.TestCase):
    """GemmaObserver must NEVER produce 'approved' or 'decision' keys."""

    def test_parsed_opinion_has_no_approved_key(self):
        obs = _observer_enabled()
        inner = json.dumps({
            "opinion": "GOOD", "confidence": 0.8,
            "reason": "ok", "concerns": [],
            "hallucination_guardrail_ack": True,
        })
        result = obs._parse_opinion(inner)
        self.assertNotIn("approved", result)
        self.assertNotIn("decision", result)

    def test_error_opinion_has_no_approved_key(self):
        result = GemmaObserver._error_opinion("some error")
        self.assertNotIn("approved", result)
        self.assertNotIn("decision", result)

    def test_unavailable_opinion_has_no_approved_key(self):
        result = GemmaObserver._unavailable_opinion("disabled")
        self.assertNotIn("approved", result)
        self.assertNotIn("decision", result)
        self.assertEqual(result["alignment"], "UNAVAILABLE")

    def test_callback_exception_does_not_crash(self):
        def bad_cb(sig, op):
            raise RuntimeError("crash")
        obs = _observer_enabled(callback=bad_cb)
        with patch.object(obs, "_run", return_value={
            "alignment": "WEAK", "confidence": 0.0,
            "key_observation": "x", "concern": "y", "gemma_error": None,
        }):
            obs.submit(_signal(), "ctx")
            time.sleep(0.3)


class TestGemmaPromptBuilding(unittest.TestCase):
    def test_prompt_includes_signal_fields(self):
        obs = _observer_enabled()
        prompt = obs._build_prompt(_signal(symbol="NIFTY"), "ADX 28")
        self.assertIn("NIFTY", prompt)
        self.assertIn("ADX 28", prompt)

    def test_prompt_does_not_invent_data(self):
        obs = _observer_enabled()
        prompt = obs._build_prompt(_signal(), "clean context only")
        self.assertNotIn("Fibonacci", prompt)
        self.assertNotIn("MACD", prompt)


class TestValidConstants(unittest.TestCase):
    def test_valid_opinions(self):
        self.assertEqual(_VALID_OPINIONS, {"GOOD", "BAD", "UNCLEAR"})

    def test_valid_alignments_includes_unavailable(self):
        self.assertIn("UNAVAILABLE", _VALID_ALIGNMENTS)
        self.assertIn("STRONG", _VALID_ALIGNMENTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
