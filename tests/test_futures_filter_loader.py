"""
tests/test_futures_filter_loader.py
-------------------------------------
Tests for load_active_filters and apply_promoted_filters from
tools/futures_filter_loader.py.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.futures_filter_loader import load_active_filters, apply_promoted_filters


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _active_filter(
    filt_id="FILT-20260509-001",
    scope="futures",
    status="active",
    symbol="NIFTY",
    strategy="VP-01 Counter Bull Trap",
    direction=None,
    block_when=None,
) -> dict:
    """Return a minimal valid active filter dict."""
    if block_when is None:
        block_when = {"rsi14_lt": 20}
    flt = {
        "id": filt_id,
        "scope": scope,
        "status": status,
        "symbol": symbol,
        "strategy": strategy,
        "filter": {"block_when": block_when},
    }
    if direction is not None:
        flt["direction"] = direction
    return flt


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ===========================================================================
# load_active_filters
# ===========================================================================

class TestLoadActiveFilters:

    def test_returns_empty_when_no_wiki_dir(self, tmp_path):
        nonexistent = tmp_path / "no_wiki"
        result = load_active_filters(nonexistent)
        assert result == []

    def test_returns_empty_when_no_promoted_filters_dir(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        result = load_active_filters(wiki_dir)
        assert result == []

    def test_skips_gitkeep(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)
        (filters_dir / ".gitkeep").write_text("", encoding="utf-8")

        result = load_active_filters(wiki_dir)
        assert result == []

    def test_loads_valid_active_filter(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        flt = _active_filter()
        _write_json(filters_dir / "FILT-20260509-001.json", flt)

        result = load_active_filters(wiki_dir)
        assert len(result) == 1
        assert result[0]["id"] == "FILT-20260509-001"
        assert result[0]["status"] == "active"

    def test_skips_non_active_filter(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        flt = _active_filter(status="retired")
        _write_json(filters_dir / "FILT-retired.json", flt)

        result = load_active_filters(wiki_dir)
        assert result == []

    def test_skips_invalid_json(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        (filters_dir / "bad.json").write_text("{not valid json", encoding="utf-8")

        result = load_active_filters(wiki_dir)
        assert result == []

    def test_skips_non_futures_symbol(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        flt = _active_filter(symbol="RELIANCE")
        _write_json(filters_dir / "FILT-bad-symbol.json", flt)

        result = load_active_filters(wiki_dir)
        assert result == []

    def test_skips_non_futures_symbol_ns(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        flt = _active_filter(symbol="INFY.NS")
        _write_json(filters_dir / "FILT-infy-ns.json", flt)

        result = load_active_filters(wiki_dir)
        assert result == []

    def test_loads_multiple_filters(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        flt1 = _active_filter(filt_id="FILT-20260509-001", symbol="NIFTY")
        flt2 = _active_filter(filt_id="FILT-20260509-002", symbol="BANKNIFTY")
        _write_json(filters_dir / "FILT-20260509-001.json", flt1)
        _write_json(filters_dir / "FILT-20260509-002.json", flt2)

        result = load_active_filters(wiki_dir)
        assert len(result) == 2
        ids = {f["id"] for f in result}
        assert ids == {"FILT-20260509-001", "FILT-20260509-002"}


# ===========================================================================
# apply_promoted_filters
# ===========================================================================

class TestApplyPromotedFilters:

    def test_no_filters_allows_all(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01", "direction": "SELL"}
        indicators = {"rsi14": 10, "adx14": 25}
        ok, reason = apply_promoted_filters(signal, indicators, [])
        assert ok is True
        assert reason == ""

    def test_blocks_matching_signal_rsi_lt(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"rsi14": 15}  # below threshold of 20
        flt = _active_filter(block_when={"rsi14_lt": 20})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is False
        assert reason  # reason must be non-empty

    def test_allows_when_rsi_above_threshold(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"rsi14": 25}  # above threshold of 20
        flt = _active_filter(block_when={"rsi14_lt": 20})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is True
        assert reason == ""

    def test_blocks_rsi_gt(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"rsi14": 75}  # above threshold of 70
        flt = _active_filter(block_when={"rsi14_gt": 70})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is False

    def test_blocks_adx_lt(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"adx14": 15}  # below threshold of 20
        flt = _active_filter(block_when={"adx14_lt": 20})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is False

    def test_skips_when_indicator_is_none(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"rsi14": None}  # None → data not available
        flt = _active_filter(block_when={"rsi14_lt": 20})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is True, "Missing data should not block the signal"
        assert reason == ""

    def test_symbol_mismatch_skips_filter(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"rsi14": 10}  # would block if filter applied
        flt = _active_filter(symbol="BANKNIFTY", block_when={"rsi14_lt": 20})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is True, "Filter for BANKNIFTY should not apply to NIFTY signal"

    def test_strategy_mismatch_skips_filter(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-05 3EMA Trend", "direction": "SELL"}
        indicators = {"rsi14": 10}  # would block if filter applied
        flt = _active_filter(strategy="VP-01 Counter Bull Trap", block_when={"rsi14_lt": 20})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is True, "Filter for VP-01 should not apply to VP-05 signal"

    def test_direction_mismatch_skips_filter(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "BUY"}
        indicators = {"rsi14": 10}  # would block if filter applied for SELL
        flt = _active_filter(direction="SELL", block_when={"rsi14_lt": 20})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is True, "Filter for SELL should not apply to BUY signal"

    def test_unknown_filter_field_skips_safely(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"rsi14": 10}
        flt = _active_filter(block_when={"weird_field": 5})  # unsupported field

        # Should NOT crash; unknown field is skipped
        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is True
        assert reason == ""

    def test_vwap_price_below_blocks(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"price_above_vwap": False}  # price IS below VWAP
        flt = _active_filter(block_when={"price_below_vwap": True})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is False

    def test_vwap_price_above_blocks(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"price_above_vwap": True}  # price IS above VWAP
        flt = _active_filter(block_when={"price_above_vwap": True})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is False

    def test_vwap_none_skips(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"price_above_vwap": None}
        flt = _active_filter(block_when={"price_below_vwap": True})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])
        assert ok is True


# ===========================================================================
# Integration: reject reason contains filter id
# ===========================================================================

class TestRejectReasonContainsFilterId:

    def test_reject_reason_contains_filter_id(self):
        signal = {"symbol": "NIFTY", "strategy": "VP-01 Counter Bull Trap", "direction": "SELL"}
        indicators = {"rsi14": 10}
        flt = _active_filter(filt_id="FILT-20260509-999", block_when={"rsi14_lt": 20})

        ok, reason = apply_promoted_filters(signal, indicators, [flt])

        assert ok is False
        assert "FILT-20260509-999" in reason, (
            f"Expected filter id 'FILT-20260509-999' in reason, got: {reason!r}"
        )


# ===========================================================================
# JSON format loading tests (Agent B: promoted filters written as .json)
# ===========================================================================

class TestJSONFilterLoading:

    def test_json_filter_loads_without_pyyaml(self, tmp_path):
        """A valid JSON filter file must load regardless of PyYAML availability."""
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        flt = _active_filter(filt_id="FILT-20260509-001")
        _write_json(filters_dir / "FILT-20260509-001.json", flt)

        result = load_active_filters(wiki_dir)
        assert len(result) == 1
        assert result[0]["id"] == "FILT-20260509-001"

    def test_json_filter_canonical_id(self, tmp_path):
        """Filter JSON must have 'id' field (canonical); loader must return it."""
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        flt = _active_filter(filt_id="FILT-20260509-001")
        assert "id" in flt, "Helper must produce 'id' key"
        _write_json(filters_dir / "FILT-20260509-001.json", flt)

        result = load_active_filters(wiki_dir)
        assert len(result) == 1
        assert result[0]["id"] == "FILT-20260509-001"

    def test_yaml_extension_with_json_content(self, tmp_path):
        """JSON content written to a .yaml file must still parse correctly."""
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        flt = _active_filter(filt_id="FILT-20260509-002")
        # Write JSON content but with .yaml extension
        path = filters_dir / "FILT-20260509-002.yaml"
        path.write_text(json.dumps(flt, indent=2), encoding="utf-8")

        result = load_active_filters(wiki_dir)
        assert len(result) == 1
        assert result[0]["id"] == "FILT-20260509-002"

    def test_filter_blocks_signal_end_to_end(self, tmp_path):
        """Load a JSON filter and apply it to block a matching signal."""
        wiki_dir = tmp_path / "wiki"
        filters_dir = wiki_dir / "promoted_filters"
        filters_dir.mkdir(parents=True)

        flt = _active_filter(
            filt_id="FILT-20260509-001",
            symbol="NIFTY",
            strategy="VP-01 Counter Bull Trap",
            direction="SELL",
            block_when={"rsi14_lt": 20},
        )
        _write_json(filters_dir / "FILT-20260509-001.json", flt)

        filters = load_active_filters(wiki_dir)
        assert len(filters) == 1

        nifty_sell_signal = {
            "symbol": "NIFTY",
            "strategy": "VP-01 Counter Bull Trap",
            "direction": "SELL",
        }
        indicators = {"rsi14": 15, "adx14": 30}
        allowed, reason = apply_promoted_filters(nifty_sell_signal, indicators, filters)

        assert allowed is False
        assert "FILT-20260509-001" in reason, f"Expected filter id in reason, got: {reason!r}"

    def test_no_filters_means_no_behavior_change(self):
        """Empty filter list must always return (True, '')."""
        signals_to_check = [
            {"symbol": "NIFTY", "strategy": "VP-01", "direction": "SELL"},
            {"symbol": "BANKNIFTY", "strategy": "VP-07", "direction": "BUY"},
            {"symbol": "FINNIFTY", "strategy": "VP-19", "direction": "SELL"},
        ]
        for sig in signals_to_check:
            ok, reason = apply_promoted_filters(sig, {"rsi14": 10, "adx14": 5}, [])
            assert ok is True, f"Empty filter list should allow all signals, got ok={ok} for {sig}"
            assert reason == ""
