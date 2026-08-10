"""Portfolio queries must never be delegated to Gemini outside trading hours."""

from unittest.mock import MagicMock, patch

from scripts.blitztrader_agent import BlitzTraderAgent


def test_portfolio_query_detection():
    assert BlitzTraderAgent._is_portfolio_query("pnl?")
    assert BlitzTraderAgent._is_portfolio_query("show positions")
    assert BlitzTraderAgent._is_portfolio_query("", "/status")
    assert not BlitzTraderAgent._is_portfolio_query("what happened yesterday?")


def test_portfolio_query_does_not_call_gemini():
    agent = BlitzTraderAgent()
    agent._telegram = MagicMock()
    agent._agent = MagicMock()

    with patch.object(agent, "_answer_portfolio_query") as answer:
        if agent._is_portfolio_query("pnl?"):
            agent._answer_portfolio_query()
        else:
            agent._answer_message("pnl?")

    answer.assert_called_once_with()
    agent._agent.run_iteration.assert_not_called()
