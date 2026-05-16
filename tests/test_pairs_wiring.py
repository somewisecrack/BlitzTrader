"""
tests/test_pairs_wiring.py

Validates the BlitzTrader + Pairs integration wiring:
  - config constants are present and correctly typed
  - search_scrip uses _post_private (not _api.searchscrip)
  - disk guard raises on low disk
  - pairs scan zero-candidate diagnostic message
  - combined status shape (futures + pairs)
  - Gemini not in pairs modules
  - context_builder pairs additions
"""
from __future__ import annotations

import math
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Stub heavy scientific libs so tests run without full install ──────────────
for lib in ("numpy", "pandas", "scipy", "statsmodels", "yfinance"):
    if lib not in sys.modules:
        sys.modules[lib] = types.ModuleType(lib)

if "google.genai" not in sys.modules:
    google_mod = sys.modules.get("google", types.ModuleType("google"))
    genai_mod = types.ModuleType("google.genai")
    genai_types = types.ModuleType("google.genai.types")
    genai_types.Tool = object
    genai_types.Schema = lambda **kwargs: kwargs
    genai_types.FunctionDeclaration = lambda **kwargs: kwargs
    genai_mod.types = genai_types
    google_mod.genai = genai_mod
    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = genai_types

import numpy as _np_stub
_np_stub.ndarray = object
_np_stub.nan = float("nan")

import pandas as _pd_stub
_pd_stub.DataFrame = object
_pd_stub.Series = object

# Root on path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
#   CONFIG CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigConstants(unittest.TestCase):
    """All pairs-related config constants must exist and have sane values."""

    def test_pairs_base_capital(self):
        from config import PAIRS_BASE_CAPITAL
        self.assertEqual(PAIRS_BASE_CAPITAL, 500_000)

    def test_pairs_leverage(self):
        from config import PAIRS_LEVERAGE
        self.assertEqual(PAIRS_LEVERAGE, 2)

    def test_pairs_gross_capital(self):
        from config import PAIRS_BASE_CAPITAL, PAIRS_LEVERAGE, PAIRS_GROSS_CAPITAL
        self.assertEqual(PAIRS_GROSS_CAPITAL, 1_000_000)
        self.assertEqual(PAIRS_GROSS_CAPITAL, PAIRS_BASE_CAPITAL * PAIRS_LEVERAGE)

    def test_no_max_open_pairs_constant(self):
        """MAX_OPEN_PAIRS removed — no hard cap on number of pairs."""
        import config
        self.assertFalse(
            hasattr(config, "MAX_OPEN_PAIRS"),
            "MAX_OPEN_PAIRS should have been removed from config",
        )

    def test_no_per_pair_capital_constant(self):
        """PER_PAIR_CAPITAL removed — allocation is dynamic."""
        import config
        self.assertFalse(
            hasattr(config, "PER_PAIR_CAPITAL"),
            "PER_PAIR_CAPITAL should have been removed from config",
        )

    def test_pairs_state_file_is_path(self):
        from config import PAIRS_STATE_FILE
        self.assertIsInstance(PAIRS_STATE_FILE, Path)

    def test_pairs_exchange(self):
        from config import PAIRS_EXCHANGE
        self.assertEqual(PAIRS_EXCHANGE, "NSE")

    def test_pairs_product(self):
        from config import PAIRS_PRODUCT
        self.assertIsInstance(PAIRS_PRODUCT, str)
        self.assertTrue(len(PAIRS_PRODUCT) > 0)

    def test_pair_intervals(self):
        from config import PAIR_INTERVALS
        self.assertIn("15m", PAIR_INTERVALS)
        self.assertIn("30m", PAIR_INTERVALS)
        self.assertIn("1h", PAIR_INTERVALS)

    def test_interval_periods_keys_match_intervals(self):
        from config import PAIR_INTERVALS, INTERVAL_PERIODS
        for iv in PAIR_INTERVALS:
            self.assertIn(iv, INTERVAL_PERIODS, f"Missing period for interval {iv}")

    def test_nifty50_symbols_count(self):
        from config import NIFTY50_SYMBOLS
        self.assertEqual(len(NIFTY50_SYMBOLS), 50)

    def test_nifty50_symbols_no_ns_suffix(self):
        from config import NIFTY50_SYMBOLS
        for sym in NIFTY50_SYMBOLS:
            self.assertFalse(sym.endswith(".NS"), f"{sym} must not include .NS suffix")

    def test_scanner_thresholds_present(self):
        from config import (
            ADF_PVALUE_LIMIT, BATCH_SIZE, BLOCK_LEN_FACTOR, ENSEMBLE_M,
            HURST_LIMIT, MAX_TOTAL_SIMS, MIN_BARS, RNG_SEED, SIMS_PER_DRAW,
            USE_BOOTSTRAP_RESID, Z_SCORE_LIMIT,
        )
        self.assertGreater(ADF_PVALUE_LIMIT, 0)
        self.assertGreater(BATCH_SIZE, 0)
        self.assertGreater(ENSEMBLE_M, 0)
        self.assertGreater(Z_SCORE_LIMIT, 0)
        self.assertIsInstance(USE_BOOTSTRAP_RESID, bool)

    def test_disk_guard_threshold(self):
        from config import MIN_FREE_DISK_MB
        self.assertGreaterEqual(MIN_FREE_DISK_MB, 1024, "Disk threshold must be at least 1 GB")

    def test_pair_scan_time_format(self):
        from config import PAIR_SCAN_TIME
        h, m = PAIR_SCAN_TIME.split(":")
        self.assertEqual(int(h), 8)
        self.assertEqual(int(m), 30)

    def test_pair_exit_time_format(self):
        from config import PAIR_EXIT_TIME
        h, m = PAIR_EXIT_TIME.split(":")
        self.assertEqual(int(h), 15)
        self.assertEqual(int(m), 15)


