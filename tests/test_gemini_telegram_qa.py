"""
tests/test_gemini_telegram_qa.py
----------------------------------
Verifies that Gemini/Telegram Q&A is wired correctly and that the
live trading path is entirely Gemini-free.

Boundary rules enforced here:
  ALLOWED — Gemini answers free-form Telegram questions (chat path)
  ALLOWED — Gemini writes the EOD journal/reflection (eod path)
  ALLOWED — Gemini proposes post-market wiki hypotheses (wiki path)
  FORBIDDEN — Gemini in the live signal review / order placement path
  FORBIDDEN — Gemini used for direct trade approval / order placement
"""
from __future__ import annotations

import inspect
import os
import sys
import types as module_types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ── project root on sys.path ────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

# ── Stub google.genai so agent_loop / main can be imported without the SDK ──
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
                raise NotImplementedError("Stub — not a real Gemini call")

    genai_mod.Client = _FakeClient
    google_mod.genai = genai_mod
    sys.modules.setdefault("google", google_mod)
    sys.modules.setdefault("google.genai", genai_mod)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state_manager(pnl: float = 0.0, trade_count: int = 0):
    sm = MagicMock()
    sm.get_state.return_value = {
        "daily_pnl": pnl,
        "daily_pnl_pct": 0.0,
        "virtual_capital": 500_000.0,
        "available_balance": 500_000.0,
        "margin_used": 0.0,
        "positions": [],
        "trade_count": trade_count,
        "notifications_sent": {},
    }
    sm.get_daily_pnl.return_value = (pnl, 0.0)
    return sm


def _make_order_exec():
    oe = MagicMock()
    oe.get_open_positions.return_value = {"positions": [], "count": 0}
    return oe


def _make_trader(pnl: float = 0.0):
    """Build a BlitzTrader with all deps mocked — no real Shoonya or Gemini."""
    import main as _main
    trader = _main.BlitzTrader.__new__(_main.BlitzTrader)
    trader._state = _make_state_manager(pnl)
    trader._order_exec = _make_order_exec()
    trader._telegram = MagicMock()
    trader._telegram.send_telegram.return_value = {"status": "sent"}
    trader._feed = MagicMock()
    trader._shoonya = MagicMock()
    trader._active_tokens = {
        "NIFTY": {"tsym": "NIFTY28APR26F", "lot_size": 25, "token": "66691", "exchange": "NFO"},
        "BANKNIFTY": {"tsym": "BANKNIFTY28APR26F", "lot_size": 15, "token": "66688", "exchange": "NFO"},
    }
    trader._agent = MagicMock()
    trader._agent.run_iteration.return_value = "Gemini mock response"
    trader._agent.get_last_error.return_value = None
    trader._llm_disabled_reason = None
    trader._llm_disabled_notified = False
    trader._market_data = MagicMock()
    trader._journal = MagicMock()
    trader._promoted_futures_filters = []
    trader._goals = MagicMock()
    trader._goals.has_goals.return_value = False
    trader._spread_portfolio = None  # options path; not initialized in unit tests
    return trader


# ---------------------------------------------------------------------------
# 1. System prompt describes the correct Gemini role boundary
# ---------------------------------------------------------------------------

