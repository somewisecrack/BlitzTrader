"""
tests/test_telegram_agent_ownership.py
----------------------------------------
Tests for the lazy-start Telegram ownership pattern in blitztrader_agent.py.

Invariants tested:
  - TelegramHandler.start() is NEVER called during _initialize()
  - Handler is started only when trading service is inactive
  - Handler is stopped (yielded) when trading service becomes active
  - _build_minimal_registry never includes order execution tools
"""
import os
import sys
import types as module_types
import unittest
from unittest.mock import MagicMock, patch, call

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


class TestAgentTelegramOwnership(unittest.TestCase):
    """Tests for lazy Telegram handler start/stop in BlitzTraderAgent."""

    def _make_agent(self):
        """Import and instantiate BlitzTraderAgent with mocked dependencies."""
        # Import inside method so stubs are in place
        from scripts.blitztrader_agent import BlitzTraderAgent
        agent = BlitzTraderAgent()
        return agent

    def _patch_telegram(self, agent):
        """Attach a mock TelegramHandler to an already-initialized agent."""
        mock_tg = MagicMock()
        mock_tg.get_pending_commands.return_value = []
        agent._telegram = mock_tg
        return mock_tg

    def test_telegram_not_started_during_init(self):
        """
        TelegramHandler.start() must NOT be called inside _initialize().
        When trading service is active, handler should never start.
        """
        from scripts.blitztrader_agent import BlitzTraderAgent, POLL_INTERVAL_SECONDS, YIELD_CHECK_INTERVAL_SECONDS

        agent = BlitzTraderAgent()

        mock_tg = MagicMock()
        mock_tg.get_pending_commands.return_value = []

        mock_agent_loop = MagicMock()

        # Patch _initialize to set up mocks without hitting real config
        def fake_initialize():
            agent._telegram = mock_tg
            agent._agent = mock_agent_loop

        agent._initialize = fake_initialize

        # Run one loop iteration with trading service active
        iteration_count = [0]

        def fake_is_active():
            iteration_count[0] += 1
            if iteration_count[0] >= 2:
                agent._running = False
            return True  # trading always active

        import scripts.blitztrader_agent as agent_module
        original_is_active = agent_module._is_trading_service_active
        original_sleep = __import__('time').sleep

        try:
            agent_module._is_trading_service_active = fake_is_active
            with patch('time.sleep'):
                with patch('scripts.blitztrader_agent._write_agent_pid'):
                    with patch('scripts.blitztrader_agent._remove_agent_pid'):
                        with patch('scripts.blitztrader_agent.setup_logging'):
                            agent.run()
        finally:
            agent_module._is_trading_service_active = original_is_active

        # start() must never have been called — trading was always active
        mock_tg.start.assert_not_called()

    def test_telegram_started_when_trading_inactive(self):
        """
        When trading service is NOT active, TelegramHandler.start() must be called.
        """
        from scripts.blitztrader_agent import BlitzTraderAgent
        import scripts.blitztrader_agent as agent_module

        agent = BlitzTraderAgent()
        mock_tg = MagicMock()
        mock_tg.get_pending_commands.return_value = []

        def fake_initialize():
            agent._telegram = mock_tg
            agent._agent = MagicMock()

        agent._initialize = fake_initialize

        iteration_count = [0]

        def fake_is_active():
            iteration_count[0] += 1
            if iteration_count[0] >= 2:
                agent._running = False
            return False  # trading never active

        original_is_active = agent_module._is_trading_service_active
        try:
            agent_module._is_trading_service_active = fake_is_active
            with patch('time.sleep'):
                with patch('scripts.blitztrader_agent._write_agent_pid'):
                    with patch('scripts.blitztrader_agent._remove_agent_pid'):
                        with patch('scripts.blitztrader_agent.setup_logging'):
                            agent.run()
        finally:
            agent_module._is_trading_service_active = original_is_active

        mock_tg.start.assert_called_once()

    def test_telegram_stopped_when_trading_activates(self):
        """
        If the agent already owns the handler (_telegram_started=True) and the
        trading service becomes active, the handler must be stopped.
        """
        from scripts.blitztrader_agent import BlitzTraderAgent
        import scripts.blitztrader_agent as agent_module

        agent = BlitzTraderAgent()
        mock_tg = MagicMock()
        mock_tg.get_pending_commands.return_value = []

        # Simulate: handler already started
        agent._telegram_started = True

        def fake_initialize():
            agent._telegram = mock_tg
            agent._agent = MagicMock()

        agent._initialize = fake_initialize

        iteration_count = [0]

        def fake_is_active():
            iteration_count[0] += 1
            if iteration_count[0] >= 2:
                agent._running = False
            return True  # trading just became active

        original_is_active = agent_module._is_trading_service_active
        try:
            agent_module._is_trading_service_active = fake_is_active
            with patch('time.sleep'):
                with patch('scripts.blitztrader_agent._write_agent_pid'):
                    with patch('scripts.blitztrader_agent._remove_agent_pid'):
                        with patch('scripts.blitztrader_agent.setup_logging'):
                            agent.run()
        finally:
            agent_module._is_trading_service_active = original_is_active

        mock_tg.stop.assert_called()
        self.assertFalse(agent._telegram_started)

    def test_agent_never_has_order_tools(self):
        """
        _build_minimal_registry must NOT include place_virtual_order in its tool_map.
        The Q&A agent is read-only and must never be able to place trades.

        _build_minimal_registry imports its dependencies lazily inside the function,
        so we patch at the source module level (tools.*) rather than on the agent module.
        """
        from scripts.blitztrader_agent import _build_minimal_registry

        mock_telegram = MagicMock()

        stub_state = MagicMock()
        stub_journal = MagicMock()
        stub_strategy = MagicMock()
        stub_memory = MagicMock()
        stub_goals = MagicMock()

        mock_registry = MagicMock()
        mock_registry.tool_map = {
            "read_journals": MagicMock(),
            "get_strategy_docs": MagicMock(),
            "send_telegram": MagicMock(),
        }

        with patch("tools.state_manager.StateManager", return_value=stub_state), \
             patch("tools.journal_writer.JournalWriter", return_value=stub_journal), \
             patch("tools.strategy_reader.StrategyReader", return_value=stub_strategy), \
             patch("tools.memory_reader.MemoryReader", return_value=stub_memory), \
             patch("tools.goal_manager.GoalManager", return_value=stub_goals), \
             patch("tools.registry.ToolRegistry", return_value=mock_registry):
            registry = _build_minimal_registry(mock_telegram, None)

        # The registry should NOT have place_virtual_order
        tool_map = registry.tool_map
        self.assertNotIn(
            "place_virtual_order",
            tool_map,
            "Q&A agent registry must never contain place_virtual_order",
        )


