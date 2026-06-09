"""
tests/test_options_chain_adapters.py

Tests for the OptionsChain adapter methods used by ATMOptionRecorder:
  - get_nearest_expiry()
  - resolve_option()

Also covers eod_backup runtime_dir default path consistency.
"""
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_chain(client=None):
    from tools.options_chain import OptionsChain
    return OptionsChain(client or MagicMock())


def _shoonya_search_result(symbol, expiry_date, strike, option_type):
    """Build a minimal Shoonya search_scrip result row."""
    exd = expiry_date.strftime("%-d-%b-%Y")     # "26-Jun-2026" → stored as-is
    exp_suffix = expiry_date.strftime("%d%b%y").upper()  # "26JUN26"
    option_code = {"CE": "C", "PE": "P"}[option_type]
    tsym = f"{symbol}{exp_suffix}{option_code}{strike}"
    return {
        "instname": "OPTIDX",
        "token": f"TOK{strike}{option_type}",
        "tsym": tsym,
        "exd": expiry_date.strftime("%-d-%b-%Y"),
        "strprc": str(strike),
        "optt": option_type,
        "ls": "50",
    }


# ── Tests: get_nearest_expiry ──────────────────────────────────────────────────

class TestGetNearestExpiry:
    def test_returns_string_in_expected_format(self):
        """get_nearest_expiry returns 'D-MMM-YYYY' (no zero-pad on day)."""
        client = MagicMock()
        expiry = date(2026, 6, 26)
        # search_scrip returns one option row for this expiry
        client.search_scrip.return_value = [_shoonya_search_result("NIFTY", expiry, 24500, "CE")]
        chain = _make_chain(client)
        result = chain.get_nearest_expiry("NIFTY")
        assert result is not None
        assert "JUN" in result
        assert "2026" in result
        # Should be parseable back to a date via datetime.strptime
        from datetime import datetime
        # strptime %d accepts both zero-padded ("06") and non-padded ("6") days;
        # %b is case-insensitive in CPython so "JUN" and "Jun" both work.
        parsed = datetime.strptime(result, "%d-%b-%Y").date()
        assert parsed == expiry

    def test_returns_none_when_no_expiries(self):
        client = MagicMock()
        client.search_scrip.return_value = []
        chain = _make_chain(client)
        result = chain.get_nearest_expiry("NIFTY")
        assert result is None

    def test_returns_nearest_when_multiple_expiries(self):
        """When multiple expiries are available, the earliest is returned."""
        client = MagicMock()
        near = date(2026, 6, 26)
        far = date(2026, 7, 31)
        rows = [
            _shoonya_search_result("NIFTY", near, 24500, "CE"),
            _shoonya_search_result("NIFTY", far, 24500, "CE"),
        ]
        client.search_scrip.return_value = rows
        chain = _make_chain(client)
        result = chain.get_nearest_expiry("NIFTY")
        # Nearest must appear first
        assert result is not None
        from datetime import datetime
        for fmt in ("%-d-%b-%Y", "%d-%b-%Y"):
            try:
                parsed = datetime.strptime(result, fmt).date()
                break
            except ValueError:
                continue
        assert parsed == near

    def test_unknown_symbol_returns_none(self):
        client = MagicMock()
        chain = _make_chain(client)
        result = chain.get_nearest_expiry("SENSEX")
        assert result is None


# ── Tests: resolve_option ──────────────────────────────────────────────────────

class TestResolveOption:
    def test_resolves_valid_contract(self):
        client = MagicMock()
        expiry = date(2026, 6, 26)
        expiry_str = expiry.strftime("%-d-%b-%Y")
        client.search_scrip.return_value = [
            _shoonya_search_result("NIFTY", expiry, 24500, "CE")
        ]
        chain = _make_chain(client)
        result = chain.resolve_option("NIFTY", expiry_str, 24500, "CE")
        assert result is not None
        assert "token" in result
        assert "tsym" in result
        assert result["token"] == "TOK24500CE"

    def test_bad_expiry_string_returns_none(self):
        chain = _make_chain()
        result = chain.resolve_option("NIFTY", "not-a-date", 24500, "CE")
        assert result is None

    def test_resolve_option_pe(self):
        client = MagicMock()
        expiry = date(2026, 6, 26)
        expiry_str = expiry.strftime("%-d-%b-%Y")
        client.search_scrip.return_value = [
            _shoonya_search_result("NIFTY", expiry, 24500, "PE")
        ]
        chain = _make_chain(client)
        result = chain.resolve_option("NIFTY", expiry_str, 24500, "PE")
        assert result is not None
        assert result["token"] == "TOK24500PE"

    def test_no_matching_scrip_returns_none(self):
        client = MagicMock()
        client.search_scrip.return_value = []
        expiry = date(2026, 6, 26)
        expiry_str = expiry.strftime("%-d-%b-%Y")
        chain = _make_chain(client)
        result = chain.resolve_option("NIFTY", expiry_str, 24500, "CE")
        assert result is None


# ── Tests: eod_backup runtime_dir default ─────────────────────────────────────

class TestEodBackupRuntimeDirDefault:
    def test_default_runtime_dir_matches_config_default(self):
        """eod_backup default runtime_dir must match config.py default (both /opt/blitztrader)."""
        import subprocess, sys
        # Extract default from eod_backup.py argparse help text
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "scripts/eod_backup.py", "--help"],
            capture_output=True, text=True,
            cwd=repo_root,
        )
        assert "/opt/blitztrader/runtime" not in result.stdout, (
            "eod_backup default runtime dir must not be /opt/blitztrader/runtime "
            "(would mismatch config.py when RUNTIME_STORAGE_DIR is unset)"
        )
        assert "/opt/blitztrader" in result.stdout

    def test_rclone_default_remote_is_gdrive(self):
        """When RCLONE_REMOTE is not set, eod_backup uses 'gdrive'."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "RCLONE_REMOTE"}
        import subprocess, sys
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "scripts/eod_backup.py", "--help"],
            capture_output=True, text=True,
            cwd=repo_root,
            env={**env, "PYTHONPATH": str(repo_root)},
        )
        assert "gdrive" in result.stdout
