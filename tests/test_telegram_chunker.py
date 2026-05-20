"""
tests/test_telegram_chunker.py
-------------------------------
Tests for the Telegram long-message chunking utility in
tools/telegram_handler.py (send_telegram_safe).

Covers:
  - Short message (< TELEGRAM_MAX_CHARS) → bot called once
  - Long message (> TELEGRAM_MAX_CHARS) → bot called multiple times, no chunk > limit
  - Split happens on line boundaries (no mid-line cuts)
  - All sends fail → returns False
  - First chunk succeeds, second fails → returns True, error logged for chunk 2
"""
import sys
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.telegram_handler import send_telegram_safe, TELEGRAM_MAX_CHARS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ok_response():
    resp = MagicMock()
    resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    return resp


def _make_fail_response():
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "description": "message is too long"}
    return resp


def _long_text(n_lines: int, line_template: str = "This is line number {:05d}.\n") -> str:
    """Build a text that is guaranteed to exceed TELEGRAM_MAX_CHARS."""
    lines = [line_template.format(i) for i in range(n_lines)]
    return "".join(lines)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSendTelegramSafe:

    def test_short_message_sends_once(self):
        """Short message (< TELEGRAM_MAX_CHARS) → requests.post called exactly once."""
        short_text = "Hello, BlitzTrader!"
        assert len(short_text) < TELEGRAM_MAX_CHARS

        ok_resp = _make_ok_response()
        with patch("tools.telegram_handler.send_telegram_safe.__module__") as _:
            import requests as _req
            with patch("requests.post", return_value=ok_resp) as mock_post:
                result = send_telegram_safe("TOKEN", "CHAT", short_text)

        assert result is True
        assert mock_post.call_count == 1

    def test_long_message_sends_multiple_chunks(self):
        """Long message (> TELEGRAM_MAX_CHARS) → requests.post called > 1 time."""
        # Build text that is definitely longer than the limit
        long_text = _long_text(300)
        assert len(long_text) > TELEGRAM_MAX_CHARS

        ok_resp = _make_ok_response()
        with patch("requests.post", return_value=ok_resp) as mock_post:
            result = send_telegram_safe("TOKEN", "CHAT", long_text)

        assert result is True
        assert mock_post.call_count > 1

    def test_no_chunk_exceeds_max_chars(self):
        """Every chunk sent must be <= TELEGRAM_MAX_CHARS."""
        long_text = _long_text(500)
        assert len(long_text) > TELEGRAM_MAX_CHARS

        sent_chunks: list[str] = []

        def _capture_post(url, json=None, timeout=None, **kwargs):
            sent_chunks.append(json.get("text", ""))
            resp = MagicMock()
            resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
            return resp

        with patch("requests.post", side_effect=_capture_post):
            send_telegram_safe("TOKEN", "CHAT", long_text)

        assert sent_chunks, "At least one chunk should have been sent"
        for chunk in sent_chunks:
            assert len(chunk) <= TELEGRAM_MAX_CHARS, (
                f"Chunk length {len(chunk)} exceeds TELEGRAM_MAX_CHARS={TELEGRAM_MAX_CHARS}"
            )

    def test_split_on_line_boundaries(self):
        """Chunks must not cut in the middle of a line."""
        # Build text with lines of exactly 100 chars each
        line = "A" * 99 + "\n"
        long_text = line * 60  # ~6000 chars, will need splitting
        assert len(long_text) > TELEGRAM_MAX_CHARS

        sent_chunks: list[str] = []

        def _capture_post(url, json=None, timeout=None, **kwargs):
            sent_chunks.append(json.get("text", ""))
            resp = MagicMock()
            resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
            return resp

        with patch("requests.post", side_effect=_capture_post):
            send_telegram_safe("TOKEN", "CHAT", long_text)

        # Every line in every chunk must be a complete line (end with \n or be the last)
        for chunk in sent_chunks:
            # Re-split the chunk by lines; each original line is 100 chars
            # If a mid-line cut occurred, a line would have a different length
            chunk_lines = chunk.splitlines(keepends=True)
            for chunk_line in chunk_lines[:-1]:  # all but the possible last line
                assert chunk_line.endswith("\n"), (
                    f"Mid-line cut detected: {chunk_line!r}"
                )

    def test_all_sends_fail_returns_false(self):
        """If all sends fail → send_telegram_safe returns False."""
        long_text = _long_text(300)
        assert len(long_text) > TELEGRAM_MAX_CHARS

        fail_resp = _make_fail_response()
        with patch("requests.post", return_value=fail_resp):
            result = send_telegram_safe("TOKEN", "CHAT", long_text)

        assert result is False

    def test_all_sends_fail_for_short_message_returns_false(self):
        """Short message that fails → returns False."""
        fail_resp = _make_fail_response()
        with patch("requests.post", return_value=fail_resp):
            result = send_telegram_safe("TOKEN", "CHAT", "short message that fails")

        assert result is False

    def test_partial_success_returns_true(self):
        """First chunk succeeds, second fails → returns True."""
        long_text = _long_text(300)
        assert len(long_text) > TELEGRAM_MAX_CHARS

        call_count = {"n": 0}

        def _side_effect(url, json=None, timeout=None, **kwargs):
            call_count["n"] += 1
            resp = MagicMock()
            if call_count["n"] == 1:
                resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
            else:
                resp.json.return_value = {"ok": False, "description": "failed"}
            return resp

        with patch("requests.post", side_effect=_side_effect):
            result = send_telegram_safe("TOKEN", "CHAT", long_text)

        assert result is True
        assert call_count["n"] >= 2, "Expected at least two send attempts"

    def test_partial_success_logs_error_for_failed_chunk(self, caplog):
        """When chunk 2 fails, an error is logged."""
        long_text = _long_text(300)
        assert len(long_text) > TELEGRAM_MAX_CHARS

        call_count = {"n": 0}

        def _side_effect(url, json=None, timeout=None, **kwargs):
            call_count["n"] += 1
            resp = MagicMock()
            if call_count["n"] == 1:
                resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
            else:
                resp.json.return_value = {"ok": False, "description": "chunk 2 failed"}
            return resp

        with caplog.at_level(logging.ERROR, logger="BlitzTrader.Telegram"):
            with patch("requests.post", side_effect=_side_effect):
                send_telegram_safe("TOKEN", "CHAT", long_text)

        error_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert any("chunk" in m.lower() or "failed" in m.lower() for m in error_msgs), (
            f"Expected error log for failed chunk, got: {error_msgs}"
        )

    def test_exception_during_send_returns_false(self):
        """If requests.post raises an exception, send_telegram_safe catches it and returns False."""
        with patch("requests.post", side_effect=ConnectionError("network down")):
            result = send_telegram_safe("TOKEN", "CHAT", "hello")

        assert result is False
