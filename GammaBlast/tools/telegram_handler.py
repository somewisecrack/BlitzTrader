"""
GammaBlast/tools/telegram_handler.py — Telegram sender for GammaBlast.

Reads credentials from environment variables:
    GAMMABLAST_TELEGRAM_BOT_TOKEN
    GAMMABLAST_TELEGRAM_CHAT_ID

Never reads BlitzTrader environment variables.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("GammaBlast.Telegram")

TELEGRAM_MAX_CHARS = 3500  # conservative limit below Telegram's 4096-char cap


# ---------------------------------------------------------------------------
# Module-level send helper
# ---------------------------------------------------------------------------

def send_telegram_safe(bot_token: str, chat_id: str, text: str) -> bool:
    """
    Send *text* to the given Telegram chat, splitting on line boundaries
    if the message exceeds TELEGRAM_MAX_CHARS characters.

    Uses sendMessage with parse_mode HTML.

    Returns:
        True  — at least one chunk was delivered successfully.
        False — every chunk failed (or text was empty).

    Never raises; all exceptions are caught and logged.
    """
    import requests as _requests

    if not text:
        logger.warning("send_telegram_safe: empty text, nothing to send")
        return False

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
            logger.error(
                "GammaBlast.Telegram send failed: %s",
                data.get("description", "unknown error"),
            )
            return False
        except Exception as exc:
            logger.error("GammaBlast.Telegram send exception: %s", exc)
            return False

    if len(text) <= TELEGRAM_MAX_CHARS:
        return _post(text)

    # Split on line boundaries — never cut a line in the middle
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
            logger.error(
                "GammaBlast.Telegram chunk %d/%d failed", i, len(chunks)
            )
    return sent_any


# ---------------------------------------------------------------------------
# TelegramHandler class
# ---------------------------------------------------------------------------

class TelegramHandler:
    """
    GammaBlast Telegram message sender.

    Credentials are resolved at construction time from the two environment
    variables GAMMABLAST_TELEGRAM_BOT_TOKEN and GAMMABLAST_TELEGRAM_CHAT_ID.
    Both must be non-empty for messages to be sent; missing credentials trigger
    a warning and every send() returns False.

    Usage:
        handler = TelegramHandler(bot_token=os.environ["GAMMABLAST_TELEGRAM_BOT_TOKEN"],
                                  chat_id=os.environ["GAMMABLAST_TELEGRAM_CHAT_ID"])
        handler.send("<b>Hello</b> from GammaBlast")
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        if not bot_token or not chat_id:
            logger.warning(
                "GammaBlast.Telegram: bot_token or chat_id is empty — "
                "messages will not be sent"
            )

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send(self, text: str) -> bool:
        """
        Send *text* via Telegram.

        Returns True if at least one chunk was delivered; False otherwise.
        Never raises.
        """
        if not self._bot_token or not self._chat_id:
            logger.warning("GammaBlast.Telegram: credentials not configured, skipping send")
            return False
        ok = send_telegram_safe(self._bot_token, self._chat_id, text)
        if ok:
            logger.info("GammaBlast.Telegram: message sent (len=%d)", len(text))
        else:
            logger.error("GammaBlast.Telegram: all chunks failed")
        return ok

    # ------------------------------------------------------------------
    # Structured message helpers
    # ------------------------------------------------------------------

    def send_startup(self, symbol: str, expiry: str) -> bool:
        """Send a startup notification for the GammaBlast scanner session."""
        text = (
            "<b>GammaBlast Scanner Started</b>\n"
            f"Symbol : <code>{symbol}</code>\n"
            f"Expiry : <code>{expiry}</code>\n"
            "Mode   : Virtual-only expiry-day scan"
        )
        return self.send(text)

    def send_login_result(self, success: bool, details: str = "") -> bool:
        """Send broker login result notification."""
        status = "Login successful" if success else "Login FAILED"
        icon = "✅" if success else "❌"
        text = f"{icon} <b>{status}</b>"
        if details:
            text += f"\n{details}"
        return self.send(text)

    def send_candidate_update(
        self,
        candidate_id: str,
        symbol: str,
        strike: int,
        option_type: str,
        status: str,
        confidence: float,
        reason: str = "",
    ) -> bool:
        """
        Send a candidate stage-transition notification.

        Args:
            candidate_id: Unique candidate identifier.
            symbol:       Underlying, e.g. "NIFTY".
            strike:       Strike price.
            option_type:  "CE" or "PE".
            status:       Stage name, e.g. "COILED_DETECTED".
            confidence:   Confidence score in [0.0, 1.0].
            reason:       Optional human-readable reason (shown for rejections).
        """
        text = (
            f"<b>Candidate Update — {status}</b>\n"
            f"ID         : <code>{candidate_id}</code>\n"
            f"Instrument : {symbol} {strike} {option_type}\n"
            f"Confidence : {confidence:.0%}"
        )
        if reason:
            text += f"\nReason     : {reason}"
        return self.send(text)

    def send_virtual_entry(
        self,
        symbol: str,
        strike: int,
        option_type: str,
        entry_price: float,
        lots: int,
    ) -> bool:
        """
        Send a virtual position entry notification.

        Args:
            symbol:      Underlying, e.g. "NIFTY".
            strike:      Strike price.
            option_type: "CE" or "PE".
            entry_price: Option premium at entry.
            lots:        Number of lots entered.
        """
        text = (
            "<b>Virtual Entry</b>\n"
            f"Instrument  : {symbol} {strike} {option_type}\n"
            f"Entry price : ₹{entry_price:,.2f}\n"
            f"Lots        : {lots}"
        )
        return self.send(text)

    def send_virtual_exit(
        self,
        symbol: str,
        strike: int,
        option_type: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        reason: str,
    ) -> bool:
        """
        Send a virtual position exit notification.

        Args:
            symbol:      Underlying, e.g. "NIFTY".
            strike:      Strike price.
            option_type: "CE" or "PE".
            entry_price: Option premium at entry.
            exit_price:  Option premium at exit.
            pnl:         Net virtual P&L for this position (positive = profit).
            reason:      Exit reason, e.g. "TARGET_HIT", "STOP_LOSS", "EOD_FORCE_CLOSE".
        """
        pnl_sign = "+" if pnl >= 0 else ""
        text = (
            "<b>Virtual Exit</b>\n"
            f"Instrument  : {symbol} {strike} {option_type}\n"
            f"Entry price : ₹{entry_price:,.2f}\n"
            f"Exit price  : ₹{exit_price:,.2f}\n"
            f"P&amp;L      : ₹{pnl_sign}{pnl:,.2f}\n"
            f"Reason      : {reason}"
        )
        return self.send(text)

    def send_eod_summary(
        self,
        symbol: str,
        positions_closed: int,
        total_pnl: float,
        candidates_seen: int,
    ) -> bool:
        """
        Send the end-of-day summary for the GammaBlast session.

        Args:
            symbol:           Underlying scanned today.
            positions_closed: Number of virtual positions closed.
            total_pnl:        Net virtual P&L for the day.
            candidates_seen:  Total candidates that entered the watchlist.
        """
        pnl_sign = "+" if total_pnl >= 0 else ""
        text = (
            "<b>GammaBlast EOD Summary</b>\n"
            f"Symbol            : {symbol}\n"
            f"Candidates seen   : {candidates_seen}\n"
            f"Positions closed  : {positions_closed}\n"
            f"Net virtual P&amp;L : ₹{pnl_sign}{total_pnl:,.2f}"
        )
        return self.send(text)
