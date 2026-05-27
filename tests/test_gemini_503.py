"""
tests/test_gemini_503.py
------------------------
Tests for Gemini 503/UNAVAILABLE resilience in agent_loop.py.

Covers:
  - Gemini 503 → _call_with_retry returns None, last_error kind = "service_unavailable"
  - Gemini DeadlineExceeded → same treatment
  - Deterministic pnl/status command does not call Gemini at all
"""
import sys
import time
import types as module_types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── Stub google.genai so agent_loop.py can be imported without the SDK ────────
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


# ---------------------------------------------------------------------------
# Minimal stubs so AgentLoop can be imported without real Gemini credentials
# ---------------------------------------------------------------------------

class _FakeRegistryStub:
    def get_tool_definitions(self):
        return []
    def execute(self, tool_name, tool_input):
        return {}


# ---------------------------------------------------------------------------
# Test: 503 UNAVAILABLE  →  _call_with_retry returns None
# ---------------------------------------------------------------------------

class TestGemini503Resilience:

    def _make_agent(self):
        from agent_loop import AgentLoop
        from unittest.mock import MagicMock as _MM
        import google.genai as _genai

        # Ensure genai.Client exists in the stub (other tests may have set a
        # minimal stub without it)
        if not hasattr(_genai, "Client"):
            _genai.Client = _MM

        with patch("agent_loop.genai.Client", return_value=MagicMock()):
            agent = AgentLoop(
                api_key="FAKE_KEY",
                model="gemini-fake",
                tool_registry=_FakeRegistryStub(),
                system_prompt="test",
                max_tokens=128,
                api_timeout_seconds=5,
            )
        return agent

    def _call_with_fake_error(self, err, max_retries=1):
        """Helper: call _call_with_retry on a fresh agent, injecting a fake error."""
        agent = self._make_agent()
        import agent_loop as _al

        # agent_loop imports `types` from google.genai at module level.
        # If a prior test file loaded a minimal google.genai stub that omits
        # GenerateContentConfig, _call_with_retry will crash before it even
        # reaches the patched _generate_content_with_timeout.  Ensure the
        # attribute exists on the already-imported types namespace.
        if not hasattr(_al.types, "GenerateContentConfig"):
            _al.types.GenerateContentConfig = MagicMock()

        with patch.object(agent, "_generate_content_with_timeout", side_effect=err):
            result = agent._call_with_retry(
                [], model="gemini-fake", max_tokens=128, max_retries=max_retries
            )
        return result, agent.get_last_error()

    def test_503_error_returns_none_and_sets_last_error(self):
        """Gemini 503 UNAVAILABLE → _call_with_retry returns None, last_error is set."""
        class FakeServiceUnavailable(Exception):
            pass

        err = FakeServiceUnavailable("503 UNAVAILABLE: The service is unavailable")
        result, last_err = self._call_with_fake_error(err)

        assert result is None
        assert last_err is not None
        assert last_err.get("kind") == "service_unavailable"

    def test_unavailable_in_message_triggers_503_path(self):
        """Exception message containing 'UNAVAILABLE' → service_unavailable error kind."""
        err = RuntimeError("gRPC status UNAVAILABLE: backend unreachable")
        result, last_err = self._call_with_fake_error(err)

        assert result is None
        assert last_err["kind"] == "service_unavailable"

    def test_deadline_exceeded_triggers_503_path(self):
        """Exception class named 'DeadlineExceeded' → service_unavailable error kind."""
        class DeadlineExceeded(Exception):
            pass

        err = DeadlineExceeded("Deadline exceeded for Gemini call")
        result, last_err = self._call_with_fake_error(err)

        assert result is None
        assert last_err["kind"] == "service_unavailable"

    def test_503_in_message_triggers_503_path(self):
        """Exception message containing '503' → service_unavailable error kind."""
        err = Exception("HTTP error: 503 Service Unavailable from endpoint")
        result, last_err = self._call_with_fake_error(err)

        assert result is None
        assert last_err["kind"] == "service_unavailable"

    def test_normal_api_error_does_not_trigger_503_path(self):
        """A generic error (not 503/UNAVAILABLE) should NOT produce service_unavailable."""
        err = ValueError("Unexpected model error: malformed JSON response")
        result, last_err = self._call_with_fake_error(err)

        assert result is None
        # Should NOT be service_unavailable for a generic error
        assert last_err is None or last_err.get("kind") != "service_unavailable"


# ---------------------------------------------------------------------------
# Test: deterministic pnl/status commands do NOT call Gemini
# ---------------------------------------------------------------------------

class TestDeterministicCommandsBypassGemini:
    """
    Verify that pnl / status / positions queries are resolved by
    _try_answer_simple_chat without ever calling _run_agent_iteration.
    """

    def _make_state_manager(self, pnl=1234.56, trade_count=3):
        sm = MagicMock()
        sm.get_state.return_value = {
            "daily_pnl": pnl,
            "daily_pnl_pct": 1.23,
            "virtual_capital": 100000.0,
            "available_balance": 90000.0,
            "margin_used": 10000.0,
            "positions": [],
            "trade_count": trade_count,
            "notifications_sent": {},
        }
        sm.get_daily_pnl.return_value = (pnl, 1.23)
        return sm

    def _make_order_exec(self):
        oe = MagicMock()
        oe.get_open_positions.return_value = {"positions": [], "count": 0}
        return oe

    def test_status_query_does_not_call_gemini(self):
        """
        A 'pnl' / 'status' message is handled by _try_answer_simple_chat
        and must NOT trigger _run_agent_iteration.
        """
        import main as _main

        trader = _main.BlitzTrader.__new__(_main.BlitzTrader)
        trader._state = self._make_state_manager()
        trader._order_exec = self._make_order_exec()
        trader._telegram = MagicMock()
        trader._telegram.send_telegram.return_value = {"status": "sent"}
        trader._feed = MagicMock()
        trader._shoonya = MagicMock()
        trader._active_tokens = {}
        trader._agent = MagicMock()
        trader._spread_portfolio = None  # options path; not initialized in unit tests

        chat_msgs = [{"command": "", "text": "show me the pnl", "timestamp": 0}]

        # _try_answer_simple_chat should handle this and return True
        # without ever calling _run_agent_iteration
        with patch.object(trader, "_run_agent_iteration") as mock_rai:
            result = trader._try_answer_simple_chat(chat_msgs)

        assert result is True, "Expected _try_answer_simple_chat to return True for pnl query"
        mock_rai.assert_not_called()

    def test_positions_query_does_not_call_gemini(self):
        """'positions' keyword is also handled without Gemini."""
        import main as _main

        trader = _main.BlitzTrader.__new__(_main.BlitzTrader)
        trader._state = self._make_state_manager()
        trader._order_exec = self._make_order_exec()
        trader._telegram = MagicMock()
        trader._telegram.send_telegram.return_value = {"status": "sent"}
        trader._feed = MagicMock()
        trader._shoonya = MagicMock()
        trader._active_tokens = {}
        trader._agent = MagicMock()
        trader._spread_portfolio = None  # options path; not initialized in unit tests

        chat_msgs = [{"command": "", "text": "what are my open positions?", "timestamp": 0}]

        with patch.object(trader, "_run_agent_iteration") as mock_rai:
            result = trader._try_answer_simple_chat(chat_msgs)

        assert result is True
        mock_rai.assert_not_called()