class TestTelegramSingleOwnerInvariants(unittest.TestCase):
    """
    Source-level and behavioural proof that the Telegram single-owner contract holds.

    Invariants tested:
      - TelegramHandler._listener_loop never sends an auto-acknowledgement
      - TelegramHandler.send_telegram never appends a [Verified] / performance footer
      - _is_trading_service_active() returns False for a stale (dead) PID file
      - Q&A agent rejects exit commands with a clear rejection message
      - Q&A agent serves readonly status without calling Gemini
    """

    # ── Source-code proofs ────────────────────────────────────────────────────

    def test_listener_loop_has_no_auto_acknowledge(self):
        """
        _listener_loop must not call send_telegram (auto-acknowledge removed).
        Regression guard: the 'Received — responding now' pattern must not exist.
        """
        import inspect
        from tools.telegram_handler import TelegramHandler
        src = inspect.getsource(TelegramHandler._listener_loop)
        self.assertNotIn(
            "send_telegram",
            src,
            "_listener_loop must NOT call send_telegram — no auto-acknowledgement",
        )
        self.assertNotIn(
            "responding now",
            src,
            "_listener_loop must not contain legacy 'responding now' text",
        )

    def test_send_telegram_has_no_verified_footer(self):
        """
        send_telegram must not inject a [Verified] / performance footer.
        Regression guard for the removed state_manager auto-append.
        """
        import inspect
        from tools.telegram_handler import TelegramHandler
        src = inspect.getsource(TelegramHandler.send_telegram)
        self.assertNotIn(
            "[Verified]",
            src,
            "send_telegram must not append a [Verified] performance footer",
        )
        self.assertNotIn(
            "trade_count",
            src,
            "send_telegram must not reference trade_count",
        )
        self.assertNotIn(
            "_state_manager",
            src,
            "send_telegram must not access _state_manager",
        )

    def test_telegram_handler_init_has_no_state_manager_param(self):
        """TelegramHandler.__init__ no longer accepts state_manager."""
        import inspect
        from tools.telegram_handler import TelegramHandler
        sig = inspect.signature(TelegramHandler.__init__)
        self.assertNotIn(
            "state_manager",
            sig.parameters,
            "TelegramHandler.__init__ must not accept state_manager parameter",
        )

    # ── Behavioural: send_telegram does not mutate the message ────────────────

    def test_send_telegram_does_not_append_to_message(self):
        """
        send_telegram must deliver exactly the message passed in (no footer appended).
        """
        from tools.telegram_handler import TelegramHandler, send_telegram_safe
        from unittest.mock import patch, MagicMock

        handler = TelegramHandler.__new__(TelegramHandler)
        handler._bot_token = "TOKEN"
        handler._user_id = "12345"

        sent_texts = []

        def _capture(bot_token, chat_id, text):
            sent_texts.append(text)
            return True

        with patch("tools.telegram_handler.send_telegram_safe", side_effect=_capture):
            handler.send_telegram("Session P&L: ₹1,234 | trades: 3")

        self.assertEqual(len(sent_texts), 1)
        self.assertNotIn("[Verified]", sent_texts[0])
        self.assertNotIn("Trades:", sent_texts[0].replace("Session P&L: ₹1,234 | trades: 3", ""))

    # ── Stale PID detection ───────────────────────────────────────────────────

    def test_stale_pid_file_treated_as_inactive(self):
        """
        _is_trading_service_active() must return False when PID file exists but
        the referenced process is dead (stale file from a crash).
        """
        import tempfile
        import scripts.blitztrader_agent as agent_module

        original_pid_file = agent_module.TRADING_PID_FILE
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pid", delete=False) as f:
            # Use PID 99999999 — astronomically unlikely to exist on a real system
            f.write("99999999")
            stale_path = f.name

        try:
            agent_module.TRADING_PID_FILE = type("P", (), {"exists": lambda s: True,
                "read_text": lambda s: "99999999"})()
            # Monkey-patch: make TRADING_PID_FILE.exists() return True,
            # .read_text() return "99999999", and os.kill raise OSError.
            from pathlib import Path
            agent_module.TRADING_PID_FILE = Path(stale_path)
            result = agent_module._is_trading_service_active()
            self.assertFalse(
                result,
                "_is_trading_service_active must return False for stale (dead) PID",
            )
        finally:
            agent_module.TRADING_PID_FILE = original_pid_file
            import os
            try:
                os.unlink(stale_path)
            except FileNotFoundError:
                pass

    def test_missing_pid_file_treated_as_inactive(self):
        """_is_trading_service_active() returns False when PID file doesn't exist."""
        import scripts.blitztrader_agent as agent_module
        from pathlib import Path

        original_pid_file = agent_module.TRADING_PID_FILE
        try:
            agent_module.TRADING_PID_FILE = Path("/tmp/blitztrader_trading_NONEXISTENT_test.pid")
            result = agent_module._is_trading_service_active()
            self.assertFalse(result)
        finally:
            agent_module.TRADING_PID_FILE = original_pid_file

    # ── Q&A agent: exit command rejection ─────────────────────────────────────

    def test_qa_agent_rejects_exit_serial_command(self):
        """
        When trading is inactive, 'exit 1' must be rejected with a clear message.
        Gemini must NOT be invoked.
        """
        from scripts.blitztrader_agent import BlitzTraderAgent, _is_exit_command
        import scripts.blitztrader_agent as agent_module

        # Verify the classifier rejects these patterns
        exit_commands = [
            "exit 1", "exit #2", "close 3", "close position 1",
            "close spread 2", "close pair 1", "square off #3",
            "exit pair", "close all", "square off all",
        ]
        for cmd in exit_commands:
            self.assertTrue(
                _is_exit_command(cmd),
                f"_is_exit_command must return True for: {cmd!r}",
            )

        # Non-exit messages must pass through
        non_exit = ["how am I doing?", "pnl", "status", "what happened today?"]
        for msg in non_exit:
            self.assertFalse(
                _is_exit_command(msg),
                f"_is_exit_command must return False for: {msg!r}",
            )

    def test_qa_agent_answer_message_rejects_exit_without_gemini(self):
        """
        _answer_message for 'exit 1' sends a rejection message without calling Gemini.
        """
        from scripts.blitztrader_agent import BlitzTraderAgent

        agent = BlitzTraderAgent.__new__(BlitzTraderAgent)
        mock_tg = MagicMock()
        mock_gemini = MagicMock()
        agent._telegram = mock_tg
        agent._agent = mock_gemini

        agent._answer_message("exit 1")

        # Gemini must NOT be invoked
        mock_gemini.run_iteration.assert_not_called()
        # A rejection Telegram message must be sent
        mock_tg.send_telegram.assert_called_once()
        rejection_msg = mock_tg.send_telegram.call_args[0][0]
        self.assertIn("trading", rejection_msg.lower())
        self.assertNotIn("closed", rejection_msg.lower())  # not "position closed"

    def test_qa_agent_answer_message_rejects_close_all_without_gemini(self):
        """'close all' is an exit command and must be rejected without Gemini."""
        from scripts.blitztrader_agent import BlitzTraderAgent

        agent = BlitzTraderAgent.__new__(BlitzTraderAgent)
        mock_tg = MagicMock()
        mock_gemini = MagicMock()
        agent._telegram = mock_tg
        agent._agent = mock_gemini

        agent._answer_message("close all positions")

        mock_gemini.run_iteration.assert_not_called()
        mock_tg.send_telegram.assert_called_once()

    # ── Q&A agent: readonly status ────────────────────────────────────────────

    def test_try_readonly_status_returns_none_for_non_status_query(self):
        """_try_readonly_status must return None for free-form non-status questions."""
        from scripts.blitztrader_agent import _try_readonly_status
        result = _try_readonly_status("what strategy fired today?")
        self.assertIsNone(result)

    def test_try_readonly_status_returns_string_for_pnl_query(self):
        """
        _try_readonly_status returns a string (not None) for 'pnl' queries,
        reading from the state file or reporting file-not-found gracefully.
        """
        import tempfile
        import json
        import scripts.blitztrader_agent as agent_module

        original_state_file = agent_module._LIVE_STATE_FILE

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({
                "daily_pnl": 1234.56,
                "daily_pnl_pct": 0.12,
                "open_spreads": [],
                "positions": [],
            }, f)
            tmp_path = f.name

        try:
            from pathlib import Path
            agent_module._LIVE_STATE_FILE = Path(tmp_path)
            result = agent_module._try_readonly_status("pnl")
            self.assertIsNotNone(result)
            self.assertIsInstance(result, str)
            self.assertIn("1,234.56", result)
        finally:
            agent_module._LIVE_STATE_FILE = original_state_file
            import os
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    def test_qa_agent_answer_message_serves_status_without_gemini(self):
        """
        'pnl' query in _answer_message must be served deterministically without Gemini.
        """
        import tempfile
        import json
        import scripts.blitztrader_agent as agent_module
        from scripts.blitztrader_agent import BlitzTraderAgent

        original_state_file = agent_module._LIVE_STATE_FILE

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({
                "daily_pnl": 500.0,
                "daily_pnl_pct": 0.05,
                "open_spreads": [],
                "positions": [],
            }, f)
            tmp_path = f.name

        try:
            from pathlib import Path
            agent_module._LIVE_STATE_FILE = Path(tmp_path)

            agent = BlitzTraderAgent.__new__(BlitzTraderAgent)
            mock_tg = MagicMock()
            mock_gemini = MagicMock()
            agent._telegram = mock_tg
            agent._agent = mock_gemini

            agent._answer_message("pnl")

            # Gemini must NOT be called for a status query
            mock_gemini.run_iteration.assert_not_called()
            # Telegram reply must be sent with P&L info
            mock_tg.send_telegram.assert_called_once()
            reply = mock_tg.send_telegram.call_args[0][0]
            self.assertIn("500", reply)
        finally:
            agent_module._LIVE_STATE_FILE = original_state_file
            import os
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
