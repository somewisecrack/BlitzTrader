"""
config.py — GammaBlast configuration.
Virtual-only expiry-day options scanner for NIFTY (Tuesday) and SENSEX (Thursday).
"""
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "GammaBlast"

BASE_DIR = Path(__file__).parent

# ── env loading ───────────────────────────────────────────────────────────────

def _load_env():
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)

_load_env()


def _opt(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _opt_int(key: str, default: int) -> int:
    raw = _opt(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _opt_float(key: str, default: float) -> float:
    raw = _opt(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ── runtime paths ─────────────────────────────────────────────────────────────

RUNTIME_STORAGE_DIR = Path(_opt("RUNTIME_STORAGE_DIR", str(BASE_DIR))).expanduser()

JOURNALS_DIR      = RUNTIME_STORAGE_DIR / "journals"
LOGS_DIR          = RUNTIME_STORAGE_DIR / "logs"
DATA_EXPORTS_DIR  = RUNTIME_STORAGE_DIR / "data_exports"
STATE_FILE        = RUNTIME_STORAGE_DIR / "live_state.json"
CANDIDATE_AUDIT_DIR = RUNTIME_STORAGE_DIR / "candidate_signals"
PROMOTED_RULES_DIR  = BASE_DIR / "wiki" / "promoted_rules"

for _d in (JOURNALS_DIR, LOGS_DIR, DATA_EXPORTS_DIR, CANDIDATE_AUDIT_DIR, PROMOTED_RULES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Shoonya credentials ───────────────────────────────────────────────────────

SHOONYA_USER_ID      = _opt("SHOONYA_USER_ID")
SHOONYA_PASSWORD     = _opt("SHOONYA_PASSWORD")
SHOONYA_TOTP_SECRET  = _opt("SHOONYA_TOTP_SECRET")
SHOONYA_API_KEY      = _opt("SHOONYA_API_KEY")
SHOONYA_SECRET_CODE  = _opt("SHOONYA_SECRET_CODE")
SHOONYA_VENDOR_CODE  = _opt("SHOONYA_VENDOR_CODE")
SHOONYA_IMEI         = _opt("SHOONYA_IMEI")

# ── Telegram ──────────────────────────────────────────────────────────────────
# Use GAMMABLAST_ prefixed env vars; never fall back to BlitzTrader vars.

GAMMABLAST_TELEGRAM_BOT_TOKEN = _opt("GAMMABLAST_TELEGRAM_BOT_TOKEN")
GAMMABLAST_TELEGRAM_CHAT_ID   = _opt("GAMMABLAST_TELEGRAM_CHAT_ID")

# ── AI / Gemini (wiki-loop only — never called in live scanning path) ─────────

GEMINI_API_KEY    = _opt("GEMINI_API_KEY")
GEMINI_MODEL      = _opt("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_TIMEOUT_SECONDS = _opt_int("GEMINI_API_TIMEOUT_SECONDS", 60)

# ── Google Drive backup ───────────────────────────────────────────────────────

RCLONE_REMOTE = _opt("RCLONE_REMOTE", "gdrive")
RCLONE_FOLDER = _opt("RCLONE_FOLDER", "GammaBlast")   # NOT BlitzTrader

# ── capital — virtual-only ────────────────────────────────────────────────────

VIRTUAL_CAPITAL = _opt_int("VIRTUAL_CAPITAL", 100_000)   # ₹1,00,000

# ── instrument config ─────────────────────────────────────────────────────────

STRIKE_STEP = {
    "NIFTY":  _opt_int("NIFTY_STRIKE_STEP",  50),
    "SENSEX": _opt_int("SENSEX_STRIKE_STEP", 100),
}

ATM_OFFSETS = [-2, -1, 0, 1, 2]   # ATM-2 .. ATM+2

LOT_SIZE = {
    "NIFTY":  _opt_int("NIFTY_LOT_SIZE",  25),
    "SENSEX": _opt_int("SENSEX_LOT_SIZE", 10),
}

# ── session timing (IST) ──────────────────────────────────────────────────────

MARKET_OPEN_IST    = "09:15"
SCAN_START_IST     = "09:15"
ENTRY_CUTOFF_IST   = "15:12"   # no new virtual entries after this
EOD_FORCE_CLOSE_IST = "15:15"  # force-close all virtual positions
SESSION_END_IST    = "15:20"   # main loop exits after this

SCAN_INTERVAL_SECONDS = _opt_int("SCAN_INTERVAL_SECONDS", 60)

# ── candidate engine ──────────────────────────────────────────────────────────

# Stage 1 thresholds (COILED detection)
VOLUME_BURST_RATIO  = _opt_float("VOLUME_BURST_RATIO",  1.5)   # ≥1.5× baseline
VOLUME_BURST_MIN_IN_WINDOW = _opt_int("VOLUME_BURST_MIN_IN_WINDOW", 2)  # ≥2 of last 6 buckets
PREMIUM_MAX_RATIO   = _opt_float("PREMIUM_MAX_RATIO",  1.25)   # ≤1.25× session low
OI_HIGH_RATIO       = _opt_float("OI_HIGH_RATIO",      0.90)   # ≥90% of session high

# Stage 2 thresholds (RELEASE detection)
BID_IMBALANCE_MIN   = _opt_float("BID_IMBALANCE_MIN",  0.3)    # best-5 bid imbalance ≥+0.3
MIN_CANDIDATE_SCORE = _opt_float("MIN_CANDIDATE_SCORE", 0.5)

# ── virtual trailing exit ─────────────────────────────────────────────────────

TRAIL_ACTIVATION_MULT  = _opt_float("TRAIL_ACTIVATION_MULT",  2.0)   # start trailing at 2× entry
TRAIL_INITIAL_FRACTION = _opt_float("TRAIL_INITIAL_FRACTION", 0.40)  # trail at 40% below peak
TRAIL_TIGHT_MULT       = _opt_float("TRAIL_TIGHT_MULT",       4.0)   # tighten above 4×
TRAIL_TIGHT_FRACTION   = _opt_float("TRAIL_TIGHT_FRACTION",   0.25)  # tighten to 25% below peak
HARD_STOP_FRACTION     = _opt_float("HARD_STOP_FRACTION",     0.50)  # exit if drops 50% from entry
STALE_DATA_SECONDS     = _opt_int("STALE_DATA_SECONDS",       300)   # exit if no fresh quote 5 min

# ── drive mode ────────────────────────────────────────────────────────────────

LIVE_DRIVE_MODE = _opt("LIVE_DRIVE_MODE", "").lower() in {"1", "true", "yes", "on"}

# ── logging ───────────────────────────────────────────────────────────────────

def setup_logging():
    from datetime import datetime
    log_file = LOGS_DIR / f"gammablast_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)