class TestDataExportUpload(unittest.TestCase):
    """EOD export upload should be clear and non-spammy on failure."""

    def test_rclone_failure_includes_stderr_tail(self):
        from tools.data_recorder import DataRecorder

        with tempfile.TemporaryDirectory() as tmp:
            recorder = DataRecorder(
                base_dir=Path(tmp),
                nse_tokens={},
                rclone_remote="gdrive",
                rclone_folder="BlitzTrader",
            )
            failure = MagicMock(
                returncode=1,
                stdout="",
                stderr="line one\nrateLimitExceeded",
            )
            with patch("subprocess.run", return_value=failure):
                with self.assertRaisesRegex(RuntimeError, "rateLimitExceeded"):
                    recorder.finalize_and_upload()

    def test_blitztrader_upload_failure_is_not_sent_twice(self):
        import main
        from main import BlitzTrader

        trader = BlitzTrader.__new__(BlitzTrader)
        trader._data_recorder = MagicMock()
        trader._data_recorder.finalize_and_upload.side_effect = RuntimeError("quota failed")
        trader._telegram = MagicMock()
        trader._data_export_upload_attempted = False

        after_eod = main.IST.localize(datetime(2026, 5, 13, 15, 20))
        with patch("main.datetime") as dt:
            dt.now.return_value = after_eod
            trader._upload_data_export()
            trader._upload_data_export()

        trader._data_recorder.finalize_and_upload.assert_called_once()
        trader._telegram.send_telegram.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
#   SEARCH SCRIP USES _POST_PRIVATE
# ─────────────────────────────────────────────────────────────────────────────

