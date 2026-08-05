"""
scripts/blitztrader_agent.py — Always-on Telegram Q&A agent.

Runs continuously on the VM (24/7, weekends included) so the user can ask
questions about past sessions, strategies, wiki, and portfolio state at any time.

Coordination with the trading service (main.py / pair-credit script):
  - Any live trading service writes /tmp/blitztrader_trading.pid when alive and
    polling Telegram.  It must remove the file on clean exit.
  - This script yields Telegram polling to the trading service when that PID is active.
  - When the trading service exits (EOD, weekend, or crash), this script resumes Q&A.
  - Stale PID files (process gone) are detected via os.kill(pid, 0) and ignored.
  - No shared state or IPC — the PID file is the only coordination point.

Responsibilities:
  - Respond to free-form Telegram questions using Gemini
  - Handle deterministic status/pnl queries from state file (no Gemini needed)
  - Reject exit/abort commands with a clear "trading inactive" message
  - Never place trades, never manage positions

Start automatically on VM boot:
  sudo cp blitztrader-agent.service /etc/systemd/system/
  sudo systemctl enable --now blitztrader-agent.service
"""
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (
    GEMINI_API_KEY,
    GEMINI_SCHEDULED_MODEL,
    GEMINI_MAX_SCHEDULED_TOKENS,
    JOURNALS_DIR,
    LOGS_DIR,
    MEMORY_FILE,
    MASTER_STRATEGY_FILE,
    STRATEGIES_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_AUTHORIZED_USER_ID,
    setup_logging,
)

# ── PID coordination ────────────────────────────────────────────────────────
TRADING_PID_FILE = Path("/tmp/blitztrader_trading.pid")
AGENT_PID_FILE = Path("/tmp/blitztrader_agent.pid")

# State file written by main.py (and the pair-credit service if applicable)
_LIVE_STATE_FILE = JOURNALS_DIR.parent / "live_state.json"

POLL_INTERVAL_SECONDS = 5           # how often to check Telegram when active
YIELD_CHECK_INTERVAL_SECONDS = 10   # how often to check if trader is still alive

logger = logging.getLogger("BlitzTrader.Agent")

# Exit-command patterns — same set as main.py._try_answer_simple_chat
_EXIT_PATTERNS = [
    r'\bexit\s+#?\d+\b',
    r'\bclose\s+(?:position|spread|pair|serial)\s+#?\d+\b',
    r'\bclose\s+#?\d+\b',
    r'\bsquare\s+off\s+#?\d+\b',
    r'\bexit\s+pair\b',
    r'\bclose\s+all\b',
    r'\bsquare\s+off\s+all\b',
]

# Status-query keywords
_STATUS_KEYWORDS = frozenset(
    {"pnl", "p&l", "profit", "loss", "position", "positions", "status", "balance", "capital"}
)


def _is_trading_service_active() -> bool:
    """
    Return True if the main trading service is alive and polling Telegram.

    Checks the PID file written by main.py (or any other live trading service).
    Returns False if the file is absent or the PID it references is no longer
    running (stale file).
    """
    if not TRADING_PID_FILE.exists():
        return False
    try:
        pid = int(TRADING_PID_FILE.read_text().strip())
        # os.kill(pid, 0) raises OSError if the process doesn't exist or we
        # lack permission; both mean the file is stale.
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def _write_agent_pid():
    """Write this process's PID so the trading service can detect us."""
    AGENT_PID_FILE.write_text(str(os.getpid()))


def _remove_agent_pid():
    """Clean up PID file on exit."""
    try:
        AGENT_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _build_agent_system_prompt() -> str:
    return """\
You are BlitzTrader's always-on research assistant. The markets are closed (or you
are operating outside of trading hours). Answer questions about:
  - Past session journals and trade performance
  - Strategy logic and rules (from strategy docs)
  - Wiki hypotheses, backtest results, promoted filters
  - Session goals and memory

You have access to: read_journals, get_strategy_docs, get_session_goals, send_telegram,
get_past_journals, update_memory.

Invariants (NEVER violate):
  - You cannot place trades, modify positions, or issue orders. The market is closed.
  - You cannot access live prices (no broker connection when not trading).
  - All answers must be grounded in data you read — do not speculate.
  - Use send_telegram() to deliver your response to the user.

Keep responses concise and relevant. If you cannot answer from available data, say so.
"""


