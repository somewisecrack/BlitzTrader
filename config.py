"""
config.py — BlitzTrader configuration.
All environment variables, constants, and trading parameters.
"""
import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────
#   PATHS
# ──────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
STRATEGIES_DIR = BASE_DIR / "strategies"

# ──────────────────────────────────────────────────────────────
#   ENV LOADING
# ──────────────────────────────────────────────────────────────

def _load_env():
    """Load .env from project root."""
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        # Try parent directory
        parent_env = BASE_DIR.parent / ".env"
        if parent_env.exists():
            load_dotenv(parent_env)

_load_env()


def _require_env(key: str) -> str:
    """Get required environment variable or raise."""
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def _optional_env(key: str, default: str = "") -> str:
    """Get optional environment variable."""
    return os.environ.get(key, default).strip()


def _optional_int_env(key: str, default: int) -> int:
    """Get optional integer environment variable with safe fallback."""
    raw = _optional_env(key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.getLogger("BlitzTrader.Config").warning(
            "Invalid integer for %s=%r; using default %s", key, raw, default
        )
        return default


# ──────────────────────────────────────────────────────────────
#   RUNTIME STORAGE
# ──────────────────────────────────────────────────────────────

RUNTIME_STORAGE_DIR = Path(
    _optional_env("RUNTIME_STORAGE_DIR", str(BASE_DIR))
).expanduser()

JOURNALS_DIR = RUNTIME_STORAGE_DIR / "journals"
LOGS_DIR = RUNTIME_STORAGE_DIR / "logs"
DATA_EXPORTS_DIR = RUNTIME_STORAGE_DIR / "data_exports"
STATE_FILE = RUNTIME_STORAGE_DIR / "live_state.json"
CANDIDATE_AUDIT_DIR = RUNTIME_STORAGE_DIR / "candidate_signals"
PAIR_CREDIT_STATE_FILE = RUNTIME_STORAGE_DIR / "pair_credit_positions.json"
PAIR_CREDIT_LEDGER_FILE = RUNTIME_STORAGE_DIR / "pair_credit_ledger.jsonl"

# Ensure directories exist
JOURNALS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
STRATEGIES_DIR.mkdir(exist_ok=True)
CANDIDATE_AUDIT_DIR.mkdir(parents=True, exist_ok=True)

LIVE_DRIVE_MODE = _optional_env("LIVE_DRIVE_MODE", "").lower() in {
    "1", "true", "yes", "on"
}

# ──────────────────────────────────────────────────────────────
#   SHOONYA CREDENTIALS
# ──────────────────────────────────────────────────────────────

SHOONYA_USER_ID = _optional_env("SHOONYA_USER_ID")
SHOONYA_PASSWORD = _optional_env("SHOONYA_PASSWORD")
SHOONYA_TOTP_SECRET = _optional_env("SHOONYA_TOTP_SECRET")
SHOONYA_API_KEY = _optional_env("SHOONYA_API_KEY")
SHOONYA_SECRET_CODE = _optional_env("SHOONYA_SECRET_CODE")
SHOONYA_VENDOR_CODE = _optional_env("SHOONYA_VENDOR_CODE")
SHOONYA_IMEI = _optional_env("SHOONYA_IMEI")
SHOONYA_AUTH_CODE = _optional_env("SHOONYA_AUTH_CODE")  # OAuth authorization code (from manual login)

# ──────────────────────────────────────────────────────────────
#   TELEGRAM
# ──────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = _optional_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_AUTHORIZED_USER_ID = _optional_env("TELEGRAM_AUTHORIZED_USER_ID")

# ──────────────────────────────────────────────────────────────
#   AI / GEMINI
# ──────────────────────────────────────────────────────────────

GEMINI_API_KEY = _optional_env("GEMINI_API_KEY")
GEMINI_MODEL = _optional_env("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_DECISION_MODEL = _optional_env("GEMINI_DECISION_MODEL", GEMINI_MODEL)
GEMINI_SCHEDULED_MODEL = _optional_env("GEMINI_SCHEDULED_MODEL", "gemini-2.5-flash-lite")
GEMINI_MAX_DECISION_TOKENS = _optional_int_env("GEMINI_MAX_DECISION_TOKENS", 2048)
GEMINI_MAX_SCHEDULED_TOKENS = _optional_int_env("GEMINI_MAX_SCHEDULED_TOKENS", 768)
GEMINI_API_TIMEOUT_SECONDS = _optional_int_env("GEMINI_API_TIMEOUT_SECONDS", 45)

# Entry gatekeeper — strict timeout, lightweight model
GEMINI_GATEKEEPER_MODEL = _optional_env("GEMINI_GATEKEEPER_MODEL", "gemini-2.5-flash-lite")
GEMINI_GATEKEEPER_TIMEOUT_SECONDS = _optional_int_env("GEMINI_GATEKEEPER_TIMEOUT_SECONDS", 5)

# ──────────────────────────────────────────────────────────────
#   DATA EXPORT / GOOGLE DRIVE
# ──────────────────────────────────────────────────────────────

# Option 1: mounted Google Drive folder, e.g. /mnt/gdrive/MyDrive
GOOGLE_DRIVE_UPLOAD_DIR = _optional_env("GOOGLE_DRIVE_UPLOAD_DIR")

# Option 2: rclone remote configured on the VM, e.g. RCLONE_REMOTE=gdrive
RCLONE_REMOTE = _optional_env("RCLONE_REMOTE")
RCLONE_FOLDER = _optional_env("RCLONE_FOLDER", "BlitzTrader")

# ──────────────────────────────────────────────────────────────
#   TRADING PARAMETERS
# ──────────────────────────────────────────────────────────────

VIRTUAL_CAPITAL = 1_000_000  # ₹10,00,000
TRADE_SYMBOLS = ("NIFTY", "BANKNIFTY")

# ──────────────────────────────────────────────────────────────
#   PAIR CREDIT-SPREAD REPLACEMENT MODE
# ──────────────────────────────────────────────────────────────

PAIR_CREDIT_REPLACEMENT_MODE = (
    _optional_env("PAIR_CREDIT_REPLACEMENT_MODE", "true").lower()
    in {"1", "true", "yes", "on"}
)

_LOCAL_OMNISPREAD_BACKEND = Path("/Users/rahulgirishkumar/PROJECTS/omnispread/backend")
_VM_OMNISPREAD_BACKEND = Path("/opt/omnispread/backend")
_VM_OMNISPREAD_ROOT = Path("/opt/omnispread")
_DEFAULT_OMNISPREAD_BACKEND = next(
    (
        path
        for path in (
            _LOCAL_OMNISPREAD_BACKEND,
            _VM_OMNISPREAD_BACKEND,
            _VM_OMNISPREAD_ROOT,
        )
        if path.exists()
    ),
    _VM_OMNISPREAD_BACKEND,
)
OMNISPREAD_BACKEND_PATH = Path(
    _optional_env(
        "OMNISPREAD_BACKEND_PATH",
        str(_DEFAULT_OMNISPREAD_BACKEND),
    )
).expanduser()

PAIR_CREDIT_PRESET = _optional_env("PAIR_CREDIT_PRESET", "nifty_50")
PAIR_CREDIT_PERIOD = _optional_env("PAIR_CREDIT_PERIOD", "1y")
PAIR_CREDIT_INTERVAL = _optional_env("PAIR_CREDIT_INTERVAL", "1d")
PAIR_CREDIT_TOP_N = _optional_int_env("PAIR_CREDIT_TOP_N", 50)
PAIR_CREDIT_CAPITAL = float(_optional_env("PAIR_CREDIT_CAPITAL", str(VIRTUAL_CAPITAL)))
PAIR_CREDIT_STRIKE_RULE = _optional_env("PAIR_CREDIT_STRIKE_RULE", "vol")
PAIR_CREDIT_SOLD_SD = float(_optional_env("PAIR_CREDIT_SOLD_SD", "1.0"))
PAIR_CREDIT_HEDGE_SD = float(_optional_env("PAIR_CREDIT_HEDGE_SD", "2.5"))
PAIR_CREDIT_VOL_GATE_ENABLED = (
    _optional_env("PAIR_CREDIT_VOL_GATE_ENABLED", "true").lower()
    in {"1", "true", "yes", "on"}
)
PAIR_CREDIT_IV_HV_MIN_RATIO = float(_optional_env("PAIR_CREDIT_IV_HV_MIN_RATIO", "1.0"))
PAIR_CREDIT_HV_LOOKBACK_MULTIPLIER = _optional_int_env("PAIR_CREDIT_HV_LOOKBACK_MULTIPLIER", 2)
PAIR_CREDIT_HV_MIN_LOOKBACK_DAYS = _optional_int_env("PAIR_CREDIT_HV_MIN_LOOKBACK_DAYS", 5)
PAIR_CREDIT_HV_MAX_LOOKBACK_DAYS = _optional_int_env("PAIR_CREDIT_HV_MAX_LOOKBACK_DAYS", 30)

# ──────────────────────────────────────────────────────────────
#   OPTION SPREAD TRADING PARAMETERS
# ──────────────────────────────────────────────────────────────

# Maximum simultaneous open option spreads
MAX_OPEN_OPTION_SPREADS = _optional_int_env("MAX_OPEN_OPTION_SPREADS", 10)
# Alias used in legacy guardrail checks
MAX_POSITIONS = MAX_OPEN_OPTION_SPREADS

# Risk per spread
OPTION_SPREAD_MAX_RISK_PCT = float(_optional_env("OPTION_SPREAD_MAX_RISK_PCT", "1.0")) / 100.0
OPTION_SPREAD_MAX_RISK_RUPEES = _optional_int_env("OPTION_SPREAD_MAX_RISK_RUPEES", 10000)

# Exit thresholds (fraction of max possible loss/profit)
SPREAD_MAX_LOSS_EXIT_FRACTION = 0.60   # exit if loss >= 60% of max loss
SPREAD_CREDIT_TP_FRACTION     = 0.60   # credit spread: take profit at 60% of max profit
SPREAD_DEBIT_TP_FRACTION      = 0.70   # debit spread:  take profit at 70% of max profit

# Expiry selection
ALLOW_SAME_DAY_EXPIRY_CREDIT_SPREADS = (
    _optional_env("ALLOW_SAME_DAY_EXPIRY_CREDIT_SPREADS", "false").lower() == "true"
)

# Strike-width config (points) — used when no promoted filter overrides
NIFTY_SPREAD_WIDTH     = _optional_int_env("NIFTY_SPREAD_WIDTH", 100)
BANKNIFTY_SPREAD_WIDTH = _optional_int_env("BANKNIFTY_SPREAD_WIDTH", 300)

# Minimum bid for sell leg (avoid zero-bid shorts)
MIN_SELL_LEG_BID = float(_optional_env("MIN_SELL_LEG_BID", "5.0"))

# Maximum bid-ask spread fraction allowed for each option leg
MAX_BID_ASK_SPREAD_FRACTION = float(_optional_env("MAX_BID_ASK_SPREAD_FRACTION", "0.30"))

# Minimum notional leg value to avoid near-zero/stale options
MIN_OPTION_LTP = float(_optional_env("MIN_OPTION_LTP", "2.0"))

# Gamma-risk guard: reject credit spreads within this many days of same-expiry
# unless ALLOW_SAME_DAY_EXPIRY_CREDIT_SPREADS=true
MIN_DAYS_TO_EXPIRY_CREDIT_SPREAD = _optional_int_env("MIN_DAYS_TO_EXPIRY_CREDIT_SPREAD", 1)

# ── Live vs virtual execution ──────────────────────────────────────────────────
# LIVE_ORDER_EXECUTION=false (default): spread fills are simulated using
# SpreadCandidate fill_price estimates — no Shoonya place_order calls are made.
# LIVE_ORDER_EXECUTION=true: real broker orders are placed.  Only enable when
# the live order path has been fully tested and broker connectivity is confirmed.
LIVE_ORDER_EXECUTION = _optional_env("LIVE_ORDER_EXECUTION", "false").lower() == "true"

# ──────────────────────────────────────────────────────────────
#   DISK GUARD
# ──────────────────────────────────────────────────────────────

MIN_FREE_DISK_MB = 2048   # abort if less than 2 GB free
MAX_RISK_PCT = OPTION_SPREAD_MAX_RISK_PCT
MAX_DAILY_LOSS_PCT = 0.05  # 5% daily loss limit
MAX_DAILY_LOSS_AMOUNT = VIRTUAL_CAPITAL * MAX_DAILY_LOSS_PCT  # ₹50,000

# Time constraints (IST)
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:15"
NO_NEW_ENTRY_AFTER = "15:05"
SESSION_START = "09:00"
SESSION_END = "15:25"  # Buffer after market close for EOD

# Agent loop
# Python scanner (get_strategy_signals) runs every SCAN_INTERVAL_SECONDS — no LLM cost.
SCAN_INTERVAL_SECONDS = 60
# Deprecated: market-analysis Gemini calls are no longer scheduled. Gemini is
# invoked for scanner candidates, trader chat, startup, and EOD only.
LOOP_INTERVAL_SECONDS = 900
SIGNAL_LLM_COOLDOWN_SECONDS = 60
MAX_PENDING_SIGNALS_PER_ITERATION = 8
TELEGRAM_POLL_INTERVAL_SECONDS = 3   # How often to check for new Telegram messages
LIMIT_ORDER_TIMEOUT_SECONDS = 300    # 5 minutes

# ──────────────────────────────────────────────────────────────
#   NSE TOKEN MAP (hardcoded Shoonya tokens)
# ──────────────────────────────────────────────────────────────

NSE_TOKENS = {
    "NIFTY": {"exchange": "NSE", "token": "26000"},
    "BANKNIFTY": {"exchange": "NSE", "token": "26009"},
    "INDIA VIX": {"exchange": "NSE", "token": "26017"},
}

# BSE SENSEX spot index token for live-feed subscription (recording only — not traded).
# Shoonya BSE spot index: token "1" on exchange "BSE".
# Override via SENSEX_SPOT_TOKEN / SENSEX_SPOT_EXCHANGE env vars if Shoonya ever changes it.
SENSEX_SPOT_TOKEN: str = _optional_env("SENSEX_SPOT_TOKEN", "1")
SENSEX_SPOT_EXCHANGE: str = _optional_env("SENSEX_SPOT_EXCHANGE", "BSE")

# NFO exchange for options/futures
NFO_EXCHANGE = "NFO"

# ──────────────────────────────────────────────────────────────
#   STRATEGY DOCS PATH
# ──────────────────────────────────────────────────────────────

# Read from existing master trading library, plus any NSE-specific docs
TRADING_FOLDER_PATH = _optional_env(
    "TRADING_FOLDER_PATH",
    str(BASE_DIR.parent)  # defaults to /TRADING
)

MASTER_STRATEGY_FILE = STRATEGIES_DIR / "master_trading_library.md"
MEMORY_FILE = JOURNALS_DIR / "memory.md"

# ──────────────────────────────────────────────────────────────
#   LOGGING SETUP
# ──────────────────────────────────────────────────────────────

def setup_logging():
    """Configure logging for the session."""
    from datetime import datetime
    log_file = LOGS_DIR / f"blitztrader_{datetime.now().strftime('%Y%m%d')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)
