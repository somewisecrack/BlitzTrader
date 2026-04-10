"""
main.py — BlitzTrader session lifecycle orchestrator.

This is the entry point. It manages the full trading day:
  9:00 AM  → Login, init agent, read memory, load strategies, set goals
  9:15 AM  → Start event-driven agentic loop
  3:15 PM  → Force EOD sequence, update memory
  3:25 PM  → Cleanup and exit

The LLM (Mixtral via Groq) is the brain. This script is the skeleton.
Two types of agent iterations:
  - Scheduled: every 60 seconds for market analysis
  - Chat:      immediately on any Telegram message
"""
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    DATA_EXPORTS_DIR,
    GOOGLE_DRIVE_UPLOAD_DIR,
    JOURNALS_DIR,
    LOGS_DIR,
    LOOP_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
    MASTER_STRATEGY_FILE,
    MAX_DAILY_LOSS_AMOUNT,
    MAX_POSITIONS,
    MAX_RISK_PCT,
    MEMORY_FILE,
    NO_NEW_ENTRY_AFTER,
    NSE_TOKENS,
    RCLONE_FOLDER,
    RCLONE_REMOTE,
    SHOONYA_API_KEY,
    SHOONYA_AUTH_CODE,
    SHOONYA_IMEI,
    SHOONYA_PASSWORD,
    SHOONYA_SECRET_CODE,
    SHOONYA_TOTP_SECRET,
    SHOONYA_USER_ID,
    SHOONYA_VENDOR_CODE,
    STATE_FILE,
    STRATEGIES_DIR,
    TELEGRAM_AUTHORIZED_USER_ID,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_POLL_INTERVAL_SECONDS,
    VIRTUAL_CAPITAL,
    setup_logging,
)
from broker.shoonya_client import ShoonyaClient
from broker.live_feed import LiveFeedManager
from tools.state_manager import StateManager
from tools.virtual_ledger import VirtualLedger
from tools.market_data import MarketDataTools
from tools.data_recorder import DataRecorder
from tools.order_execution import OrderExecutionTools
from tools.telegram_handler import TelegramHandler
from tools.journal_writer import JournalWriter
from tools.strategy_reader import StrategyReader
from tools.memory_reader import MemoryReader
from tools.goal_manager import GoalManager
from tools.registry import ToolRegistry
from agent_loop import AgentLoop
from context_builder import (
    SYSTEM_PROMPT,
    build_abort_context,
    build_chat_context,
    build_eod_context,
    build_iteration_context,
    build_startup_context,
)

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger("BlitzTrader.Main")