class TestSystemPromptRoleBoundary:
    """SYSTEM_PROMPT must correctly describe Gemini as the reporting layer only."""

    def test_system_prompt_describes_gemini_as_reporting_layer(self):
        from context_builder import SYSTEM_PROMPT
        # The canonical boundary phrase set in context_builder.py
        assert "reporting and reflection layer" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must describe Gemini as the 'reporting and reflection layer'"
        )

    def test_system_prompt_says_python_owns_execution(self):
        from context_builder import SYSTEM_PROMPT
        assert "Python owns live trade" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must state that Python owns live trade decisions"
        )

    def test_system_prompt_instructs_gemini_to_use_send_telegram_for_chat(self):
        from context_builder import SYSTEM_PROMPT
        assert "send_telegram" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must instruct Gemini to use send_telegram() to reply on Telegram"
        )

    def test_system_prompt_does_not_instruct_gemini_to_approve_trades(self):
        from context_builder import SYSTEM_PROMPT
        prompt_lower = SYSTEM_PROMPT.lower()
        # Must not have an affirmative instruction like "you approve" or "gemini approves"
        forbidden_phrases = [
            "gemini approves",
            "you approve the trade",
            "approve or reject the signal",  # the affirmative version for Gemini
        ]
        for phrase in forbidden_phrases:
            assert phrase not in prompt_lower, (
                f"SYSTEM_PROMPT must not instruct Gemini to approve live trades: found '{phrase}'"
            )

    def test_system_prompt_lists_send_telegram_as_available_tool(self):
        from context_builder import SYSTEM_PROMPT
        tools_line = next(
            (line for line in SYSTEM_PROMPT.splitlines() if line.startswith("Available tools:")),
            ""
        )
        assert "send_telegram" in tools_line, (
            "send_telegram must appear in Available tools so Gemini can respond on Telegram"
        )

    def test_system_prompt_role_boundary_note_is_present(self):
        from context_builder import SYSTEM_PROMPT
        assert "ROLE BOUNDARY" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must contain a ROLE BOUNDARY section"
        )


# ---------------------------------------------------------------------------
# 2. build_chat_context includes the user's message
# ---------------------------------------------------------------------------

class TestBuildChatContext:
    """build_chat_context() must produce a context string that includes user messages."""

    def test_chat_context_includes_user_message_text(self):
        from context_builder import build_chat_context
        state_manager = _make_state_manager()
        order_exec = _make_order_exec()
        ctx = build_chat_context(
            chat_messages=[{"text": "what strategy fired today?"}],
            state_manager=state_manager,
            order_execution=order_exec,
        )
        assert "what strategy fired today?" in ctx, (
            "build_chat_context must include the user's message text in the context string"
        )

    def test_chat_context_includes_session_pnl(self):
        from context_builder import build_chat_context
        state_manager = _make_state_manager(pnl=1234.56)
        order_exec = _make_order_exec()
        ctx = build_chat_context(
            chat_messages=[{"text": "how am I doing?"}],
            state_manager=state_manager,
            order_execution=order_exec,
        )
        assert "1234" in ctx or "P&L" in ctx, (
            "build_chat_context must include session P&L in context"
        )

    def test_chat_context_instructs_gemini_to_use_send_telegram(self):
        from context_builder import build_chat_context
        state_manager = _make_state_manager()
        order_exec = _make_order_exec()
        ctx = build_chat_context(
            chat_messages=[{"text": "status?"}],
            state_manager=state_manager,
            order_execution=order_exec,
        )
        assert "send_telegram" in ctx, (
            "build_chat_context must instruct Gemini to respond via send_telegram()"
        )

    def test_chat_context_does_not_include_pairs_section(self):
        """Futures-only runtime: build_chat_context must not include pairs summary."""
        from context_builder import build_chat_context
        state_manager = _make_state_manager()
        order_exec = _make_order_exec()
        ctx = build_chat_context(
            chat_messages=[{"text": "status?"}],
            state_manager=state_manager,
            order_execution=order_exec,
        )
        assert "Pairs capital" not in ctx, (
            "build_chat_context must not include pairs capital in futures-only runtime"
        )

    def test_chat_context_includes_futures_pnl(self):
        """build_chat_context must include futures P&L."""
        from context_builder import build_chat_context
        state_manager = _make_state_manager(pnl=500.0)
        order_exec = _make_order_exec()
        ctx = build_chat_context(
            chat_messages=[{"text": "status?"}],
            state_manager=state_manager,
            order_execution=order_exec,
        )
        assert "P&L" in ctx or "500" in ctx, (
            "build_chat_context must include futures P&L information"
        )


# ---------------------------------------------------------------------------
# 3. Free-form chat (non-status) routes to Gemini via _run_agent_iteration
# ---------------------------------------------------------------------------

