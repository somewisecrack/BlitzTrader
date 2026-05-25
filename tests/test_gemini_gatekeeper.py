"""
tests/test_gemini_gatekeeper.py
---------------------------------
Unit tests for GeminiGatekeeper — the live entry gatekeeper.

Invariants under test:
  - Timeout → REJECT (never approve on slow response)
  - API error → REJECT
  - Valid JSON APPROVE → approved=True with all fields
  - Valid JSON REJECT → approved=False
  - Invalid JSON → REJECT (auto)
  - Missing required field → REJECT (auto)
  - Invalid decision value → REJECT (auto)
  - Code-fence-wrapped JSON is parsed correctly
  - _build_prompt includes signal fields
  - evaluate() result always has required keys
"""
import json
import queue
import threading
import unittest
from unittest.mock import MagicMock, patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.gemini_gatekeeper import GeminiGatekeeper, _REQUIRED_FIELDS, _VALID_DECISIONS


def _gatekeeper(timeout: int = 5) -> GeminiGatekeeper:
    return GeminiGatekeeper(api_key="test-key", model="gemini-2.5-flash-lite", timeout_seconds=timeout)


def _signal(**kwargs) -> dict:
    base = {
        "symbol": "NIFTY",
        "strategy": "VP-05 3EMA Trend",
        "direction": "BUY",
        "interval": "3",
        "entry_reference": 24000.0,
        "stop_loss": 23900.0,
        "target": 24200.0,
    }
    base.update(kwargs)
    return base


_GOOD_APPROVE = json.dumps({
    "decision": "APPROVE",
    "confidence": 0.82,
    "must_not_override_python_guardrails": True,
    "reason": "EMA stack bullish, ADX strong",
    "risk_notes": "Market opening volatility possible",
    "conditions_checked": ["EMA stacked bull", "ADX > 25", "RSI not overbought"],
})

_GOOD_REJECT = json.dumps({
    "decision": "REJECT",
    "confidence": 0.73,
    "reason": "RSI divergence — momentum weakening",
    "risk_notes": "Potential exhaustion candle",
    "conditions_checked": ["RSI < 50", "EMA not fully aligned"],
    "must_not_override_python_guardrails": True,
})


class TestGatekeeperApprove(unittest.TestCase):
    """Happy-path: Gemini responds with a valid APPROVE JSON."""

    def test_approve_returns_approved_true(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=_GOOD_APPROVE):
            result = gk.evaluate(_signal(), "NIFTY 3m | price ₹24000")
        self.assertTrue(result["approved"])
        self.assertEqual(result["decision"], "APPROVE")
        self.assertAlmostEqual(result["confidence"], 0.82)
        self.assertIn("EMA stack bullish", result["reason"])
        self.assertIsNone(result["gatekeeper_error"])

    def test_approve_conditions_checked_passed_through(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=_GOOD_APPROVE):
            result = gk.evaluate(_signal(), "context")
        self.assertIn("EMA stacked bull", result["conditions_checked"])
        self.assertEqual(len(result["conditions_checked"]), 3)

    def test_result_always_has_all_required_keys(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=_GOOD_APPROVE):
            result = gk.evaluate(_signal(), "context")
        for key in ("decision", "approved", "confidence", "reason", "risk_notes",
                    "conditions_checked", "gatekeeper_error", "elapsed_seconds"):
            self.assertIn(key, result, f"Missing key: {key}")


class TestGatekeeperReject(unittest.TestCase):
    """Gemini says REJECT — must propagate correctly."""

    def test_reject_returns_approved_false(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=_GOOD_REJECT):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])
        self.assertEqual(result["decision"], "REJECT")
        self.assertIsNone(result["gatekeeper_error"])


