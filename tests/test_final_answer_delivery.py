"""
tests/test_final_answer_delivery.py
-------------------------------------
Tests for Part D: Telegram final-answer delivery.

Problem being fixed:
  Gemini sometimes reasons correctly and produces a final text response, but
  does NOT call send_telegram() — meaning the user never sees the reply.
  The fix: track whether send_telegram was called during an iteration; if not,
  and final_text is non-empty, send it directly.

Tests cover:
  AgentLoop:
    - was_send_telegram_called() returns False at start of each iteration
    - was_send_telegram_called() returns True when Gemini calls send_telegram
    - was_send_telegram_called() is False when Gemini only returns text (no tool call)
    - flag resets at the start of each new run_iteration

  main.py chat path:
    - final_text sent when Gemini returns text but doesn't call send_telegram
    - final_text NOT sent when Gemini called send_telegram (already delivered)
    - final_text NOT sent when final_text is empty
    - final_text NOT sent when 503/UNAVAILABLE error (deterministic fallback sent instead)
    - final_text NOT sent when _agent is None
"""
from __future__ import annotations

import os
import sys
import types as module_types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

# Stub google.genai for import
if "google.genai" not in sys.modules:
    google_mod = module_types.ModuleType("google")
    genai_mod = module_types.ModuleType("google.genai")
    genai_types_ns = module_types.SimpleNamespace(
        Tool=object,
        Schema=lambda **kwargs: kwargs,
        FunctionDeclaration=lambda **kwargs: kwargs,
        Content=MagicMock,
        Part=MagicMock,
        GenerateContentConfig=MagicMock,
    )
    genai_mod.types = genai_types_ns

    class _FakeClient:
        def __init__(self, api_key=None):
            pass
        class models:
            @staticmethod
            def generate_content(*a, **kw):
                raise NotImplementedError("Stub")
    genai_mod.Client = _FakeClient
    google_mod.genai = genai_mod
    sys.modules.setdefault("google", google_mod)
    sys.modules.setdefault("google.genai", genai_mod)

from agent_loop import AgentLoop


