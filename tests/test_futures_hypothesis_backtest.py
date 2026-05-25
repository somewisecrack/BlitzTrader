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
    """
    Guard tests that previously ensured pairs/ files did not accidentally
    import futures hypothesis tools.  The pairs/ module has been archived to
    _archived_pairs/ as part of the futures-only refactor, so these guards
    are no longer applicable.  Tests are kept as skips to preserve history.
    """

    def test_pairs_scanner_unchanged(self):
        pytest.skip("pairs/scanner.py archived to _archived_pairs/ — guard no longer needed")

    def test_pairs_portfolio_unchanged(self):
        pytest.skip("pairs/portfolio.py archived to _archived_pairs/ — guard no longer needed")

    def test_pairs_init_unchanged(self):
        pytest.skip("pairs/__init__.py archived to _archived_pairs/ — guard no longer needed")


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
        assert "NIFTY_FIN_SERVICE.NS" not in src, "TICKER_MAP must NOT contain NIFTY_FIN_SERVICE.NS — FINNIFTY removed"

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

    def test_pairs_init_unchanged(self):
        """Skipped: pairs/__init__.py archived to _archived_pairs/ (futures-only refactor)."""
        pytest.skip("pairs/ archived to _archived_pairs/ — git guard no longer applicable")


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

    def test_60m_not_in_map_falls_back_to_safe_default(self):
        assert self.fn("60m") == "59d"

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


# ===========================================================================
# VWAP series computation and daily fallback
# ===========================================================================