class TestSearchScripUsesPostPrivate(unittest.TestCase):
    """search_scrip must route through _post_private (OAuth REST path)."""

    def _build_client(self, scrips):
        from broker.shoonya_client import ShoonyaClient
        client = ShoonyaClient.__new__(ShoonyaClient)
        client._api = MagicMock()
        client._logged_in = True
        client._session = MagicMock()
        client._user_id = "TESTUSER"
        client._jkey = "testjkey"
        client._post_private = MagicMock(return_value={"stat": "Ok", "values": scrips})
        return client

    def test_search_scrip_calls_post_private(self):
        client = self._build_client([{"tsym": "INFY-EQ", "token": "1594"}])
        result = client.search_scrip("NSE", "INFY")
        client._post_private.assert_called_once()
        call_args = client._post_private.call_args
        self.assertEqual(call_args[0][0], "SearchScrip")

    def test_search_scrip_payload_has_exch_and_stext(self):
        client = self._build_client([])
        client.search_scrip("NSE", "INFY")
        payload = client._post_private.call_args[0][1]
        self.assertIn("exch", payload)
        self.assertIn("stext", payload)
        self.assertEqual(payload["exch"], "NSE")

    def test_search_scrip_does_not_call_api_searchscrip(self):
        """Regression: must NOT call the deprecated _api.searchscrip path."""
        client = self._build_client([{"tsym": "INFY-EQ", "token": "1594"}])
        client.search_scrip("NSE", "INFY")
        client._api.searchscrip.assert_not_called()

    def test_search_scrip_returns_values_on_ok(self):
        scrips = [{"tsym": "INFY-EQ", "token": "1594"}]
        client = self._build_client(scrips)
        result = client.search_scrip("NSE", "INFY")
        self.assertEqual(result, scrips)

    def test_search_scrip_returns_none_on_not_logged_in(self):
        from broker.shoonya_client import ShoonyaClient
        client = ShoonyaClient.__new__(ShoonyaClient)
        client._api = None
        client._logged_in = False
        result = client.search_scrip("NSE", "INFY")
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────────
#   RESOLVED SCRIP
# ─────────────────────────────────────────────────────────────────────────────

class TestResolvedScrip(unittest.TestCase):
    def test_resolved_scrip_dataclass(self):
        from broker.shoonya_client import ResolvedScrip
        rs = ResolvedScrip(symbol="INFY", tradingsymbol="INFY-EQ", token="1594")
        self.assertEqual(rs.symbol, "INFY")
        self.assertEqual(rs.tradingsymbol, "INFY-EQ")
        self.assertEqual(rs.token, "1594")

    def test_resolve_equity_symbol_prefers_eq(self):
        from broker.shoonya_client import ShoonyaClient
        client = ShoonyaClient.__new__(ShoonyaClient)
        client._api = MagicMock()
        client._logged_in = True
        client._user_id = "TEST"
        client._post_private = MagicMock(return_value={
            "stat": "Ok",
            "values": [
                {"tsym": "INFY-BE", "token": "9999"},
                {"tsym": "INFY-EQ", "token": "1594"},
            ],
        })
        result = client.resolve_equity_symbol("INFY")
        self.assertIsNotNone(result)
        self.assertEqual(result.tradingsymbol, "INFY-EQ")
        self.assertEqual(result.token, "1594")

    def test_resolve_equity_symbol_strips_ns_suffix(self):
        from broker.shoonya_client import ShoonyaClient
        client = ShoonyaClient.__new__(ShoonyaClient)
        client._api = MagicMock()
        client._logged_in = True
        client._user_id = "TEST"
        client._post_private = MagicMock(return_value={
            "stat": "Ok",
            "values": [{"tsym": "TCS-EQ", "token": "2044"}],
        })
        result = client.resolve_equity_symbol("TCS.NS")
        self.assertIsNotNone(result)
        self.assertEqual(result.symbol, "TCS")

    def test_resolve_equity_symbol_returns_none_on_empty(self):
        from broker.shoonya_client import ShoonyaClient
        client = ShoonyaClient.__new__(ShoonyaClient)
        client._api = MagicMock()
        client._logged_in = True
        client._user_id = "TEST"
        client._post_private = MagicMock(return_value={"stat": "Ok", "values": []})
        result = client.resolve_equity_symbol("UNKNOWN")
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────────
#   DISK GUARD
# ─────────────────────────────────────────────────────────────────────────────

