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

# Ensure directories exist
JOURNALS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
STRATEGIES_DIR.mkdir(exist_ok=True)

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
TRADE_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY")
MAX_POSITIONS = 3
MAX_DAILY_TRADES = 10
MAX_RISK_PCT = 0.05  # 5% of capital per trade
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