def _build_minimal_registry(telegram_handler, agent_loop):
    """
    Build a minimal read-only tool registry for the Q&A agent.
    Only includes read/respond tools — no order execution tools.
    """
    from tools.registry import ToolRegistry
    from tools.state_manager import StateManager
    from tools.journal_writer import JournalWriter
    from tools.strategy_reader import StrategyReader
    from tools.memory_reader import MemoryReader
    from tools.goal_manager import GoalManager

    state = StateManager(state_file=JOURNALS_DIR.parent / "live_state.json")
    journal = JournalWriter(journals_dir=JOURNALS_DIR)
    strategy = StrategyReader(master_file=MASTER_STRATEGY_FILE, strategies_dir=STRATEGIES_DIR)
    memory = MemoryReader(journals_dir=JOURNALS_DIR, memory_file=MEMORY_FILE)
    goals = GoalManager()

    registry = ToolRegistry(
        market_data=None,
        order_execution=None,     # read-only — no order tools
        telegram=telegram_handler,
        journal=journal,
        strategy_reader=strategy,
        memory_reader=memory,
        goal_manager=goals,
        live_feed=None,
        shoonya_client=None,
        active_tokens={},
    )
    return registry


def _is_exit_command(text: str) -> bool:
    """
    Return True if text looks like a position-exit command.

    These must never be routed to Gemini when the trading service is inactive —
    Gemini has no broker connection and cannot execute or simulate exits.
    """
    t = text.lower()
    return any(re.search(p, t) for p in _EXIT_PATTERNS)


def _try_readonly_status(text: str) -> str | None:
    """
    If text is a status/pnl query, try to read the last-known state from disk
    and return a formatted reply string.  Returns None if not a status query
    or if the state file cannot be read.
    """
    t = text.lower()
    if not any(kw in t for kw in _STATUS_KEYWORDS):
        return None

    try:
        if not _LIVE_STATE_FILE.exists():
            return (
                "📊 No live state file found — trading service has not run yet today.\n"
                "Start BlitzTrader to begin recording state."
            )
        state = json.loads(_LIVE_STATE_FILE.read_text())
        pnl = float(state.get("daily_pnl", 0) or 0)
        pnl_pct = float(state.get("daily_pnl_pct", 0) or 0)
        open_spreads = state.get("open_spreads") or []
        positions = state.get("positions") or []

        lines = ["📊 Last known session state (trading service inactive):"]
        lines.append(f"Session P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)")

        if open_spreads:
            lines.append(f"Open spreads: {len(open_spreads)}")
            for s in open_spreads[:5]:
                sid = s.get("spread_id", "?")
                sym = s.get("symbol", "?")
                stype = s.get("spread_type", "?")
                lines.append(f"  • {sid} — {sym} {stype}")
            if len(open_spreads) > 5:
                lines.append(f"  … and {len(open_spreads) - 5} more")
        elif positions:
            lines.append(f"Open futures: {len(positions)}")
        else:
            lines.append("No open positions.")

        return "\n".join(lines)
    except Exception:
        logger.exception("Could not read state file for Q&A status reply")
        return None


