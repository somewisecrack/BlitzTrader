"""
main.py — BlitzTrader session lifecycle orchestrator.

This is the entry point. It manages the full trading day:
  8:20 AM  → Login, initialize services
  9:15 AM  → Start deterministic Python trading loop
  3:15 PM  → Force EOD sequence, update memory
  3:25 PM  → Cleanup and exit

Gemini is used for:
  - live entry gatekeeper (5-second timeout, structured JSON, approve/reject only)
  - free-form Telegram chat on demand
  - end-of-day summarization/reflection

Gemma is used as a non-blocking observer (never in decision path):
  - submits async opinion on every candidate signal for journaling
"""
import logging
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    CANDIDATE_AUDIT_DIR,
    GEMINI_API_KEY,
    GEMINI_DECISION_MODEL,
    GEMINI_API_TIMEOUT_SECONDS,
    GEMINI_MAX_DECISION_TOKENS,
    GEMINI_MAX_SCHEDULED_TOKENS,
    GEMINI_SCHEDULED_MODEL,
    GEMINI_GATEKEEPER_MODEL,
    GEMINI_GATEKEEPER_TIMEOUT_SECONDS,
    GEMMA_OBSERVER_ENABLED,
    GEMMA_OBSERVER_URL,
    GEMMA_OBSERVER_MODEL,
    GEMMA_OBSERVER_TIMEOUT_SECONDS,
    DATA_EXPORTS_DIR,
    GOOGLE_DRIVE_UPLOAD_DIR,
    LIVE_DRIVE_MODE,
    JOURNALS_DIR,
    LOGS_DIR,
    MIN_FREE_DISK_MB,
    RUNTIME_STORAGE_DIR,
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
    TRADE_SYMBOLS,
    VIRTUAL_CAPITAL,
    setup_logging,
)
from broker.shoonya_client import ShoonyaClient, assert_client_identity
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
from tools.futures_filter_loader import load_active_filters, apply_promoted_filters
from tools.gemini_gatekeeper import GeminiGatekeeper
from tools.gemma_observer import GemmaObserver
from tools.candidate_audit import CandidateAudit
from tools.position_serial import (
    build_status_message,
    save_position_index,
    load_position_index,
    invalidate_position_index,
)
from context_builder import (
    SYSTEM_PROMPT,
    build_chat_context,
    build_eod_context,
    build_gatekeeper_context,
)

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger("BlitzTrader.Main")