class TestGatekeeperTimeout(unittest.TestCase):
    """Timeout path — must auto-REJECT, never raise."""

    def test_timeout_returns_reject_not_raises(self):
        gk = _gatekeeper(timeout=1)
        with patch.object(gk, "_call_with_timeout", side_effect=TimeoutError("timed out after 1s")):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])
        self.assertEqual(result["decision"], "REJECT")
        self.assertIsNotNone(result["gatekeeper_error"])
        self.assertIn("timed out", result["gatekeeper_error"].lower())

    def test_timeout_error_message_included(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", side_effect=TimeoutError("5s")):
            result = gk.evaluate(_signal(), "context")
        # error field mentions timeout (exact elapsed time varies in mocked context)
        self.assertIn("timed out", result["gatekeeper_error"].lower())


class TestGatekeeperAPIError(unittest.TestCase):
    """API error (503, network, quota) — must auto-REJECT."""

    def test_503_error_auto_rejects(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", side_effect=RuntimeError("503 Service Unavailable")):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])
        self.assertIn("RuntimeError", result["gatekeeper_error"])

    def test_quota_error_auto_rejects(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", side_effect=Exception("429 RESOURCE_EXHAUSTED quota")):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])

    def test_network_error_auto_rejects(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", side_effect=ConnectionError("Connection refused")):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])


class TestGatekeeperBadJSON(unittest.TestCase):
    """Malformed / incomplete JSON responses — must all auto-REJECT."""

    def test_completely_invalid_json(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value="not json at all"):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])
        self.assertIsNotNone(result["gatekeeper_error"])

    def test_json_missing_decision_field(self):
        bad = json.dumps({
            "confidence": 0.8,
            "reason": "looks good",
            "risk_notes": "none",
            "conditions_checked": [],
            "must_not_override_python_guardrails": True,
        })
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=bad):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])
        self.assertIn("decision", result["gatekeeper_error"])

    def test_json_missing_confidence_field(self):
        bad = json.dumps({
            "decision": "APPROVE",
            "reason": "ok",
            "risk_notes": "none",
            "conditions_checked": [],
            "must_not_override_python_guardrails": True,
        })
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=bad):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])

    def test_invalid_decision_value(self):
        bad = json.dumps({
            "decision": "YES",
            "confidence": 0.9,
            "reason": "ok",
            "risk_notes": "none",
            "conditions_checked": [],
            "must_not_override_python_guardrails": True,
        })
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=bad):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])
        self.assertIn("YES", result["gatekeeper_error"])

    def test_empty_string_response(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=""):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])

    def test_code_fence_wrapped_json_is_parsed(self):
        """Gemini sometimes wraps JSON in ```json ... ``` — must still parse."""
        wrapped = "```json\n" + _GOOD_APPROVE + "\n```"
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=wrapped):
            result = gk.evaluate(_signal(), "context")
        self.assertTrue(result["approved"])

    def test_json_array_not_object(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value="[]"):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])


class TestGatekeeperPromptBuilding(unittest.TestCase):
    """Verify the prompt includes signal fields."""

    def test_prompt_includes_symbol_strategy_direction(self):
        gk = _gatekeeper()
        sig = _signal(symbol="BANKNIFTY", strategy="VP-07 Wicks Pullback", direction="SELL")
        prompt = gk._build_prompt(sig, "some context")
        self.assertIn("BANKNIFTY", prompt)
        self.assertIn("VP-07 Wicks Pullback", prompt)
        self.assertIn("SELL", prompt)

    def test_prompt_includes_stop_loss_and_target(self):
        gk = _gatekeeper()
        sig = _signal(stop_loss=23850.0, target=24300.0)
        prompt = gk._build_prompt(sig, "ctx")
        self.assertIn("23850", prompt)
        self.assertIn("24300", prompt)

    def test_prompt_includes_context(self):
        gk = _gatekeeper()
        context = "ADX 29 | RSI 42 | EMA stack bearish"
        prompt = gk._build_prompt(_signal(), context)
        self.assertIn("ADX 29", prompt)

    def test_prompt_contains_json_only_instruction(self):
        gk = _gatekeeper()
        prompt = gk._build_prompt(_signal(), "ctx")
        self.assertIn("JSON only", prompt)


class TestGatekeeperInvariantsNeverApproveOnError(unittest.TestCase):
    """Multiple edge cases that must never accidentally approve a trade."""

    def test_none_response_auto_rejects(self):
        """Patching _call_with_timeout to return None simulates SDK returning nothing."""
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=None):
            # _parse_response will get None which fails json.loads
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])

    def test_whitespace_only_response_rejects(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value="   \n  "):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])

    def test_html_response_rejects(self):
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value="<html><body>Error</body></html>"):
            result = gk.evaluate(_signal(), "context")
        self.assertFalse(result["approved"])

    def test_lowercase_approve_normalised(self):
        """decision is upper()-cased before validation; 'approve' → 'APPROVE' → valid."""
        bad = json.dumps({
            "decision": "approve",
            "confidence": 0.9,
            "reason": "ok",
            "risk_notes": "none",
            "conditions_checked": [],
            "must_not_override_python_guardrails": True,
        })
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=bad):
            result = gk.evaluate(_signal(), "context")
        # "approve".upper() == "APPROVE" → valid
        self.assertTrue(result["approved"])

    def test_mixed_case_approve_normalised(self):
        """decision is upper()-cased before validation."""
        mixed = json.dumps({
            "decision": "Approve",
            "confidence": 0.75,
            "reason": "ok",
            "risk_notes": "none",
            "conditions_checked": ["one"],
            "must_not_override_python_guardrails": True,
        })
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=mixed):
            result = gk.evaluate(_signal(), "context")
        self.assertTrue(result["approved"])