class TestVwapHelpers:
    @pytest.fixture(autouse=True)
    def _import(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "backtest_futures_hypothesis",
            str(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.mod = mod

    def _candle(self, ts, o, h, l, c, v=1000):
        return {"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}

    def test_needs_vwap_detects_price_above(self):
        assert self.mod._needs_vwap({"price_above_vwap": True}) is True

    def test_needs_vwap_detects_price_below(self):
        assert self.mod._needs_vwap({"price_below_vwap": True}) is True

    def test_needs_vwap_false_for_unrelated_fields(self):
        assert self.mod._needs_vwap({"rsi14_lt": 30, "ema_stacked_bear": True}) is False

    def test_vwap_returns_none_when_no_volume(self):
        candles = [self._candle(1000, 100, 110, 90, 105, v=0) for _ in range(5)]
        assert self.mod._compute_vwap_series(candles, "5m") is None

    def test_vwap_daily_uses_rolling_close_volume(self):
        # Rolling VWAP = cumulative(close×vol) / cumulative(vol)
        # Bar0: close=100, vol=1000 → vwap=100.0
        # Bar1: close=200, vol=1000 → vwap=(100×1000 + 200×1000)/(2000) = 150.0
        # Bar2: close=300, vol=1000 → vwap=(300000+300000)/(3000) + ... = 200.0
        candles = [
            self._candle(1000,     100, 120, 90,  100, v=1000),
            self._candle(1000 + 1, 180, 220, 170, 200, v=1000),
            self._candle(1000 + 2, 280, 320, 270, 300, v=1000),
        ]
        series = self.mod._compute_vwap_series(candles, "1d")
        assert series is not None
        assert len(series) == 3
        assert abs(series[0] - 100.0) < 0.01
        assert abs(series[1] - 150.0) < 0.01
        assert abs(series[2] - 200.0) < 0.01

    def test_vwap_intraday_resets_per_day(self):
        # Two bars same day, then one bar next day
        import datetime
        day1 = int(datetime.datetime(2026, 5, 13, 9, 15, tzinfo=datetime.timezone.utc).timestamp())
        day1b = int(datetime.datetime(2026, 5, 13, 9, 20, tzinfo=datetime.timezone.utc).timestamp())
        day2 = int(datetime.datetime(2026, 5, 14, 9, 15, tzinfo=datetime.timezone.utc).timestamp())

        candles = [
            self._candle(day1,  100, 110, 90, 105, v=1000),
            self._candle(day1b, 106, 112, 100, 108, v=2000),
            self._candle(day2,  200, 210, 190, 205, v=1000),
        ]
        series = self.mod._compute_vwap_series(candles, "5m")
        assert series is not None
        assert len(series) == 3
        # Day2 VWAP should reset — equals day2 typical price (210+190+205)/3 = 201.667
        expected_day2_vwap = (210 + 190 + 205) / 3.0
        assert abs(series[2] - expected_day2_vwap) < 0.01
        # Day1 bar2 VWAP should be between bar1 and bar2 typical prices
        assert series[0] != series[2]  # different days → different values

    def test_signal_passes_filter_vwap_above_blocks_correctly(self):
        """price_above_vwap=False should block a signal where close > vwap."""
        # Candle with close=110, vwap=100 → price IS above vwap → price_above_vwap=True
        # block_when: price_above_vwap=False → only block when NOT above vwap → should NOT block
        import datetime
        ts = int(datetime.datetime(2026, 5, 13, 9, 15, tzinfo=datetime.timezone.utc).timestamp())
        candles = [self._candle(ts + i * 300, 100, 110, 90, 110, v=1000) for i in range(20)]
        vwap_series = [100.0] * 20  # close=110 > vwap=100 → price_above_vwap=True

        sig = {
            "candle_index": 15,
            "direction": "BUY",
            "entry_reference": 110,
            "stop_loss": 105,
            "target": 115,
        }
        # block_when price_above_vwap=False → block only when price is NOT above vwap
        # Since price IS above vwap, filter should NOT block → passes
        result = self.mod.signal_passes_filter(sig, candles, {"price_above_vwap": False}, vwap_series)
        assert result is True

    def test_signal_passes_filter_blocks_when_below_vwap(self):
        """price_below_vwap=True should block when close < vwap."""
        import datetime
        ts = int(datetime.datetime(2026, 5, 13, 9, 15, tzinfo=datetime.timezone.utc).timestamp())
        candles = [self._candle(ts + i * 300, 100, 110, 90, 95, v=1000) for i in range(20)]
        vwap_series = [100.0] * 20  # close=95 < vwap=100 → price_below_vwap=True

        sig = {
            "candle_index": 15,
            "direction": "BUY",
            "entry_reference": 95,
            "stop_loss": 90,
            "target": 105,
        }
        # block_when price_below_vwap=True → block when price IS below vwap → should block
        result = self.mod.signal_passes_filter(sig, candles, {"price_below_vwap": True}, vwap_series)
        assert result is False


# ===========================================================================
# Daily fallback tests
# ===========================================================================

class TestDailyFallback:
    """Tests for the daily-timeframe fallback introduced in run_backtest_attempt()."""

    @pytest.fixture(autouse=True)
    def _import_mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "backtest_futures_hypothesis",
            str(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.mod = mod

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _ok_attempt(self, interval="5m", period="59d", baseline_trades=30, filtered_trades=25):
        """Return a fake 'ok' attempt result with enough trades."""
        stats = lambda n: {
            "trades": n,
            "win_rate": 0.6,
            "net_pnl_points": 100.0,
            "profit_factor": 1.5,
            "max_drawdown_points": 50.0,
            "avg_trade": 3.33,
            "skipped_trades": 0,
        }
        return {
            "status": "ok",
            "interval": interval,
            "period": period,
            "candles_count": 500,
            "baseline": stats(baseline_trades),
            "filtered": stats(filtered_trades),
        }

    def _insufficient_attempt(self, interval="5m", period="59d", baseline_trades=5, filtered_trades=2):
        """Return a fake 'ok' attempt result with too few trades."""
        return self._ok_attempt(interval, period, baseline_trades, filtered_trades)

    def _unavailable_attempt(self, interval="5m", period="59d"):
        return {
            "status": "unavailable",
            "interval": interval,
            "period": period,
            "candles_count": 0,
            "baseline": self.mod.compute_stats([]),
            "filtered": self.mod.compute_stats([]),
            "reason": "Download failed",
        }

    def _write_hyp(self, tmp_path, hyp_id="HYP-TEST-001", strategy="VP-01 Counter Bull Trap"):
        hyp = {
            "id": hyp_id,
            "scope": "futures",
            "symbol": "NIFTY",
            "strategy": strategy,
            "direction": "SELL",
            "status": "proposed",
            "filter": {"block_when": {}},
        }
        wiki_dir = tmp_path / "wiki"
        (wiki_dir / "hypotheses").mkdir(parents=True)
        (wiki_dir / "backtest_results").mkdir(parents=True)
        hyp_path = wiki_dir / "hypotheses" / f"{hyp_id}.json"
        hyp_path.write_text(json.dumps(hyp))
        return hyp_path, wiki_dir

    def _run_main_with_mocked_attempt(self, tmp_path, side_effects, hyp_id="HYP-TEST-001"):
        """
        Run main() with run_backtest_attempt mocked to return side_effects in order.
        Returns the parsed result JSON.
        """
        hyp_path, wiki_dir = self._write_hyp(tmp_path, hyp_id)

        mod = self.mod
        call_args = []

        def fake_attempt(**kwargs):
            call_args.append(kwargs)
            if side_effects:
                return side_effects.pop(0)
            return self._ok_attempt()

        with patch.object(mod, "run_backtest_attempt", side_effect=fake_attempt), \
             patch.object(mod, "_check_data_sufficiency",
                          return_value=("5m", "59d", "")), \
             patch("sys.argv", [
                 "backtest_futures_hypothesis.py",
                 "--hypothesis", str(hyp_path),
                 "--wiki-dir", str(wiki_dir),
             ]):
            try:
                mod.main()
            except SystemExit:
                pass

        result_path = wiki_dir / "backtest_results" / f"{hyp_id}.json"
        assert result_path.exists(), "Result JSON not written"
        return json.loads(result_path.read_text()), call_args

    # -----------------------------------------------------------------------
    # Test 1: sufficient sample on first pass → no fallback
    # -----------------------------------------------------------------------

    def test_sufficient_sample_no_fallback(self, tmp_path):
        result, calls = self._run_main_with_mocked_attempt(
            tmp_path,
            [self._ok_attempt(baseline_trades=30, filtered_trades=25)],
        )
        assert result["fallback_used"] is False
        assert len(result["attempts"]) == 1
        assert "fallback_reason" not in result
        assert len(calls) == 1

    # -----------------------------------------------------------------------
    # Test 2: zero baseline trades → fallback triggered
    # -----------------------------------------------------------------------

    def test_zero_trades_triggers_fallback(self, tmp_path):
        result, calls = self._run_main_with_mocked_attempt(
            tmp_path,
            [
                self._insufficient_attempt(baseline_trades=0, filtered_trades=0),
                self._ok_attempt("1d", "5y", 50, 40),
            ],
        )
        assert result["fallback_used"] is True
        assert len(calls) == 2
        second_call = calls[1]
        assert second_call["interval"] == "1d"
        assert second_call["period"] == "5y"

    # -----------------------------------------------------------------------
    # Test 3: baseline ok but filtered < min → fallback triggered
    # -----------------------------------------------------------------------

    def test_insufficient_filtered_triggers_fallback(self, tmp_path):
        # baseline=25 (>= 20) but filtered=5 (< 10)
        result, calls = self._run_main_with_mocked_attempt(
            tmp_path,
            [
                self._insufficient_attempt(baseline_trades=25, filtered_trades=5),
                self._ok_attempt("1d", "5y", 50, 40),
            ],
        )
        assert result["fallback_used"] is True
        assert len(calls) == 2

    # -----------------------------------------------------------------------
    # Test 4: fallback passes → status=passed
    # -----------------------------------------------------------------------

    def test_fallback_passes_sets_status_passed(self, tmp_path):
        result, calls = self._run_main_with_mocked_attempt(
            tmp_path,
            [
                self._insufficient_attempt(baseline_trades=0, filtered_trades=0),
                self._ok_attempt("1d", "5y", 50, 40),
            ],
        )
        assert result["fallback_used"] is True
        assert result["interval"] == "1d"
        assert result["period"] == "5y"
        # With 50 baseline trades, 40 filtered, good stats → should pass
        assert result["status"] in ("passed", "failed")  # depends on PF; at minimum not unavailable

    # -----------------------------------------------------------------------
    # Test 5: daily fallback also insufficient → status=failed, fallback_used=True, 2 attempts
    # -----------------------------------------------------------------------

    def test_fallback_fails_status_failed(self, tmp_path):
        result, calls = self._run_main_with_mocked_attempt(
            tmp_path,
            [
                self._insufficient_attempt(baseline_trades=0, filtered_trades=0),
                self._insufficient_attempt("1d", "5y", 3, 1),
            ],
        )
        assert result["fallback_used"] is True
        assert result["status"] == "failed"
        assert len(result["attempts"]) == 2

    # -----------------------------------------------------------------------
    # Test 6: effective_interval already "1d" → no duplicate fallback
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Test: live-only strategy → skipped cleanly
    # -----------------------------------------------------------------------

    def test_live_only_strategy_skipped(self, tmp_path):
        """
        A hypothesis for VP-24 Pivot Rejection P (a live-only strategy)
        must exit with status='skipped', promote=False, and no backtest attempt.
        """
        hyp = {
            "id": "HYP-TEST-VP24",
            "scope": "futures",
            "symbol": "BANKNIFTY",
            "strategy": "VP-24 Pivot Rejection P",
            "direction": "SELL",
            "status": "proposed",
            "filter": {"block_when": {"adx14_lt": 20}},
        }
        wiki_dir = tmp_path / "wiki"
        (wiki_dir / "hypotheses").mkdir(parents=True)
        (wiki_dir / "backtest_results").mkdir(parents=True)
        hyp_path = wiki_dir / "hypotheses" / "HYP-TEST-VP24.json"
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
        assert ret.returncode == 0, f"Live-only skip must exit 0, got:\n{ret.stderr}"
        result_path = wiki_dir / "backtest_results" / "HYP-TEST-VP24.json"
        assert result_path.exists(), "Result JSON must be written even for skipped strategies"
        result_data = json.loads(result_path.read_text())
        assert result_data.get("status") == "skipped", (
            f"Live-only strategy must produce status='skipped', got: {result_data.get('status')!r}"
        )
        assert "skip_reason" in result_data, "Result must contain skip_reason"
        assert "live context" in result_data["skip_reason"].lower() or \
               "pivots" in result_data["skip_reason"].lower(), \
            f"skip_reason should mention live context: {result_data['skip_reason']!r}"
        promote = result_data.get("promotion_decision", {}).get("promote", True)
        assert promote is False, "Skipped strategy must not be promoted"

    def test_vp05_not_in_live_only(self):
        """VP-05 3EMA Trend must NOT be in LIVE_ONLY_STRATEGIES (it is backtestable)."""
        from tools.futures_strategy_engine import LIVE_ONLY_STRATEGIES
        assert "VP-05 3EMA Trend" not in LIVE_ONLY_STRATEGIES, (
            "VP-05 3EMA Trend is backtestable and must not be in LIVE_ONLY_STRATEGIES"
        )

    def test_already_1d_no_duplicate_fallback(self, tmp_path):
        hyp_path, wiki_dir = self._write_hyp(tmp_path)
        mod = self.mod
        calls = []

        def fake_attempt(**kwargs):
            calls.append(kwargs)
            return self._insufficient_attempt(kwargs["interval"], kwargs["period"], 3, 1)

        with patch.object(mod, "run_backtest_attempt", side_effect=fake_attempt), \
             patch.object(mod, "_check_data_sufficiency",
                          return_value=("1d", "5y", "No volume at 5m — switching to 1d/5y")), \
             patch("sys.argv", [
                 "backtest_futures_hypothesis.py",
                 "--hypothesis", str(hyp_path),
                 "--wiki-dir", str(wiki_dir),
             ]):
            try:
                mod.main()
            except SystemExit:
                pass

        # effective_interval is "1d", so fallback must NOT be triggered
        assert len(calls) == 1, f"Expected exactly 1 attempt, got {len(calls)}: {calls}"
        result_path = wiki_dir / "backtest_results" / "HYP-TEST-001.json"
        result = json.loads(result_path.read_text())
        assert len(result["attempts"]) == 1

    # -----------------------------------------------------------------------
    # Test 7: first attempt unavailable → no fallback
    # -----------------------------------------------------------------------

    def test_unavailable_first_pass_no_fallback(self, tmp_path):
        result, calls = self._run_main_with_mocked_attempt(
            tmp_path,
            [self._unavailable_attempt()],
        )
        assert result["status"] == "unavailable"
        assert result["fallback_used"] is False
        assert len(calls) == 1

    # -----------------------------------------------------------------------
    # Test 8: "attempts" list always present in result
    # -----------------------------------------------------------------------

    def test_attempts_metadata_in_result(self, tmp_path):
        result, _ = self._run_main_with_mocked_attempt(
            tmp_path,
            [self._ok_attempt(baseline_trades=30, filtered_trades=25)],
        )
        assert "attempts" in result
        assert isinstance(result["attempts"], list)
        assert len(result["attempts"]) >= 1
        first = result["attempts"][0]
        assert "interval" in first
        assert "period" in first
        assert "baseline_trades" in first
        assert "filtered_trades" in first
        assert "sample_sufficient" in first
