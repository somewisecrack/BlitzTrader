"""
tests/test_safety.py — Safety audit tests for GammaBlast isolation from BlitzTrader.

Verifies that GammaBlast source files contain no references to BlitzTrader
paths, service names, env vars, or order-placement calls.

Run from /home/user/BlitzTrader/GammaBlast/:
    pytest tests/test_safety.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import subprocess

import pytest

GAMMADIR = pathlib.Path(__file__).parent.parent

# Directories / files that contain production Python code (not tests)
_PROD_PY = [
    str(GAMMADIR / "main.py"),
    str(GAMMADIR / "config.py"),
    str(GAMMADIR / "tools"),
    str(GAMMADIR / "broker"),
    str(GAMMADIR / "scripts"),
]

_ALL_PY_ARGS = ["--include", "*.py"]


def _grep(pattern: str, *paths, extra_flags: list = None, regex: bool = False) -> subprocess.CompletedProcess:
    """
    Run grep (or grep -E) over *paths*.
    Returns CompletedProcess; the caller asserts returncode != 0 to mean
    "no match found" (grep exits 1 when nothing matches — that's good).
    """
    flag = "-rE" if regex else "-r"
    cmd = ["grep", flag, pattern, *paths]
    if extra_flags:
        cmd[2:2] = extra_flags          # insert before the path args
    return subprocess.run(cmd, capture_output=True)


def _grep_py(pattern: str, *paths, regex: bool = False) -> subprocess.CompletedProcess:
    """grep restricted to *.py files."""
    flag = "-rE" if regex else "-r"
    return subprocess.run(
        ["grep", flag, "--include=*.py", pattern, *paths],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Path / folder isolation
# ---------------------------------------------------------------------------

def test_no_blitztrader_path_in_scripts():
    """No script should reference /opt/blitztrader."""
    result = _grep("/opt/blitztrader", str(GAMMADIR / "scripts"))
    assert result.returncode != 0, (
        f"Found /opt/blitztrader reference in scripts/:\n{result.stdout.decode()}"
    )


def test_no_blitztrader_rclone_folder():
    """
    No production .py or .sh file should hard-code RCLONE_FOLDER=BlitzTrader.
    Tests directory is excluded — the grep pattern appears legitimately there.
    """
    for path in _PROD_PY:
        result = _grep_py("RCLONE_FOLDER=BlitzTrader", path)
        assert result.returncode != 0, (
            f"Found RCLONE_FOLDER=BlitzTrader in {path}:\n"
            f"{result.stdout.decode()}"
        )
    # Also check .sh files (scripts dir only)
    result = subprocess.run(
        ["grep", "-r", "--include=*.sh", "RCLONE_FOLDER=BlitzTrader",
         str(GAMMADIR / "scripts")],
        capture_output=True,
    )
    assert result.returncode != 0, (
        f"Found RCLONE_FOLDER=BlitzTrader in .sh scripts:\n{result.stdout.decode()}"
    )


def test_no_blitztrader_service_ref_in_py():
    """No production .py file should reference the blitztrader.service unit."""
    result = _grep_py("blitztrader\\.service", *_PROD_PY)
    assert result.returncode != 0, (
        f"Found 'blitztrader.service' in Python files:\n{result.stdout.decode()}"
    )


# ---------------------------------------------------------------------------
# Order-placement isolation (virtual-only)
# ---------------------------------------------------------------------------

def test_no_place_order_in_main_or_tools():
    """
    GammaBlast is virtual-only — place_order must never be *called* in
    main.py, tools/, or scripts/.

    We search for ``place_order(`` (a function call) to distinguish actual
    calls from docstring statements like "NEVER calls place_order".
    """
    result = _grep_py("place_order(", *_PROD_PY, regex=False)
    assert result.returncode != 0, (
        f"Found 'place_order(' call (virtual-only scanner must not place orders):\n"
        f"{result.stdout.decode()}"
    )


def test_no_cancel_order_calls():
    """
    cancel_order must not be called anywhere in GammaBlast production source.
    Searches for ``cancel_order(`` to exclude docstring mentions.
    """
    result = _grep_py("cancel_order(", *_PROD_PY, regex=False)
    assert result.returncode != 0, (
        f"Found 'cancel_order(' call (virtual-only scanner must not cancel orders):\n"
        f"{result.stdout.decode()}"
    )


# ---------------------------------------------------------------------------
# Forbidden config constants (production files only, excluding tests)
# ---------------------------------------------------------------------------

def test_no_max_risk_per_trade_pct():
    """MAX_RISK_PER_TRADE_PCT must not be defined in any production .py file."""
    result = _grep_py("MAX_RISK_PER_TRADE_PCT", *_PROD_PY)
    assert result.returncode != 0, (
        f"Found MAX_RISK_PER_TRADE_PCT in GammaBlast source:\n"
        f"{result.stdout.decode()}"
    )


def test_no_max_daily_loss_pct():
    """MAX_DAILY_LOSS_PCT must not be defined in any production .py file."""
    result = _grep_py("MAX_DAILY_LOSS_PCT", *_PROD_PY)
    assert result.returncode != 0, (
        f"Found MAX_DAILY_LOSS_PCT in GammaBlast source:\n"
        f"{result.stdout.decode()}"
    )


def test_no_max_open_positions():
    """MAX_OPEN_POSITIONS must not be defined in any production .py file."""
    result = _grep_py("MAX_OPEN_POSITIONS", *_PROD_PY)
    assert result.returncode != 0, (
        f"Found MAX_OPEN_POSITIONS in GammaBlast source:\n"
        f"{result.stdout.decode()}"
    )


def test_no_no_new_entry_after_1505():
    """
    NO_NEW_ENTRY_AFTER with value 15:05 must not appear in production files.
    GammaBlast uses ENTRY_CUTOFF_IST = '15:12'.
    """
    for pattern in ("NO_NEW_ENTRY_AFTER=15:05", r"NO_NEW_ENTRY_AFTER.*15:05"):
        result = subprocess.run(
            ["grep", "-rE", "--include=*.py", pattern, *_PROD_PY],
            capture_output=True,
        )
        assert result.returncode != 0, (
            f"Found forbidden pattern '{pattern}' in GammaBlast production source:\n"
            f"{result.stdout.decode()}"
        )


# ---------------------------------------------------------------------------
# Telegram env-var isolation
# ---------------------------------------------------------------------------

def test_telegram_uses_gammablast_envvar():
    """
    tools/telegram_handler.py must reference GAMMABLAST_TELEGRAM_BOT_TOKEN —
    never the BlitzTrader equivalent.
    """
    handler_file = GAMMADIR / "tools" / "telegram_handler.py"
    assert handler_file.exists(), f"Expected {handler_file} to exist"
    content = handler_file.read_text(encoding="utf-8")
    assert "GAMMABLAST_TELEGRAM_BOT_TOKEN" in content, (
        "telegram_handler.py must use GAMMABLAST_TELEGRAM_BOT_TOKEN env var"
    )


# ---------------------------------------------------------------------------
# rclone folder name
# ---------------------------------------------------------------------------

def test_rclone_folder_not_blitztrader():
    """
    When RCLONE_FOLDER env var is not set, config.RCLONE_FOLDER must
    default to 'GammaBlast', not 'BlitzTrader'.
    """
    import os
    saved = os.environ.pop("RCLONE_FOLDER", None)
    try:
        from config import RCLONE_FOLDER
        assert RCLONE_FOLDER == "GammaBlast", (
            f"RCLONE_FOLDER default must be 'GammaBlast', got '{RCLONE_FOLDER}'"
        )
    finally:
        if saved is not None:
            os.environ["RCLONE_FOLDER"] = saved


# ---------------------------------------------------------------------------
# Broker client identity
# ---------------------------------------------------------------------------

def test_client_app_is_gammablast():
    """broker/shoonya_client.py must declare CLIENT_APP = 'GammaBlast'."""
    from broker.shoonya_client import CLIENT_APP
    assert CLIENT_APP == "GammaBlast", (
        f"Expected CLIENT_APP='GammaBlast', got '{CLIENT_APP}'"
    )


# ---------------------------------------------------------------------------
# No BlitzTrader imports in tools/
# ---------------------------------------------------------------------------

def test_no_blitztrader_import_in_tools():
    """
    No file under tools/ should have an actual Python import statement that
    imports from BlitzTrader.

    We match lines that begin (optionally with whitespace) with
    'import BlitzTrader' or 'from BlitzTrader import' — real import
    statements, not docstring mentions.
    """
    tools_dir = GAMMADIR / "tools"
    for py_file in tools_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("import BlitzTrader"), (
                f"{py_file.name} line contains actual 'import BlitzTrader': {line!r}"
            )
            # "from BlitzTrader import ..." — must not be an actual import line
            # (docstrings like "does NOT import anything from BlitzTrader" are fine)
            if stripped.startswith("from BlitzTrader"):
                assert False, (
                    f"{py_file.name} contains actual 'from BlitzTrader ...' import: {line!r}"
                )
