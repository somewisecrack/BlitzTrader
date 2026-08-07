"""
tools/telegram_handler.py — Telegram send + command listener for BlitzTrader.

send_telegram(message) — tool for Claude
TelegramCommandListener — async polling for user commands (/confirm, /abort, etc.)
"""
import html
import logging
import re
import threading
import time
from typing import Optional, Callable
from collections import deque

logger = logging.getLogger("BlitzTrader.Telegram")

TELEGRAM_MAX_CHARS = 3500  # conservative limit below Telegram's 4096-char cap


def send_telegram_safe(bot_token: str, chat_id: str, text: str) -> bool:
    """
    Send text to Telegram, splitting on line boundaries if > TELEGRAM_MAX_CHARS.

    Returns True if at least one chunk was sent successfully, False otherwise.
    Does NOT raise; all exceptions are caught and logged.
    """
    import requests as _requests

    def _post(chunk: str) -> bool:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = _requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                return True
            logger.error("Telegram send failed: %s", data.get("description", "Unknown error"))
            return False
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False

    if len(text) <= TELEGRAM_MAX_CHARS:
        return _post(text)

    # Split on line boundaries — never cut mid-line
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) > TELEGRAM_MAX_CHARS:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)

    sent_any = False
    for i, chunk in enumerate(chunks, 1):
        ok = _post(chunk)
        if ok:
            sent_any = True
        else:
            logger.error("Telegram send failed chunk %d/%d", i, len(chunks))
    return sent_any


class TelegramHandler:
    """
    Handles Telegram messaging (send + receive).
    Runs command listener in a background thread.
    """

    def __init__(self, bot_token: str, authorized_user_id: str, state_manager=None):
        self._bot_token = bot_token
        self._user_id = authorized_user_id
        # Retained as an optional argument for existing callers.  The transport
        # must never alter a message with state from a different trading mode.
        del state_manager
        self._bot = None
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False

        # Command queue — commands received from Telegram, consumed by agent loop
        self._command_queue: deque[dict] = deque(maxlen=50)

        # Valid commands
        self._valid_commands = {
            "/confirm", "/abort", "/status", "/pause", "/resume",
        }

    def start(self):
        """Initialize bot and start command listener."""
        if not self._bot_token or not self._user_id:
            logger.warning("Telegram not configured (missing token or user_id)")
            return

        self._running = True
        self._listener_thread = threading.Thread(
            target=self._listener_loop,
            name="BlitzTrader-Telegram",
            daemon=True,
        )
        self._listener_thread.start()
        logger.info("Telegram handler started")

    def stop(self):
        """Stop the command listener."""
        self._running = False
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=10)
        logger.info("Telegram handler stopped")

    # ──────────────────────────────────────────────────────────
    #   SEND (tool callable by Claude)
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _format_message(message: str) -> str:
        """
        Normalise Claude's message for Telegram HTML mode.

        Claude often writes literal \\n escape sequences and **markdown** bold.
        This converts them to proper HTML so Telegram renders them correctly.
        """
        # 1. Literal \n → real newline (Claude writes escape sequences as text)
        message = message.replace('\\n', '\n')

        # 2. Escape HTML special chars BEFORE adding any tags
        message = html.escape(message)

        # 3. **bold** → <b>bold</b>  (multi-line safe)
        message = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', message, flags=re.DOTALL)

        # 4. *italic* → <i>italic</i>  (single asterisk, not already converted)
        message = re.sub(r'\*([^*\n]+?)\*', r'<i>\1</i>', message)

        # 5. __underline__ → <u>underline</u>
        message = re.sub(r'__(.+?)__', r'<u>\1</u>', message, flags=re.DOTALL)

        return message

    def send_telegram(self, message: str) -> dict:
        """
        Send a message to the authorized Telegram user.

        Long messages (> TELEGRAM_MAX_CHARS) are automatically split on line
        boundaries so Telegram's 4096-char limit is never exceeded.

        :param message: Text message to send (supports **bold**, *italic*, \\n)
        :returns: {status: "sent"} on success, {error: ...} on failure
        """
        if not self._bot_token or not self._user_id:
            logger.warning("Telegram not configured, message not sent")
            return {"status": "skipped", "reason": "Telegram not configured"}

        formatted = self._format_message(message)
        ok = send_telegram_safe(self._bot_token, self._user_id, formatted)
        if ok:
            logger.info("Telegram message sent (len=%d)", len(formatted))
            return {"status": "sent"}
        else:
            logger.error("Telegram send failed (all chunks failed)")
            return {"error": "send failed"}

    # ──────────────────────────────────────────────────────────
    #   COMMAND LISTENER (background thread)
    # ──────────────────────────────────────────────────────────

    def _listener_loop(self):
        """Poll Telegram for incoming commands from authorized user."""
        import requests

        offset = None
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"

        while self._running:
            try:
                params = {"timeout": 10, "allowed_updates": ["message"]}
                if offset:
                    params["offset"] = offset

                resp = requests.get(url, params=params, timeout=15)
                data = resp.json()

                if not data.get("ok"):
                    time.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    msg = update.get("message", {})
                    from_user = str(msg.get("from", {}).get("id", ""))
                    text = msg.get("text", "").strip().lower()

                    # Only process from authorized user
                    if from_user != str(self._user_id):
                        logger.warning(f"Ignored message from unauthorized user ID: '{from_user}'. Text: '{text}'")
                        continue

                    # Accept all messages to enable free-form chat
                    self._command_queue.append({
                        "command": text.split()[0] if text and text.startswith("/") else "",
                        "text": text,
                        "timestamp": time.time(),
                    })
                    logger.info(f"Telegram message received: {text[:50]}")

            except Exception:
                logger.exception("Error in Telegram listener")
                time.sleep(10)

    # ──────────────────────────────────────────────────────────
    #   COMMAND QUEUE (consumed by agent loop)
    # ──────────────────────────────────────────────────────────

    def get_pending_commands(self) -> list[dict]:
        """Get all pending commands and clear the queue."""
        commands = list(self._command_queue)
        self._command_queue.clear()
        return commands

    def wait_for_command(self, command: str, timeout: float = 600) -> bool:
        """
        Block until a specific command is received, or timeout.

        :param command: Command to wait for (e.g., '/confirm')
        :param timeout: Max seconds to wait
        :returns: True if command received, False if timeout
        """
        start = time.time()
        while time.time() - start < timeout:
            commands = self.get_pending_commands()
            for cmd in commands:
                if cmd["command"] == command:
                    return True
            time.sleep(1)
        return False

    def inject_command(self, command: str, context: str = "") -> str:
        """
        Convert a Telegram command into a context message for the agent.

        :param command: The command received
        :returns: Message to inject into agent context
        """
        injections = {
            "/confirm": "User confirmed. Begin trading.",
            "/abort": (
                "ABORT commanded by user. Close all positions immediately "
                "and shut down. This overrides everything."
            ),
            "/status": (
                "User requested status. Call get_open_positions() and get_daily_pnl() "
                "and respond with current state via send_telegram()."
            ),
            "/pause": (
                "User has paused new entries. Monitor existing positions only. "
                "Do not open new trades until /resume is received."
            ),
            "/resume": (
                "User has resumed. You may open new trades per strategy rules."
            ),
        }
        
        # If it's a known command, inject the predefined instructions
        if command in injections:
            return injections[command]
            
        # Otherwise, pass the raw text to the agent
        return context