class TestFreeChatRoutesToGemini:
    """
    Non-deterministic questions (strategy analysis, market commentary, logs) must
    reach Gemini via build_chat_context + _run_agent_iteration.
    """

    def test_unknown_question_routes_to_gemini(self):
        """A free-form question that doesn't match the simple-chat path must call Gemini."""
        trader = _make_trader()
        chat_msgs = [{"command": "", "text": "explain the last trade", "timestamp": 0}]

        # _try_answer_simple_chat should return False for this (no 'pnl'/'position' keywords)
        result = trader._try_answer_simple_chat(chat_msgs)
        assert result is False, (
            "_try_answer_simple_chat must return False for a non-status question"
        )

    def test_strategy_question_not_handled_deterministically(self):
        """'What strategy fired?' doesn't contain status keywords → falls through to Gemini."""
        trader = _make_trader()
        chat_msgs = [{"command": "", "text": "what strategy fired today?", "timestamp": 0}]
        result = trader._try_answer_simple_chat(chat_msgs)
        assert result is False

    def test_build_chat_context_is_callable_for_any_question(self):
        """build_chat_context must not crash on arbitrary questions."""
        from context_builder import build_chat_context
        state_manager = _make_state_manager()
        order_exec = _make_order_exec()
        ctx = build_chat_context(
            chat_messages=[{"text": "explain VIX impact on today's VSA signals"}],
            state_manager=state_manager,
            order_execution=order_exec,
        )
        assert isinstance(ctx, str) and len(ctx) > 50, (
            "build_chat_context must return a non-trivial context string"
        )


# ---------------------------------------------------------------------------
# 4. Live signal processing does NOT call Gemini
# ---------------------------------------------------------------------------

class TestLiveSignalPathGeminiFree:
    """
    _process_tradeable_signals_python and _review_signal_python must never
    call AgentLoop.run_iteration or any Gemini API.
    """

    def test_process_tradeable_signals_python_never_calls_run_iteration(self):
        """
        Processing a scanner signal from start to fill must not call
        _run_agent_iteration or agent.run_iteration.
        """
        trader = _make_trader()

        # Wire up a successful indicator read and order placement
        trader._market_data.get_indicators.return_value = {
            "current_price": 24000.0,
            "ema20": 24020.0,
            "adx14": 28.0,
            "rsi14": 45.0,
            "avg_volume_20": 50_000.0,
            "ema_stacked_bull": False,
            "ema_stacked_bear": True,
        }
        trader._order_exec.place_virtual_order.return_value = {
            "status": "FILLED",
            "fill_price": 23990.0,
            "order_id": "test-001",
        }
        trader._journal.log_decision.return_value = {"status": "logged"}

        signals = [{
            "symbol": "NIFTY",
            "interval": "5",
            "strategy": "VP-01 Counter Bull Trap",
            "direction": "SELL",
            "stop_loss": 24040.0,
            "target": 23920.0,
            "execution_symbol": "NIFTY28APR26F",
            "lot_size": 25,
        }]

        with patch.object(trader, "_run_agent_iteration") as mock_rai:
            trader._process_tradeable_signals_python(signals)

        mock_rai.assert_not_called(), (
            "_run_agent_iteration must NOT be called during signal processing"
        )
        trader._agent.run_iteration.assert_not_called(), (
            "AgentLoop.run_iteration must NOT be called during signal processing"
        )

    def test_review_signal_python_is_pure_python(self):
        """
        _review_signal_python approves/rejects signals using only Python indicator
        checks. It must not call Gemini.
        """
        trader = _make_trader()
        trader._market_data.get_indicators.return_value = {
            "current_price": 24000.0,
            "ema20": 24020.0,
            "adx14": 28.0,
            "rsi14": 45.0,
            "avg_volume_20": 50_000.0,
            "ema_stacked_bull": False,
            "ema_stacked_bear": True,
        }

        with patch.object(trader, "_run_agent_iteration") as mock_rai:
            approved, context, reason = trader._review_signal_python({
                "symbol": "NIFTY",
                "interval": "5",
                "strategy": "VP-01 Counter Bull Trap",
                "direction": "SELL",
                "stop_loss": 24040.0,
                "target": 23920.0,
            })

        assert approved is True
        assert "Python approved" in reason
        mock_rai.assert_not_called()
        trader._agent.run_iteration.assert_not_called()

    def test_review_signal_python_rejects_ema_mismatch_without_gemini(self):
        """EMA stack mismatch is a Python-enforced hard rule, not a Gemini judgment call."""
        trader = _make_trader()
        trader._market_data.get_indicators.return_value = {
            "current_price": 24000.0,
            "ema20": 23980.0,
            "adx14": 28.0,
            "rsi14": 55.0,
            "avg_volume_20": 50_000.0,
            "ema_stacked_bull": True,   # bullish stack
            "ema_stacked_bear": False,
        }

        with patch.object(trader, "_run_agent_iteration") as mock_rai:
            approved, _ctx, reason = trader._review_signal_python({
                "symbol": "NIFTY",
                "interval": "5",
                "strategy": "VP-01 Counter Bull Trap",
                "direction": "SELL",          # SELL into bullish stack → rejected
                "stop_loss": 24040.0,
                "target": 23920.0,
            })

        assert approved is False
        assert "EMA stack" in reason or "bullish" in reason.lower()
        mock_rai.assert_not_called()

    def test_review_signal_python_rejects_weak_adx_without_gemini(self):
        """ADX < 18 rejection for trend strategies is a Python rule, not Gemini opinion."""
        trader = _make_trader()
        trader._market_data.get_indicators.return_value = {
            "current_price": 24000.0,
            "ema20": 24020.0,
            "adx14": 12.0,             # too weak
            "rsi14": 40.0,
            "avg_volume_20": 50_000.0,
            "ema_stacked_bull": False,
            "ema_stacked_bear": True,
        }

        with patch.object(trader, "_run_agent_iteration") as mock_rai:
            approved, _ctx, reason = trader._review_signal_python({
                "symbol": "NIFTY",
                "interval": "5",
                "strategy": "VP-01 Counter Bull Trap",
                "direction": "SELL",
                "stop_loss": 24040.0,
                "target": 23920.0,
            })

        assert approved is False
        assert "ADX" in reason
        mock_rai.assert_not_called()

    def test_filter_tradeable_signals_never_calls_gemini(self):
        """Hard guardrails applied in _filter_tradeable_signals must not call Gemini."""
        from datetime import datetime
        import pytz

        trader = _make_trader()
        now = pytz.timezone("Asia/Kolkata").localize(datetime(2026, 5, 25, 10, 0))

        signal = {
            "symbol": "NIFTY",
            "interval": "5",
            "strategy": "VP-01 Counter Bull Trap",
            "direction": "SELL",
            "stop_loss": 24040.0,
            "target": 23920.0,
        }

        with patch.object(trader, "_run_agent_iteration") as mock_rai:
            tradeable, blocked = trader._filter_tradeable_signals([signal], now)

        mock_rai.assert_not_called()
        trader._agent.run_iteration.assert_not_called()
        assert len(tradeable) == 1   # signal should pass guardrails


