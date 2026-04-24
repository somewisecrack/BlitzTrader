"""
main.py — BlitzTrader session lifecycle orchestrator.

This is the entry point. It manages the full trading day:
  9:00 AM  → Login, init agent, read memory, load strategies, set goals
  9:15 AM  → Start event-driven agentic loop
  3:15 PM  → Force EOD sequence, update memory
  3:25 PM  → Cleanup and exit

The LLM (Mixtral via Groq) is the brain. This script is the skeleton.
Two types of agent iterations:
  - Signal review: only when the Python scanner finds a trade candidate
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
    GEMINI_DECISION_MODEL,
    GEMINI_API_TIMEOUT_SECONDS,
    GEMINI_MAX_DECISION_TOKENS,
    GEMINI_MAX_SCHEDULED_TOKENS,
    GEMINI_SCHEDULED_MODEL,
    DATA_EXPORTS_DIR,
    GOOGLE_DRIVE_UPLOAD_DIR,
    JOURNALS_DIR,
    LOGS_DIR,
    MAX_PENDING_SIGNALS_PER_ITERATION,
    SCAN_INTERVAL_SECONDS,
    SIGNAL_LLM_COOLDOWN_SECONDS,
    MASTER_STRATEGY_FILE,
    MAX_DAILY_LOSS_AMOUNT,
    MAX_DAILY_TRADES,
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
    TRADE_SYMBOLS,
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
from tools.market_calendar import get_market_holiday_name, is_nse_trading_day
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
        self._llm_disabled_reason = None
        self._llm_disabled_notified = False

    def run(self):
        """Run the full trading session."""
        setup_logging()
        logger.info("=" * 60)
        logger.info("  BlitzTrader — Starting Session")
        logger.info("=" * 60)

        today = datetime.now(IST).date()
        if not is_nse_trading_day(today):
            holiday_name = get_market_holiday_name(today)
            reason = holiday_name or "weekend"
            logger.info(
                "NSE market closed today (%s): %s. "
                "Skipping Shoonya login, feed startup, Gemini, and trading loop.",
                today.isoformat(),
                reason,
            )
            return

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

        # Resolve front-month futures for traded instruments so we get real
        # volume data.  Fall back to index tokens only where we have a known
        # index token; FINNIFTY trades only if its futures contract resolves.
        active_tokens = dict(NSE_TOKENS)  # starts with VIX index token
        for sym in TRADE_SYMBOLS:
            fut = self._shoonya.get_front_month_futures_token(sym)
            if fut:
                active_tokens[sym] = {
                    "exchange": fut["exchange"],
                    "token":    fut["token"],
                    "tsym":     fut["tsym"],
                    "expiry":   fut["expiry"],
                }
                if fut.get("lot_size"):
                    active_tokens[sym]["lot_size"] = fut["lot_size"]
                logger.info(
                    f"✓ {sym} → futures {fut['tsym']} "
                    f"(token {fut['token']}, expiry {fut['expiry']}, "
                    f"lot_size {fut.get('lot_size', 'unknown')})"
                )
            elif sym not in active_tokens:
                logger.warning(f"Futures lookup failed for {sym} — skipping this instrument")
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
        existing_state = self._state.load_state() if STATE_FILE.exists() else None
        resume_today = False
        if existing_state:
            try:
                session_day = datetime.fromtimestamp(
                    float(existing_state.get("session_start", 0)), IST
                ).date()
                resume_today = session_day == datetime.now(IST).date()
            except Exception:
                resume_today = False
        if resume_today:
            logger.warning(
                "Resuming existing intraday state: session=%s positions=%d",
                existing_state.get("session_id", "?"),
                len(existing_state.get("positions", [])),
            )
        else:
            self._state.init_state()
        logger.info("✓ State initialized")

        # 4. Virtual ledger
        ledger_lot_sizes = {
            sym: info["lot_size"]
            for sym, info in active_tokens.items()
            if sym in TRADE_SYMBOLS and info.get("lot_size")
        }
        ledger = VirtualLedger(lot_sizes=ledger_lot_sizes)

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
            max_daily_trades=MAX_DAILY_TRADES,
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
            model=GEMINI_DECISION_MODEL,
            tool_registry=registry,
            system_prompt=SYSTEM_PROMPT,
            max_tokens=GEMINI_MAX_DECISION_TOKENS,
            api_timeout_seconds=GEMINI_API_TIMEOUT_SECONDS,
        )
        logger.info(
            "✓ Agent loop initialized (decision_model=%s, scheduled_model=%s, "
            "decision_tokens=%s, scheduled_tokens=%s)",
            GEMINI_DECISION_MODEL,
            GEMINI_SCHEDULED_MODEL,
            GEMINI_MAX_DECISION_TOKENS,
            GEMINI_MAX_SCHEDULED_TOKENS,
        )
        logger.info("All components initialized successfully")

    def _run_agent_iteration(
        self,
        context: str,
        model: str,
        max_tokens: int,
        max_tool_rounds: int = None,
        phase: str = "unknown",
    ) -> str:
        """
        Run Gemini with a hard circuit breaker for quota/billing failures.

        If the project hits a daily quota or monthly spending cap, retrying on
        every scanner signal makes Telegram look dead and hammers the API. Disable
        LLM calls for the session; deterministic feed recording, SL/target checks,
        and EOD close safety still run in Python.
        """
        if self._llm_disabled_reason:
            logger.warning(
                "Skipping Gemini phase=%s model=%s because LLM is disabled: %s",
                phase,
                model,
                self._llm_disabled_reason,
            )
            return ""

        response = self._agent.run_iteration(
            context,
            model=model,
            max_tokens=max_tokens,
            max_tool_rounds=max_tool_rounds,
        )
        err = self._agent.get_last_error() if self._agent else None
        if err and err.get("kind") in {
            "monthly_spending_cap",
            "daily_quota_exhausted",
            "api_timeout",
        }:
            self._llm_disabled_reason = err.get("kind")
            logger.critical(
                "Disabling Gemini for the rest of the session after %s during %s",
                self._llm_disabled_reason,
                phase,
            )
            if self._telegram and not self._llm_disabled_notified:
                self._telegram.send_telegram(
                    "⚠️ Gemini disabled for this session: "
                    f"{self._llm_disabled_reason}. The bot will keep recording feed data "
                    "and enforcing deterministic SL/target/EOD close, but it will not "
                    "open new LLM-approved trades or answer free-form chat until billing/quota is fixed."
                )
                self._llm_disabled_notified = True
        return response

    def _try_answer_simple_chat(self, chat_messages: list[dict]) -> bool:
        """
        Answer common operational questions without Gemini.

        This keeps Telegram useful even when Gemini is slow, capped, or disabled.
        """
        if not self._telegram or not self._state:
            return False
        text = " ".join((m.get("text") or "").lower() for m in chat_messages)
        wants_capital = any(
            phrase in text
            for phrase in (
                "capital",
                "balance",
                "available balance",
                "available funds",
                "margin available",
                "virtual capital",
            )
        )
        wants_status = any(
            word in text
            for word in ("pnl", "p&l", "profit", "loss", "position", "positions", "status")
        )
        if not wants_status and not wants_capital:
            return False

        state = self._state.get_state()
        pnl = float(state.get("daily_pnl", 0) or 0)
        pnl_pct = float(state.get("daily_pnl_pct", 0) or 0)
        capital = float(state.get("virtual_capital", 0) or 0)
        available_balance = float(state.get("available_balance", 0) or 0)
        margin_used = float(state.get("margin_used", 0) or 0)
        positions = self._order_exec.get_open_positions() if self._order_exec else {"positions": []}
        lines = []
        if wants_capital:
            lines.extend([
                f"Virtual capital: ₹{capital:,.2f}",
                f"Available balance: ₹{available_balance:,.2f}",
                f"Margin used: ₹{margin_used:,.2f}",
            ])
        if wants_status or not wants_capital:
            lines.extend([
                f"Current P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)",
                f"Open positions: {positions.get('count', 0)}",
            ])
        for pos in positions.get("positions", []):
            lines.append(
                f"- {pos.get('direction')} {pos.get('symbol')} "
                f"qty {pos.get('quantity')} | entry ₹{pos.get('entry_price')} | "
                f"LTP ₹{pos.get('current_price')} | uPnL ₹{pos.get('unrealized_pnl'):+,.2f}"
            )
        self._telegram.send_telegram("\n".join(lines))
        logger.info("Answered simple Telegram status/P&L chat without Gemini")
        return True

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
        response = self._run_agent_iteration(
            startup_context,
            model=GEMINI_DECISION_MODEL,
            max_tokens=GEMINI_MAX_DECISION_TOKENS,
            phase="startup",
        )
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
          - Signal-triggered LLM: Gemini approves/rejects scanner candidates.
          - Chat: immediate LLM response to Telegram messages (within 3 seconds).

        Runs until market close or abort.
        """
        logger.info("=== TRADING LOOP STARTED ===")
        iteration = 0
        consecutive_errors = 0
        last_signal_llm_at = None  # time of last signal-triggered LLM iteration
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
                close_result = self._order_exec.close_all_positions() if self._order_exec else {}
                logger.info("Deterministic EOD close result: %s", close_result)
                eod_context = build_eod_context()
                self._run_agent_iteration(
                    eod_context,
                    model=GEMINI_DECISION_MODEL,
                    max_tokens=GEMINI_MAX_DECISION_TOKENS,
                    phase="eod",
                )
                if self._telegram:
                    pnl, pnl_pct = self._state.get_daily_pnl()
                    self._telegram.send_telegram(
                        f"EOD complete. Deterministic close_all executed. "
                        f"Trades: {self._state.get_state().get('trade_count', 0)} | "
                        f"P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)."
                    )
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
                if self._order_exec:
                    close_result = self._order_exec.close_all_positions()
                    logger.info("Abort deterministic close result: %s", close_result)
                abort_context = build_abort_context()
                self._run_agent_iteration(
                    abort_context,
                    model=GEMINI_DECISION_MODEL,
                    max_tokens=GEMINI_MAX_DECISION_TOKENS,
                    phase="abort",
                )
                self._running = False
                return

            # Re-queue /pause, /resume, /status etc. for the next agent interaction.
            for cmd in other_cmds:
                self._telegram._command_queue.append(cmd)

            # ── Chat iteration: respond immediately at ANY time of day ──
            if chat_msgs:
                logger.info(f"Chat message received — responding immediately")
                try:
                    if self._try_answer_simple_chat(chat_msgs):
                        consecutive_errors = 0
                        continue
                    if self._llm_disabled_reason:
                        self._telegram.send_telegram(
                            "Gemini is currently unavailable "
                            f"({self._llm_disabled_reason}). Market scanner, "
                            "feed recording, and deterministic SL/target/EOD safety "
                            "continue, but I cannot reason or answer conversationally "
                            "until the Gemini billing/quota issue is fixed."
                        )
                        consecutive_errors = 0
                        continue
                    context = build_chat_context(
                        chat_messages=chat_msgs,
                        state_manager=self._state,
                        order_execution=self._order_exec,
                    )
                    self._run_agent_iteration(
                        context,
                        model=GEMINI_SCHEDULED_MODEL,
                        max_tokens=GEMINI_MAX_SCHEDULED_TOKENS,
                        max_tool_rounds=6,
                        phase="chat",
                    )
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
                        tradeable_sigs, blocked_sigs = self._filter_tradeable_signals(
                            new_sigs,
                            now,
                            existing_pending=pending_signals,
                        )
                        if blocked_sigs:
                            logger.info(
                                "Scanner: %d signal(s) blocked by hard guardrails: %s",
                                len(blocked_sigs),
                                [
                                    f"{s.get('symbol')} {s.get('strategy')}: {s.get('blocked_reason')}"
                                    for s in blocked_sigs
                                ],
                            )
                        pending_signals.extend(tradeable_sigs)
                        if len(pending_signals) > MAX_PENDING_SIGNALS_PER_ITERATION:
                            dropped = len(pending_signals) - MAX_PENDING_SIGNALS_PER_ITERATION
                            pending_signals = pending_signals[-MAX_PENDING_SIGNALS_PER_ITERATION:]
                            logger.warning(
                                "Scanner signal queue capped at %d; dropped %d older signal(s)",
                                MAX_PENDING_SIGNALS_PER_ITERATION,
                                dropped,
                            )
                        logger.info(
                            f"Scanner: {len(new_sigs)} new signal(s), "
                            f"{len(tradeable_sigs)} queued for Gemini review → "
                            f"{[s['strategy'] + ' ' + s['direction'] for s in tradeable_sigs]}"
                        )
                    if scan_result.get("notes"):
                        logger.debug(f"Scanner notes: {scan_result['notes']}")
                except Exception:
                    logger.exception("Background scanner error (non-fatal)")

            # ── LLM iteration: signal-triggered only ──
            due_for_signal_llm = bool(pending_signals) and (
                # Don't fire signal-triggered LLM in the same 3-second tick as the scanner.
                last_scan_at is None or (now - last_scan_at).total_seconds() >= 1
            ) and (
                last_signal_llm_at is None
                or (now - last_signal_llm_at).total_seconds() >= SIGNAL_LLM_COOLDOWN_SECONDS
            )
            if self._llm_disabled_reason and pending_signals:
                logger.warning(
                    "LLM disabled (%s); dropping %d pending signal(s) without Gemini.",
                    self._llm_disabled_reason,
                    len(pending_signals),
                )
                pending_signals.clear()

            if not self._llm_disabled_reason and due_for_signal_llm:
                iteration += 1
                trigger = "signals"
                logger.info(
                    f"--- Iteration {iteration} [{trigger}] "
                    f"({now.strftime('%H:%M:%S')} IST) "
                    f"pending_signals={len(pending_signals)} ---"
                )

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
                    model = GEMINI_DECISION_MODEL
                    max_tokens = GEMINI_MAX_DECISION_TOKENS
                    max_tool_rounds = 10
                    last_signal_llm_at = now

                    logger.info(
                        "Gemini route: trigger=%s model=%s max_tokens=%s",
                        trigger,
                        model,
                        max_tokens,
                    )
                    response = self._run_agent_iteration(
                        context,
                        model=model,
                        max_tokens=max_tokens,
                        max_tool_rounds=max_tool_rounds,
                        phase=trigger,
                    )
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

    def _filter_tradeable_signals(
        self,
        signals: list[dict],
        now: datetime,
        existing_pending: list[dict] | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """
        Apply only non-judgmental hard guardrails before spending a Gemini call.

        Python does not score setup quality here. It only filters candidates that
        cannot legally be traded regardless of Gemini's opinion: paused state,
        daily cap/loss, no-entry cutoff, max positions, unresolved futures, or
        no-pyramiding conflicts.
        """
        state = self._state.get_state() if self._state else {}
        positions = state.get("positions", []) or []
        pending_orders = state.get("pending_orders", []) or []
        trades = state.get("trades", []) or []

        daily_entries = len(trades) + len(positions) + len(pending_orders)
        blocked_reason = None
        if state.get("is_paused"):
            blocked_reason = "Trading paused by user command"
        elif state.get("is_stopped"):
            blocked_reason = "Trading stopped by daily-loss guardrail"
        elif float(state.get("daily_pnl", 0) or 0) <= -MAX_DAILY_LOSS_AMOUNT:
            blocked_reason = "Daily loss limit reached"
        elif daily_entries >= MAX_DAILY_TRADES:
            blocked_reason = f"Daily trade cap reached ({daily_entries}/{MAX_DAILY_TRADES})"
        elif len(positions) >= MAX_POSITIONS:
            blocked_reason = f"Maximum open positions reached ({len(positions)}/{MAX_POSITIONS})"
        else:
            cutoff_h, cutoff_m = map(int, NO_NEW_ENTRY_AFTER.split(":"))
            cutoff = now.replace(hour=cutoff_h, minute=cutoff_m, second=0, microsecond=0)
            if now >= cutoff:
                blocked_reason = f"No new entries after {NO_NEW_ENTRY_AFTER} IST"

        if blocked_reason:
            blocked = []
            for sig in signals:
                sig_copy = dict(sig)
                sig_copy["blocked_reason"] = blocked_reason
                blocked.append(sig_copy)
            return [], blocked

        occupied = {
            self._logical_instrument(pos.get("symbol", ""))
            for pos in positions
        }
        occupied.update(
            self._logical_instrument(order.get("symbol", ""))
            for order in pending_orders
        )
        occupied.update(
            self._logical_instrument(sig.get("symbol", ""))
            for sig in (existing_pending or [])
        )
        occupied.discard(None)

        tradeable = []
        blocked = []
        queued_instruments = set()
        for sig in signals:
            sig_copy = dict(sig)
            instrument = self._logical_instrument(sig_copy.get("symbol", ""))
            if not instrument:
                sig_copy["blocked_reason"] = "Unknown signal instrument"
                blocked.append(sig_copy)
                continue
            token_info = (self._active_tokens or {}).get(instrument, {})
            if not token_info.get("tsym"):
                sig_copy["blocked_reason"] = f"No resolved futures tsym for {instrument}"
                blocked.append(sig_copy)
                continue
            if instrument in occupied:
                sig_copy["blocked_reason"] = f"No pyramiding: {instrument} already open or pending"
                blocked.append(sig_copy)
                continue
            if instrument in queued_instruments:
                sig_copy["blocked_reason"] = f"Duplicate {instrument} candidate already queued this scan"
                blocked.append(sig_copy)
                continue

            sig_copy["execution_symbol"] = token_info["tsym"]
            sig_copy["lot_size"] = token_info.get("lot_size")
            tradeable.append(sig_copy)
            queued_instruments.add(instrument)

        return tradeable, blocked

    @staticmethod
    def _logical_instrument(symbol: str) -> str | None:
        """Map logical names and futures tsyms to the base instrument."""
        sym = (symbol or "").upper()
        if "BANKNIFTY" in sym:
            return "BANKNIFTY"
        if "FINNIFTY" in sym:
            return "FINNIFTY"
        if "NIFTY" in sym:
            return "NIFTY"
        return None

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