class BlitzTrader:
    """
    Main session orchestrator.
    """

    def __init__(self):
        self._running = False
        self._shoonya = None
        self._feed = None
        self._telegram = None
        self._agent = None
        self._state = None
        self._order_exec = None
        self._market_data = None
        self._journal = None
        self._goals = None
        self._data_recorder = None
        self._active_tokens = None

    def run(self):
        """Run the full trading session."""
        setup_logging()
        logger.info("=" * 60)
        logger.info("  BlitzTrader — Starting Session")
        logger.info("=" * 60)

        # Register signal handler for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            self._initialize()
            try:
                self._startup_phase()
            except Exception:
                logger.exception("Startup phase failed — continuing to trading loop anyway")
                self._telegram.send_telegram("⚠️ Startup incomplete due to API error. Entering trading loop anyway.")
            self._trading_loop()
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception:
            logger.exception("Fatal error in main loop")
        finally:
            self._shutdown()

    # ──────────────────────────────────────────────────────────
    #   INITIALIZATION
    # ──────────────────────────────────────────────────────────

    def _initialize(self):
        """Initialize all components."""
        logger.info("Initializing components...")

        # 0. Start Telegram FIRST so we can notify user during login retries
        self._telegram = TelegramHandler(TELEGRAM_BOT_TOKEN, TELEGRAM_AUTHORIZED_USER_ID)
        self._telegram.start()
        logger.info("✓ Telegram handler started")

        self._telegram.send_telegram(
            "🤖 BlitzTrader starting up...\nWaiting for Shoonya API to come online."
        )

        # 1. Shoonya login (retry until success)
        self._shoonya = ShoonyaClient()
        max_retries = 100
        retry_interval = 300  # 5 minutes between retries

        for attempt in range(1, max_retries + 1):
            success, msg = self._shoonya.login(
                user_id=SHOONYA_USER_ID,
                password=SHOONYA_PASSWORD,
                totp_secret=SHOONYA_TOTP_SECRET,
                api_key=SHOONYA_API_KEY,
                vendor_code=SHOONYA_VENDOR_CODE,
                imei=SHOONYA_IMEI,
                secret_code=SHOONYA_SECRET_CODE,
                auth_code=SHOONYA_AUTH_CODE,
            )
            if success:
                break

            now_ist = datetime.now(IST).strftime("%H:%M:%S")
            logger.warning(
                f"Shoonya login attempt {attempt}/{max_retries} failed: {msg}. "
                f"Retrying in {retry_interval}s..."
            )

            if attempt % 5 == 0:
                self._telegram.send_telegram(
                    f"⏳ Shoonya API still offline (attempt {attempt}). "
                    f"Time: {now_ist} IST. Will keep retrying..."
                )

            commands = self._telegram.get_pending_commands()
            for cmd in commands:
                if cmd["command"] == "/abort":
                    raise RuntimeError("Aborted by user during login retry")

            time.sleep(retry_interval)
        else:
            self._telegram.send_telegram(
                "❌ BlitzTrader: Shoonya login failed after all retries. Session aborted."
            )
            raise RuntimeError(f"Shoonya login failed after {max_retries} attempts")

        logger.info("✓ Shoonya login successful")
        self._telegram.send_telegram("✅ Shoonya login successful! Initializing agent...")

        # 2. Data recorder and live feed (WebSocket) with health alerts
        self._data_recorder = DataRecorder(
            base_dir=DATA_EXPORTS_DIR,
            nse_tokens=NSE_TOKENS,
            google_drive_upload_dir=GOOGLE_DRIVE_UPLOAD_DIR,
            rclone_remote=RCLONE_REMOTE,
            rclone_folder=RCLONE_FOLDER,
        )
        logger.info(f"✓ Data recorder initialized at {self._data_recorder.day_dir}")

        def on_feed_health_alert(msg: str):
            logger.warning(msg)
            self._telegram.send_telegram(msg)

        def on_tick(token, quote):
            if self._data_recorder:
                self._data_recorder.record_feed_tick(token, quote)
            if not self._order_exec:
                return
            self._order_exec.check_pending_limit_orders(token)
            # Deterministic SL/target enforcement — fires on every tick
            auto_closed = self._order_exec.check_sl_target()
            for ac in auto_closed:
                logger.info(
                    f"AUTO-CLOSE [{ac['auto_close_reason']}]: "
                    f"{ac['symbol']} P&L ₹{ac.get('pnl', 0):+,.2f}"
                )

        self._feed = LiveFeedManager(
            self._shoonya,
            on_tick_callback=on_tick,
            on_health_alert=on_feed_health_alert,
        )
        self._feed.start()

        # Resolve front-month futures for NIFTY and BANKNIFTY so we get real
        # volume data.  Fall back to index tokens if futures lookup fails.
        active_tokens = dict(NSE_TOKENS)  # starts with VIX index token
        for sym in ("NIFTY", "BANKNIFTY"):
            fut = self._shoonya.get_front_month_futures_token(sym)
            if fut:
                active_tokens[sym] = {
                    "exchange": fut["exchange"],
                    "token":    fut["token"],
                    "tsym":     fut["tsym"],
                    "expiry":   fut["expiry"],
                }
                logger.info(
                    f"✓ {sym} → futures {fut['tsym']} "
                    f"(token {fut['token']}, expiry {fut['expiry']})"
                )
            else:
                logger.warning(f"Futures lookup failed for {sym} — using index token")

        # Store resolved tokens so the iteration context can surface them to the agent.
        self._active_tokens = active_tokens

        subscribe_tokens = [
            (info["exchange"], info["token"])
            for info in active_tokens.values()
        ]
        self._feed.subscribe(subscribe_tokens)
        logger.info(f"✓ WebSocket feed started, subscribed to {len(subscribe_tokens)} tokens")

        # Update recorder token map now that futures tsyms are resolved
        # (DataRecorder was created before futures lookup — update it now)
        self._data_recorder.update_token_map(active_tokens)

        # 3. State manager
        self._state = StateManager(STATE_FILE, VIRTUAL_CAPITAL)
        self._state.init_state()
        logger.info("✓ State initialized")

        # 4. Virtual ledger
        ledger = VirtualLedger()

        # 5. Market data tools
        market_data = MarketDataTools(
            self._shoonya,
            self._feed,
            active_tokens,
            data_recorder=self._data_recorder,
        )
        self._market_data = market_data

        # 6. Order execution tools (pass active_tokens so futures tsym resolves correctly)
        self._order_exec = OrderExecutionTools(
            state_manager=self._state,
            virtual_ledger=ledger,
            live_feed=self._feed,
            shoonya_client=self._shoonya,
            max_positions=MAX_POSITIONS,
            max_risk_amount=VIRTUAL_CAPITAL * MAX_RISK_PCT,
            max_daily_loss=MAX_DAILY_LOSS_AMOUNT,
            no_entry_after=NO_NEW_ENTRY_AFTER,
            active_tokens=active_tokens,
        )

        # 7. Journal (with state_manager for ground-truth injection)
        journal = JournalWriter(JOURNALS_DIR, VIRTUAL_CAPITAL, state_manager=self._state)
        self._journal = journal

        # 8. Memory reader (with state_manager for ground-truth injection)
        memory = MemoryReader(JOURNALS_DIR, MEMORY_FILE, state_manager=self._state)

        # Inject state_manager into Telegram for ground-truth footers
        self._telegram._state_manager = self._state
        logger.info("✓ Memory reader initialized")

        # 9. Goal manager
        self._goals = GoalManager()

        # 10. Strategy reader
        strategy = StrategyReader(MASTER_STRATEGY_FILE, STRATEGIES_DIR)

        # 11. Tool registry
        registry = ToolRegistry(
            market_data=market_data,
            order_execution=self._order_exec,
            telegram=self._telegram,
            journal=journal,
            strategy_reader=strategy,
            memory_reader=memory,
            goal_manager=self._goals,
        )

        # 12. Agent loop
        self._agent = AgentLoop(
            api_key=GEMINI_API_KEY,
            model=GEMINI_MODEL,
            tool_registry=registry,
            system_prompt=SYSTEM_PROMPT,
        )
        logger.info("✓ Agent loop initialized")
        logger.info("All components initialized successfully")

    # ──────────────────────────────────────────────────────────
    #   STARTUP PHASE (9:00 - 9:15 AM)
    # ──────────────────────────────────────────────────────────

    def _startup_phase(self):
        """
        Pre-market startup:
        - Mixtral reads memory and past journals
        - Mixtral reads today's strategies
        - Mixtral sets session goals
        - Mixtral sends Telegram summary
        """
        logger.info("=== STARTUP PHASE ===")

        startup_context = build_startup_context()
        response = self._agent.run_iteration(startup_context)
        logger.info(f"Startup response: {(response or '')[:200]}...")

        self._telegram.send_telegram("🚀 Startup complete. Moving to autonomous trading loop.")
        logger.info("✓ Startup complete — trading will begin")
        self._running = True

    # ──────────────────────────────────────────────────────────
    #   TRADING LOOP (9:15 AM - 3:15 PM)
    # ──────────────────────────────────────────────────────────

    def _trading_loop(self):
        """
        Event-driven agentic loop.

        Three types of activity:
          - Background scanner: pure-Python get_strategy_signals() every 60 s — no LLM cost.
          - Signal-triggered LLM: fires immediately when the scanner finds a new signal.
          - Scheduled LLM: full market context check every 5 minutes regardless of signals.
          - Chat: immediate LLM response to Telegram messages (within 3 seconds).

        Runs until market close or abort.
        """
        logger.info("=== TRADING LOOP STARTED ===")
        iteration = 0
        consecutive_errors = 0
        last_scheduled_at = None   # time of last LLM iteration (scheduled or signal-triggered)
        last_scan_at = None        # time of last background Python scanner run
        pending_signals: list = [] # signals queued for next LLM iteration
        last_feed_health_alert = None  # avoid spam
        feed_disconnect_start = None  # track how long feed is disconnected

        while self._running:
            now = datetime.now(IST)

            # ── Check EOD ──
            eod_time = now.replace(hour=15, minute=15, second=0, microsecond=0)
            if now >= eod_time:
                logger.info("=== EOD SEQUENCE ===")
                eod_context = build_eod_context()
                self._agent.run_iteration(eod_context)
                if self._feed:
                    self._feed.stop()
                self._upload_data_export()
                self._running = False
                return

            # ── Monitor feed health (log only, never send to Telegram) ──
            if not self._feed.is_connected:
                if feed_disconnect_start is None:
                    feed_disconnect_start = now
                disconnect_duration = (now - feed_disconnect_start).total_seconds()
                if disconnect_duration > 30:
                    if last_feed_health_alert is None or (now - last_feed_health_alert).total_seconds() > 300:
                        logger.warning(f"WebSocket feed disconnected for {disconnect_duration:.0f}s")
                        last_feed_health_alert = now
            else:
                if feed_disconnect_start is not None:
                    logger.info("✓ WebSocket feed reconnected")
                feed_disconnect_start = None
                last_feed_health_alert = None

            # ── Drain Telegram queue (ALWAYS — even pre-market) ──
            commands = self._telegram.get_pending_commands()
            abort_cmds = [c for c in commands if c["command"] == "/abort"]
            chat_msgs  = [c for c in commands if not c["command"]]  # free-form text
            other_cmds = [c for c in commands if c["command"] and c["command"] != "/abort"]

            # ── Handle abort (highest priority, any time) ──
            if abort_cmds:
                logger.warning("ABORT received!")
                abort_context = build_abort_context()
                self._agent.run_iteration(abort_context)
                self._running = False
                return

            # Re-queue /pause, /resume, /status etc. for next scheduled iteration
            for cmd in other_cmds:
                self._telegram._command_queue.append(cmd)

            # ── Chat iteration: respond immediately at ANY time of day ──
            if chat_msgs:
                logger.info(f"Chat message received — responding immediately")
                try:
                    context = build_chat_context(
                        chat_messages=chat_msgs,
                        state_manager=self._state,
                        order_execution=self._order_exec,
                    )
                    self._agent.run_iteration(context)
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    logger.exception("Error in chat iteration")
                    self._telegram.send_telegram(
                        f"⚠️ Error responding to your message: {str(e)[:200]}"
                    )

            # ── Wait for market open (only blocks scheduled analysis, not chat) ──
            market_open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
            if now < market_open_time:
                wait_secs = (market_open_time - now).total_seconds()
                # Log at most once per minute to avoid spam
                if (not hasattr(self, '_last_wait_log')
                        or (now - self._last_wait_log).total_seconds() >= 60):
                    logger.info(f"Waiting {wait_secs:.0f}s for market open...")
                    self._last_wait_log = now
                time.sleep(min(wait_secs, TELEGRAM_POLL_INTERVAL_SECONDS))
                continue

            # ── Notify once when market opens ──
            if not getattr(self, '_market_open_notified', False):
                self._telegram.send_telegram(
                    "Market is now open. BlitzTrader is active and scanning for setups. "
                    "All systems ready."
                )
                logger.info("Market open — Telegram notified")
                self._market_open_notified = True

            # ── Background scanner: pure-Python, no LLM, every 60 seconds ──
            elif (
                last_scan_at is None
                or (now - last_scan_at).total_seconds() >= SCAN_INTERVAL_SECONDS
            ):
                last_scan_at = now
                try:
                    scan_result = self._market_data.get_strategy_signals()
                    new_sigs = scan_result.get("signals", [])
                    if new_sigs:
                        pending_signals.extend(new_sigs)
                        logger.info(
                            f"Scanner: {len(new_sigs)} new signal(s) → "
                            f"{[s['strategy'] + ' ' + s['direction'] for s in new_sigs]}"
                        )
                    if scan_result.get("notes"):
                        logger.debug(f"Scanner notes: {scan_result['notes']}")
                except Exception:
                    logger.exception("Background scanner error (non-fatal)")

            # ── LLM iteration: signal-triggered OR every 5 minutes ──
            due_for_scheduled_llm = (
                last_scheduled_at is None
                or (now - last_scheduled_at).total_seconds() >= LOOP_INTERVAL_SECONDS
            )
            due_for_signal_llm = bool(pending_signals) and (
                # Don't fire signal-triggered LLM in the same 3-second tick as the
                # scanner; scheduled baseline analysis should not be blocked by this.
                last_scan_at is None or (now - last_scan_at).total_seconds() >= 1
            )
            if due_for_signal_llm or due_for_scheduled_llm:
                iteration += 1
                trigger = "signals" if pending_signals else "scheduled"
                logger.info(
                    f"--- Iteration {iteration} [{trigger}] "
                    f"({now.strftime('%H:%M:%S')} IST) "
                    f"pending_signals={len(pending_signals)} ---"
                )
                # Update timer BEFORE running — prevents re-entry if iteration is slow.
                # Signals are preserved even if Gemini fails (cleared only on success below).
                last_scheduled_at = now

                try:
                    context = build_iteration_context(
                        state_manager=self._state,
                        telegram_handler=self._telegram,
                        order_execution=self._order_exec,
                        goal_manager=self._goals,
                        pending_signals=pending_signals if pending_signals else None,
                        active_tokens=self._active_tokens,
                    )
                    # Clear only after context is built and iteration succeeds.
                    # Captured here so that on failure the signals remain in
                    # pending_signals and will be included in the next iteration.
                    signals_this_iter = list(pending_signals)
                    response = self._agent.run_iteration(context)
                    pending_signals.clear()  # success — safe to discard
                    logger.info(f"Iteration {iteration} response: {(response or '')[:200]}...")

                    if iteration % 10 == 0:
                        usage = self._agent.get_token_usage()
                        logger.info(f"Token usage: {usage}")

                    consecutive_errors = 0
                    self._agent.reset()

                except Exception as e:
                    consecutive_errors += 1
                    err_msg = str(e)
                    logger.exception(f"Error in iteration {iteration}")
                    # pending_signals was NOT cleared — signals survive for retry

                    self._telegram.send_telegram(
                        f"⚠️ SYSTEM ERROR (Attempt {consecutive_errors}/3):\n{err_msg[:200]}"
                    )

                    if consecutive_errors >= 3:
                        logger.critical("Maximum consecutive errors reached. Auto-killing session.")
                        self._telegram.send_telegram(
                            "🛑 CRITICAL FAILURE: Agent crashed 3 times in a row. "
                            "Force-closing all positions and aborting session."
                        )
                        try:
                            remaining = self._state.get_open_positions()
                            if remaining:
                                self._telegram.send_telegram(
                                    f"Closing {len(remaining)} positions automatically."
                                )
                                self._order_exec.close_all_positions()
                        except Exception as close_err:
                            self._telegram.send_telegram(
                                f"Failed to close positions! Error: {close_err}"
                            )
                        self._running = False
                        return

            # ── Check daily loss limit ──
            state = self._state.get_state()
            if state.get("is_stopped"):
                logger.warning("Trading stopped — daily loss limit hit")
                self._telegram.send_telegram(
                    "🛑 BlitzTrader STOPPED: Daily loss limit hit. "
                    "All positions closed."
                )
                self._running = False
                return

            # ── Short sleep — stay responsive to Telegram ──
            time.sleep(TELEGRAM_POLL_INTERVAL_SECONDS)

    # ──────────────────────────────────────────────────────────
    #   SHUTDOWN
    # ──────────────────────────────────────────────────────────

    def _shutdown(self):
        """Clean shutdown of all components."""
        logger.info("=== SHUTTING DOWN ===")

        # Force close any remaining positions
        try:
            if self._order_exec:
                remaining = self._state.get_open_positions()
                if remaining:
                    logger.warning(f"Force closing {len(remaining)} remaining positions")
                    self._order_exec.close_all_positions()
        except Exception:
            logger.exception("Error closing positions during shutdown")

        # Log final state and update journal summary
        if self._state:
            pnl, pnl_pct = self._state.get_daily_pnl()
            trades = self._state.get_trades()
            logger.info(f"Final P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)")
            logger.info(f"Total trades: {len(trades)}")

            if self._journal:
                wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
                end_capital = VIRTUAL_CAPITAL + pnl
                self._journal.update_session_summary(
                    end_capital=end_capital,
                    net_pnl=pnl,
                    total_trades=len(trades),
                    wins=wins,
                )

        if self._agent:
            usage = self._agent.get_token_usage()
            logger.info(f"Total token usage: {usage}")

        # Stop feed before exporting so CSV files are no longer being appended.
        if self._feed:
            self._feed.stop()

        self._upload_data_export()

        # Stop notification channel last so export status can be sent.
        if self._telegram:
            self._telegram.stop()

        logger.info("BlitzTrader session ended")

    def _upload_data_export(self):
        """Upload today's feed/indicator CSV exports once."""
        if not self._data_recorder:
            return
        now = datetime.now(IST)
        eod_time = now.replace(hour=15, minute=15, second=0, microsecond=0)
        if now < eod_time:
            logger.info("Skipping data export upload before 15:15 IST")
            return
        try:
            result = self._data_recorder.finalize_and_upload()
            logger.info(f"Data export result: {result}")
            if self._telegram and result.get("status") == "uploaded":
                self._telegram.send_telegram(
                    f"📊 Data CSV export uploaded to Google Drive.\n"
                    f"Destination: {result.get('destination')}"
                )
            elif self._telegram and result.get("status") == "no_destination_configured":
                self._telegram.send_telegram(
                    "⚠️ Data CSV export saved locally but not uploaded. "
                    "Set GOOGLE_DRIVE_UPLOAD_DIR or RCLONE_REMOTE."
                )
        except Exception as e:
            logger.exception("Failed to upload data export")
            if self._telegram:
                self._telegram.send_telegram(f"⚠️ Failed to upload data CSV export: {str(e)[:200]}")

    def _signal_handler(self, signum, frame):
        """Handle SIGINT/SIGTERM gracefully."""
        logger.info(f"Received signal {signum}, shutting down...")
        self._running = False


def main():
    """Entry point."""
    trader = BlitzTrader()
    trader.run()


if __name__ == "__main__":
    main()