# ---------------------------------------------------------------------------
# 5. Source code proof: _process_tradeable_signals_python has no AgentLoop call
# ---------------------------------------------------------------------------

class TestSourceCodeGatekeeper:
    """
    Source-code–level checks that live execution methods have no Gemini references.
    These run against the actual source without imports.
    """

    def _get_method_source(self, method_name: str) -> str:
        import main as _main
        method = getattr(_main.BlitzTrader, method_name)
        return inspect.getsource(method)

    def test_process_tradeable_signals_has_no_run_iteration_call(self):
        src = self._get_method_source("_process_tradeable_signals_python")
        assert "run_iteration" not in src, (
            "_process_tradeable_signals_python must not call run_iteration (Gemini)"
        )

    def test_process_tradeable_signals_has_no_agent_attribute_access(self):
        src = self._get_method_source("_process_tradeable_signals_python")
        assert "self._agent" not in src, (
            "_process_tradeable_signals_python must not access self._agent"
        )

    def test_review_signal_python_has_no_run_iteration_call(self):
        src = self._get_method_source("_review_signal_python")
        assert "run_iteration" not in src, (
            "_review_signal_python must not call run_iteration (Gemini)"
        )

    def test_review_signal_python_has_no_agent_attribute_access(self):
        src = self._get_method_source("_review_signal_python")
        assert "self._agent" not in src, (
            "_review_signal_python must not access self._agent"
        )

    def test_filter_tradeable_signals_has_no_agent_access(self):
        src = self._get_method_source("_filter_tradeable_signals")
        assert "self._agent" not in src, (
            "_filter_tradeable_signals must not access self._agent"
        )

    def test_trading_loop_uses_build_chat_context_for_free_form_messages(self):
        """main.py must call build_chat_context for the chat iteration path."""
        main_src = (_REPO / "main.py").read_text()
        assert "build_chat_context" in main_src, (
            "main.py must call build_chat_context for free-form Telegram messages"
        )

    def test_trading_loop_chat_path_calls_run_agent_iteration(self):
        """The chat path must eventually call _run_agent_iteration (Gemini)."""
        src = self._get_method_source("_trading_loop")
        assert "_run_agent_iteration" in src, (
            "_trading_loop must call _run_agent_iteration for the chat path"
        )

    def test_eod_path_calls_run_agent_iteration(self):
        """EOD sequence must call _run_agent_iteration (Gemini for EOD summary)."""
        src = self._get_method_source("_trading_loop")
        # The eod sequence must include a _run_agent_iteration call
        assert "eod_context" in src and "_run_agent_iteration" in src, (
            "EOD path must call _run_agent_iteration for Gemini reflection"
        )

    def test_startup_phase_does_not_call_run_agent_iteration(self):
        """
        Startup is deterministic Python — Gemini is intentionally not called here
        (as stated in the _startup_phase docstring).
        """
        src = self._get_method_source("_startup_phase")
        assert "run_iteration" not in src, (
            "_startup_phase must not call Gemini — startup is deterministic Python"
        )
        assert "_run_agent_iteration" not in src, (
            "_startup_phase must not call _run_agent_iteration"
        )


