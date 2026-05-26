"""
scripts/blitztrader_agent.py — Always-on Telegram Q&A agent.

Runs continuously on the VM (24/7, weekends included) so the user can ask
questions about past sessions, strategies, wiki, and portfolio state at any time.

Coordination with the trading service (main.py):
  - main.py writes /tmp/blitztrader_trading.pid when it's alive and polling Telegram
  - This script yields Telegram polling to main.py when that PID file is active
  - When main.py exits (EOD or weekend), this script resumes Q&A polling
  - No shared state or IPC — PID file is the only coordination point

Responsibilities:
  - Respond to free-form Telegram questions using Gemini
  - Never place trades, never manage positions
  - Read-only access to journals, wiki, strategy docs, memory, goals

Start automatically on VM boot:
  sudo cp blitztrader-agent.service /etc/systemd/system/
  sudo systemctl enable --now blitztrader-agent.service
"""
import logging
import os
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
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_AUTHORIZED_USER_ID,
    setup_logging,
)

# ── PID coordination ────────────────────────────────────────────────────────
TRADING_PID_FILE = Path("/tmp/blitztrader_trading.pid")
AGENT_PID_FILE = Path("/tmp/blitztrader_agent.pid")

POLL_INTERVAL_SECONDS = 5           # how often to check Telegram when active
YIELD_CHECK_INTERVAL_SECONDS = 10   # how often to check if trader is still alive

logger = logging.getLogger("BlitzTrader.Agent")


def _is_trading_service_active() -> bool:
    """
    Return True if the main trading service is alive and polling Telegram.

    Checks the PID file written by main.py. Returns False if the file is absent
    or the PID it references is no longer running.
    """
    if not TRADING_PID_FILE.exists():
        return False
    try:
        pid = int(TRADING_PID_FILE.read_text().strip())
        # os.kill(pid, 0) raises OSError if pid doesn't exist
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

    state = StateManager(state_file=Path(JOURNALS_DIR).parent / "live_state.json")
    journal = JournalWriter(journals_dir=Path(JOURNALS_DIR))
    strategy = StrategyReader(strategy_file=Path(MASTER_STRATEGY_FILE))
    memory = MemoryReader(memory_file=Path(MEMORY_FILE))
    goals = GoalManager(state_manager=state)

    registry = ToolRegistry(
        state_manager=state,
        order_execution=None,     # read-only — no order tools
        telegram_handler=telegram_handler,
        market_data=None,
        journal=journal,
        strategy=strategy,
        memory=memory,
        goals=goals,
        shoonya_client=None,
        active_tokens={},
        live_feed=None,
    )
    return registry


class BlitzTraderAgent:
    """
    Always-on Telegram Q&A agent for BlitzTrader.

    Polls Telegram for messages and answers using Gemini.
    Yields polling to main.py while trading is active.
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
        """Send one message to Gemini and deliver the response via Telegram."""
        try:
            from context_builder import build_chat_context
            from tools.state_manager import StateManager
            state = StateManager(
                state_file=Path(JOURNALS_DIR).parent / "live_state.json"
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

                        # Ignore trading commands — those are for the live trader
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