class TestGatekeeperRequiredFields(unittest.TestCase):
    """Verify _REQUIRED_FIELDS matches spec."""

    def test_required_fields_are_complete(self):
        expected = {
            "decision", "confidence", "reason", "risk_notes",
            "conditions_checked", "must_not_override_python_guardrails",
        }
        self.assertEqual(_REQUIRED_FIELDS, expected)

    def test_valid_decisions_are_approve_reject(self):
        self.assertEqual(_VALID_DECISIONS, {"APPROVE", "REJECT"})


class TestGatekeeperHardening(unittest.TestCase):
    """New validation rules: ack field, confidence range, forbidden fields."""

    def _approve_base(self, **overrides) -> dict:
        base = {
            "decision": "APPROVE",
            "confidence": 0.8,
            "reason": "strong setup",
            "risk_notes": "watch news",
            "conditions_checked": ["EMA aligned"],
            "must_not_override_python_guardrails": True,
        }
        base.update(overrides)
        return base

    def test_missing_ack_field_rejects(self):
        bad = self._approve_base()
        del bad["must_not_override_python_guardrails"]
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])
        self.assertIn("must_not_override_python_guardrails", result["gatekeeper_error"])

    def test_ack_false_rejects(self):
        bad = self._approve_base(must_not_override_python_guardrails=False)
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])

    def test_ack_string_true_rejects(self):
        """Ack must be boolean true, not string 'true'."""
        bad = self._approve_base(must_not_override_python_guardrails="true")
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])

    def test_confidence_too_high_rejects(self):
        bad = self._approve_base(confidence=1.5)
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])
        self.assertIn("1.5", result["gatekeeper_error"])

    def test_confidence_negative_rejects(self):
        bad = self._approve_base(confidence=-0.1)
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])

    def test_confidence_string_rejects(self):
        bad = self._approve_base(confidence="high")
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])
        self.assertIn("str", result["gatekeeper_error"])

    def test_confidence_exactly_zero_passes(self):
        """confidence=0.0 is valid (REJECT with zero confidence is allowed)."""
        bad = self._approve_base(confidence=0.0, decision="REJECT")
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])
        self.assertIsNone(result["gatekeeper_error"])

    def test_confidence_exactly_one_passes(self):
        bad = self._approve_base(confidence=1.0)
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertTrue(result["approved"])

    def test_forbidden_stop_loss_field_rejects(self):
        """If Gemini tries to suggest a stop_loss override, auto-reject."""
        bad = self._approve_base(stop_loss=23800.0)
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])
        self.assertIn("stop_loss", result["gatekeeper_error"])

    def test_forbidden_quantity_field_rejects(self):
        bad = self._approve_base(quantity=2)
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])
        self.assertIn("quantity", result["gatekeeper_error"])

    def test_forbidden_target_field_rejects(self):
        bad = self._approve_base(target=24500.0)
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])

    def test_forbidden_leverage_field_rejects(self):
        bad = self._approve_base(leverage=2)
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])

    def test_conditions_checked_not_list_rejects(self):
        bad = self._approve_base(conditions_checked="EMA aligned, ADX strong")
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(bad)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertFalse(result["approved"])

    def test_risk_notes_as_list_is_valid(self):
        """risk_notes can be a list of strings."""
        ok = self._approve_base(risk_notes=["sudden reversal", "news risk"])
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(ok)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertTrue(result["approved"])

    def test_system_prompt_contains_guardrail_instructions(self):
        """Verify system prompt explicitly forbids position-size/SL/target override."""
        from tools.gemini_gatekeeper import _GATEKEEPER_SYSTEM_PROMPT
        self.assertIn("must_not_override_python_guardrails", _GATEKEEPER_SYSTEM_PROMPT)
        self.assertIn("Position size", _GATEKEEPER_SYSTEM_PROMPT)
        self.assertIn("Stop-loss", _GATEKEEPER_SYSTEM_PROMPT)

    def test_gatekeeper_entry_flow_test_uses_new_ack_field(self):
        """Smoke test: a complete valid APPROVE round-trip with new schema."""
        full = {
            "decision": "APPROVE",
            "confidence": 0.87,
            "reason": "Clean EMA stack with strong ADX",
            "risk_notes": "Pre-news caution",
            "conditions_checked": ["EMA bull stack", "ADX > 25", "RSI not overbought"],
            "must_not_override_python_guardrails": True,
        }
        gk = _gatekeeper()
        with patch.object(gk, "_call_with_timeout", return_value=json.dumps(full)):
            result = gk.evaluate(_signal(), "ctx")
        self.assertTrue(result["approved"])
        self.assertAlmostEqual(result["confidence"], 0.87)
        self.assertIsNone(result["gatekeeper_error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