# ---------------------------------------------------------------------------
# 6. EOD context builder produces valid output (Gemini reporting, not execution)
# ---------------------------------------------------------------------------

class TestEODContextBuilderGeminiRole:
    """build_eod_context must produce instructions for Gemini reporting, not execution."""

    def test_eod_context_instructs_gemini_to_call_get_todays_trades_first(self):
        from context_builder import build_eod_context
        ctx = build_eod_context()
        assert "get_todays_trades" in ctx, (
            "EOD context must instruct Gemini to call get_todays_trades() — reporting, not trading"
        )

    def test_eod_context_instructs_gemini_to_send_telegram_summary(self):
        from context_builder import build_eod_context
        ctx = build_eod_context()
        assert "send_telegram" in ctx, (
            "EOD context must instruct Gemini to send EOD summary via Telegram"
        )

    def test_eod_context_does_not_delegate_closing_to_legacy_tool(self):
        """EOD spread closing is deterministic Python; Gemini only reports."""
        from context_builder import build_eod_context
        ctx = build_eod_context()
        assert "do NOT call close_all_positions" in ctx, (
            "EOD context must explicitly forbid legacy futures close_all_positions()"
        )

    def test_eod_context_forbids_inventing_data(self):
        from context_builder import build_eod_context
        ctx = build_eod_context()
        ctx_lower = ctx.lower()
        assert "do not" in ctx_lower or "must not" in ctx_lower or "never" in ctx_lower, (
            "EOD context must include anti-fabrication guardrails"
        )


# ---------------------------------------------------------------------------
# 7. TelegramHandler queues messages correctly for the agent loop
# ---------------------------------------------------------------------------