class TestDiskGuard(unittest.TestCase):
    """_check_disk_space() must abort when free disk is below MIN_FREE_DISK_MB."""

    def _make_trader(self):
        """Build a minimal BlitzTrader instance without triggering real imports."""
        import importlib
        import sys as _sys

        # Stub out heavy modules needed by main.py imports
        for mod in ("NorenRestApiPy", "NorenRestApiPy.NorenApi", "pyotp",
                    "google", "google.genai", "websocket"):
            if mod not in _sys.modules:
                _sys.modules[mod] = types.ModuleType(mod)

        from main import BlitzTrader
        trader = BlitzTrader.__new__(BlitzTrader)
        trader._telegram = MagicMock()
        trader._pairs_scanner = MagicMock()
        trader._pairs_portfolio = MagicMock()
        trader._pairs_candidates = []
        trader._pairs_scan_done = False
        trader._pairs_opened = False
        return trader

    def test_disk_guard_passes_on_ample_disk(self):
        trader = self._make_trader()
        ample = MagicMock()
        ample.free = 10 * 1024 * 1024 * 1024  # 10 GB
        with patch("shutil.disk_usage", return_value=ample):
            trader._check_disk_space()   # should not raise

    def test_disk_guard_raises_on_low_disk(self):
        trader = self._make_trader()
        low = MagicMock()
        low.free = 100 * 1024 * 1024   # 100 MB — below any sane threshold
        with patch("shutil.disk_usage", return_value=low):
            with self.assertRaises(RuntimeError):
                trader._check_disk_space()

    def test_disk_guard_sends_telegram_on_abort(self):
        trader = self._make_trader()
        low = MagicMock()
        low.free = 100 * 1024 * 1024
        with patch("shutil.disk_usage", return_value=low):
            try:
                trader._check_disk_space()
            except RuntimeError:
                pass
        trader._telegram.send_telegram.assert_called_once()
        msg = trader._telegram.send_telegram.call_args[0][0]
        self.assertIn("DISK", msg.upper())


# ─────────────────────────────────────────────────────────────────────────────
#   ZERO-CANDIDATE DIAGNOSTIC
# ─────────────────────────────────────────────────────────────────────────────

class TestZeroCandidateDiagnostic(unittest.TestCase):
    """When pairs scan finds 0 candidates, Telegram must get an explanatory message."""

    def _make_trader(self):
        import sys as _sys
        for mod in ("NorenRestApiPy", "NorenRestApiPy.NorenApi", "pyotp",
                    "google", "google.genai", "websocket"):
            if mod not in _sys.modules:
                _sys.modules[mod] = types.ModuleType(mod)
        from main import BlitzTrader
        trader = BlitzTrader.__new__(BlitzTrader)
        trader._telegram = MagicMock()
        trader._pairs_scanner = MagicMock()
        trader._pairs_portfolio = MagicMock()
        trader._pairs_candidates = []
        trader._pairs_scan_done = False
        trader._pairs_opened = False
        trader._shoonya = MagicMock()
        return trader

    def test_zero_candidates_from_scan_sends_diagnostic(self):
        trader = self._make_trader()
        trader._pairs_scanner.run_scan.return_value = []
        trader._run_pairs_scan()
        msg = trader._telegram.send_telegram.call_args[0][0]
        self.assertIn("0 candidate", msg)

    def test_zero_candidates_at_open_sends_diagnostic(self):
        trader = self._make_trader()
        trader._pairs_scan_done = True
        trader._pairs_candidates = []
        trader._open_pairs_positions()
        msg = trader._telegram.send_telegram.call_args[0][0]
        self.assertIn("0", msg)

    def test_scan_not_done_at_open_sends_diagnostic(self):
        trader = self._make_trader()
        trader._pairs_scan_done = False
        trader._pairs_candidates = []
        trader._open_pairs_positions()
        msg = trader._telegram.send_telegram.call_args[0][0]
        self.assertIn("scan did not complete", msg)


# ─────────────────────────────────────────────────────────────────────────────
#   COMBINED STATUS SHAPE
# ─────────────────────────────────────────────────────────────────────────────

class TestCombinedStatusShape(unittest.TestCase):
    """_fmt_pairs_summary must return a non-empty string with key fields."""

    def _make_trader(self):
        import sys as _sys
        for mod in ("NorenRestApiPy", "NorenRestApiPy.NorenApi", "pyotp",
                    "google", "google.genai", "websocket"):
            if mod not in _sys.modules:
                _sys.modules[mod] = types.ModuleType(mod)
        from main import BlitzTrader
        return BlitzTrader.__new__(BlitzTrader)

    def test_fmt_pairs_summary_contains_capital(self):
        trader = self._make_trader()
        status = {
            "capital": 1_000_000,
            "realized_pnl": 500.0,
            "unrealized_pnl": -200.0,
            "net_pnl": 300.0,
            "open_pairs": 3,
            "positions": [],
        }
        summary = trader._fmt_pairs_summary(status)
        self.assertIn("capital", summary.lower())
        self.assertIn("300", summary)

    def test_fmt_pairs_summary_lists_positions(self):
        trader = self._make_trader()
        status = {
            "capital": 1_000_000,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
            "open_pairs": 1,
            "positions": [
                {"pair": "INFY/TCS", "timeframe": "1h", "closed": False,
                 "realized_pnl": 0.0, "unrealized_pnl": 150.0},
            ],
        }
        summary = trader._fmt_pairs_summary(status)
        self.assertIn("INFY/TCS", summary)


