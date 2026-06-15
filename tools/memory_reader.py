"""
tools/memory_reader.py — Cross-session persistent memory for BlitzTrader.

Allows Claude to remember key learnings, patterns, and decisions across
trading sessions. Two types of memory:
  1. memory.md — a persistent file Claude owns and rewrites with lessons
  2. Recent journals — last 7 trading days, auto-loaded at startup
"""
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytz

logger = logging.getLogger("BlitzTrader.MemoryReader")
IST = pytz.timezone("Asia/Kolkata")

# Max chars loaded from journals — protects context window
MAX_JOURNAL_CHARS = 30_000
MAX_SINGLE_JOURNAL_CHARS = 8_000
MAX_MEMORY_CHARS = 20_000


class MemoryReader:
    """
    Manages cross-session memory for BlitzTrader.
    Claude reads at session start, writes lessons at EOD.
    """

    def __init__(self, journals_dir: Path, memory_file: Path, state_manager=None):
        self._journals_dir = journals_dir
        self._memory_file = memory_file
        self._state_manager = state_manager
        memory_file.parent.mkdir(exist_ok=True)

    # ──────────────────────────────────────────────────────────
    #   TOOLS (callable by Claude)
    # ──────────────────────────────────────────────────────────

    def get_past_journals(self) -> dict:
        """
        Read persistent memory and last 7 days of trading journals.
        Call this at session start to learn from past performance.

        :returns: {memory_content, recent_journals: [{date, content}], total_chars}
        """
        memory_content = self._read_memory()
        recent_journals = self._read_recent_journals(days=7)

        total_chars = len(memory_content) + sum(
            len(j["content"]) for j in recent_journals
        )

        logger.info(
            f"Loaded memory ({len(memory_content)} chars) + "
            f"{len(recent_journals)} journals ({total_chars} total chars)"
        )

        return {
            "memory_content": memory_content,
            "recent_journals": recent_journals,
            "journal_count": len(recent_journals),
            "total_chars": total_chars,
        }

    def update_memory(self, content: str) -> dict:
        """
        Overwrite the persistent memory file with new insights and lessons.
        Call this at EOD after reflecting on today's session. You own this
        file entirely — rewrite, consolidate, and refine as you see fit.

        :param content: Your updated memory — lessons, rules, patterns to remember
        :returns: {status, chars_saved}
        """
        content = content.strip()
        if not content:
            return {"error": "Content cannot be empty."}

        tmp_path = self._memory_file.with_suffix(".md.tmp")
        now = datetime.now(IST)

        # Build ground truth footer from state_manager (immutable, LLM cannot override)
        ground_truth = ""
        if self._state_manager:
            state = self._state_manager.get_state()
            trades = state.get("trades", [])
            spreads = state.get("spreads_traded", [])
            pnl = state.get("daily_pnl", 0)
            trade_count = len(trades) + len(spreads)
            trade_details = [
                {k: t[k] for k in ("symbol", "direction", "pnl") if k in t}
                for t in trades
            ]
            spread_details = [
                {
                    k: spread[k]
                    for k in ("symbol", "spread_type", "strategy", "realized_pnl")
                    if k in spread
                }
                for spread in spreads
            ]
            ground_truth = (
                f"\n\n---\n"
                f"## VERIFIED SESSION DATA (auto-generated, not editable by agent)\n"
                f"- Date: {now.strftime('%Y-%m-%d')}\n"
                f"- Actual trades executed: {trade_count}\n"
                f"- Futures trades: {len(trades)}\n"
                f"- Option spreads: {len(spreads)}\n"
                f"- Actual P&L: ₹{pnl:+,.2f}\n"
                f"- Futures trade details: {trade_details or 'None'}\n"
                f"- Option spread details: {spread_details or 'None'}\n"
            )

        full_content = (
            f"# BlitzTrader Persistent Memory\n"
            f"Last updated: {now.strftime('%Y-%m-%d %H:%M IST')}\n\n"
            f"{content}"
            f"{ground_truth}"
        )

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(full_content)
            os.replace(tmp_path, self._memory_file)
            logger.info(f"Memory updated: {len(full_content)} chars")
            return {
                "status": "saved",
                "chars_saved": len(full_content),
                "timestamp": now.strftime("%Y-%m-%d %H:%M IST"),
            }
        except OSError as e:
            logger.error(f"Failed to write memory: {e}")
            return {"error": str(e)}

    # ──────────────────────────────────────────────────────────
    #   INTERNAL HELPERS
    # ──────────────────────────────────────────────────────────

    def _read_memory(self) -> str:
        """Read the persistent memory file."""
        if not self._memory_file.exists():
            return "No persistent memory yet. This is your first session."
        try:
            content = self._memory_file.read_text(encoding="utf-8").strip()
            if not content:
                return "Memory file exists but contains no entries yet."
            if len(content) > MAX_MEMORY_CHARS:
                content = "[...older memory truncated...]\n\n" + content[-MAX_MEMORY_CHARS:]
            return content
        except OSError as e:
            return f"Could not read memory file: {e}"

    def _read_recent_journals(self, days: int = 7) -> list[dict]:
        """Load last N days of completed journal files (excludes today)."""
        if not self._journals_dir.exists():
            return []

        journals = []
        today = datetime.now(IST).date()
        total_chars = 0

        for i in range(1, days + 1):
            date = today - timedelta(days=i)
            path = self._journals_dir / f"{date.strftime('%Y%m%d')}.md"

            if not path.exists():
                continue

            try:
                content = path.read_text(encoding="utf-8").strip()
                if not content:
                    continue

                # Truncate individual journals
                if len(content) > MAX_SINGLE_JOURNAL_CHARS:
                    content = content[:MAX_SINGLE_JOURNAL_CHARS] + "\n...[truncated]"

                # Stop if we've hit the total budget
                if total_chars + len(content) > MAX_JOURNAL_CHARS:
                    break

                journals.append({
                    "date": date.strftime("%d %b %Y (%A)"),
                    "content": content,
                })
                total_chars += len(content)

            except OSError:
                logger.exception(f"Failed to read journal {path}")

        return journals
