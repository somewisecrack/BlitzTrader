"""Telegram transport must not append unrelated portfolio state to messages."""

from unittest.mock import Mock, patch

from tools.telegram_handler import TelegramHandler


def test_performance_message_is_sent_unchanged_with_state_manager():
    state_manager = Mock()
    state_manager.get_state.return_value = {"trade_count": 99, "daily_pnl": 123456.78}
    handler = TelegramHandler("token", "chat", state_manager=state_manager)
    message = "Pair-credit virtual portfolio\nTotal unrealized P&L: Rs -7,488.75"

    with patch("tools.telegram_handler.send_telegram_safe", return_value=True) as send:
        result = handler.send_telegram(message)

    assert result == {"status": "sent"}
    send.assert_called_once()
    formatted_message = send.call_args.args[2]
    assert "Pair-credit virtual portfolio" in formatted_message
    assert "Total unrealized P&amp;L: Rs -7,488.75" in formatted_message
    assert "[Verified]" not in formatted_message
    state_manager.get_state.assert_not_called()
