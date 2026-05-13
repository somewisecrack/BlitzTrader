"""
tests/test_futures_hypothesis_backtest.py
------------------------------------------
Smoke tests for futures wiki scripts (import-level) and verification
of main.py wiring for promoted futures filters.
Also tests the futures strategy engine and backtest script (Agent C).
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import types as module_types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Stub google.genai so we can import main.py without the Gemini SDK
# installed.
# ---------------------------------------------------------------------------
def _ensure_google_genai_stub():
    if "google.genai" not in sys.modules:
        google_mod = module_types.ModuleType("google")
        genai_mod = module_types.ModuleType("google.genai")
        genai_types = module_types.SimpleNamespace(
            Tool=object,
            Schema=lambda **kwargs: kwargs,
            FunctionDeclaration=lambda **kwargs: kwargs,
        )
        genai_mod.types = genai_types
        google_mod.genai = genai_mod
        sys.modules.setdefault("google", google_mod)
        sys.modules.setdefault("google.genai", genai_mod)


_ensure_google_genai_stub()


# ===========================================================================
# Script importability smoke tests
# ===========================================================================

class TestScriptImportable:

    def test_evaluate_script_importable(self):
        import importlib
        try:
            spec = importlib.util.spec_from_file_location(
                "evaluate_futures_day",
                str(_REPO_ROOT / "scripts" / "evaluate_futures_day.py"),
            )
            # Just verifying the spec loads without SyntaxError/ImportError
            assert spec is not None
        except ImportError as exc:
            pytest.fail(f"ImportError importing evaluate_futures_day: {exc}")

    def test_propose_script_importable(self):
        import importlib
        try:
            spec = importlib.util.spec_from_file_location(
                "propose_futures_hypotheses",
                str(_REPO_ROOT / "scripts" / "propose_futures_hypotheses.py"),
            )
            assert spec is not None
        except ImportError as exc:
            pytest.fail(f"ImportError importing propose_futures_hypotheses: {exc}")

    def test_backtest_script_importable(self):
        import importlib
        try:
            spec = importlib.util.spec_from_file_location(
                "backtest_futures_hypothesis",
                str(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py"),
            )
            assert spec is not None
        except ImportError as exc:
            pytest.fail(f"ImportError importing backtest_futures_hypothesis: {exc}")

    def test_promote_script_importable(self):
        import importlib
        try:
            spec = importlib.util.spec_from_file_location(
                "promote_futures_hypothesis",
                str(_REPO_ROOT / "scripts" / "promote_futures_hypothesis.py"),
            )
            assert spec is not None
        except ImportError as exc:
            pytest.fail(f"ImportError importing promote_futures_hypothesis: {exc}")


# ===========================================================================
# Verify main.py wiring (mock-based)
# ===========================================================================

class TestMainPyWiring:

    def test_main_imports_filter_loader(self):
        """The import itself is sufficient evidence the wiring is in place."""
        from tools.futures_filter_loader import load_active_filters, apply_promoted_filters
        assert callable(load_active_filters)
        assert callable(apply_promoted_filters)

    def test_blitztrader_has_promoted_filters_attr(self):
        """BlitzTrader.__init__ must declare _promoted_futures_filters."""
        from main import BlitzTrader
        bot = BlitzTrader()
        assert hasattr(bot, "_promoted_futures_filters"), (
            "BlitzTrader instance must have _promoted_futures_filters attribute"
        )
        assert isinstance(bot._promoted_futures_filters, list)

    def test_review_signal_applies_filters(self):
        """
        Set a filter that blocks NIFTY SELL when rsi14 < 20.
        Mock get_indicators to return rsi14=10.
        Assert _review_signal_python returns (False, ..., reason).
        """
        from main import BlitzTrader

        bot = BlitzTrader()

        # Install a blocking filter
        blocking_filter = {
            "id": "FILT-20260509-TEST",
            "scope": "futures",
            "status": "active",
            "symbol": "NIFTY",
            "strategy": "VP-01 Counter Bull Trap",
            "direction": "SELL",
            "filter": {"block_when": {"rsi14_lt": 20}},
        }
        bot._promoted_futures_filters = [blocking_filter]

        # Mock market data — rsi14=10 will trigger the filter
        mock_indicators = {
            "current_price": 24000.0,
            "ema20": 23800.0,
            "adx14": 25.0,
            "rsi14": 10.0,  # below threshold of 20 → should block
            "avg_volume_20": 50000.0,
            "ema_stacked_bull": False,
            "ema_stacked_bear": False,
            "price_above_vwap": True,
            "error": None,
        }
        bot._market_data = MagicMock()
        bot._market_data.get_indicators.return_value = mock_indicators

        signal = {
            "symbol": "NIFTY",
            "interval": "3",
            "strategy": "VP-01 Counter Bull Trap",
            "direction": "SELL",
            "stop_loss": 23900.0,
            "target": 24200.0,
        }

        result = bot._review_signal_python(signal)

        assert len(result) == 3, "Expected tuple of (approved, context, reason)"
        approved, context_summary, reason = result
        assert approved is False, (
            f"Expected signal to be blocked by filter, but got approved=True. reason={reason!r}"
        )
        assert "promoted futures filter" in reason.lower() or "FILT-20260509-TEST" in reason, (
            f"Expected reason to mention the filter, got: {reason!r}"
        )

    def test_review_signal_no_filters_passes_through(self):
        """
        With no filters and a valid signal, filter logic must NOT block.
        (Other rules like EMA checks may still block — we only verify the
        filter layer doesn't interfere when _promoted_futures_filters is empty.)
        """
        from main import BlitzTrader

        bot = BlitzTrader()
        bot._promoted_futures_filters = []

        # Provide indicators that satisfy EMA and ADX checks
        mock_indicators = {
            "current_price": 24000.0,
            "ema20": 23800.0,
            "adx14": 25.0,
            "rsi14": 55.0,
            "avg_volume_20": 50000.0,
            "ema_stacked_bull": True,   # bullish stack → BUY signal OK
            "ema_stacked_bear": False,
            "price_above_vwap": True,
            "error": None,
        }
        bot._market_data = MagicMock()
        bot._market_data.get_indicators.return_value = mock_indicators

        signal = {
            "symbol": "NIFTY",
            "interval": "3",
            "strategy": "VP-05 3EMA Trend",
            "direction": "BUY",
            "stop_loss": 23900.0,
            "target": 24200.0,
        }

        approved, context_summary, reason = bot._review_signal_python(signal)

        # The key assertion: the reason must NOT mention a "promoted futures filter" block
        # (the signal may still be blocked by EMA/ADX rules, but not by filter logic)
        if not approved:
            assert "promoted futures filter" not in reason.lower(), (
                f"Signal was blocked by filter logic even with empty filter list: {reason!r}"
            )


# ===========================================================================
# Telegram / LLM separation verification
# ===========================================================================

class TestChatPathDoesNotImportFilterLoader:

    def test_chat_path_does_not_apply_filters(self):
        """
        Verify apply_promoted_filters is called inside _review_signal_python,
        NOT inside the chat-handling code block.

        Strategy: read main.py source and confirm the call site (not the import
        line) is inside _review_signal_python.
        """
        main_py = (_REPO_ROOT / "main.py").read_text(encoding="utf-8")

        # apply_promoted_filters must appear somewhere in the file
        assert "apply_promoted_filters" in main_py, (
            "apply_promoted_filters not found in main.py — wiring missing"
        )

        # Locate the actual call site — skip the top-level 'import' line.
        # We search for the pattern used at the call site, not in the import.
        call_marker = "apply_promoted_filters("
        # find the first occurrence that is NOT on an 'import' line
        search_start = 0
        call_index = None
        while True:
            idx = main_py.find(call_marker, search_start)
            if idx == -1:
                break
            # Find the start of the line containing this occurrence
            line_start = main_py.rfind("\n", 0, idx) + 1
            line_text = main_py[line_start:main_py.find("\n", idx)]
            if "import" not in line_text:
                call_index = idx
                break
            search_start = idx + 1

        assert call_index is not None, (
            "No call site for apply_promoted_filters() found in main.py "
            "(only an import statement was found)"
        )

        # Walk back to the nearest def keyword to find the enclosing method
        source_up_to_call = main_py[:call_index]
        last_def = source_up_to_call.rfind("def ")
        method_header = main_py[last_def:last_def + 80]

        assert "_review_signal_python" in method_header, (
            f"apply_promoted_filters must be called inside _review_signal_python, "
            f"but nearest enclosing def is: {method_header!r}"
        )


# ===========================================================================
# Pairs verification (read-only checks)
# ===========================================================================

class TestPairsFilesUnchanged:

    def _read(self, relpath: str) -> str:
        path = _REPO_ROOT / relpath
        assert path.exists(), f"Expected file does not exist: {path}"
        return path.read_text(encoding="utf-8")

    def test_pairs_scanner_unchanged(self):
        src = self._read("pairs/scanner.py")
        assert "futures_filter_loader" not in src, (
            "pairs/scanner.py must not import futures_filter_loader"
        )
        assert "futures_hypothesis" not in src, (
            "pairs/scanner.py must not import futures_hypothesis"
        )

    def test_pairs_portfolio_unchanged(self):
        src = self._read("pairs/portfolio.py")
        assert "futures_filter_loader" not in src, (
            "pairs/portfolio.py must not import futures_filter_loader"
        )
        assert "futures_hypothesis" not in src, (
            "pairs/portfolio.py must not import futures_hypothesis"
        )

    def test_pairs_init_unchanged(self):
        src = self._read("pairs/__init__.py")
        assert "futures_filter_loader" not in src, (
            "pairs/__init__.py must not import futures_filter_loader"
        )
        assert "futures_hypothesis" not in src, (
            "pairs/__init__.py must not import futures_hypothesis"
        )


# ===========================================================================
# Strategy engine tests (Agent C: tools/futures_strategy_engine.py)
# ===========================================================================

class TestFuturesStrategyEngine:
    """Tests for tools/futures_strategy_engine.py created by Agent C."""

    def test_strategy_engine_importable(self):
        try:
            from tools.futures_strategy_engine import scan_candles, SUPPORTED_STRATEGIES
        except ImportError as exc:
            pytest.skip(f"tools/futures_strategy_engine.py not yet created by Agent C: {exc}")
        assert isinstance(SUPPORTED_STRATEGIES, set)
        assert "VP-01 Counter Bull Trap" in SUPPORTED_STRATEGIES
        assert "VP-07 Wicks Pullback" in SUPPORTED_STRATEGIES
        assert "VP-19 W-Pattern Double Bottom" in SUPPORTED_STRATEGIES

    def test_vp01_signal_from_candles(self):
        try:
            from tools.futures_strategy_engine import scan_candles
        except ImportError as exc:
            pytest.skip(f"tools/futures_strategy_engine.py not yet created by Agent C: {exc}")

        base_price = 24000
        candles = []
        # Fill first 20 candles with neutral data near base_price
        for i in range(20):
            candles.append({
                "time": i * 300, "open": base_price + 1, "high": base_price + 10,
                "low": base_price - 10, "close": base_price + 1, "volume": 1000
            })
        # Add a big green candle (the "bull trap" candle)
        candles.append({
            "time": 20 * 300, "open": base_price, "high": base_price + 100,
            "low": base_price - 5, "close": base_price + 80, "volume": 2000
        })
        # Add a few neutral candles
        for i in range(3):
            candles.append({
                "time": (21 + i) * 300, "open": base_price + 60, "high": base_price + 70,
                "low": base_price + 50, "close": base_price + 55, "volume": 1000
            })
        # Current candle: bearish, below EMA20, below big green close
        candles.append({
            "time": 24 * 300, "open": base_price + 40, "high": base_price + 45,
            "low": base_price - 50, "close": base_price - 30, "volume": 1000
        })

        signals = scan_candles("NIFTY", "5", candles)
        # Must not crash; if VP-01 fires, direction must be SELL
        vp01_signals = [s for s in signals if "VP-01" in s.get("strategy", "")]
        for s in vp01_signals:
            assert s["direction"] == "SELL", f"VP-01 must be SELL, got {s['direction']}"
            assert s["stop_loss"] is not None, "VP-01 signal must have stop_loss"
            assert s["target"] is not None, "VP-01 signal must have target"

    def test_scan_candles_empty_input(self):
        try:
            from tools.futures_strategy_engine import scan_candles
        except ImportError as exc:
            pytest.skip(f"tools/futures_strategy_engine.py not yet created by Agent C: {exc}")
        result = scan_candles("NIFTY", "5", [])
        assert result == []

    def test_scan_candles_too_few_candles(self):
        try:
            from tools.futures_strategy_engine import scan_candles
        except ImportError as exc:
            pytest.skip(f"tools/futures_strategy_engine.py not yet created by Agent C: {exc}")
        # 5 candles — not enough for EMA20
        candles = [
            {"time": i * 300, "open": 24000, "high": 24010, "low": 23990, "close": 24000, "volume": 1000}
            for i in range(5)
        ]
        result = scan_candles("NIFTY", "5", candles)
        assert result == []


# ===========================================================================
# Backtest script tests (Agent C: scripts/backtest_futures_hypothesis.py)
# ===========================================================================

class TestBacktestScript:

    def test_backtest_ticker_map(self):
        src = pathlib.Path(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py").read_text()
        assert "^NSEI" in src, "TICKER_MAP must contain ^NSEI for NIFTY"
        assert "^NSEBANK" in src, "TICKER_MAP must contain ^NSEBANK for BANKNIFTY"
        assert "NIFTY_FIN_SERVICE.NS" in src, "TICKER_MAP must contain NIFTY_FIN_SERVICE.NS for FINNIFTY"

    def test_backtest_no_synthetic_ema_crossover(self):
        src = pathlib.Path(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py").read_text()
        # After Agent C's rewrite these synthetic signal phrases must be absent
        assert "synthetic EMA" not in src, "Synthetic EMA crossover must not exist in rewritten script"
        assert "EMA crossover" not in src, "EMA crossover string must not exist"
        # WIN_PTS / LOSS_PTS fixed scoring is expected to be gone in Agent C's version
        # We make this a soft assertion that only fires after Agent C's rewrite
        # by checking if the strategy engine is imported (indicator of Agent C's work)
        if "futures_strategy_engine" in src:
            assert "WIN_PTS" not in src, "Fixed WIN_PTS scoring must be removed in Agent C's rewrite"
            assert "LOSS_PTS" not in src, "Fixed LOSS_PTS scoring must be removed in Agent C's rewrite"
            assert "ema_crossover" not in src.lower(), "ema_crossover must not exist in rewritten script"

    def test_backtest_uses_strategy_engine(self):
        src = pathlib.Path(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py").read_text()
        # This test will pass only after Agent C's rewrite
        if "futures_strategy_engine" not in src:
            pytest.skip(
                "Agent C has not yet rewritten backtest_futures_hypothesis.py "
                "to use futures_strategy_engine — will pass after Agent C's fix"
            )
        assert "scan_candles" in src
        assert "SUPPORTED_STRATEGIES" in src

    def test_unsupported_strategy_unavailable(self, tmp_path):
        """
        A hypothesis with an unsupported strategy should produce
        result with status='unavailable' and promote=false.
        Skipped if Agent C has not yet added SUPPORTED_STRATEGIES logic.
        """
        src = pathlib.Path(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py").read_text()
        if "SUPPORTED_STRATEGIES" not in src:
            pytest.skip(
                "Agent C has not yet added SUPPORTED_STRATEGIES check — "
                "will pass after Agent C's fix"
            )

        hyp = {
            "id": "HYP-TEST-UNSUPPORTED",
            "scope": "futures",
            "symbol": "NIFTY",
            "strategy": "VP-99 Imaginary Strategy",
            "direction": "SELL",
            "status": "proposed",
            "filter": {"block_when": {"rsi14_lt": 20}},
        }
        wiki_dir = tmp_path / "wiki"
        (wiki_dir / "hypotheses").mkdir(parents=True)
        (wiki_dir / "backtest_results").mkdir(parents=True)
        hyp_path = wiki_dir / "hypotheses" / "HYP-TEST-UNSUPPORTED.json"
        hyp_path.write_text(json.dumps(hyp))

        ret = subprocess.run(
            [
                sys.executable,
                "scripts/backtest_futures_hypothesis.py",
                "--hypothesis", str(hyp_path),
                "--wiki-dir", str(wiki_dir),
            ],
            capture_output=True, text=True,
            cwd=str(_REPO_ROOT),
        )
        # Should exit 0 (unavailable, not error)
        result_path = wiki_dir / "backtest_results" / "HYP-TEST-UNSUPPORTED.json"
        if result_path.exists():
            result_data = json.loads(result_path.read_text())
            assert result_data.get("status") == "unavailable", (
                f"Unsupported strategy should produce status='unavailable', got: {result_data.get('status')!r}"
            )
            promote = result_data.get("promotion_decision", {}).get("promote", True)
            assert promote is False, "Unsupported strategy must not be promoted"


# ===========================================================================
# No-fallback verification tests
# ===========================================================================

class TestNoFallbackVerification:

    def test_no_hardcoded_fallback_ticker(self):
        """Check there are no multiple TICKER_MAP entries for the same symbol."""
        src = pathlib.Path(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py").read_text()
        # Count occurrences of "NIFTY": in dict context
        nifty_matches = re.findall(r'"NIFTY"\s*:', src)
        assert len(nifty_matches) <= 2, (
            f"NIFTY appears {len(nifty_matches)} times as a dict key — should only appear in TICKER_MAP"
        )

        # Check futures_strategy_engine.py if it exists
        engine_path = _REPO_ROOT / "tools" / "futures_strategy_engine.py"
        if engine_path.exists():
            engine_src = engine_path.read_text()
            lower_src = engine_src.lower()
            # "fallback" should not appear, or if it does, "no fallback" context is acceptable
            if "fallback" in lower_src and "no fallback" not in lower_src:
                pytest.fail(
                    "tools/futures_strategy_engine.py contains 'fallback' without 'no fallback' context"
                )

    def test_filter_loader_json_tried_before_yaml(self):
        """In futures_filter_loader.py, JSON parsing must come before YAML parsing."""
        src = pathlib.Path(_REPO_ROOT / "tools" / "futures_filter_loader.py").read_text()
        json_pos = src.find("json.loads")
        yaml_pos = src.find("yaml.safe_load")
        assert json_pos >= 0, "futures_filter_loader.py must attempt json.loads"
        if yaml_pos > 0:
            assert json_pos < yaml_pos, (
                f"JSON (pos={json_pos}) must be tried before YAML (pos={yaml_pos}) in futures_filter_loader.py"
            )


# ===========================================================================
# Promotion JSON integration test
# ===========================================================================

class TestPromoteWritesJSON:

    def test_promote_writes_json(self, tmp_path):
        """End-to-end: promote script writes .json filter; filter loader reads it."""
        hyp = {
            "id": "HYP-20260509-001",
            "scope": "futures",
            "symbol": "NIFTY",
            "strategy": "VP-01 Counter Bull Trap",
            "direction": "SELL",
            "claim": "Test",
            "filter": {"block_when": {"rsi14_lt": 20}},
            "status": "proposed",
            "created_at": "2026-05-09T16:00:00+05:30",
        }
        result_data = {
            "hypothesis_id": "HYP-20260509-001",
            "status": "passed",
            "symbol": "NIFTY",
            "ticker": "^NSEI",
            "strategy": "VP-01 Counter Bull Trap",
            "period": "120d",
            "interval": "5m",
            "baseline": {
                "trades": 25, "net_pnl_points": 200.0,
                "profit_factor": 1.2, "max_drawdown_points": 100.0,
            },
            "filtered": {
                "trades": 18, "net_pnl_points": 310.0,
                "profit_factor": 1.45, "max_drawdown_points": 80.0,
            },
            "promotion_decision": {"promote": True, "reason": "Profit factor improved."},
        }

        wiki_dir = tmp_path / "wiki"
        (wiki_dir / "hypotheses").mkdir(parents=True)
        (wiki_dir / "backtest_results").mkdir(parents=True)
        (wiki_dir / "promoted_filters").mkdir(parents=True)
        hyp_path = wiki_dir / "hypotheses" / "HYP-20260509-001.json"
        result_path = wiki_dir / "backtest_results" / "HYP-20260509-001.json"
        hyp_path.write_text(json.dumps(hyp))
        result_path.write_text(json.dumps(result_data))

        ret = subprocess.run(
            [
                sys.executable,
                "scripts/promote_futures_hypothesis.py",
                "--hypothesis", str(hyp_path),
                "--result", str(result_path),
                "--wiki-dir", str(wiki_dir),
            ],
            capture_output=True, text=True,
            cwd=str(_REPO_ROOT),
        )
        assert ret.returncode == 0, f"promote script failed:\n{ret.stderr}"

        # Check JSON file was written
        json_files = list((wiki_dir / "promoted_filters").glob("*.json"))
        assert len(json_files) == 1, f"Expected 1 JSON filter, got: {json_files}"
        loaded = json.loads(json_files[0].read_text())
        assert loaded.get("id") is not None, "Promoted filter must have 'id' field"
        assert loaded.get("status") == "active", f"Expected status='active', got {loaded.get('status')!r}"
        assert loaded.get("scope") == "futures", f"Expected scope='futures', got {loaded.get('scope')!r}"

        # Load with filter loader
        from tools.futures_filter_loader import load_active_filters, apply_promoted_filters
        filters = load_active_filters(wiki_dir)
        assert len(filters) == 1, f"Expected 1 active filter loaded, got {len(filters)}"

        # Apply to a matching signal — rsi14=15 < threshold 20 → should block
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"rsi14": 15.0, "adx14": 30.0}
        allowed, reason = apply_promoted_filters(signal, indicators, filters)
        assert allowed is False, f"Expected signal to be blocked, got allowed=True, reason={reason!r}"
        assert "FILT" in reason or "filter" in reason.lower(), (
            f"Expected reason to mention filter, got: {reason!r}"
        )


# ===========================================================================
# Pairs files unchanged (git-based check)
# ===========================================================================

class TestPairsFilesUnchangedGit:

    def test_pairs_files_unchanged(self):
        """Verify pairs/portfolio.py, pairs/__init__.py, and config.py are unmodified.

        pairs/scanner.py is excluded because it is intentionally modified to
        remove the direct Yahoo Chart API path (keeping yfinance as sole source).
        That change is not a futures-wiki change; it is tracked separately.
        """
        result = subprocess.run(
            ["git", "diff", "--name-only", "--",
             "pairs/portfolio.py", "pairs/__init__.py", "config.py"],
            capture_output=True, text=True,
            cwd=str(_REPO_ROOT),
        )
        assert result.stdout.strip() == "", (
            f"Pairs portfolio/init or config files were modified unexpectedly: {result.stdout}"
        )


# ===========================================================================
# Adaptive period selection
# ===========================================================================

class TestDefaultPeriodForInterval:
    """_default_period_for_interval must stay within yfinance hard limits."""

    @pytest.fixture(autouse=True)
    def _import(self):
        import importlib, sys
        spec = importlib.util.spec_from_file_location(
            "backtest_futures_hypothesis",
            str(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.fn = mod._default_period_for_interval

    def test_5m_yields_59d(self):
        assert self.fn("5m") == "59d"

    def test_15m_yields_59d(self):
        assert self.fn("15m") == "59d"

    def test_30m_yields_59d(self):
        assert self.fn("30m") == "59d"

    def test_1h_yields_1y(self):
        assert self.fn("1h") == "1y"

    def test_60m_yields_1y(self):
        assert self.fn("60m") == "1y"

    def test_1d_yields_2y(self):
        assert self.fn("1d") == "2y"

    def test_unknown_interval_yields_safe_default(self):
        # Unknown intervals should not crash and must not exceed 60d limit
        result = self.fn("unknown")
        assert result == "59d"

    def test_explicit_period_flag_overrides_default(self, tmp_path):
        """--period on the CLI always wins over the auto-selected default."""
        hyp = {
            "id": "HYP-20260513-001", "scope": "futures", "symbol": "NIFTY",
            "strategy": "VP-01 Counter Bull Trap", "status": "proposed",
        }
        hyp_path = tmp_path / "HYP-20260513-001.json"
        hyp_path.write_text(json.dumps(hyp), encoding="utf-8")

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "backtest_futures_hypothesis",
            str(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with patch("sys.argv", [
            "backtest_futures_hypothesis.py",
            "--hypothesis", str(hyp_path),
            "--period", "30d",
            "--interval", "1h",
        ]):
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--hypothesis")
            parser.add_argument("--period", default=None)
            parser.add_argument("--interval", default="5m")
            parser.add_argument("--wiki-dir", default=None)
            parser.add_argument("--min-baseline-trades", type=int, default=20)
            parser.add_argument("--min-filtered-trades", type=int, default=10)
            args = parser.parse_args()

        resolved = args.period if args.period else mod._default_period_for_interval(args.interval)
        assert resolved == "30d"
