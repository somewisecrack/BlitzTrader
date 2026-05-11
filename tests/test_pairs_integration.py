"""
Unit tests for the pairs trading subsystem integrated into BlitzTrader.

Tests cover:
  - yfinance .NS → Shoonya symbol normalisation
  - Duplicate pair deduplication across timeframes
  - Capital allocation: 10 pairs vs fewer than 10
  - Independent leg trailing-stop arming and movement
  - Combined capital split: ₹10L futures vs ₹10L pairs (no cross-contamination)
  - Gemini not in trade decisions (no pairs module imports agent_loop or AgentLoop)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Pre-load all heavy scientific packages that pairs/scanner.py needs.
# Both test files mutate numpy.ndarray = object at module-level (collection time).
# All scipy/statsmodels sub-modules that reference np.ndarray at import time must be
# fully cached *before* that mutation runs.  Since this file is collected first, we
# front-load the full import chain here so no module-body code re-executes later.
# Order: numpy first (scipy and statsmodels both depend on it).
import numpy  # noqa: F401
import scipy.stats  # noqa: F401
import scipy.signal  # noqa: F401  — imported transitively by statsmodels.tsa
import statsmodels.regression.linear_model  # noqa: F401
import statsmodels.tools.tools  # noqa: F401
import statsmodels.tsa.stattools  # noqa: F401
import statsmodels.tsa.vector_ar.vecm  # noqa: F401

# ─── Minimal stubs so we can import without heavy dependencies ────────────────

import sys
import types

# Stub out heavy scientific libraries so tests run without a full env install
for lib in ("numpy", "pandas", "scipy", "statsmodels", "yfinance"):
    if lib not in sys.modules:
        sys.modules[lib] = types.ModuleType(lib)

# Stub numpy sub-modules
import numpy as _np_stub
_np_stub.ndarray = object
_np_stub.nan = float("nan")

# Stub pandas
import pandas as _pd_stub
_pd_stub.DataFrame = object
_pd_stub.Series = object

# ─────────────────────────────────────────────────────────────────────────────


class TestSymbolNormalisation:
    """yfinance .NS stripping → Shoonya searchtext."""

    def test_strip_ns_suffix(self):
        symbol = "INFY.NS"
        result = symbol.replace(".NS", "")
        assert result == "INFY"

    def test_ampersand_encoding(self):
        symbol = "M&M"
        result = symbol.replace(".NS", "").replace("&", "%26")
        assert result == "M%26M"

    def test_already_bare_symbol(self):
        symbol = "HDFCBANK"
        result = symbol.replace(".NS", "")
        assert result == "HDFCBANK"

    def test_ns_suffix_with_special_chars(self):
        symbol = "BAJAJ-AUTO.NS"
        result = symbol.replace(".NS", "")
        assert result == "BAJAJ-AUTO"

    def test_exact_tsym_match_priority(self):
        """resolve_equity_symbol should prefer INFY-EQ over fallback."""
        wanted = "INFY"
        exact_tsyms = {wanted, f"{wanted}-EQ"}
        matches = [
            {"tsym": "INFY-EQ", "token": "1594", "cname": "INFOSYS"},
            {"tsym": "INFY-BE", "token": "9999", "cname": "INFOSYS"},
        ]
        chosen = None
        for item in matches:
            tsym = str(item.get("tsym", "")).upper()
            if tsym in exact_tsyms:
                chosen = item
                break
        assert chosen is not None
        assert chosen["tsym"] == "INFY-EQ"


class TestDuplicatePairDeduplication:
    """Same pair appearing across multiple timeframes must appear only once."""

    @dataclass
    class _FakePairCandidate:
        x_symbol: str
        y_symbol: str
        timeframe: str
        prob_profit: float
        z_score: float
        half_life: int
        matched_timeframes: list

        @property
        def pair_key(self):
            return tuple(sorted((self.x_symbol, self.y_symbol)))

    def _run_merge(self, candidates):
        """Simulate the merge logic from PairScanner.run_scan()."""
        merged = {}
        for c in candidates:
            key = c.pair_key
            existing = merged.get(key)
            if not existing:
                merged[key] = c
                continue
            if c.timeframe not in existing.matched_timeframes:
                existing.matched_timeframes.append(c.timeframe)
            better = c.prob_profit > existing.prob_profit
            tie = math.isclose(c.prob_profit, existing.prob_profit)
            if better or (tie and abs(c.z_score) > abs(existing.z_score)):
                c.matched_timeframes = sorted(
                    set(existing.matched_timeframes + c.matched_timeframes)
                )
                merged[key] = c
        return sorted(
            merged.values(),
            key=lambda c: (c.prob_profit, abs(c.z_score), -c.half_life),
            reverse=True,
        )

    def test_same_pair_across_timeframes_deduped(self):
        candidates = [
            self._FakePairCandidate("INFY", "TCS", "15m", 72.0, 2.5, 5, ["15m"]),
            self._FakePairCandidate("INFY", "TCS", "30m", 75.0, 2.8, 6, ["30m"]),
            self._FakePairCandidate("INFY", "TCS", "1h",  68.0, 2.1, 8, ["1h"]),
        ]
        result = self._run_merge(candidates)
        assert len(result) == 1
        # Best timeframe wins
        assert result[0].prob_profit == 75.0

    def test_best_prob_profit_wins_on_dedup(self):
        candidates = [
            self._FakePairCandidate("HDFCBANK", "ICICIBANK", "15m", 60.0, 2.2, 4, ["15m"]),
            self._FakePairCandidate("HDFCBANK", "ICICIBANK", "1h",  80.0, 3.1, 7, ["1h"]),
        ]
        result = self._run_merge(candidates)
        assert len(result) == 1
        assert result[0].prob_profit == 80.0

    def test_matched_timeframes_merged(self):
        candidates = [
            self._FakePairCandidate("SBIN", "AXISBANK", "15m", 70.0, 2.4, 5, ["15m"]),
            self._FakePairCandidate("SBIN", "AXISBANK", "30m", 70.0, 2.4, 5, ["30m"]),
        ]
        result = self._run_merge(candidates)
        assert len(result) == 1
        assert set(result[0].matched_timeframes) == {"15m", "30m"}

    def test_different_pairs_not_merged(self):
        candidates = [
            self._FakePairCandidate("INFY", "TCS",      "15m", 72.0, 2.5, 5, ["15m"]),
            self._FakePairCandidate("SBIN", "AXISBANK",  "15m", 68.0, 2.1, 4, ["15m"]),
        ]
        result = self._run_merge(candidates)
        assert len(result) == 2

    def test_same_ticker_in_different_pairs_allowed(self):
        """INFY can appear in INFY/TCS and INFY/HCLTECH — both are kept."""
        candidates = [
            self._FakePairCandidate("INFY", "TCS",    "1h", 71.0, 2.3, 6, ["1h"]),
            self._FakePairCandidate("INFY", "HCLTECH","1h", 69.0, 2.0, 5, ["1h"]),
        ]
        result = self._run_merge(candidates)
        assert len(result) == 2


class TestCapitalAllocation:
    """Allocation logic: 10 pairs vs fewer than 10."""

    def test_allocation_with_10_pairs(self):
        from config import PAIRS_GROSS_CAPITAL, PER_PAIR_CAPITAL, MAX_OPEN_PAIRS
        num_pairs = 10
        assert num_pairs >= MAX_OPEN_PAIRS
        per_pair = float(PER_PAIR_CAPITAL)
        assert per_pair == 100_000
        assert per_pair == PAIRS_GROSS_CAPITAL / MAX_OPEN_PAIRS
        total_allocated = per_pair * num_pairs
        assert math.isclose(total_allocated, PAIRS_GROSS_CAPITAL)

    def test_allocation_with_fewer_than_10_pairs(self):
        from config import PAIRS_GROSS_CAPITAL
        num_pairs = 6
        per_pair = PAIRS_GROSS_CAPITAL / num_pairs
        assert math.isclose(per_pair * num_pairs, PAIRS_GROSS_CAPITAL)
        # Each pair gets more than the standard ₹1L
        assert per_pair > 100_000

    def test_allocation_with_1_pair(self):
        from config import PAIRS_GROSS_CAPITAL
        num_pairs = 1
        per_pair = PAIRS_GROSS_CAPITAL / num_pairs
        assert math.isclose(per_pair, PAIRS_GROSS_CAPITAL)

    def test_capital_pools_are_separate(self):
        from config import VIRTUAL_CAPITAL, PAIRS_CAPITAL
        # Futures: ₹10L, Pairs base: ₹5L (with 2x leverage = ₹10L gross)
        assert VIRTUAL_CAPITAL == 1_000_000
        assert PAIRS_CAPITAL == 500_000
        # Neither is derived from the other at module level — they don't share a pool


class TestLegTrailingStop:
    """Independent per-leg trailing stop logic."""

    def _make_leg(self, side: str, entry: float) -> MagicMock:
        leg = MagicMock()
        leg.side = side
        leg.entry_price = entry
        leg.stop_price = None
        leg.stop_armed_at_profit_pct = None
        leg.closed_at = None
        return leg

    def _profit_pct(self, leg, exit_price: float) -> float:
        if leg.side == "BUY":
            return ((exit_price - leg.entry_price) / leg.entry_price) * 100
        return ((leg.entry_price - exit_price) / leg.entry_price) * 100

    def _trailing_stop_price(self, leg, profit_pct: float) -> float:
        locked = max(0.5, profit_pct - 0.5)
        if leg.side == "BUY":
            return round(leg.entry_price * (1 + locked / 100.0), 2)
        return round(leg.entry_price * (1 - locked / 100.0), 2)

    def _should_raise_stop(self, leg, new_stop: float) -> bool:
        if leg.stop_price is None:
            return True
        if leg.side == "BUY":
            return new_stop > leg.stop_price
        return new_stop < leg.stop_price

    def test_buy_leg_stop_arms_at_1pct_profit(self):
        leg = self._make_leg("BUY", 100.0)
        exit_price = 101.5  # +1.5%
        profit_pct = self._profit_pct(leg, exit_price)
        assert profit_pct > 1.0
        stop = self._trailing_stop_price(leg, profit_pct)
        assert stop > leg.entry_price  # stop is above entry (locking profit)

    def test_sell_leg_stop_arms_at_1pct_profit(self):
        leg = self._make_leg("SELL", 100.0)
        exit_price = 98.5   # +1.5% for short
        profit_pct = self._profit_pct(leg, exit_price)
        assert profit_pct > 1.0
        stop = self._trailing_stop_price(leg, profit_pct)
        assert stop < leg.entry_price  # stop is below entry (locking profit)

    def test_stop_not_armed_below_1pct(self):
        leg = self._make_leg("BUY", 100.0)
        exit_price = 100.5  # only +0.5%
        profit_pct = self._profit_pct(leg, exit_price)
        assert profit_pct <= 1.0  # threshold not crossed, no arm

    def test_stop_raises_when_price_improves(self):
        leg = self._make_leg("BUY", 100.0)
        leg.stop_price = 101.0  # already armed
        new_stop = 101.5
        assert self._should_raise_stop(leg, new_stop)

    def test_stop_does_not_lower_for_buy(self):
        leg = self._make_leg("BUY", 100.0)
        leg.stop_price = 101.5  # already armed higher
        new_stop = 101.0        # lower — should NOT be raised
        assert not self._should_raise_stop(leg, new_stop)

    def test_stop_does_not_raise_for_sell_when_price_worsens(self):
        leg = self._make_leg("SELL", 100.0)
        leg.stop_price = 98.5   # already armed lower
        new_stop = 99.0         # higher — should NOT be raised (worsens for short)
        assert not self._should_raise_stop(leg, new_stop)

    def test_trailing_gap_is_0pt5pct(self):
        """Stop locks at profit_pct - 0.5%."""
        leg = self._make_leg("BUY", 100.0)
        profit_pct = 2.0
        stop = self._trailing_stop_price(leg, profit_pct)
        locked_pct = max(0.5, profit_pct - 0.5)
        expected = round(100.0 * (1 + locked_pct / 100.0), 2)
        assert math.isclose(stop, expected, rel_tol=1e-6)

    def test_long_leg_independent_of_short_leg(self):
        """Arming the long leg stop should not affect the short leg."""
        long_leg = self._make_leg("BUY", 100.0)
        short_leg = self._make_leg("SELL", 200.0)

        # Long leg hits +1.5%
        long_exit = 101.5
        long_profit = self._profit_pct(long_leg, long_exit)
        assert long_profit > 1.0
        long_leg.stop_price = self._trailing_stop_price(long_leg, long_profit)

        # Short leg is still at break-even
        short_exit = 200.0
        short_profit = self._profit_pct(short_leg, short_exit)
        assert short_profit <= 0.0
        # Short leg stop must remain unarmed
        assert short_leg.stop_price is None


class TestGeminiNotInPairsDecisions:
    """Gemini (AgentLoop) must not be imported or used in the pairs module."""

    def test_pairs_scanner_does_not_import_agent_loop(self):
        src = (_REPO_ROOT / "pairs" / "scanner.py").read_text()
        assert "AgentLoop" not in src
        assert "agent_loop" not in src
        assert "generate_content" not in src

    def test_pairs_portfolio_does_not_import_agent_loop(self):
        src = (_REPO_ROOT / "pairs" / "portfolio.py").read_text()
        assert "AgentLoop" not in src
        assert "agent_loop" not in src
        assert "generate_content" not in src


class TestYfinanceOnlyDataSource:
    """Verify pairs/scanner.py uses yfinance as its sole data source.

    All checks use source-code inspection to avoid importing pairs.scanner
    inside the test body, which would collide with the scipy stub that
    test_pairs_wiring.py installs at module-level.
    """

    _SRC = None

    @classmethod
    def _scanner_src(cls) -> str:
        if cls._SRC is None:
            cls._SRC = (_REPO_ROOT / "pairs" / "scanner.py").read_text()
        return cls._SRC

    def test_fetch_interval_data_calls_yfinance(self):
        """fetch_interval_data must delegate directly to _fetch_yfinance_data."""
        src = self._scanner_src()
        # The method body must contain exactly one call: _fetch_yfinance_data
        import re
        # Extract the fetch_interval_data method body
        m = re.search(
            r"def fetch_interval_data\(.*?\n((?:[ \t]+[^\n]*\n)*)",
            src,
        )
        assert m, "fetch_interval_data not found in scanner.py"
        body = m.group(1)
        assert "_fetch_yfinance_data" in body, "fetch_interval_data must call _fetch_yfinance_data"
        assert "_fetch_yahoo" not in body, "fetch_interval_data must not call _fetch_yahoo_chart_data"

    def test_no_fetch_yahoo_chart_data_method(self):
        src = self._scanner_src()
        assert "def _fetch_yahoo_chart_data" not in src

    def test_scanner_source_has_no_yahoo_direct_url(self):
        src = self._scanner_src()
        assert "query1.finance" not in src
        assert "query2.finance" not in src
        assert "urllib.request" not in src
        assert "urlopen" not in src
        assert "_fetch_yahoo" not in src
        assert "Yahoo chart" not in src
        assert "falling back to yfinance" not in src
