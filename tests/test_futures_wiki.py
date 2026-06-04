"""
tests/test_futures_wiki.py
--------------------------
Tests for hypothesis and backtest result schema helpers from
tools/futures_hypothesis.py.
Also tests journal parsing via scripts/evaluate_futures_day.py.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.futures_hypothesis import (
    validate_hypothesis,
    validate_backtest_result,
    promote_if_passed,
    write_hypothesis,
    load_hypothesis,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_hypothesis(**overrides) -> dict:
    """Return a well-formed hypothesis, optionally overridden."""
    base = {
        "id": "HYP-20260509-001",
        "scope": "futures",
        "symbol": "NIFTY",
        "strategy": "VP-01 Counter Bull Trap",
        "direction": "SELL",
        "status": "proposed",
        "filter": {
            "block_when": {
                "rsi14_lt": 20,
            }
        },
    }
    base.update(overrides)
    return base


def _minimal_backtest_result(**overrides) -> dict:
    """Return a well-formed backtest result dict."""
    base = {
        "hypothesis_id": "HYP-20260509-001",
        "status": "passed",
        "symbol": "NIFTY",
        "baseline": {
            "trades": 50,
            "net_pnl_points": 120.0,
            "profit_factor": 1.8,
            "max_drawdown_points": 30.0,
        },
        "filtered": {
            "trades": 38,
            "net_pnl_points": 160.0,
            "profit_factor": 2.1,
            "max_drawdown_points": 20.0,
        },
        "promotion_decision": {
            "promote": True,
            "reason": "Filter improved profit factor by 17%.",
        },
    }
    base.update(overrides)
    return base


# ===========================================================================
# Hypothesis validation
# ===========================================================================

class TestValidateHypothesis:

    def test_valid_hypothesis_passes(self):
        hyp = _minimal_hypothesis()
        ok, err = validate_hypothesis(hyp)
        assert ok is True
        assert err == ""

    def test_rejects_non_futures_scope(self):
        hyp = _minimal_hypothesis(scope="equities")
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "scope" in err.lower()

    def test_rejects_dot_ns_symbol(self):
        hyp = _minimal_hypothesis(symbol="RELIANCE.NS")
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert ".NS" in err or "pairs" in err.lower() or "suffix" in err.lower()

    def test_rejects_nifty50_stock(self):
        # INFY is a NIFTY 50 stock, not a futures index symbol
        hyp = _minimal_hypothesis(symbol="INFY")
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "symbol" in err.lower()

    def test_rejects_cointegration_field(self):
        hyp = _minimal_hypothesis()
        hyp["cointegration"] = 0.01
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "cointegration" in err.lower() or "pairs" in err.lower()

    def test_rejects_z_score_field(self):
        hyp = _minimal_hypothesis()
        hyp["z_score"] = 2.5
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "z_score" in err.lower() or "pairs" in err.lower()

    def test_rejects_hedge_ratio_field(self):
        hyp = _minimal_hypothesis()
        hyp["hedge_ratio"] = 1.3
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "hedge_ratio" in err.lower() or "pairs" in err.lower()

    def test_rejects_coint_pvalue_field(self):
        hyp = _minimal_hypothesis()
        hyp["coint_pvalue"] = 0.03
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "coint_pvalue" in err.lower() or "pairs" in err.lower()

    def test_gemini_gatekeeper_phrase_now_allowed(self):
        # "gemini gatekeeper" is intentionally allowed — Gemini is back as live entry gatekeeper.
        # Evidence and descriptions may freely reference it.
        hyp = _minimal_hypothesis(claim="Use gemini gatekeeper to approve trades")
        ok, err = validate_hypothesis(hyp)
        assert ok is True, f"Expected gemini gatekeeper reference to pass, got: {err}"

    def test_rejects_live_approval_phrase(self):
        hyp = _minimal_hypothesis(notes="requires live approval from the agent")
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "prohibited" in err.lower() or "live approval" in err.lower()

    def test_rejects_unsupported_filter_field(self):
        hyp = _minimal_hypothesis()
        hyp["filter"] = {"block_when": {"unknown_field": 42}}
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "unsupported" in err.lower() or "unknown_field" in err.lower()

    def test_accepts_all_valid_symbols(self):
        for sym in ("NIFTY", "BANKNIFTY"):
            hyp = _minimal_hypothesis(symbol=sym)
            ok, err = validate_hypothesis(hyp)
            assert ok is True, f"Expected {sym} to be valid, got err={err!r}"

    def test_rejects_finnifty_symbol(self):
        """FINNIFTY is no longer in the active futures universe and must be rejected."""
        hyp = _minimal_hypothesis(symbol="FINNIFTY")
        ok, err = validate_hypothesis(hyp)
        assert ok is False, "FINNIFTY should be rejected — removed from active futures universe"

    def test_accepts_optional_direction_absent(self):
        hyp = _minimal_hypothesis()
        hyp.pop("direction", None)
        ok, err = validate_hypothesis(hyp)
        assert ok is True, f"Unexpected rejection: {err}"

    def test_rejects_invalid_direction(self):
        hyp = _minimal_hypothesis(direction="HOLD")
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "direction" in err.lower()

    def test_rejects_invalid_status(self):
        hyp = _minimal_hypothesis(status="unknown")
        ok, err = validate_hypothesis(hyp)
        assert ok is False
        assert "status" in err.lower()


# ===========================================================================
# Backtest result validation
# ===========================================================================

class TestValidateBacktestResult:

    def test_valid_backtest_result_passes(self):
        result = _minimal_backtest_result()
        ok, err = validate_backtest_result(result)
        assert ok is True
        assert err == ""

    def test_rejects_backtest_without_hypothesis_id(self):
        result = _minimal_backtest_result()
        del result["hypothesis_id"]
        ok, err = validate_backtest_result(result)
        assert ok is False
        assert "hypothesis_id" in err.lower()

    def test_rejects_backtest_invalid_status(self):
        result = _minimal_backtest_result(status="running")
        ok, err = validate_backtest_result(result)
        assert ok is False
        assert "status" in err.lower()

    def test_rejects_backtest_non_futures_symbol(self):
        result = _minimal_backtest_result(symbol="SENSEX")
        ok, err = validate_backtest_result(result)
        assert ok is False
        assert "symbol" in err.lower()

    def test_rejects_backtest_missing_baseline_fields(self):
        result = _minimal_backtest_result()
        del result["baseline"]["trades"]
        ok, err = validate_backtest_result(result)
        assert ok is False
        assert "baseline" in err.lower() or "trades" in err.lower()

    def test_rejects_backtest_missing_filtered_section(self):
        result = _minimal_backtest_result()
        del result["filtered"]
        ok, err = validate_backtest_result(result)
        assert ok is False
        assert "filtered" in err.lower()

    def test_rejects_backtest_missing_promotion_decision(self):
        result = _minimal_backtest_result()
        del result["promotion_decision"]
        ok, err = validate_backtest_result(result)
        assert ok is False
        assert "promotion_decision" in err.lower()

    def test_rejects_backtest_promote_not_bool(self):
        result = _minimal_backtest_result()
        result["promotion_decision"]["promote"] = "true"  # string, not bool
        ok, err = validate_backtest_result(result)
        assert ok is False
        assert "promote" in err.lower() or "bool" in err.lower()


# ===========================================================================
# promote_if_passed
# ===========================================================================

class TestPromoteIfPassed:

    def test_promote_if_passed_returns_filter(self):
        hyp = _minimal_hypothesis()
        result = _minimal_backtest_result()
        result["promotion_decision"]["promote"] = True

        promoted = promote_if_passed(hyp, result)

        assert promoted is not None
        assert promoted["scope"] == "futures"
        assert promoted["status"] == "active"
        assert promoted["symbol"] == "NIFTY"
        assert "id" in promoted
        assert promoted["id"].startswith("FILT-")

    def test_promote_if_passed_returns_none_on_false(self):
        hyp = _minimal_hypothesis()
        result = _minimal_backtest_result()
        result["promotion_decision"]["promote"] = False

        promoted = promote_if_passed(hyp, result)

        assert promoted is None


# ===========================================================================
# write_hypothesis / load_hypothesis round-trip
# ===========================================================================

class TestWriteLoadRoundtrip:

    def test_write_load_roundtrip(self, tmp_path):
        hyp = _minimal_hypothesis()
        dest = tmp_path / "hypotheses" / "HYP-20260509-001.json"

        write_hypothesis(hyp, dest)

        assert dest.exists()
        loaded = load_hypothesis(dest)

        assert loaded["id"] == hyp["id"]
        assert loaded["scope"] == hyp["scope"]
        assert loaded["symbol"] == hyp["symbol"]
        assert loaded["strategy"] == hyp["strategy"]
        assert loaded["direction"] == hyp["direction"]
        assert loaded["status"] == hyp["status"]
        assert loaded["filter"]["block_when"]["rsi14_lt"] == 20


# ===========================================================================
# Journal parsing (tests for Agent A's parse_journal implementation)
# ===========================================================================

SAMPLE_JOURNAL = """
# BlitzTrader Daily Journal — 09 May 2026