# ─────────────────────────────────────────────────────────────────────────────
#   CONTEXT BUILDER — PAIRS ADDITIONS
# ─────────────────────────────────────────────────────────────────────────────

class TestContextBuilderPairs(unittest.TestCase):
    def test_build_chat_context_includes_pairs_section(self):
        from context_builder import build_chat_context
        ctx = build_chat_context(
            chat_messages=[{"text": "how are pairs doing?"}],
            state_manager=MagicMock(get_state=lambda: {
                "daily_pnl": 0, "daily_pnl_pct": 0, "positions": []
            }),
            order_execution=MagicMock(),
            pairs_summary="Pairs capital: ₹10,00,000 | Open pairs: 2 | Net P&L: ₹500",
        )
        self.assertIn("PAIRS TRADING STATUS", ctx)
        self.assertIn("Open pairs: 2", ctx)

    def test_build_chat_context_without_pairs_omits_section(self):
        from context_builder import build_chat_context
        ctx = build_chat_context(
            chat_messages=[{"text": "status?"}],
            state_manager=MagicMock(get_state=lambda: {
                "daily_pnl": 0, "daily_pnl_pct": 0, "positions": []
            }),
            order_execution=MagicMock(),
        )
        self.assertNotIn("PAIRS TRADING STATUS", ctx)

    def test_build_eod_context_includes_pairs(self):
        from context_builder import build_eod_context
        ctx = build_eod_context(
            pairs_summary="Pairs capital: ₹10,00,000 | Net P&L: ₹1000"
        )
        self.assertIn("PAIRS TRADING EOD STATE", ctx)
        self.assertIn("Net P&L", ctx)

    def test_build_eod_context_without_pairs_clean(self):
        from context_builder import build_eod_context
        ctx = build_eod_context()
        self.assertNotIn("PAIRS TRADING EOD STATE", ctx)

    def test_build_pairs_scan_summary_zero(self):
        from context_builder import build_pairs_scan_summary
        s = build_pairs_scan_summary([], scan_done=True)
        self.assertIn("0 candidate", s)

    def test_build_pairs_scan_summary_not_run(self):
        from context_builder import build_pairs_scan_summary
        s = build_pairs_scan_summary([], scan_done=False)
        self.assertIn("not yet run", s)

    def test_build_pairs_scan_summary_with_candidates(self):
        from context_builder import build_pairs_scan_summary
        c = MagicMock()
        c.x_symbol = "INFY"
        c.y_symbol = "TCS"
        c.timeframe = "1h"
        c.prob_profit = 72.5
        c.z_score = 2.3
        c.half_life = 5
        s = build_pairs_scan_summary([c], scan_done=True)
        self.assertIn("INFY", s)
        self.assertIn("1 candidate", s)


# ─────────────────────────────────────────────────────────────────────────────
#   GEMINI NOT IN PAIRS MODULES
# ─────────────────────────────────────────────────────────────────────────────

class TestGeminiNotInPairsModules(unittest.TestCase):
    def _src(self, filename: str) -> str:
        return (Path(__file__).parent.parent / filename).read_text()

    def test_scanner_no_gemini(self):
        src = self._src("pairs/scanner.py")
        self.assertNotIn("AgentLoop", src)
        self.assertNotIn("agent_loop", src)
        self.assertNotIn("generate_content", src)
        self.assertNotIn("genai", src)

    def test_portfolio_no_gemini(self):
        src = self._src("pairs/portfolio.py")
        self.assertNotIn("AgentLoop", src)
        self.assertNotIn("agent_loop", src)
        self.assertNotIn("generate_content", src)
        self.assertNotIn("genai", src)


if __name__ == "__main__":
    unittest.main()