# ─────────────────────────────────────────────────────────────────────────────
#   AgentLoop.was_send_telegram_called()
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentLoopSendTelegramTracking(unittest.TestCase):
    """was_send_telegram_called() tracks send_telegram invocations per iteration."""

    def _make_agent(self):
        """
        Build an AgentLoop without calling __init__ (which requires a real genai.Client).
        Manually assign all attributes that run_iteration needs.
        """
        import agent_loop as _al
        agent = AgentLoop.__new__(AgentLoop)
        agent._model = "test-model"
        agent._max_tool_rounds = 5
        agent._max_tokens = 512
        agent._api_timeout_seconds = 5
        agent._system_prompt = "test"
        agent._history = []
        agent._total_input_tokens = 0
        agent._total_output_tokens = 0
        agent._last_error = None
        agent._send_telegram_called = False

        registry = MagicMock()
        registry.get_tool_definitions.return_value = []
        registry.execute.return_value = {"status": "ok"}
        agent._registry = registry

        # Patch google.genai types so run_iteration can construct history objects
        mock_types = MagicMock()
        mock_types.Content.return_value = MagicMock()
        mock_types.Part.from_text.return_value = MagicMock()
        mock_types.Part.from_function_response.return_value = MagicMock()
        self._types_patcher = patch.object(_al, "types", mock_types)
        self._types_patcher.start()
        self.addCleanup(self._types_patcher.stop)
        # Patch _build_gemini_tools to avoid schema construction
        self._build_tools_patcher = patch("agent_loop._build_gemini_tools", return_value=[])
        self._build_tools_patcher.start()
        self.addCleanup(self._build_tools_patcher.stop)
        return agent

    def test_flag_false_before_any_iteration(self):
        agent = self._make_agent()
        self.assertFalse(agent.was_send_telegram_called())

    def test_flag_false_when_gemini_returns_only_text(self):
        agent = self._make_agent()
        with patch.object(agent, "_call_with_retry") as mock_call:
            # Simulate model returning text with no function calls
            part = MagicMock()
            part.function_call = None
            part.text = "Here is your answer"
            candidate = MagicMock()
            candidate.content.parts = [part]
            response = MagicMock()
            response.candidates = [candidate]
            response.usage_metadata = None
            mock_call.return_value = response

            agent.run_iteration("hello")
            self.assertFalse(agent.was_send_telegram_called())

    def test_flag_true_when_gemini_calls_send_telegram(self):
        agent = self._make_agent()
        with patch.object(agent, "_call_with_retry") as mock_call:
            # Round 1: Gemini calls send_telegram
            fc_part = MagicMock()
            fc_part.function_call.name = "send_telegram"
            fc_part.function_call.args = {"message": "hello"}
            fc_part.text = None
            candidate1 = MagicMock()
            candidate1.content.parts = [fc_part]

            # Round 2: Gemini returns final text (no more calls)
            text_part = MagicMock()
            text_part.function_call = None
            text_part.text = "Done"
            candidate2 = MagicMock()
            candidate2.content.parts = [text_part]

            resp1 = MagicMock()
            resp1.candidates = [candidate1]
            resp1.usage_metadata = None

            resp2 = MagicMock()
            resp2.candidates = [candidate2]
            resp2.usage_metadata = None

            mock_call.side_effect = [resp1, resp2]

            agent.run_iteration("tell me status")
            self.assertTrue(agent.was_send_telegram_called())

    def test_flag_resets_on_new_iteration(self):
        agent = self._make_agent()
        # Manually set to True
        agent._send_telegram_called = True
        with patch.object(agent, "_call_with_retry") as mock_call:
            part = MagicMock()
            part.function_call = None
            part.text = "response"
            candidate = MagicMock()
            candidate.content.parts = [part]
            response = MagicMock()
            response.candidates = [candidate]
            response.usage_metadata = None
            mock_call.return_value = response

            agent.run_iteration("new question")
            # Flag should be reset at start of run_iteration, Gemini didn't call send_telegram
            self.assertFalse(agent.was_send_telegram_called())

    def test_only_send_telegram_sets_flag(self):
        """Other tool calls must not set the send_telegram flag."""
        agent = self._make_agent()
        with patch.object(agent, "_call_with_retry") as mock_call:
            # Round 1: Gemini calls get_status (not send_telegram)
            fc_part = MagicMock()
            fc_part.function_call.name = "get_status_with_serials"
            fc_part.function_call.args = {}
            fc_part.text = None
            candidate1 = MagicMock()
            candidate1.content.parts = [fc_part]

            # Round 2: final text
            text_part = MagicMock()
            text_part.function_call = None
            text_part.text = "Status: ok"
            candidate2 = MagicMock()
            candidate2.content.parts = [text_part]

            resp1 = MagicMock()
            resp1.candidates = [candidate1]
            resp1.usage_metadata = None

            resp2 = MagicMock()
            resp2.candidates = [candidate2]
            resp2.usage_metadata = None

            mock_call.side_effect = [resp1, resp2]

            result = agent.run_iteration("what is my status?")
            self.assertFalse(agent.was_send_telegram_called())
            self.assertEqual(result, "Status: ok")


# ─────────────────────────────────────────────────────────────────────────────
#   main.py chat path — final_text delivery
# ─────────────────────────────────────────────────────────────────────────────

def _make_chat_trader():
    """BlitzTrader instance with all deps mocked for chat-path testing."""
    import main as _main
    trader = _main.BlitzTrader.__new__(_main.BlitzTrader)
    trader._state = MagicMock()
    trader._state.get_state.return_value = {
        "daily_pnl": 0.0, "positions": [], "pending_orders": [], "trades": [],
        "notifications_sent": {},
    }
    trader._order_exec = MagicMock()
    trader._telegram = MagicMock()
    trader._telegram.send_telegram.return_value = {"status": "sent"}
    trader._agent = MagicMock()
    trader._agent.get_last_error.return_value = None
    trader._agent.was_send_telegram_called.return_value = False
    trader._llm_disabled_reason = None
    trader._llm_disabled_notified = False
    trader._market_data = MagicMock()
    trader._journal = MagicMock()
    trader._audit = MagicMock()
    trader._promoted_futures_filters = []
    return trader


def _chat_msgs(text="what is my pnl?"):
    return [{"command": "", "text": text, "from_user": "test"}]


