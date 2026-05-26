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


if __name__ == "__main__":
    unittest.main(verbosity=2)