class BlitzTraderAgent:
    """
    Always-on Telegram Q&A agent for BlitzTrader.

    Polls Telegram for messages and answers using Gemini.
    Yields polling to main.py (or any live trading service) while trading is active.
    """

    def __init__(self):
        self._running = True
        self._telegram = None
        self._agent = None
        self._telegram_started = False

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        logger.info("Shutdown signal received — stopping Q&A agent")
        self._running = False
        if self._telegram_started and self._telegram:
            self._telegram.stop()
            self._telegram_started = False
        _remove_agent_pid()

    def _initialize(self):
        from tools.telegram_handler import TelegramHandler
        from agent_loop import AgentLoop

        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_AUTHORIZED_USER_ID:
            logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_AUTHORIZED_USER_ID not configured")
            sys.exit(1)

        if not GEMINI_API_KEY:
            logger.error("GEMINI_API_KEY not configured")
            sys.exit(1)

        self._telegram = TelegramHandler(
            bot_token=TELEGRAM_BOT_TOKEN,
            authorized_user_id=TELEGRAM_AUTHORIZED_USER_ID,
        )
        # Do NOT start the handler here — lazy start in the main loop
        # to avoid dual-polling when the trading service is also active.

        self._agent = AgentLoop(
            api_key=GEMINI_API_KEY,
            model=GEMINI_SCHEDULED_MODEL,
            tool_registry=_build_minimal_registry(self._telegram, None),
            system_prompt=_build_agent_system_prompt(),
            max_tool_rounds=6,
            max_tokens=GEMINI_MAX_SCHEDULED_TOKENS,
        )

        logger.info(
            "BlitzTrader Q&A agent initialized (pid=%d). Trading PID file: %s",
            os.getpid(), TRADING_PID_FILE,
        )

    def _answer_message(self, message_text: str) -> None:
        """
        Handle one incoming Telegram message.

        Routing priority:
          1. Exit/close commands → reject with clear 'not active' message (never Gemini)
          2. Status/pnl queries → serve from state file (deterministic, no Gemini)
          3. Everything else → route to Gemini Q&A
        """
        # 1. Exit commands — never routable to Gemini; no broker connection
        if _is_exit_command(message_text):
            self._telegram.send_telegram(
                "⛔ No live trading session is active — cannot execute exit commands.\n"
                "Start the trading service to manage open positions."
            )
            return

        # 2. Status/pnl — try to serve deterministically from last-known state
        status_reply = _try_readonly_status(message_text)
        if status_reply is not None:
            self._telegram.send_telegram(status_reply)
            return

        # 3. Free-form question — route to Gemini
        try:
            from context_builder import build_chat_context
            from tools.state_manager import StateManager
            state = StateManager(
                state_file=JOURNALS_DIR.parent / "live_state.json"
            )
            context = build_chat_context(
                chat_messages=[{"command": "", "text": message_text, "from_user": "user"}],
                state_manager=state,
                order_execution=None,
            )
            final_text = self._agent.run_iteration(
                context,
                model=GEMINI_SCHEDULED_MODEL,
                max_tokens=GEMINI_MAX_SCHEDULED_TOKENS,
                max_tool_rounds=6,
            )
            # Deliver final_text if Gemini didn't call send_telegram
            if final_text and not self._agent.was_send_telegram_called():
                self._telegram.send_telegram(final_text)
        except Exception:
            logger.exception("Error answering message: %r", message_text[:80])
            self._telegram.send_telegram(
                "⚠️ Error processing your request. Please try again."
            )

    def run(self):
        self._setup_signal_handlers()
        setup_logging()
        _write_agent_pid()

        try:
            self._initialize()
        except Exception:
            logger.exception("Failed to initialize Q&A agent")
            _remove_agent_pid()
            sys.exit(1)

        while self._running:
            trading_active = _is_trading_service_active()

            # ── Manage Telegram handler ownership ───────────────────────────
            if trading_active and self._telegram_started:
                self._telegram.stop()
                self._telegram_started = False
                logger.info("Yielding Telegram to trading service")
            elif not trading_active and not self._telegram_started:
                self._telegram.start()
                self._telegram_started = True
                logger.info("Starting Telegram polling (trading service inactive)")
                self._telegram.send_telegram(
                    "📖 BlitzTrader Q&A agent is online. "
                    "Ask me anything about past sessions, strategies, or results."
                )

            # ── Process messages when we own the handler ─────────────────────
            if self._telegram_started:
                try:
                    commands = self._telegram.get_pending_commands()
                    for cmd in commands:
                        text = cmd.get("text", "").strip()
                        if not text:
                            continue
                        command = cmd.get("command", "")

                        # Slash-only trading commands — only active during live trading
                        if command in {"/abort", "/pause", "/resume"}:
                            self._telegram.send_telegram(
                                f"⚠️ Command {command!r} is only active during live trading hours. "
                                "The trading service is not currently running."
                            )
                            continue

                        logger.info("Q&A message: %r", text[:80])
                        self._answer_message(text)

                except Exception:
                    logger.exception("Error in Q&A polling loop (non-fatal)")

                time.sleep(POLL_INTERVAL_SECONDS)
            else:
                # Trading service is active — yield and check again later
                time.sleep(YIELD_CHECK_INTERVAL_SECONDS)

        logger.info("BlitzTrader Q&A agent stopped")


if __name__ == "__main__":
    BlitzTraderAgent().run()