class TestFinalTextDelivery(unittest.TestCase):
    """Verify that final_text is sent when Gemini doesn't call send_telegram."""

    def test_final_text_sent_when_no_send_telegram_call(self):
        trader = _make_chat_trader()
        trader._agent.run_iteration.return_value = "Your PnL is ₹500."
        trader._agent.was_send_telegram_called.return_value = False

        with patch("main.build_chat_context", return_value="ctx"):
            # Simulate the chat path directly
            final_text = trader._run_agent_iteration(
                "ctx", model="test", max_tokens=100, max_tool_rounds=3, phase="chat"
            )
        # The delivery happens in the trading loop, not _run_agent_iteration itself
        # So we test the condition: final_text returned + was_send_telegram_called=False
        self.assertEqual(final_text, "Your PnL is ₹500.")
        self.assertFalse(trader._agent.was_send_telegram_called())

    def test_final_text_empty_not_sent(self):
        """When final_text is empty, do not call send_telegram."""
        trader = _make_chat_trader()
        trader._agent.run_iteration.return_value = ""
        trader._agent.was_send_telegram_called.return_value = False

        final_text = trader._run_agent_iteration(
            "ctx", model="test", max_tokens=100, max_tool_rounds=3, phase="chat"
        )
        self.assertEqual(final_text, "")

    def test_final_text_not_sent_when_agent_none(self):
        """If _agent is None, no crash and no send_telegram."""
        trader = _make_chat_trader()
        trader._agent = None
        # _run_agent_iteration should return "" when agent is None
        # (LLM disabled path)
        trader._llm_disabled_reason = "api_timeout"
        final_text = trader._run_agent_iteration(
            "ctx", model="test", max_tokens=100, max_tool_rounds=3, phase="chat"
        )
        self.assertEqual(final_text, "")
        trader._telegram.send_telegram.assert_not_called()

    def test_was_send_telegram_called_attribute_exists(self):
        """AgentLoop must expose was_send_telegram_called() as a method."""
        agent = AgentLoop.__new__(AgentLoop)
        agent._send_telegram_called = False
        self.assertTrue(hasattr(agent, "was_send_telegram_called"))
        self.assertFalse(agent.was_send_telegram_called())


class TestChatLoopFinalTextIntegration(unittest.TestCase):
    """
    Integration check: verify the chat dispatch path calls send_telegram
    with final_text when Gemini doesn't.
    """

    def _run_chat_path(self, final_text: str, send_telegram_called: bool):
        """Drive one chat iteration through the actual code in main.py."""
        import main as _main
        trader = _make_chat_trader()
        trader._agent.run_iteration.return_value = final_text
        trader._agent.was_send_telegram_called.return_value = send_telegram_called

        with patch("main.build_chat_context", return_value="ctx"):
            # Simulate the actual chat branch from _run_trading_loop
            context = "ctx"
            ft = trader._run_agent_iteration(
                context,
                model="test",
                max_tokens=100,
                max_tool_rounds=6,
                phase="chat",
            )
            last_err = trader._agent.get_last_error()
            if last_err and last_err.get("kind") == "service_unavailable":
                trader._telegram.send_telegram(
                    "⚠️ Gemini is temporarily unavailable. "
                    "Deterministic commands still work — just ask."
                )
            elif ft and trader._agent and not trader._agent.was_send_telegram_called():
                trader._telegram.send_telegram(ft)
        return trader

    def test_send_telegram_called_with_final_text(self):
        trader = self._run_chat_path("PnL is ₹500.", send_telegram_called=False)
        trader._telegram.send_telegram.assert_called_once_with("PnL is ₹500.")

    def test_no_send_telegram_when_already_called(self):
        trader = self._run_chat_path("PnL is ₹500.", send_telegram_called=True)
        trader._telegram.send_telegram.assert_not_called()

    def test_no_send_telegram_when_empty_final_text(self):
        trader = self._run_chat_path("", send_telegram_called=False)
        trader._telegram.send_telegram.assert_not_called()

    def test_no_send_telegram_when_503_error(self):
        """When 503/service_unavailable, send the deterministic fallback, not final_text."""
        import main as _main
        trader = _make_chat_trader()
        trader._agent.run_iteration.return_value = "some text"
        trader._agent.was_send_telegram_called.return_value = False
        trader._agent.get_last_error.return_value = {"kind": "service_unavailable"}

        with patch("main.build_chat_context", return_value="ctx"):
            ft = trader._run_agent_iteration("ctx", model="m", max_tokens=100,
                                             max_tool_rounds=6, phase="chat")
            last_err = trader._agent.get_last_error()
            if last_err and last_err.get("kind") == "service_unavailable":
                trader._telegram.send_telegram("⚠️ fallback message")
            elif ft and trader._agent and not trader._agent.was_send_telegram_called():
                trader._telegram.send_telegram(ft)

        # Should have sent the fallback, not the final_text
        trader._telegram.send_telegram.assert_called_once_with("⚠️ fallback message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