assert_client_identity("BlitzTrader")


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
        self._data_export_upload_attempted = False
        self._active_tokens = None
        self._llm_disabled_reason = None
        self._llm_disabled_notified = False
        # Promoted futures filters (loaded from wiki/promoted_filters at startup)
        self._promoted_futures_filters: list = []
        # Gemini entry gatekeeper (approve/reject every Python-passed signal)
        self._gatekeeper: GeminiGatekeeper | None = None
        # Gemma observer (async, non-blocking, never in decision path)
        self._gemma: GemmaObserver | None = None
        # Per-signal Gemma opinions keyed by signal_id (for Telegram notifications)
        self._gemma_opinions: dict[str, dict] = {}

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

        # 0a. Disk guard — abort before doing anything if disk is critically low
        self._check_disk_space()

        # 0b. Storage mount health check — ensure Google Drive is accessible
        self._check_storage_mount()

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
            direct_drive_mode=LIVE_DRIVE_MODE,
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
        # index token (NIFTY, BANKNIFTY).
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
            state_manager=self._state,
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
            live_feed=self._feed,
            shoonya_client=self._shoonya,
            active_tokens=self._active_tokens,
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
        # Load promoted futures filters from wiki/ (non-fatal if missing)
        self._promoted_futures_filters = self._load_promoted_futures_filters()

        # Wire Gemini entry gatekeeper (APPROVE/REJECT after Python guardrails pass)
        if GEMINI_API_KEY:
            self._gatekeeper = GeminiGatekeeper(
                api_key=GEMINI_API_KEY,
                model=GEMINI_GATEKEEPER_MODEL,
                timeout_seconds=GEMINI_GATEKEEPER_TIMEOUT_SECONDS,
            )
            logger.info(
                "Gemini gatekeeper wired: model=%s timeout=%ds",
                GEMINI_GATEKEEPER_MODEL, GEMINI_GATEKEEPER_TIMEOUT_SECONDS,
            )
        else:
            logger.warning(
                "GEMINI_API_KEY not set — gatekeeper disabled, "
                "ALL Python-approved signals will be auto-REJECTED"
            )

        # Wire Gemma observer (async, non-blocking, never in decision path)
        # Uses local Ollama — NOT the Google API. Disabled by default on resource-constrained VMs.
        self._gemma = GemmaObserver(
            enabled=GEMMA_OBSERVER_ENABLED,
            url=GEMMA_OBSERVER_URL,
            model=GEMMA_OBSERVER_MODEL,
            timeout_seconds=GEMMA_OBSERVER_TIMEOUT_SECONDS,
            callback=self._on_gemma_opinion,
        )
        if GEMMA_OBSERVER_ENABLED:
            logger.info(
                "Gemma observer ENABLED: url=%s model=%s timeout=%ds",
                GEMMA_OBSERVER_URL, GEMMA_OBSERVER_MODEL, GEMMA_OBSERVER_TIMEOUT_SECONDS,
            )
        else:
            logger.info(
                "Gemma observer DISABLED (GEMMA_OBSERVER_ENABLED=false). "
                "Opinions recorded as UNAVAILABLE. Trading unaffected."
            )

        # Candidate audit log — durable JSONL record of every signal at every stage
        self._audit = CandidateAudit(CANDIDATE_AUDIT_DIR)
        logger.info("Candidate audit log: %s", CANDIDATE_AUDIT_DIR)

        logger.info("All components initialized successfully")

        # Log startup configuration for verification
        logger.info(
            "STARTUP CONFIG: "
            f"Futures capital ₹{VIRTUAL_CAPITAL:,.0f} | "
            f"State file: {STATE_FILE}"
        )

    def _load_promoted_futures_filters(self) -> list:
        """Load active promoted futures filters from wiki/promoted_filters at startup."""
        try:
            wiki_dir = Path(__file__).parent / "wiki"
            filters = load_active_filters(wiki_dir)
            if filters:
                logger.info(
                    "Loaded %d active promoted futures filter(s) from %s",
                    len(filters),
                    wiki_dir / "promoted_filters",
                )
            return filters
        except Exception:
            logger.warning("Failed to load promoted futures filters — continuing with no filters", exc_info=True)
            return []

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
                    "and enforcing deterministic entries/SL/target/EOD close, but it will not "
                    "answer free-form chat or generate the Gemini EOD summary until billing/quota is fixed."
                )
                self._llm_disabled_notified = True
        return response

    def _try_answer_simple_chat(self, chat_messages: list[dict]) -> bool:
        """
        Answer common operational questions without Gemini.

        This keeps Telegram useful even when Gemini is slow, capped, or disabled.
        Also handles exit-by-serial commands deterministically.
        """
        import re as _re
        if not self._telegram or not self._state:
            return False
        text = " ".join((m.get("text") or "").lower() for m in chat_messages)

        # ── Exit-by-serial: "exit 2", "close position 2", "square off #3", "close serial 2" ──
        exit_patterns = [
            r'\bexit\s+#?(\d+)\b',
            r'\bclose\s+(?:position\s+|serial\s+)?#?(\d+)\b',
            r'\bsquare\s+off\s+#?(\d+)\b',
        ]
        for pattern in exit_patterns:
            m = _re.search(pattern, text)
            if m:
                serial = int(m.group(1))
                logger.info("Exit-by-serial command detected: serial=%d", serial)
                from tools.position_serial import exit_position_by_serial as _exit_serial
                result = _exit_serial(
                    serial=serial,
                    state_manager=self._state,
                    pairs_portfolio=None,
                    order_execution=self._order_exec,
                    shoonya_client=self._shoonya,
                    telegram_handler=self._telegram,
                    active_tokens=self._active_tokens,
                    live_feed=self._feed,
                )
                if not result.get("success"):
                    self._telegram.send_telegram(
                        f"Cannot exit serial #{serial}: {result.get('error', 'Unknown error')}"
                    )
                return True

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

        # Use serial-numbered status format for status/positions requests
        if wants_status and self._order_exec:
            try:
                msg, index_payload = build_status_message(
                    state_manager=self._state,
                    pairs_portfolio=None,
                    live_feed=self._feed,
                    shoonya_client=self._shoonya,
                    active_tokens=self._active_tokens,
                )
                if wants_capital:
                    # Prepend capital info
                    state = self._state.get_state()
                    capital = float(state.get("virtual_capital", 0) or 0)
                    available_balance = float(state.get("available_balance", 0) or 0)
                    margin_used = float(state.get("margin_used", 0) or 0)
                    cap_lines = [
                        f"Futures capital: ₹{capital:,.2f}",
                        f"Available balance: ₹{available_balance:,.2f}",
                        f"Margin used: ₹{margin_used:,.2f}",
                        "",
                    ]
                    msg = "\n".join(cap_lines) + msg
                save_position_index(index_payload)
                self._telegram.send_telegram(msg)
                logger.info("Answered Telegram status with serial-numbered positions")
                return True
            except Exception:
                logger.exception("Serial status build failed — falling back to simple status")

        # Fallback: capital-only request or serial build failed
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
                f"Futures capital: ₹{capital:,.2f}",
                f"Available balance: ₹{available_balance:,.2f}",
                f"Margin used: ₹{margin_used:,.2f}",
            ])
        if wants_status or not wants_capital:
            lines.extend([
                f"Futures P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)",
                f"Open futures positions: {positions.get('count', 0)}",
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
        Pre-market startup is deterministic.

        Gemini is intentionally not used here so the live session only spends
        on free-form chat and the EOD summary.
        """
        logger.info("=== STARTUP PHASE ===")
        if self._goals and not self._goals.has_goals():
            self._goals.set_session_goals([
                "Trade only deterministic scanner-confirmed futures setups",
                "Respect one-lot, no-pyramiding, and daily-loss guardrails",
                "Let Python-managed SL/target/trailing logic handle risk",
            ])
            logger.info("✓ Deterministic startup goals set")

        self._telegram.send_telegram("🚀 Startup complete. Moving to autonomous trading loop.")
        logger.info("✓ Startup complete — trading will begin")
        self._running = True

    # ──────────────────────────────────────────────────────────
    #   TRADING LOOP (9:15 AM - 3:15 PM)
    # ──────────────────────────────────────────────────────────

    def _trading_loop(self):
        """
        Deterministic intraday loop.

        Three types of activity:
          - Background scanner: pure-Python get_strategy_signals() every 60 s.
          - Trade execution: pure-Python signal review and order placement.
          - Chat: immediate Telegram response; Gemini only for free-form asks.

        Runs until market close or abort.
        """
        logger.info("=== TRADING LOOP STARTED ===")
        consecutive_errors = 0
        last_scan_at = None
        last_feed_health_alert = None  # avoid spam
        feed_disconnect_start = None  # track how long feed is disconnected

        while self._running:
            now = datetime.now(IST)

            # ── Check EOD ──
            eod_time = now.replace(hour=15, minute=15, second=0, microsecond=0)
            if now >= eod_time:
                logger.info("=== EOD SEQUENCE ===")
                # Close futures positions
                close_result = self._order_exec.close_all_positions() if self._order_exec else {}
                logger.info("Deterministic futures EOD close result: %s", close_result)

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
                        f"EOD complete.\n"
                        f"Trades: {self._state.get_state().get('trade_count', 0)} | "
                        f"P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)"
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
                if self._journal:
                    self._journal.log_decision(
                        action="ABORT",
                        reason="User abort command received. Deterministic close_all_positions executed.",
                    )
                if self._telegram:
                    self._telegram.send_telegram("🛑 Abort received. All open positions closed. Session stopping.")
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
                            "feed recording, deterministic trade execution, and SL/target/EOD safety "
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
                    # Detect Gemini 503/UNAVAILABLE and send deterministic fallback
                    last_err = self._agent.get_last_error() if self._agent else None
                    if last_err and last_err.get("kind") == "service_unavailable":
                        self._telegram.send_telegram(
                            "⚠️ Gemini is temporarily unavailable. "
                            "Deterministic commands (pnl / status / positions / exit N) "
                            "still work — just ask."
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
                if (not hasattr(self, '_last_wait_log')
                        or (now - self._last_wait_log).total_seconds() >= 60):
                    logger.info(f"Waiting {wait_secs:.0f}s for market open...")
                    self._last_wait_log = now
                time.sleep(min(wait_secs, TELEGRAM_POLL_INTERVAL_SECONDS))
                continue

            # ── Notify once when market opens ──
            state = self._state.get_state()
            notifications_sent = state.get("notifications_sent", {}) or {}
            today_key = now.strftime("%Y-%m-%d")
            market_open_key = f"market_open_ready:{today_key}"
            if not notifications_sent.get(market_open_key):
                self._telegram.send_telegram(
                    "Market is now open. BlitzTrader is active and scanning for setups. "
                    "All systems ready."
                )
                logger.info("Market open — Telegram notified")
                notifications_sent[market_open_key] = datetime.now(IST).isoformat()
                self._state.update_state(notifications_sent=notifications_sent)

            # ── Background scanner + deterministic execution: every 60 seconds ──
            if (
                last_scan_at is None
                or (now - last_scan_at).total_seconds() >= SCAN_INTERVAL_SECONDS
            ):
                last_scan_at = now
                try:
                    scan_result = self._market_data.get_strategy_signals()
                    new_sigs = scan_result.get("signals", [])
                    if new_sigs:
                        # Assign signal_id to ALL raw candidates before any filtering
                        for sig in new_sigs:
                            if not sig.get("_signal_id"):
                                sig["_signal_id"] = (
                                    f"{sig.get('symbol', '?')}_"
                                    f"{sig.get('strategy', '?')}_"
                                    f"{sig.get('direction', '?')}_"
                                    f"{int(time.time())}"
                                )
                            # Audit every raw candidate
                            self._audit.record(
                                signal_id=sig["_signal_id"],
                                stage="RAW_CANDIDATE",
                                signal=sig,
                            )
                            # Submit ALL raw candidates to Gemma (observer only, async)
                            if self._gemma:
                                self._gemma.submit(sig, "")

                        tradeable_sigs, blocked_sigs = self._filter_tradeable_signals(
                            new_sigs,
                            now,
                            existing_pending=[],
                        )
                        for sig in blocked_sigs:
                            self._audit.record(
                                signal_id=sig.get("_signal_id", ""),
                                stage="HARD_GUARDRAIL_BLOCKED",
                                signal=sig,
                                reason=sig.get("blocked_reason", ""),
                            )
                        for sig in tradeable_sigs:
                            self._audit.record(
                                signal_id=sig.get("_signal_id", ""),
                                stage="HARD_GUARDRAIL_PASSED",
                                signal=sig,
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
                        logger.info(
                            f"Scanner: {len(new_sigs)} new signal(s), "
                            f"{len(tradeable_sigs)} passed hard guardrails → "
                            f"{[s['strategy'] + ' ' + s['direction'] for s in tradeable_sigs]}"
                        )
                        if tradeable_sigs:
                            self._process_tradeable_signals_python(tradeable_sigs)
                    if scan_result.get("notes"):
                        logger.debug(f"Scanner notes: {scan_result['notes']}")
                except Exception:
                    logger.exception("Background scanner error (non-fatal)")

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
        daily loss limit, no-entry cutoff, max positions, unresolved futures, or
        no-pyramiding conflicts.
        """
        state = self._state.get_state() if self._state else {}
        positions = state.get("positions", []) or []
        pending_orders = state.get("pending_orders", []) or []

        blocked_reason = None
        if state.get("is_paused"):
            blocked_reason = "Trading paused by user command"
        elif state.get("is_stopped"):
            blocked_reason = "Trading stopped by daily-loss guardrail"
        elif float(state.get("daily_pnl", 0) or 0) <= -MAX_DAILY_LOSS_AMOUNT:
            blocked_reason = "Daily loss limit reached"
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

        trading_date_str = now.strftime("%Y-%m-%d")
        tradeable = []
        blocked = []
        queued_instruments = set()
        for sig in signals:
            sig_copy = dict(sig)
            # ── Stale signal guard (filter layer, defence-in-depth) ──────────
            sig_date = sig_copy.get("signal_date")
            if sig_date and sig_date != trading_date_str:
                sig_copy["blocked_reason"] = (
                    f"Stale signal trigger candle: signal_date={sig_date}, "
                    f"trading_date={trading_date_str}"
                )
                logger.info(
                    "Stale signal trigger candle: signal_date=%s, trading_date=%s "
                    "— strategy=%s sym=%s direction=%s — blocked at filter layer",
                    sig_date,
                    trading_date_str,
                    sig_copy.get("strategy", ""),
                    sig_copy.get("symbol", ""),
                    sig_copy.get("direction", ""),
                )
                blocked.append(sig_copy)
                continue
            # ─────────────────────────────────────────────────────────────────
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

    def _process_tradeable_signals_python(self, signals: list[dict]) -> None:
        """
        Entry flow for scanner-detected signals that passed hard guardrails.

        Three-stage gate (invariants NEVER violated):
          1. Python hard review   — deterministic indicator/risk checks (no LLM)
          2. Gemma observer       — already submitted for ALL raw candidates in the scan loop;
                                   gk_context (richer) is re-submitted here for the Telegram path
          3. Gemini gatekeeper    — 5-second timeout, structured JSON; timeout/error = REJECT

        Only after all three stages pass does Python place the order.
        Exits (SL/trailing/target/EOD/manual) are always deterministic Python-only.
        """
        for signal in signals:
            execution_symbol = signal.get("execution_symbol") or signal.get("symbol", "")
            strategy = signal.get("strategy", "")
            symbol = signal.get("symbol", "")
            direction = signal.get("direction", "")
            signal_id = signal.get("_signal_id") or (
                f"{symbol}_{strategy}_{direction}_{int(time.time())}"
            )
            signal["_signal_id"] = signal_id
            try:
                # ── Stage 1: Python hard review ──────────────────────────────
                approved, context_summary, python_reason = self._review_signal_python(signal)
                if not approved:
                    self._audit.record(
                        signal_id=signal_id,
                        stage="PYTHON_REVIEW_REJECTED",
                        signal=signal,
                        reason=python_reason,
                    )
                    self._journal.log_decision(
                        action="REJECT",
                        symbol=execution_symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason=python_reason,
                    )
                    continue

                # ── Build indicator context (richer than scan-loop submit) ──
                indicators = self._market_data.get_indicators(
                    symbol=symbol,
                    interval=str(signal.get("interval", "")),
                )
                gk_context = build_gatekeeper_context(
                    signal, indicators if isinstance(indicators, dict) else {}
                )

                self._audit.record(
                    signal_id=signal_id,
                    stage="PYTHON_REVIEW_PASSED",
                    signal=signal,
                    reason=python_reason,
                )

                # ── Stage 2: Gemma observer (re-submit with richer context) ──
                # Note: ALL raw candidates were already submitted with empty context
                # in the scan loop. This re-submit provides gk_context for journaling.
                if self._gemma:
                    self._gemma.submit(signal, gk_context)

                # ── Stage 3: Gemini gatekeeper (APPROVE / REJECT, 5-second SLA) ──
                if not self._gatekeeper:
                    # No API key configured — reject all signals
                    self._audit.record(
                        signal_id=signal_id,
                        stage="GATEKEEPER_REJECTED",
                        signal=signal,
                        reason="Gemini gatekeeper not configured (missing API key) — auto-REJECT",
                    )
                    self._journal.log_decision(
                        action="REJECT",
                        symbol=execution_symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason="Gemini gatekeeper not configured (missing API key) — auto-REJECT",
                    )
                    continue

                gate_result = self._gatekeeper.evaluate(signal, gk_context)
                gate_reason = gate_result.get("reason", "")
                gate_error = gate_result.get("gatekeeper_error")
                gate_confidence = gate_result.get("confidence", 0.0)
                gate_conditions = gate_result.get("conditions_checked", [])
                gate_risk_notes = gate_result.get("risk_notes", "")

                if not gate_result.get("approved"):
                    reject_reason = (
                        f"Gemini gatekeeper REJECT ({gate_confidence:.0%}): {gate_reason}"
                    )
                    if gate_error:
                        reject_reason = f"Gemini gatekeeper auto-REJECT: {gate_error}"
                    self._audit.record(
                        signal_id=signal_id,
                        stage="GATEKEEPER_REJECTED",
                        signal=signal,
                        reason=reject_reason,
                        details={"gate_confidence": gate_confidence, "gate_error": gate_error},
                    )
                    self._journal.log_decision(
                        action="REJECT",
                        symbol=execution_symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason=reject_reason,
                    )
                    logger.info(
                        "Gatekeeper rejected %s %s %s — %s",
                        symbol, strategy, direction, reject_reason,
                    )
                    continue

                self._audit.record(
                    signal_id=signal_id,
                    stage="GATEKEEPER_APPROVED",
                    signal=signal,
                    details={
                        "gate_confidence": gate_confidence,
                        "gate_reason": gate_reason,
                        "conditions_checked": gate_conditions,
                    },
                )

                # ── Place order (Python-controlled) ──────────────────────────
                quantity = signal.get("lot_size") or self._active_tokens.get(
                    self._logical_instrument(symbol),
                    {},
                ).get("lot_size")
                result = self._order_exec.place_virtual_order(
                    symbol=execution_symbol,
                    direction=direction.upper(),
                    quantity=quantity,
                    order_type="MARKET",
                    stop_loss=signal.get("stop_loss"),
                    target=signal.get("target"),
                    strategy=strategy,
                )
                status = str(result.get("status", "")).upper()
                if status in {"FILLED", "PENDING"}:
                    action = "ENTER_LONG" if direction.upper() == "BUY" else "ENTER_SHORT"
                    fill_price = result.get("fill_price")
                    fill_note = (
                        f"Python+Gemini approved {strategy} {direction} on {symbol}. "
                        f"Gatekeeper: {gate_reason} (confidence {gate_confidence:.0%}). "
                        f"Python: {python_reason}."
                    )
                    if fill_price is not None:
                        fill_note += f" Fill ₹{fill_price:.2f}."
                    self._audit.record(
                        signal_id=signal_id,
                        stage="ORDER_PLACED",
                        signal=signal,
                        details={"fill_price": fill_price, "status": status},
                    )
                    self._journal.log_decision(
                        action=action,
                        symbol=execution_symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason=fill_note,
                    )
                    # Send enriched Telegram notification
                    self._notify_entry(
                        signal=signal,
                        fill_price=fill_price,
                        gate_result=gate_result,
                        python_reason=python_reason,
                        signal_id=signal_id,
                    )
                else:
                    order_error = result.get("error") or result.get("message") or "Order rejected by execution layer."
                    self._audit.record(
                        signal_id=signal_id,
                        stage="ORDER_REJECTED",
                        signal=signal,
                        reason=order_error,
                    )
                    self._journal.log_decision(
                        action="REJECT",
                        symbol=execution_symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason=order_error,
                    )
            except Exception as exc:
                logger.exception(
                    "Python execution failed for signal %s %s %s",
                    signal.get("symbol"),
                    signal.get("strategy"),
                    signal.get("direction"),
                )
                self._journal.log_decision(
                    action="REJECT",
                    symbol=execution_symbol,
                    strategy_applied=strategy,
                    reason=f"Python execution error: {exc}",
                )

    def _on_gemma_opinion(self, signal: dict, opinion: dict) -> None:
        """
        Callback invoked by GemmaObserver daemon thread when Gemma responds.

        INVARIANT: This callback NEVER affects trade decisions.
        It records the opinion for journaling, deferred Telegram messages, and the audit log.
        """
        signal_id = signal.get("_signal_id", "")
        if signal_id:
            self._gemma_opinions[signal_id] = opinion
        symbol = signal.get("symbol", "?")
        strategy = signal.get("strategy", "?")
        if not opinion.get("gemma_error"):
            logger.info(
                "Gemma observer recorded: %s %s → %s (%.0f%%) — %s",
                symbol, strategy,
                opinion.get("alignment", "?"),
                float(opinion.get("confidence", 0)) * 100,
                opinion.get("key_observation", ""),
            )
        # Audit the Gemma opinion — purely for the durable record
        self._audit.record(
            signal_id=signal_id,
            stage="GEMMA_OPINION",
            signal=signal,
            reason=opinion.get("gemma_error") or "",
            details={
                "alignment": opinion.get("alignment"),
                "confidence": opinion.get("confidence"),
                "key_observation": opinion.get("key_observation"),
                "concern": opinion.get("concern"),
            },
        )

    def _notify_entry(
        self,
        signal: dict,
        fill_price,
        gate_result: dict,
        python_reason: str,
        signal_id: str,
    ) -> None:
        """
        Send an enriched Telegram entry notification that includes Gemini and Gemma context.
        """
        symbol = signal.get("symbol", "?")
        strategy = signal.get("strategy", "?")
        direction = signal.get("direction", "?")
        stop_loss = signal.get("stop_loss")
        target = signal.get("target")

        gate_confidence = gate_result.get("confidence", 0.0)
        gate_reason = gate_result.get("reason", "")
        gate_conditions = gate_result.get("conditions_checked", [])
        gate_risk_notes = gate_result.get("risk_notes", "")

        # Try to include the Gemma opinion if it arrived in time (best-effort)
        gemma = self._gemma_opinions.get(signal_id, {})
        gemma_line = ""
        if gemma and not gemma.get("gemma_error"):
            gemma_line = (
                f"\n🔬 **Gemma**: {gemma.get('alignment','?')} "
                f"({gemma.get('confidence', 0):.0%}) — "
                f"{gemma.get('key_observation', '')}"
            )

        price_str = f"₹{fill_price:.2f}" if fill_price is not None else "MARKET"
        sl_str = f"₹{stop_loss:.2f}" if stop_loss is not None else "—"
        tgt_str = f"₹{target:.2f}" if target is not None else "—"

        conditions_str = ""
        if gate_conditions:
            conditions_str = "\n✔ " + "\n✔ ".join(gate_conditions[:4])

        msg = (
            f"🚀 **ENTRY** {direction} {symbol} @ {price_str}\n"
            f"Strategy: {strategy}\n"
            f"SL: {sl_str}  |  Target: {tgt_str}\n"
            f"\n🛡 **Gemini** ({gate_confidence:.0%}): {gate_reason}"
            f"{conditions_str}"
        )
        if gate_risk_notes:
            msg += f"\n⚠ Risk note: {gate_risk_notes}"
        msg += gemma_line

        try:
            self._telegram.send_telegram(msg)
        except Exception:
            logger.exception("Failed to send entry Telegram notification")

    def _review_signal_python(self, signal: dict) -> tuple[bool, str, str]:
        """
        Deterministically accept/reject a scanner signal using Python rules only.
        """
        symbol = str(signal.get("symbol", "")).upper()
        interval = str(signal.get("interval", ""))
        direction = str(signal.get("direction", "")).upper()
        strategy = str(signal.get("strategy", ""))

        indicators = self._market_data.get_indicators(symbol=symbol, interval=interval)
        if not isinstance(indicators, dict) or indicators.get("error"):
            return False, "", indicators.get("error") or f"Failed to get indicators for {symbol} {interval}m"

        price = indicators.get("current_price")
        ema20 = indicators.get("ema20")
        adx14 = indicators.get("adx14")
        rsi14 = indicators.get("rsi14")
        avg_volume_20 = float(indicators.get("avg_volume_20") or 0)
        ema_bull = bool(indicators.get("ema_stacked_bull"))
        ema_bear = bool(indicators.get("ema_stacked_bear"))
        context_summary = (
            f"{symbol} {interval}m | price ₹{price:.2f} | EMA20 {ema20} | "
            f"ADX {adx14} | RSI {rsi14} | avgVol20 {avg_volume_20:.0f}"
        )

        # Apply promoted futures filters (Python-driven, backtest-validated)
        if self._promoted_futures_filters:
            filter_allowed, filter_reason = apply_promoted_filters(
                signal, indicators, self._promoted_futures_filters
            )
            if not filter_allowed:
                return False, context_summary, filter_reason

        if direction == "BUY" and ema_bear:
            return False, context_summary, "Rejected by Python: higher-timeframe EMA stack remains bearish."
        if direction == "SELL" and ema_bull:
            return False, context_summary, "Rejected by Python: higher-timeframe EMA stack remains bullish."

        trend_strategies = ("VP-01", "VP-05", "VP-07", "VP-17", "VP-20", "VP-24")
        if adx14 is not None and adx14 < 18 and strategy.startswith(trend_strategies):
            return False, context_summary, f"Rejected by Python: ADX {adx14:.2f} too weak for this setup."

        if signal.get("requires_volume_confirmation"):
            candle_data = self._market_data.get_candles(symbol=symbol, interval=interval, count=2)
            candles = candle_data.get("candles", []) if isinstance(candle_data, dict) else []
            latest_volume = float(candles[-1].get("volume", 0)) if candles else 0
            if avg_volume_20 <= 0:
                return False, context_summary, "Rejected by Python: volume confirmation required but avg_volume_20 is unavailable."
            if latest_volume < avg_volume_20:
                return False, context_summary, (
                    f"Rejected by Python: latest candle volume {latest_volume:.0f} "
                    f"is below avg_volume_20 {avg_volume_20:.0f}."
                )

        stop_loss = signal.get("stop_loss")
        target = signal.get("target")
        if stop_loss is None or target is None:
            return False, context_summary, "Rejected by Python: signal is missing stop-loss or target."

        reason = (
            f"Python approved {strategy} {direction} on {symbol} {interval}m. "
            f"Scanner conditions valid; stop ₹{stop_loss:.2f}, target ₹{target:.2f}."
        )
        return True, context_summary, reason

    @staticmethod
    def _logical_instrument(symbol: str) -> str | None:
        """Map logical names and futures tsyms to the base instrument.
        Only NIFTY and BANKNIFTY are in the active futures universe.
        FINNIFTY is explicitly excluded — return None so the signal is blocked.
        """
        sym = (symbol or "").upper()
        if "BANKNIFTY" in sym:
            return "BANKNIFTY"
        if "FINNIFTY" in sym:
            # FINNIFTY removed from active futures universe — do not map to NIFTY
            return None
        if "NIFTY" in sym:
            return "NIFTY"
        return None

    # ──────────────────────────────────────────────────────────
    #   DISK GUARD
    # ──────────────────────────────────────────────────────────

    def _check_disk_space(self) -> None:
        """Abort session if free disk is critically low."""
        try:
            usage = shutil.disk_usage(RUNTIME_STORAGE_DIR)
            free_mb = usage.free / (1024 * 1024)
            logger.info("Disk free: %.0f MB (threshold: %d MB)", free_mb, MIN_FREE_DISK_MB)
            if free_mb < MIN_FREE_DISK_MB:
                msg = (
                    f"DISK FULL: only {free_mb:.0f} MB free on "
                    f"{RUNTIME_STORAGE_DIR} (need {MIN_FREE_DISK_MB} MB). "
                    "BlitzTrader will not start trading. Free disk space and restart."
                )
                logger.critical(msg)
                if self._telegram:
                    self._telegram.send_telegram(f"🚨 {msg}")
                raise RuntimeError(msg)
        except RuntimeError:
            raise
        except Exception:
            logger.exception("Disk space check failed — proceeding with caution")

    def _check_storage_mount(self) -> None:
        """Verify storage mount is healthy before trading starts."""
        try:
            # Check if path exists and is accessible
            if not RUNTIME_STORAGE_DIR.exists():
                raise RuntimeError(f"Storage path does not exist: {RUNTIME_STORAGE_DIR}")

            # Try to list directory contents
            list(RUNTIME_STORAGE_DIR.iterdir())
            logger.info(f"✓ Storage mount healthy: {RUNTIME_STORAGE_DIR}")

        except RuntimeError:
            raise
        except Exception as e:
            msg = (
                f"⚠️ STORAGE MOUNT ISSUE: Cannot access {RUNTIME_STORAGE_DIR}. "
                f"Error: {str(e)[:100]}. "
                "Journaling may not work reliably. Fix mount and restart."
            )
            logger.critical(msg)
            if self._telegram:
                self._telegram.send_telegram(msg)
            raise RuntimeError(msg)

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
        """Upload today's audit exports once."""
        if not self._data_recorder:
            return
        if self._data_export_upload_attempted:
            logger.info("Skipping data export upload; already attempted this session")
            return
        now = datetime.now(IST)
        eod_time = now.replace(hour=15, minute=15, second=0, microsecond=0)
        if now < eod_time:
            logger.info("Skipping data export upload before 15:15 IST")
            return
        self._data_export_upload_attempted = True
        try:
            result = self._data_recorder.finalize_and_upload()
            logger.info(f"Data export result: {result}")
            if self._telegram and result.get("status") == "uploaded":
                self._telegram.send_telegram(
                    f"📊 Data export uploaded to Google Drive.\n"
                    f"Destination: {result.get('destination')}"
                )
            elif self._telegram and result.get("status") == "already_on_drive":
                self._telegram.send_telegram(
                    f"📊 Data already stored directly on Google Drive.\n"
                    f"Destination: {result.get('destination')}"
                )
            elif self._telegram and result.get("status") == "no_destination_configured":
                self._telegram.send_telegram(
                    "⚠️ Data export saved locally but not uploaded. "
                    "Set GOOGLE_DRIVE_UPLOAD_DIR or RCLONE_REMOTE."
                )
        except Exception as e:
            logger.exception("Failed to upload data export")
            if self._telegram:
                self._telegram.send_telegram(f"⚠️ Failed to upload data export: {str(e)[:200]}")

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
