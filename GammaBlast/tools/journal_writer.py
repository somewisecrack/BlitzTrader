"""
GammaBlast/tools/journal_writer.py — Daily markdown journal for GammaBlast.

Appends structured markdown entries to journals/YYYYMMDD.md (IST date).
The journal is a human-readable record of scanner events and virtual trades —
it never influences scanning or position logic.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytz

logger = logging.getLogger("GammaBlast.JournalWriter")

IST = pytz.timezone("Asia/Kolkata")


class JournalWriter:
    """
    Writes structured markdown journal entries for GammaBlast.

    One file per trading day: <journals_dir>/YYYYMMDD.md  (IST date)

    Header written on first creation:
        # GammaBlast Daily Journal — DD Mon YYYY
        Virtual capital: ₹1,00,000
        ## Events

    Subsequent calls to log_event() and write_eod_summary() append to the
    same file.  All methods are fail-silent — OSError is logged, never raised.
    """

    def __init__(self, journals_dir: Path, virtual_capital: float = 100_000) -> None:
        self._dir = Path(journals_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._capital = virtual_capital
        # Track which date-files have already had their header written so we
        # don't re-write the header on every call within the same session.
        self._header_written: set[str] = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _journal_path(self) -> Path:
        """Return today's journal file path (IST date)."""
        today = datetime.now(IST).strftime("%Y%m%d")
        return self._dir / f"{today}.md"

    def _ensure_header(self) -> Path:
        """Create the journal file with the GammaBlast header if it doesn't exist."""
        path = self._journal_path()
        date_key = path.name

        if date_key in self._header_written and path.exists():
            return path

        if not path.exists():
            today_str = datetime.now(IST).strftime("%d %b %Y")
            # Format virtual_capital with Indian comma grouping (e.g. ₹1,00,000)
            cap_str = _format_inr(self._capital)
            header = (
                f"# GammaBlast Daily Journal — {today_str}\n"
                f"Virtual capital: ₹{cap_str}\n"
                "## Events\n"
            )
            try:
                path.write_text(header, encoding="utf-8")
                logger.info("Created journal: %s", path)
            except OSError:
                logger.exception("JournalWriter: failed to create journal file %s", path)
                return path

        self._header_written.add(date_key)
        return path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_event(
        self,
        action: str,
        symbol: str = "",
        reason: str = "",
        details: str = "",
    ) -> None:
        """
        Append a timestamped markdown bullet to today's journal file.

        Args:
            action:  Short verb describing the event, e.g. "COILED_DETECTED",
                     "VIRTUAL_ENTRY", "EOD_FORCE_CLOSE".
            symbol:  Instrument token or display name (optional).
            reason:  Human-readable reason for the event (optional).
            details: Additional detail string — appended inline (optional).
        """
        try:
            path = self._ensure_header()
            now = datetime.now(IST)
            timestamp = now.strftime("%H:%M:%S")

            parts: list[str] = [f"**[{timestamp}]** `{action.upper()}`"]
            if symbol:
                parts.append(f"— {symbol}")
            if reason:
                parts.append(f"| {reason}")
            if details:
                parts.append(f"| {details}")

            line = "- " + " ".join(parts) + "\n"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            logger.info("Journal event: %s %s", action, symbol or "(no symbol)")
        except OSError:
            logger.exception("JournalWriter.log_event failed for action=%s", action)

    def write_eod_summary(
        self,
        pnl: float,
        positions_closed: int,
        candidates_seen: int,
    ) -> None:
        """
        Append an EOD summary section to today's journal file.

        Args:
            pnl:              Net virtual P&L for the day (positive = profit).
            positions_closed: Number of virtual positions closed during the session.
            candidates_seen:  Total number of candidates that entered the watchlist.
        """
        try:
            path = self._ensure_header()
            now = datetime.now(IST)
            timestamp = now.strftime("%H:%M:%S")

            pnl_sign = "+" if pnl >= 0 else ""
            summary = (
                "\n## EOD Summary\n"
                f"- **Time:** {timestamp} IST\n"
                f"- **Virtual P&L:** ₹{pnl_sign}{pnl:,.2f}\n"
                f"- **Positions closed:** {positions_closed}\n"
                f"- **Candidates seen:** {candidates_seen}\n"
            )
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(summary)
            logger.info(
                "Journal EOD summary written: pnl=%.2f positions=%d candidates=%d",
                pnl,
                positions_closed,
                candidates_seen,
            )
        except OSError:
            logger.exception("JournalWriter.write_eod_summary failed")


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _format_inr(amount: float) -> str:
    """
    Format a rupee amount with Indian comma grouping.
    e.g. 100000 → "1,00,000"  and  500000 → "5,00,000"
    """
    # Split integer and decimal parts
    int_part = int(amount)
    dec_part = round((amount - int_part) * 100)

    s = str(int_part)
    if len(s) <= 3:
        result = s
    else:
        # Last 3 digits, then groups of 2
        result = s[-3:]
        s = s[:-3]
        while s:
            result = s[-2:] + "," + result
            s = s[:-2]

    if dec_part:
        return f"{result}.{dec_part:02d}"
    return result