## Session Summary
- **Start Capital:** ₹10,00,000

## Decision Log

### 09:21:33 — ENTER_SHORT
**Market context:** NIFTY 5m | price ₹24123.50 | EMA20 24100.0
**Instrument:** NIFTY28APR26F
**Strategy applied:** VP-01 Counter Bull Trap
**Reasoning:** Python approved VP-01 Counter Bull Trap SELL on NIFTY 5m.

### 09:45:54 — REJECT
**Market context:** BANKNIFTY 5m | price ₹52100.00
**Instrument:** BANKNIFTY28APR26F
**Strategy applied:** VP-07 Wicks Pullback
**Reasoning:** Rejected by Python: higher-timeframe EMA stack remains bullish.

### 15:15:00 — EOD_REPORT
**Reasoning:** EOD complete. 1 trade.
"""


class TestJournalParsing:
    """Tests for parse_journal() in scripts/evaluate_futures_day.py."""

    @pytest.fixture(autouse=True)
    def _import_parse_journal(self):
        from scripts.evaluate_futures_day import parse_journal
        self.parse_journal = parse_journal

    def _write_journal(self, tmp_path, content=SAMPLE_JOURNAL, filename="20260509.md"):
        journals_dir = tmp_path / "journals"
        journals_dir.mkdir(parents=True, exist_ok=True)
        p = journals_dir / filename
        p.write_text(content, encoding="utf-8")
        return p

    def test_parse_journal_executed_count(self, tmp_path):
        path = self._write_journal(tmp_path)
        result = self.parse_journal(path)
        assert len(result["executed"]) == 1

    def test_parse_journal_rejected_count(self, tmp_path):
        path = self._write_journal(tmp_path)
        result = self.parse_journal(path)
        assert len(result["rejected"]) == 1

    def test_parse_journal_executed_symbol(self, tmp_path):
        path = self._write_journal(tmp_path)
        result = self.parse_journal(path)
        sym = result["executed"][0].get("symbol", "")
        assert "NIFTY" in sym.upper(), f"Expected NIFTY in symbol, got: {sym!r}"

    def test_parse_journal_executed_strategy(self, tmp_path):
        path = self._write_journal(tmp_path)
        result = self.parse_journal(path)
        assert result["executed"][0]["strategy"] == "VP-01 Counter Bull Trap"

    def test_parse_journal_rejected_symbol(self, tmp_path):
        path = self._write_journal(tmp_path)
        result = self.parse_journal(path)
        sym = result["rejected"][0].get("symbol", "")
        assert "BANKNIFTY" in sym.upper(), f"Expected BANKNIFTY in symbol, got: {sym!r}"

    def test_parse_journal_rejected_strategy(self, tmp_path):
        path = self._write_journal(tmp_path)
        result = self.parse_journal(path)
        assert result["rejected"][0]["strategy"] == "VP-07 Wicks Pullback"

    def test_parse_journal_eod_not_in_executed(self, tmp_path):
        path = self._write_journal(tmp_path)
        result = self.parse_journal(path)
        for e in result["executed"]:
            assert e.get("action", "").upper() != "EOD_REPORT", (
                "EOD_REPORT should not appear in executed list"
            )

    def test_parse_journal_eod_not_in_rejected(self, tmp_path):
        path = self._write_journal(tmp_path)
        result = self.parse_journal(path)
        for r in result["rejected"]:
            assert r.get("action", "").upper() != "EOD_REPORT", (
                "EOD_REPORT should not appear in rejected list"
            )

    def test_parse_journal_empty_file(self, tmp_path):
        path = self._write_journal(tmp_path, content="")
        result = self.parse_journal(path)
        assert result["executed"] == []
        assert result["rejected"] == []

    def test_parse_journal_no_futures_entries(self, tmp_path):
        # A journal with only a non-futures instrument
        non_futures_journal = """
# BlitzTrader Daily Journal — 09 May 2026

## Decision Log

### 10:00:00 — ENTER_LONG
**Market context:** RELIANCE 5m | price ₹2800
**Instrument:** RELIANCE
**Strategy applied:** VP-01 Counter Bull Trap
**Reasoning:** Approved.
"""
        path = self._write_journal(tmp_path, content=non_futures_journal)
        result = self.parse_journal(path)
        assert result["executed"] == []
        assert result["rejected"] == []
