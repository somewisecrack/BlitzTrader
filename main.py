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
"""
import logging
import os
import shutil
import signal
import sys
import time
import uuid
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
    PAIR_CREDIT_REPLACEMENT_MODE,
    MAX_OPEN_OPTION_SPREADS,
    OPTION_SPREAD_MAX_RISK_RUPEES,
    SPREAD_MAX_LOSS_EXIT_FRACTION,
    SPREAD_CREDIT_TP_FRACTION,
    SPREAD_DEBIT_TP_FRACTION,
    MAX_RISK_PCT,
    MEMORY_FILE,
    NO_NEW_ENTRY_AFTER,
    LIMIT_ORDER_TIMEOUT_SECONDS,
    LIVE_ORDER_EXECUTION,
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
    SENSEX_SPOT_TOKEN,
    SENSEX_SPOT_EXCHANGE,
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
from tools.candidate_audit import CandidateAudit
from tools.atm_option_recorder import ATMOptionRecorder
from tools.sensex_atm_recorder import SensexATMOptionRecorder
from tools.position_serial import (
    build_status_message,
    save_position_index,
    load_position_index,
    invalidate_position_index,
)
from tools.options_chain import OptionsChain
from tools.options_spread_builder import SpreadBuilder
from tools.options_spread_execution import SpreadExecutionEngine
from tools.options_spread_portfolio import SpreadPortfolio
from tools.pair_credit_trader import make_pair_credit_trader_from_config
from context_builder import (
    SYSTEM_PROMPT,
    build_chat_context,
    build_eod_context,
    build_gatekeeper_context,
    build_spread_gatekeeper_context,
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
        # Options spread components
        self._options_chain: OptionsChain | None = None
        self._spread_builder: SpreadBuilder | None = None
        self._spread_exec: SpreadExecutionEngine | None = None
        self._spread_portfolio: SpreadPortfolio | None = None
        self._atm_recorder: ATMOptionRecorder | None = None
        self._sensex_recorder: SensexATMOptionRecorder | None = None

    def run(self):
        """Run the full trading session."""
        setup_logging()
        logger.info("=" * 60)
        logger.info("  BlitzTrader — Starting Session")
        logger.info("=" * 60)

        if PAIR_CREDIT_REPLACEMENT_MODE:
            self._run_pair_credit_replacement_session()
            return

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

    def _run_pair_credit_replacement_session(self):
        """
        Replacement mode: virtual NIFTY50 cointegrated-pair credit spreads.

        No market-data recorder, no ATM ladder recorder, no Google Drive data
        export. Only position open/close state and ledger entries are written.
        """
        logger.info("=== PAIR CREDIT REPLACEMENT MODE ===")
        today = datetime.now(IST).date()
        if not is_nse_trading_day(today):
            holiday_name = get_market_holiday_name(today)
            logger.info("NSE market closed today (%s): %s", today.isoformat(), holiday_name or "weekend")
            return

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._telegram = TelegramHandler(TELEGRAM_BOT_TOKEN, TELEGRAM_AUTHORIZED_USER_ID)
        self._telegram.start()

        try:
            self._check_disk_space()
            self._check_storage_mount()
            self._telegram.send_telegram(
                "BlitzTrader replacement mode starting: virtual NIFTY50 pair credit spreads."
            )

            self._shoonya = ShoonyaClient()
            max_retries = 100
            retry_interval = 300
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
                logger.warning("Shoonya login attempt %d/%d failed: %s", attempt, max_retries, msg)
                if attempt % 5 == 0:
                    self._telegram.send_telegram(
                        f"Shoonya still offline/login failing (attempt {attempt}). Retrying."
                    )
                commands = self._telegram.get_pending_commands()
                if any(c.get("command") == "/abort" for c in commands):
                    raise RuntimeError("Aborted by user during login retry")
                time.sleep(retry_interval)
            else:
                self._telegram.send_telegram("BlitzTrader: Shoonya login failed after all retries.")
                raise RuntimeError("Shoonya login failed after all retries")

            self._telegram.send_telegram("Shoonya login successful. Running pre-open pair scan.")
            pair_trader = make_pair_credit_trader_from_config(telegram=self._telegram)

            expiry_results = pair_trader.close_expired_positions()
            for result in expiry_results:
                if result.get("ok"):
                    pos = result["position"]
                    self._telegram.send_telegram(
                        f"Automatic expiry exit: {pos.get('pair')} | "
                        f"P&L Rs {float(result.get('realized_pnl') or 0):+,.2f}"
                    )
                else:
                    pos = result.get("position") or {}
                    self._telegram.send_telegram(
                        f"Expiry exit pending: {pos.get('pair', '?')} | {result.get('error')}"
                    )

            self._telegram.send_telegram(pair_trader.status_message())
            allocation = pair_trader.run_opening_allocation()
            logger.info(
                "Pair-credit opening allocation: opened=%d insufficient=%d rejected=%d remaining=%.2f",
                len(allocation.get("opened", [])),
                len(allocation.get("insufficient", [])),
                len(allocation.get("rejected", [])),
                float(allocation.get("remaining", 0) or 0),
            )

            self._running = True
            self._pair_credit_monitor_loop(pair_trader)
        except KeyboardInterrupt:
            logger.info("Pair-credit mode interrupted")
        except Exception:
            logger.exception("Fatal error in pair-credit replacement mode")
            if self._telegram:
                self._telegram.send_telegram("Pair-credit replacement mode failed. Check logs.")
        finally:
            if self._telegram:
                self._telegram.stop()

    def _pair_credit_monitor_loop(self, pair_trader):
        """Monitor Telegram commands and automatic expiry exits until market close."""
        logger.info("=== PAIR CREDIT MONITOR LOOP STARTED ===")
        while self._running:
            now = datetime.now(IST)
            eod_time = now.replace(hour=15, minute=25, second=0, microsecond=0)
            if now >= eod_time:
                logger.info("Pair-credit monitor EOD reached; positions remain open unless expired")
                self._running = False
                return

            commands = self._telegram.get_pending_commands()
            for cmd in commands:
                text = (cmd.get("text") or "").lower().strip()
                command = cmd.get("command") or ""
                if command == "/abort":
                    self._telegram.send_telegram(
                        "Abort received. Service stopping; virtual pair-credit positions remain open."
                    )
                    self._running = False
                    return

                import re as _re
                m = None
                for pattern in (
                    r"\bexit\s+#?(\d+)\b",
                    r"\bclose\s+(?:position\s+|spread\s+|serial\s+)?#?(\d+)\b",
                    r"\bsquare\s+off\s+#?(\d+)\b",
                ):
                    m = _re.search(pattern, text)
                    if m:
                        break
                if m:
                    serial = int(m.group(1))
                    result = pair_trader.close_by_serial(serial)
                    if result.get("ok"):
                        pos = result["position"]
                        self._telegram.send_telegram(
                            f"Closed #{serial}: {pos.get('pair')} | "
                            f"Realized P&L Rs {float(result.get('realized_pnl') or 0):+,.2f}\n"
                            "Freed capital will be considered only during the next trading day's opening scan."
                        )
                    else:
                        self._telegram.send_telegram(f"Could not close #{serial}: {result.get('error')}")
                    continue

                if command == "/status" or any(
                    word in text for word in ("status", "position", "positions", "pnl", "p&l", "profit", "loss")
                ):
                    self._telegram.send_telegram(pair_trader.status_message())
                    continue

                self._telegram.send_telegram(
                    "Supported commands: status, positions, pnl, exit #N, /abort."
                )

            expiry_results = pair_trader.close_expired_positions()
            for result in expiry_results:
                if result.get("ok"):
                    pos = result["position"]
                    self._telegram.send_telegram(
                        f"Automatic expiry exit: {pos.get('pair')} | "
                        f"P&L Rs {float(result.get('realized_pnl') or 0):+,.2f}"
                    )
                else:
                    pos = result.get("position") or {}
                    self._telegram.send_telegram(
                        f"Expiry exit pending: {pos.get('pair', '?')} | {result.get('error')}"
                    )
            time.sleep(TELEGRAM_POLL_INTERVAL_SECONDS)

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

        # Resolve underlying market-data tokens for traded instruments so we get real
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

        # Keep the NIFTY spot index subscribed separately. Trading signals use
        # front-month futures, but weekly-option ATM classification uses spot.
        active_tokens["NIFTY_SPOT"] = dict(NSE_TOKENS["NIFTY"])

        # SENSEX spot — subscribed for the ATM option recorder only.
        # SENSEX is never traded; this subscription is for data collection.
        active_tokens["SENSEX_SPOT"] = {
            "exchange": SENSEX_SPOT_EXCHANGE,
            "token": SENSEX_SPOT_TOKEN,
        }

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

        # 7. Options spread components
        self._options_chain = OptionsChain(self._shoonya)
        self._spread_builder = SpreadBuilder(
            self._options_chain,
            max_risk_rupees=OPTION_SPREAD_MAX_RISK_RUPEES,
        )
        _virtual = not LIVE_ORDER_EXECUTION
        logger.info(
            "Execution mode: %s (LIVE_ORDER_EXECUTION=%s)",
            "VIRTUAL/SIMULATED" if _virtual else "LIVE BROKER",
            LIVE_ORDER_EXECUTION,
        )
        self._spread_exec = SpreadExecutionEngine(
            self._shoonya,
            self._state,
            max_open_spreads=MAX_OPEN_OPTION_SPREADS,
            no_entry_after=NO_NEW_ENTRY_AFTER,
            fill_timeout_seconds=LIMIT_ORDER_TIMEOUT_SECONDS,
            virtual=_virtual,
        )
        self._spread_portfolio = SpreadPortfolio(
            self._shoonya,
            self._state,
            live_feed=self._feed,
            max_loss_exit_fraction=SPREAD_MAX_LOSS_EXIT_FRACTION,
            credit_tp_fraction=SPREAD_CREDIT_TP_FRACTION,
            debit_tp_fraction=SPREAD_DEBIT_TP_FRACTION,
            virtual=_virtual,
        )
        logger.info("✓ Options spread components initialized")

        try:
            self._atm_recorder = ATMOptionRecorder(
                base_dir=DATA_EXPORTS_DIR,
                shoonya_client=self._shoonya,
                options_chain=self._options_chain,
            )
            logger.info(
                "✓ NIFTY ATM/ATM±1 recorder initialized at %s",
                self._atm_recorder.export_dir,
            )
        except Exception:
            logger.warning(
                "NIFTY ATM ladder recorder failed to initialize (non-fatal)",
                exc_info=True,
            )

        try:
            self._sensex_recorder = SensexATMOptionRecorder(
                base_dir=DATA_EXPORTS_DIR,
                shoonya_client=self._shoonya,
            )
            self._sensex_recorder.initialise()
            logger.info(
                "✓ SENSEX ATM/ATM±1 recorder initialized at %s (recording only — not traded)",
                self._sensex_recorder.export_dir,
            )
        except Exception:
            logger.warning(
                "SENSEX ATM ladder recorder failed to initialize (non-fatal)",
                exc_info=True,
            )

        # 8. Journal (with state_manager for ground-truth injection)
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

        # 12. Tool registry
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
            spread_portfolio=self._spread_portfolio,
        )

        # 13. Agent loop
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

        # Candidate audit log — durable JSONL record of every signal at every stage
        self._audit = CandidateAudit(CANDIDATE_AUDIT_DIR)
        logger.info("Candidate audit log: %s", CANDIDATE_AUDIT_DIR)

        logger.info("All components initialized successfully")

        # Log startup configuration for verification
        logger.info(
            "STARTUP CONFIG: "
            f"Option spreads capital ₹{VIRTUAL_CAPITAL:,.0f} | "
            f"Max spreads: {MAX_OPEN_OPTION_SPREADS} | "
            f"Max risk/spread: ₹{OPTION_SPREAD_MAX_RISK_RUPEES:,} | "
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

        # ── Exit-by-serial: "exit 2", "close spread 2", "square off #3", "close serial 2" ──
        exit_patterns = [
            r'\bexit\s+#?(\d+)\b',
            r'\bclose\s+(?:position\s+|spread\s+|serial\s+)?#?(\d+)\b',
            r'\bsquare\s+off\s+#?(\d+)\b',
        ]
        for pattern in exit_patterns:
            m = _re.search(pattern, text)
            if m:
                serial = int(m.group(1))
                logger.info("Exit-by-serial command detected: serial=%d", serial)
                self._manual_spread_exit_by_serial(serial)
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

        # Use spread-aware status for status/positions requests
        if wants_status and self._spread_portfolio:
            try:
                status_lines = self._spread_portfolio.build_status_lines()
                open_spreads = self._spread_portfolio.get_open_spreads()
                # Persist serial index so exit_spread_by_serial can work
                index_payload = {
                    "positions": [
                        {"serial": i + 1, "spread_id": s.spread_id}
                        for i, s in enumerate(open_spreads)
                    ],
                    "generated_at": datetime.now(IST).isoformat(),
                    "ttl_seconds": 1800,
                    "type": "spreads",
                }
                save_position_index(index_payload)

                state = self._state.get_state()
                pnl = float(state.get("daily_pnl", 0) or 0)
                pnl_pct = float(state.get("daily_pnl_pct", 0) or 0)
                lines = []
                if wants_capital:
                    capital = float(state.get("virtual_capital", 0) or 0)
                    available_balance = float(state.get("available_balance", 0) or 0)
                    lines.extend([
                        f"Option spreads capital: ₹{capital:,.2f}",
                        f"Available balance: ₹{available_balance:,.2f}",
                        "",
                    ])
                lines.append(f"Session P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)")
                lines.extend(status_lines)
                self._telegram.send_telegram("\n".join(lines))
                logger.info("Answered Telegram status with spread-aware serial index")
                return True
            except Exception:
                logger.exception("Spread status build failed — falling back to simple status")

        # Fallback: capital-only or spread portfolio not initialized
        state = self._state.get_state()
        pnl = float(state.get("daily_pnl", 0) or 0)
        pnl_pct = float(state.get("daily_pnl_pct", 0) or 0)
        capital = float(state.get("virtual_capital", 0) or 0)
        available_balance = float(state.get("available_balance", 0) or 0)
        lines = []
        if wants_capital:
            lines.extend([
                f"Option spreads capital: ₹{capital:,.2f}",
                f"Available balance: ₹{available_balance:,.2f}",
            ])
        if wants_status or not wants_capital:
            lines.append(f"Session P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)")
            open_count = (
                self._spread_portfolio.count_open_spreads()
                if self._spread_portfolio else 0
            )
            lines.append(f"Open spreads: {open_count}")
        self._telegram.send_telegram("\n".join(lines))
        logger.info("Answered simple Telegram status/P&L chat without Gemini")
        return True

    def _manual_spread_exit_by_serial(self, serial: int) -> None:
        """
        Handle a manual Telegram serial-exit command for option spreads.
        Validates the serial index, finds the spread, closes both legs,
        and sends Telegram confirmation.  NEVER opens a new position.
        """
        if self._spread_portfolio is None:
            self._telegram.send_telegram("⚠️ Spread portfolio not initialised — cannot exit.")
            return

        index = load_position_index()
        if not index:
            self._telegram.send_telegram(
                "⚠️ No position index found. "
                "Send 'status' or 'positions' first to generate the index."
            )
            return

        if index.get("type") != "spreads":
            self._telegram.send_telegram(
                "⚠️ Position index is for legacy futures positions. "
                "Send 'status' to refresh."
            )
            return

        # Freshness check (30 min TTL)
        try:
            gen = datetime.fromisoformat(index["generated_at"].rstrip("Z")).replace(tzinfo=IST)
            age = (datetime.now(IST) - gen).total_seconds()
            if age > 1800:
                self._telegram.send_telegram(
                    f"⚠️ Position index is stale ({int(age//60)}m old). "
                    "Send 'status' to refresh."
                )
                return
        except Exception:
            pass  # If we can't parse, proceed

        # Resolve serial → spread_id
        spread_id = None
        for p in index.get("positions", []):
            if p.get("serial") == serial:
                spread_id = p.get("spread_id")
                break
        if spread_id is None:
            valid = [p["serial"] for p in index.get("positions", [])]
            self._telegram.send_telegram(
                f"⚠️ Serial #{serial} not found. Valid serials: {valid}"
            )
            return

        # Find live spread
        open_spreads = self._spread_portfolio.get_open_spreads()
        target_spread = next((s for s in open_spreads if s.spread_id == spread_id), None)
        if target_spread is None:
            self._telegram.send_telegram(
                f"⚠️ Spread {spread_id} (serial #{serial}) is no longer open — "
                "may have already been closed."
            )
            return

        # Close it
        result = self._spread_portfolio.close_spread(
            spread=target_spread,
            reason=f"manual Telegram exit serial #{serial}",
        )
        if result.get("ok"):
            pnl_val = result.get("realized_pnl", 0)
            invalidate_position_index()
            self._telegram.send_telegram(
                f"✅ Spread #{serial} [{spread_id}] closed manually.\n"
                f"{target_spread.symbol} {target_spread.spread_type} expiry {target_spread.expiry}\n"
                f"Realized P&L: ₹{pnl_val:+,.2f}"
            )
            self._audit.record(
                signal_id=spread_id,
                stage="SPREAD_EXITED",
                signal={"symbol": target_spread.symbol, "strategy": target_spread.strategy, "direction": target_spread.direction},
                reason=f"manual Telegram exit serial #{serial}",
                details={"realized_pnl": pnl_val},
            )
        else:
            err = result.get("error", "unknown error")
            self._telegram.send_telegram(
                f"⚠️ Failed to close spread #{serial}: {err}"
            )
            logger.error("Manual spread exit serial #%d failed: %s", serial, err)

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
                "Trade only deterministic scanner-confirmed option vertical spread setups",
                "Max 10 simultaneous open spreads; no pyramiding per instrument",
                "Let Python-managed spread exit thresholds handle risk (60%/60%/70%)",
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

        # Write PID file so blitztrader-agent.service yields Telegram polling to us.
        # Removed in _remove_trading_pid() on any exit path.
        _TRADING_PID_FILE = Path("/tmp/blitztrader_trading.pid")
        try:
            _TRADING_PID_FILE.write_text(str(os.getpid()))
            logger.info("Trading PID file written: %s (pid=%d)", _TRADING_PID_FILE, os.getpid())
        except Exception:
            logger.warning("Could not write trading PID file — agent coordination unavailable")

        try:
            while self._running:
                now = datetime.now(IST)

                # ── Check EOD ──
                eod_time = now.replace(hour=15, minute=15, second=0, microsecond=0)
                if now >= eod_time:
                    logger.info("=== EOD SEQUENCE ===")
                    # Close all option spreads first (deterministic Python, before Gemini EOD)
                    if self._spread_portfolio:
                        try:
                            eod_exits = self._spread_portfolio.check_and_exit_spreads(force_close_all=True)
                            for exit_result in eod_exits:
                                spread_id = exit_result.get("spread_id", "?")
                                pnl_val = exit_result.get("realized_pnl", 0)
                                if exit_result.get("ok"):
                                    self._telegram.send_telegram(
                                        f"🔒 EOD close [{spread_id}] ₹{pnl_val:+,.2f}"
                                    )
                                    self._audit.record(
                                        signal_id=spread_id,
                                        stage="SPREAD_EXITED",
                                        signal={"symbol": exit_result.get("symbol", ""), "strategy": "", "direction": ""},
                                        reason="EOD forced close",
                                        details={"realized_pnl": pnl_val},
                                    )
                                else:
                                    logger.error("EOD spread exit failed [%s]: %s", spread_id, exit_result.get("error"))
                                    self._telegram.send_telegram(
                                        f"⚠️ EOD spread exit failed [{spread_id}]: {exit_result.get('error')}"
                                    )
                            logger.info("EOD spread close complete: %d spreads processed", len(eod_exits))
                        except Exception:
                            logger.exception("EOD spread close error")

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
                    self._remove_trading_pid()
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
                    if self._spread_portfolio:
                        try:
                            abort_exits = self._spread_portfolio.check_and_exit_spreads(force_close_all=True)
                            logger.info("Abort spread close: %d spread(s) processed", len(abort_exits))
                        except Exception:
                            logger.exception("Abort spread close error")
                    if self._journal:
                        self._journal.log_decision(
                            action="ABORT",
                            reason="User abort command received. Deterministic spread close executed.",
                        )
                    if self._telegram:
                        self._telegram.send_telegram("🛑 Abort received. All open spreads closed. Session stopping.")
                    self._remove_trading_pid()
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
                        final_text = self._run_agent_iteration(
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
                        # Deliver final_text if Gemini reasoned but didn't call send_telegram
                        elif (
                            final_text
                            and self._agent
                            and not self._agent.was_send_telegram_called()
                        ):
                            self._telegram.send_telegram(final_text)
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
                            # Assign signal_id to ALL raw candidates before any filtering.
                            # Format: YYYYMMDD_HHMMSSfff_SYMBOL_D_xxxxxxxx
                            # uuid4 suffix guarantees uniqueness even for same-second signals.
                            for sig in new_sigs:
                                if not sig.get("_signal_id"):
                                    _ts = datetime.now(IST).strftime("%Y%m%d_%H%M%S%f")[:20]
                                    _sym = sig.get("symbol", "UNK")
                                    _dir = (sig.get("direction") or "X")[:1].upper()
                                    _uid = uuid.uuid4().hex[:8]
                                    sig["_signal_id"] = f"{_ts}_{_sym}_{_dir}_{_uid}"
                                # Audit every raw candidate
                                self._audit.record(
                                    signal_id=sig["_signal_id"],
                                    stage="RAW_CANDIDATE",
                                    signal=sig,
                                )

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

                    # ── Spread portfolio: check for P&L exit conditions ──
                    if self._spread_portfolio:
                        try:
                            exits = self._spread_portfolio.check_and_exit_spreads(force_close_all=False)
                            for exit_result in exits:
                                spread_id = exit_result.get("spread_id", "?")
                                if exit_result.get("ok"):
                                    reason_str = exit_result.get("reason", "exit triggered")
                                    pnl_val = exit_result.get("realized_pnl", 0)
                                    self._telegram.send_telegram(
                                        f"📉 SPREAD EXIT [{spread_id}]\n"
                                        f"Reason: {reason_str}\n"
                                        f"Realized P&L: ₹{pnl_val:+,.2f}"
                                    )
                                    self._audit.record(
                                        signal_id=spread_id,
                                        stage="SPREAD_EXITED",
                                        signal={"symbol": exit_result.get("symbol", ""), "strategy": "", "direction": ""},
                                        reason=reason_str,
                                        details={"realized_pnl": pnl_val},
                                    )
                                else:
                                    err_str = exit_result.get("error", "unknown error")
                                    self._telegram.send_telegram(
                                        f"⚠️ Spread exit failed [{spread_id}]: {err_str}"
                                    )
                                    self._audit.record(
                                        signal_id=spread_id,
                                        stage="SPREAD_EXIT_FAILED",
                                        signal={"symbol": exit_result.get("symbol", ""), "strategy": "", "direction": ""},
                                        reason=err_str,
                                    )
                        except Exception:
                            logger.exception("Spread portfolio monitoring error (non-fatal)")

                # NIFTY weekly-option research recorder. Classification uses the
                # spot index token; every activated ATM/ATM±1 strike remains
                # sampled for the rest of the session.
                if self._atm_recorder and self._feed:
                    spot_token = self._active_tokens.get("NIFTY_SPOT", {}).get("token")
                    if spot_token:
                        spot_ltp = self._feed.get_ltp(spot_token)
                        if spot_ltp and spot_ltp > 0:
                            self._atm_recorder.update_atm("NIFTY", spot_ltp)
                    try:
                        self._atm_recorder.sample_due_contracts()
                    except Exception:
                        logger.warning(
                            "NIFTY ATM ladder sample failed (non-fatal)",
                            exc_info=True,
                        )

                # SENSEX ATM recorder — recording only, never traded.
                if self._sensex_recorder and self._feed:
                    sensex_info = self._active_tokens.get("SENSEX_SPOT", {})
                    sensex_token = sensex_info.get("token")
                    sensex_exch = sensex_info.get("exchange", SENSEX_SPOT_EXCHANGE)
                    if sensex_token:
                        sensex_ltp = self._feed.get_ltp(sensex_token)
                        if sensex_ltp and sensex_ltp > 0:
                            try:
                                self._sensex_recorder.update_atm(sensex_ltp)
                            except Exception:
                                logger.warning(
                                    "SENSEX ATM update failed (non-fatal)",
                                    exc_info=True,
                                )
                    try:
                        self._sensex_recorder.sample_due_contracts()
                    except Exception:
                        logger.warning(
                            "SENSEX ATM ladder sample failed (non-fatal)",
                            exc_info=True,
                        )

                # ── Check daily loss limit ──
                state = self._state.get_state()
                if state.get("is_stopped"):
                    logger.warning("Trading stopped — daily loss limit hit")
                    self._telegram.send_telegram(
                        "🛑 BlitzTrader STOPPED: Daily loss limit hit. "
                        "All positions closed."
                    )
                    self._remove_trading_pid()
                    self._running = False
                    return

                # ── Short sleep — stay responsive to Telegram ──
                time.sleep(TELEGRAM_POLL_INTERVAL_SECONDS)
        finally:
            self._remove_trading_pid()

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

        # Use spread portfolio count for max-positions check (options path)
        open_spread_count = (
            self._spread_portfolio.count_open_spreads()
            if self._spread_portfolio is not None
            else len(positions)
        )

        blocked_reason = None
        if state.get("is_paused"):
            blocked_reason = "Trading paused by user command"
        elif state.get("is_stopped"):
            blocked_reason = "Trading stopped by daily-loss guardrail"
        elif float(state.get("daily_pnl", 0) or 0) <= -MAX_DAILY_LOSS_AMOUNT:
            blocked_reason = "Daily loss limit reached"
        elif open_spread_count >= MAX_POSITIONS:
            blocked_reason = f"Maximum open spreads reached ({open_spread_count}/{MAX_POSITIONS})"
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
        if self._spread_portfolio is not None:
            try:
                occupied.update(
                    self._logical_instrument(spread.symbol)
                    for spread in self._spread_portfolio.get_open_spreads()
                )
            except Exception:
                logger.exception("Could not read open spreads for no-pyramiding guardrail")
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
            if instrument in occupied:
                sig_copy["blocked_reason"] = f"No pyramiding: {instrument} already open or pending"
                blocked.append(sig_copy)
                continue
            if instrument in queued_instruments:
                sig_copy["blocked_reason"] = f"Duplicate {instrument} candidate already queued this scan"
                blocked.append(sig_copy)
                continue

            tradeable.append(sig_copy)
            queued_instruments.add(instrument)

        return tradeable, blocked

    def _process_tradeable_signals_python(self, signals: list[dict]) -> None:
        """
        Entry flow for scanner-detected signals that passed hard guardrails.

        Two-stage gate (invariants NEVER violated):
          1. Python hard review   — deterministic indicator/risk checks (no LLM)
          2. Gemini gatekeeper    — 5-second timeout, structured JSON; timeout/error = REJECT

        Only after both stages pass does Python place the order.
        Exits (SL/trailing/target/EOD/manual) are always deterministic Python-only.
        """
        for signal in signals:
            symbol = signal.get("symbol", "")
            strategy = signal.get("strategy", "")
            direction = signal.get("direction", "")
            signal_id = signal.get("_signal_id") or (
                f"{datetime.now(IST).strftime('%Y%m%d_%H%M%S%f')[:20]}_"
                f"{symbol}_{(direction or 'X')[:1].upper()}_{uuid.uuid4().hex[:8]}"
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
                        symbol=symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason=python_reason,
                    )
                    continue

                # ── Stage 2a: Build spread candidate ─────────────────────────
                underlying_price = None
                try:
                    price_data = self._market_data.get_spot_price(symbol)
                    if isinstance(price_data, dict):
                        underlying_price = float(price_data.get("spot_price") or price_data.get("ltp") or 0) or None
                except Exception:
                    logger.warning("Could not get spot price for %s — SpreadBuilder will use None", symbol)

                candidate = self._spread_builder.build(signal, underlying_price)
                if candidate is None:
                    self._audit.record(
                        signal_id=signal_id,
                        stage="SPREAD_BUILD_REJECTED",
                        signal=signal,
                        reason="SpreadBuilder returned None (no valid spread candidate)",
                    )
                    self._journal.log_decision(
                        action="REJECT",
                        symbol=symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason="Spread build failed (SpreadBuilder returned None)",
                    )
                    continue

                self._audit.record(
                    signal_id=signal_id,
                    stage="SPREAD_BUILT",
                    signal=signal,
                    details={
                        "spread_type": candidate.spread_type,
                        "expiry": str(candidate.expiry),
                        "net_debit_or_credit": candidate.net_debit_or_credit,
                        "max_profit": candidate.max_profit,
                        "max_loss": candidate.max_loss,
                    },
                )

                # ── Build spread-aware indicator context ──────────────────────
                indicators = self._market_data.get_indicators(
                    symbol=symbol,
                    interval=str(signal.get("interval", "")),
                )
                indicators_dict = indicators if isinstance(indicators, dict) else {}

                self._audit.record(
                    signal_id=signal_id,
                    stage="PYTHON_REVIEW_PASSED",
                    signal=signal,
                    reason=python_reason,
                )

                # ── Stage 2b: Gemini gatekeeper (APPROVE/REJECT, 5-second SLA) ──
                if not self._gatekeeper:
                    self._audit.record(
                        signal_id=signal_id,
                        stage="GATEKEEPER_REJECTED",
                        signal=signal,
                        reason="Gemini gatekeeper not configured (missing API key) — auto-REJECT",
                    )
                    self._journal.log_decision(
                        action="REJECT",
                        symbol=symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason="Gemini gatekeeper not configured (missing API key) — auto-REJECT",
                    )
                    continue

                gk_context = build_spread_gatekeeper_context(candidate, indicators_dict)
                gate_result = self._gatekeeper.evaluate(signal, gk_context)
                gate_reason = gate_result.get("reason", "")
                gate_error = gate_result.get("gatekeeper_error")
                gate_confidence = gate_result.get("confidence", 0.0)
                gate_conditions = gate_result.get("conditions_checked", [])

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
                        symbol=symbol,
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

                # ── Stage 3: Place spread (Python-controlled, long leg first) ──
                result = self._spread_exec.place_spread(candidate)
                if result.get("ok"):
                    open_spread = result["spread"]
                    self._audit.record(
                        signal_id=signal_id,
                        stage="SPREAD_ORDER_PLACED",
                        signal=signal,
                        details={
                            "spread_id": open_spread.spread_id,
                            "long_tsym": open_spread.long_tsym,
                            "short_tsym": open_spread.short_tsym,
                            "long_fill": open_spread.long_fill_price,
                            "short_fill": open_spread.short_fill_price,
                        },
                    )
                    action = "ENTER_LONG" if direction.upper() in ("BUY", "BULLISH") else "ENTER_SHORT"
                    self._journal.log_decision(
                        action=action,
                        symbol=symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason=(
                            f"Spread placed: {open_spread.spread_type} {direction} on {symbol}. "
                            f"Long {open_spread.long_tsym}@{open_spread.long_fill_price:.2f}, "
                            f"Short {open_spread.short_tsym}@{open_spread.short_fill_price:.2f}. "
                            f"Gatekeeper: {gate_reason} ({gate_confidence:.0%})."
                        ),
                    )
                    # Subscribe option leg tokens for accurate WebSocket P&L feed
                    if self._spread_portfolio:
                        self._spread_portfolio.subscribe_spread_legs(open_spread)
                    self._notify_spread_entry(
                        signal=signal,
                        candidate=candidate,
                        open_spread=open_spread,
                        gate_result=gate_result,
                        signal_id=signal_id,
                    )
                else:
                    spread_error = result.get("error") or "Spread execution layer rejected"
                    self._audit.record(
                        signal_id=signal_id,
                        stage="SPREAD_ORDER_REJECTED",
                        signal=signal,
                        reason=spread_error,
                    )
                    self._journal.log_decision(
                        action="REJECT",
                        symbol=symbol,
                        strategy_applied=strategy,
                        market_context_summary=context_summary,
                        reason=spread_error,
                    )
                    if result.get("emergency"):
                        logger.error(
                            "EMERGENCY CLOSE triggered for %s %s: %s",
                            symbol, strategy, spread_error,
                        )
                        self._telegram.send_telegram(
                            f"🚨 EMERGENCY CLOSE: {symbol} spread short leg failed. "
                            f"Long leg closed to avoid naked exposure.\n{spread_error}"
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
                    symbol=symbol,
                    strategy_applied=strategy,
                    reason=f"Python execution error: {exc}",
                )

    def _notify_spread_entry(
        self,
        signal: dict,
        candidate,
        open_spread,
        gate_result: dict,
        signal_id: str,
    ) -> None:
        """
        Send an enriched Telegram entry notification for a filled option vertical spread.
        NO SL/Target fields — spread has max_profit/max_loss/breakeven instead.
        """
        from tools.options_spread_execution import OpenSpread as _OpenSpread
        from tools.options_spread_builder import SpreadCandidate as _SpreadCandidate
        from config import SPREAD_MAX_LOSS_EXIT_FRACTION, SPREAD_DEBIT_TP_FRACTION, SPREAD_CREDIT_TP_FRACTION

        is_debit = candidate.spread_type in ("BULL_CALL", "BEAR_PUT")
        tp_frac = SPREAD_DEBIT_TP_FRACTION if is_debit else SPREAD_CREDIT_TP_FRACTION
        max_loss_exit = candidate.max_loss * SPREAD_MAX_LOSS_EXIT_FRACTION
        tp_exit = candidate.max_profit * tp_frac
        dc_label = "Net debit" if is_debit else "Net credit"

        gate_confidence = gate_result.get("confidence", 0.0)
        gate_reason = gate_result.get("reason", "")
        gate_risk_notes = gate_result.get("risk_notes", "")

        spread_id = open_spread.spread_id if open_spread else signal_id

        msg = (
            f"📊 **SPREAD ENTRY**\n"
            f"[{spread_id}] {open_spread.symbol} {open_spread.spread_type} ({open_spread.direction})\n"
            f"Expiry: {open_spread.expiry}\n"
            f"\n"
            f"Long:  {open_spread.long_tsym} @ ₹{open_spread.long_fill_price:.2f}\n"
            f"Short: {open_spread.short_tsym} @ ₹{open_spread.short_fill_price:.2f}\n"
            f"\n"
            f"{dc_label}: ₹{candidate.net_debit_or_credit:.2f}/lot | Lot size: {candidate.lot_size}\n"
            f"Max profit: ₹{candidate.max_profit:,.2f} | Max loss: ₹{candidate.max_loss:,.2f}\n"
            f"Breakeven: {candidate.breakeven:,.2f}\n"
            f"\n"
            f"Take-profit at: {tp_frac:.0%} = ₹{tp_exit:,.2f} profit\n"
            f"Max-loss exit at: {SPREAD_MAX_LOSS_EXIT_FRACTION:.0%} = ₹{max_loss_exit:,.2f} loss\n"
            f"\n"
            f"🛡 Gemini: APPROVED ({gate_confidence:.0%})\n"
            f"Reason: {gate_reason}"
        )
        if gate_risk_notes:
            msg += f"\n⚠ Risk note: {gate_risk_notes}"

        try:
            self._telegram.send_telegram(msg)
        except Exception:
            logger.exception("Failed to send spread entry Telegram notification")

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

        reason = (
            f"Python approved {strategy} {direction} on {symbol} {interval}m. "
            f"Scanner conditions valid; spread will be built by SpreadBuilder."
        )
        return True, context_summary, reason

    @staticmethod
    def _remove_trading_pid() -> None:
        """Remove the PID coordination file written at trading loop start."""
        pid_file = Path("/tmp/blitztrader_trading.pid")
        try:
            pid_file.unlink(missing_ok=True)
            logger.info("Trading PID file removed")
        except Exception:
            pass

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

        # Force close any remaining option spreads
        try:
            if self._spread_portfolio:
                open_spreads = self._spread_portfolio.get_open_spreads()
                if open_spreads:
                    logger.warning("Force closing %d remaining spread(s) during shutdown", len(open_spreads))
                    exits = self._spread_portfolio.check_and_exit_spreads(force_close_all=True)
                    for er in exits:
                        if not er.get("ok"):
                            logger.error("Shutdown spread close failed [%s]: %s", er.get("spread_id"), er.get("error"))
        except Exception:
            logger.exception("Error closing spreads during shutdown")

        # Log final state and update journal summary
        if self._state:
            pnl, pnl_pct = self._state.get_daily_pnl()
            trades = self._state.get_trades()
            spreads = self._state.get_traded_spreads()
            total_trades = len(trades) + len(spreads)
            logger.info(f"Final P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)")
            logger.info(
                "Total trades: %d (%d futures, %d option spreads)",
                total_trades,
                len(trades),
                len(spreads),
            )

            if self._journal:
                wins = (
                    sum(1 for t in trades if t.get("pnl", 0) > 0)
                    + sum(1 for s in spreads if s.get("realized_pnl", 0) > 0)
                )
                end_capital = VIRTUAL_CAPITAL + pnl
                self._journal.update_session_summary(
                    end_capital=end_capital,
                    net_pnl=pnl,
                    total_trades=total_trades,
                    wins=wins,
                )

        if self._agent:
            usage = self._agent.get_token_usage()
            logger.info(f"Total token usage: {usage}")

        if self._atm_recorder:
            try:
                self._atm_recorder.flush()
            except Exception:
                logger.warning(
                    "NIFTY ATM ladder EOD flush failed (non-fatal)",
                    exc_info=True,
                )

        if self._sensex_recorder:
            try:
                self._sensex_recorder.flush()
            except Exception:
                logger.warning(
                    "SENSEX ATM ladder EOD flush failed (non-fatal)",
                    exc_info=True,
                )

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
        if RCLONE_REMOTE and not GOOGLE_DRIVE_UPLOAD_DIR:
            logger.info(
                "Data export finalized locally; the 16:00 EOD backup service "
                "owns the single rclone upload"
            )
            if self._telegram:
                self._telegram.send_telegram(
                    "📊 Data export saved locally. Google Drive backup is "
                    "scheduled for 16:00 IST."
                )
            return
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
