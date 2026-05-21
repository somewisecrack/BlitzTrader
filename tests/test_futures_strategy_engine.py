"""
tests/test_futures_strategy_engine.py
--------------------------------------
Tests for tools/futures_strategy_engine.py registry invariants.

Verifies:
  - SUPPORTED_STRATEGIES includes VP-24 variants and other live-traded strategies
  - LIVE_ONLY_STRATEGIES is a subset of SUPPORTED_STRATEGIES
  - STRATEGY_DIRECTIONS has an entry for every strategy in SUPPORTED_STRATEGIES
  - Every LIVE_ONLY_STRATEGIES entry maps to correct direction(s) in STRATEGY_DIRECTIONS
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from tools.futures_strategy_engine import (
    SUPPORTED_STRATEGIES,
    LIVE_ONLY_STRATEGIES,
    STRATEGY_DIRECTIONS,
)


class TestSupportedStrategiesRegistry:
    """SUPPORTED_STRATEGIES must include all live-traded strategies."""

    def test_vp24_pivot_rejection_p_present(self):
        assert "VP-24 Pivot Rejection P" in SUPPORTED_STRATEGIES

    def test_vp24_pivot_rejection_r1_present(self):
        assert "VP-24 Pivot Rejection R1" in SUPPORTED_STRATEGIES

    def test_vp24_pivot_bounce_s1_present(self):
        assert "VP-24 Pivot Bounce S1" in SUPPORTED_STRATEGIES

    def test_vp24_pivot_bounce_s2_present(self):
        assert "VP-24 Pivot Bounce S2" in SUPPORTED_STRATEGIES

    def test_adx_gapper_present(self):
        assert "ADX Gapper" in SUPPORTED_STRATEGIES

    def test_momentum_pinball_present(self):
        assert "Momentum Pinball" in SUPPORTED_STRATEGIES

    def test_vp09_power_candle_pullback_present(self):
        assert "VP-09 Power Candle Pullback" in SUPPORTED_STRATEGIES

    def test_vp16_gcr_present(self):
        assert "VP-16 GCR Green Candle Retracement" in SUPPORTED_STRATEGIES

    def test_core_strategies_present(self):
        """Core OHLCV-backtestable strategies must still be present."""
        for name in (
            "VP-01 Counter Bull Trap",
            "VP-02 Counter Bear Trap",
            "VP-05 3EMA Trend",
            "VP-07 Wicks Pullback",
            "VP-15 Evening Star",
            "VP-18 M-Pattern Double Top",
            "VP-19 W-Pattern Double Bottom",
            "VP-21 Extreme Candle Reversal",
        ):
            assert name in SUPPORTED_STRATEGIES, f"{name!r} missing from SUPPORTED_STRATEGIES"

    def test_supported_strategies_is_set(self):
        assert isinstance(SUPPORTED_STRATEGIES, set)

    def test_no_empty_string_in_supported(self):
        assert "" not in SUPPORTED_STRATEGIES


class TestLiveOnlyStrategiesSubset:
    """LIVE_ONLY_STRATEGIES must be a proper subset of SUPPORTED_STRATEGIES."""

    def test_live_only_is_subset_of_supported(self):
        extra = LIVE_ONLY_STRATEGIES - SUPPORTED_STRATEGIES
        assert not extra, (
            f"LIVE_ONLY_STRATEGIES contains entries not in SUPPORTED_STRATEGIES: {extra}"
        )

    def test_vp24_variants_are_live_only(self):
        for name in (
            "VP-24 Pivot Rejection P",
            "VP-24 Pivot Rejection R1",
            "VP-24 Pivot Bounce S1",
            "VP-24 Pivot Bounce S2",
        ):
            assert name in LIVE_ONLY_STRATEGIES, (
                f"{name!r} should be in LIVE_ONLY_STRATEGIES (requires prev-day pivots)"
            )

    def test_adx_gapper_is_live_only(self):
        assert "ADX Gapper" in LIVE_ONLY_STRATEGIES

    def test_core_backtestable_not_in_live_only(self):
        """OHLCV-backtestable strategies must NOT be in LIVE_ONLY_STRATEGIES."""
        for name in (
            "VP-01 Counter Bull Trap",
            "VP-02 Counter Bear Trap",
            "VP-05 3EMA Trend",
            "VP-07 Wicks Pullback",
            "VP-15 Evening Star",
            "VP-18 M-Pattern Double Top",
            "VP-19 W-Pattern Double Bottom",
            "VP-21 Extreme Candle Reversal",
        ):
            assert name not in LIVE_ONLY_STRATEGIES, (
                f"{name!r} is backtestable and must NOT be in LIVE_ONLY_STRATEGIES"
            )


class TestStrategyDirectionsCoverage:
    """STRATEGY_DIRECTIONS must have an entry for every strategy in SUPPORTED_STRATEGIES."""

    def test_all_supported_have_directions_entry(self):
        missing = SUPPORTED_STRATEGIES - set(STRATEGY_DIRECTIONS.keys())
        assert not missing, (
            f"These strategies are in SUPPORTED_STRATEGIES but missing from STRATEGY_DIRECTIONS: {missing}"
        )

    def test_directions_are_frozensets_of_valid_values(self):
        valid = {"BUY", "SELL"}
        for name, dirs in STRATEGY_DIRECTIONS.items():
            assert isinstance(dirs, frozenset), (
                f"STRATEGY_DIRECTIONS[{name!r}] must be a frozenset, got {type(dirs)}"
            )
            assert dirs.issubset(valid), (
                f"STRATEGY_DIRECTIONS[{name!r}] contains invalid values: {dirs - valid}"
            )
            assert len(dirs) >= 1, (
                f"STRATEGY_DIRECTIONS[{name!r}] must have at least one direction"
            )

    def test_vp24_rejection_p_is_sell_only(self):
        assert STRATEGY_DIRECTIONS["VP-24 Pivot Rejection P"] == frozenset({"SELL"})

    def test_vp24_bounce_s1_is_buy_only(self):
        assert STRATEGY_DIRECTIONS["VP-24 Pivot Bounce S1"] == frozenset({"BUY"})

    def test_adx_gapper_is_bidirectional(self):
        assert STRATEGY_DIRECTIONS["ADX Gapper"] == frozenset({"BUY", "SELL"})

    def test_vp01_is_sell_only(self):
        assert STRATEGY_DIRECTIONS["VP-01 Counter Bull Trap"] == frozenset({"SELL"})

    def test_vp02_is_buy_only(self):
        assert STRATEGY_DIRECTIONS["VP-02 Counter Bear Trap"] == frozenset({"BUY"})
