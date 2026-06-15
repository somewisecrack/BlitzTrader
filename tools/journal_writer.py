"""
tools/journal_writer.py — Trading journal for BlitzTrader.

Appends structured markdown entries to journals/YYYYMMDD.md.
Claude calls log_decision() after every action — including skips and holds.
"""
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import pytz

logger = logging.getLogger("BlitzTrader.Journal")

IST = pytz.timezone("Asia/Kolkata")


class JournalWriter:
    """
    Writes structured markdown journal entries.
    One file per trading day: journals/YYYYMMDD.md
    """

    def __init__(self, journals_dir: Path, virtual_capital: float = 500_000, state_manager=None):
        self._dir = journals_dir
        self._dir.mkdir(exist_ok=True)
        self._capital = virtual_capital
        self._state_manager = state_manager
        self._current_file: Path = None
        self._initialized = False

    def _get_journal_path(self) -> Path:
        """Get today's journal file path."""
        today = datetime.now(IST).strftime("%Y%m%d")
        return self._dir / f"{today}.md"

    def _ensure_header(self):
        """Create the journal file with header if it doesn't exist."""
        path = self._get_journal_path()
        if path.exists() and self._initialized:
            return path

        if not path.exists():
            today_str = datetime.now(IST).strftime("%d %b %Y")
            header = f"""# BlitzTrader Daily Journal — {today_str}

## Session Summary
- **Start Capital:** ₹{self._capital:,.0f}
- **End Capital:** —
- **Net P&L:** —
- **Total Trades:** 0
- **Win Rate:** —

---

## Decision Log

"""
            path.write_text(header, encoding="utf-8")
            logger.info(f"Created journal: {path}")

        self._current_file = path
        self._initialized = True
        return path

    # ──────────────────────────────────────────────────────────
    #   TOOL (callable by Claude)
    # ──────────────────────────────────────────────────────────

    def log_decision(
        self,
        action: str,
        symbol: str = "",
        reason: str = "",
        strategy_applied: str = "",
        market_context_summary: str = "",
    ) -> dict:
        """
        Log a trading decision to today's journal.

        :param action: ENTER_LONG, ENTER_SHORT, EXIT, HOLD, SKIP, STOP, etc.
        :param symbol: Instrument symbol (empty for HOLD/SKIP)
        :param reason: Claude's detailed reasoning
        :param strategy_applied: Which strategy triggered this
        :param market_context_summary: Brief market state summary
        :returns: {status, journal_file}
        """
        path = self._ensure_header()
        now = datetime.now(IST)
        timestamp = now.strftime("%H:%M:%S")

        entry = f"### {timestamp} — {action.upper()}\n"

        if market_context_summary:
            entry += f"**Market context:** {market_context_summary}\n"

        if symbol:
            entry += f"**Instrument:** {symbol}\n"

        if strategy_applied:
            entry += f"**Strategy applied:** {strategy_applied}\n"

        if reason:
            entry += f"**Reasoning:** {reason}\n"

        # Auto-append ground truth for EOD/STOP/summary entries
        if action.upper() in ("EOD", "EOD_SUMMARY", "STOP", "ABORT", "END") and self._state_manager:
            state = self._state_manager.get_state()
            trades = state.get("trades", [])
            spreads = state.get("spreads_traded", [])
            pnl = state.get("daily_pnl", 0)
            trade_count = len(trades) + len(spreads)
            entry += (
                f"\n**[SYSTEM VERIFIED DATA — auto-injected, not from agent]**\n"
                f"- Actual trades executed: {trade_count}\n"
                f"- Futures trades: {len(trades)}\n"
                f"- Option spreads: {len(spreads)}\n"
                f"- Actual P&L: ₹{pnl:+,.2f}\n"
            )
            if trades:
                for t in trades:
                    entry += f"- Trade: {t.get('direction', '?')} {t.get('symbol', '?')} | Entry: ₹{t.get('entry_price', 0):.2f} | Exit: ₹{t.get('exit_price', 0):.2f} | P&L: ₹{t.get('pnl', 0):+,.2f}\n"
            if spreads:
                for spread in spreads:
                    entry += (
                        f"- Spread: {spread.get('symbol', '?')} "
                        f"{spread.get('spread_type', '?')} | "
                        f"Strategy: {spread.get('strategy', '—')} | "
                        f"P&L: ₹{spread.get('realized_pnl', 0):+,.2f} | "
                        f"Status: {'closed' if spread.get('closed') else 'open'}\n"
                    )
            if not trades and not spreads:
                entry += f"- No trades were executed this session.\n"

        entry += "\n"

        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(entry)
            logger.info(f"Journal: {action} {symbol or '(no symbol)'}")
            return {
                "status": "logged",
                "journal_file": str(path),
                "timestamp": timestamp,
            }
        except OSError as e:
            logger.error(f"Failed to write journal: {e}")
            return {"error": str(e)}

    def update_session_summary(
        self,
        end_capital: float,
        net_pnl: float,
        total_trades: int,
        wins: int,
    ) -> dict:
        """
        Update the session summary at top of journal (called at EOD).
        """
        path = self._get_journal_path()
        if not path.exists():
            return {"error": "No journal file for today"}

        try:
            content = path.read_text(encoding="utf-8")

            win_rate = f"{wins / total_trades * 100:.0f}%" if total_trades > 0 else "N/A"

            summary_values = {
                "End Capital": f"₹{end_capital:,.0f}",
                "Net P&L": (
                    f"₹{net_pnl:+,.0f} "
                    f"({net_pnl / self._capital * 100:+.2f}%)"
                ),
                "Total Trades": str(total_trades),
                "Win Rate": f"{win_rate} ({wins}/{total_trades})",
            }
            for label, value in summary_values.items():
                content = re.sub(
                    rf"(?m)^- \*\*{re.escape(label)}:\*\* .*$",
                    f"- **{label}:** {value}",
                    content,
                    count=1,
                )

            path.write_text(content, encoding="utf-8")
            logger.info("Updated session summary in journal")
            return {"status": "updated"}

        except OSError as e:
            return {"error": str(e)}