class TestTelegramHandlerQueuing:
    """TelegramHandler must queue all messages (free-form and commands) for the agent."""

    def test_command_queue_accepts_free_form_text(self):
        from tools.telegram_handler import TelegramHandler
        handler = TelegramHandler.__new__(TelegramHandler)
        from collections import deque
        handler._command_queue = deque(maxlen=50)

        # Simulate what _listener_loop does when it receives a free-form message
        text = "how am I doing today?"
        handler._command_queue.append({
            "command": "",   # no slash command
            "text": text,
            "timestamp": 0.0,
        })

        commands = handler.get_pending_commands()
        assert len(commands) == 1
        assert commands[0]["command"] == ""
        assert commands[0]["text"] == text

    def test_command_queue_accepts_slash_commands(self):
        from tools.telegram_handler import TelegramHandler
        handler = TelegramHandler.__new__(TelegramHandler)
        from collections import deque
        handler._command_queue = deque(maxlen=50)

        handler._command_queue.append({
            "command": "/status",
            "text": "/status",
            "timestamp": 0.0,
        })

        commands = handler.get_pending_commands()
        assert len(commands) == 1
        assert commands[0]["command"] == "/status"

    def test_get_pending_commands_clears_queue(self):
        from tools.telegram_handler import TelegramHandler
        handler = TelegramHandler.__new__(TelegramHandler)
        from collections import deque
        handler._command_queue = deque(maxlen=50)

        for i in range(3):
            handler._command_queue.append({"command": "", "text": f"msg{i}", "timestamp": float(i)})

        first_batch = handler.get_pending_commands()
        second_batch = handler.get_pending_commands()

        assert len(first_batch) == 3
        assert len(second_batch) == 0, "Queue must be empty after get_pending_commands()"

    def test_free_form_text_classified_as_empty_command(self):
        """
        In _listener_loop: if text doesn't start with '/', command="" and text=full message.
        This is the free-form path that reaches Gemini Q&A.
        """
        # Simulate the classification logic from _listener_loop
        text = "what happened with NIFTY this morning?"
        command = text.split()[0] if text and text.startswith("/") else ""
        assert command == "", "Free-form messages must be classified with command=''"

    def test_inject_command_returns_raw_text_for_unknown_messages(self):
        """
        For non-slash messages (free-form chat), inject_command returns the raw text,
        which gets injected into Gemini's context as a user question.
        """
        from tools.telegram_handler import TelegramHandler
        handler = TelegramHandler.__new__(TelegramHandler)

        # Simulate: user sends "why did the VP-01 trade fail?"
        raw = "why did the VP-01 trade fail?"
        injected = handler.inject_command("", context=raw)
        assert injected == raw, (
            "inject_command with no slash command must return the raw text for Gemini"
        )


# ---------------------------------------------------------------------------
# 8. Wiki/proposal scripts use Gemini only for hypothesis text, not trade calls
# ---------------------------------------------------------------------------

class TestWikiGeminiUsageIsPostMarketOnly:
    """propose_futures_hypotheses.py must use Gemini only for text proposal, not trading."""

    def test_propose_script_does_not_import_order_execution(self):
        """The wiki proposal script must not import trading execution tools."""
        src = (_REPO / "scripts" / "propose_futures_hypotheses.py").read_text()
        assert "order_execution" not in src, (
            "propose_futures_hypotheses.py must not import OrderExecutionTools"
        )
        assert "place_virtual_order" not in src, (
            "propose_futures_hypotheses.py must not call place_virtual_order"
        )

    def test_propose_script_does_not_import_shoonya(self):
        src = (_REPO / "scripts" / "propose_futures_hypotheses.py").read_text()
        assert "ShoonyaClient" not in src, (
            "propose_futures_hypotheses.py must not import ShoonyaClient"
        )

    def test_propose_script_only_writes_hypothesis_json(self):
        """Output artifacts from the proposal script must only be hypothesis JSON files."""
        src = (_REPO / "scripts" / "propose_futures_hypotheses.py").read_text()
        # Must write to wiki/hypotheses, not to promoted_filters directly
        assert "promoted_filters" not in src, (
            "propose_futures_hypotheses.py must not write to promoted_filters directly — "
            "promotion requires backtest validation via a separate script"
        )

    def test_backtest_script_does_not_import_gemini(self):
        """backtest_futures_hypothesis.py must be pure Python — no Gemini SDK."""
        src = (_REPO / "scripts" / "backtest_futures_hypothesis.py").read_text()
        assert "google.genai" not in src, (
            "backtest_futures_hypothesis.py must not use the Gemini SDK"
        )
        assert "AgentLoop" not in src, (
            "backtest_futures_hypothesis.py must not use AgentLoop"
        )

    def test_promote_script_does_not_import_gemini(self):
        """promote_futures_hypothesis.py must be pure Python — no Gemini SDK."""
        src = (_REPO / "scripts" / "promote_futures_hypothesis.py").read_text()
        assert "google.genai" not in src, (
            "promote_futures_hypothesis.py must not use the Gemini SDK"
        )
        assert "AgentLoop" not in src, (
            "promote_futures_hypothesis.py must not use AgentLoop"
        )
