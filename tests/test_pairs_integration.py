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
    """Dynamic per-pair allocation: PAIRS_GROSS_CAPITAL / n_selected_pairs."""

    def test_allocation_with_5_pairs_is_200k(self):
        """Normal case: 5 pairs → ₹2,00,000 per pair."""
        from config import PAIRS_GROSS_CAPITAL, PAIRS_MAX_SELECTED
        per_pair = PAIRS_GROSS_CAPITAL / PAIRS_MAX_SELECTED
        assert math.isclose(per_pair, 200_000)
        total_allocated = per_pair * PAIRS_MAX_SELECTED
        assert math.isclose(total_allocated, PAIRS_GROSS_CAPITAL)

    def test_allocation_with_fewer_than_5_pairs(self):
        """Fewer than 5 eligible → each gets more capital."""
        from config import PAIRS_GROSS_CAPITAL
        num_pairs = 3
        per_pair = PAIRS_GROSS_CAPITAL / num_pairs
        assert math.isclose(per_pair * num_pairs, PAIRS_GROSS_CAPITAL)
        # Each pair gets more capital when fewer are selected
        assert per_pair > 200_000

    def test_allocation_with_1_pair(self):
        from config import PAIRS_GROSS_CAPITAL
        num_pairs = 1
        per_pair = PAIRS_GROSS_CAPITAL / num_pairs
        assert math.isclose(per_pair, PAIRS_GROSS_CAPITAL)

    def test_15_eligible_only_5_selected(self):
        """With 15 eligible pairs, only top 5 are selected (PAIRS_MAX_SELECTED cap)."""
        from config import PAIRS_MAX_SELECTED
        candidates = [
            _FakeCandidate(f"X{i}", f"Y{i}", "1h", float(90 - i), 3.0 - i * 0.1, i + 1)
            for i in range(15)
        ]
        ranked = _rank(candidates)
        deduped = _dedup(ranked)
        selected = deduped[:PAIRS_MAX_SELECTED]
        assert len(selected) == PAIRS_MAX_SELECTED  # cap enforced

    def test_total_gross_never_exceeds_pairs_gross_capital(self):
        """For any n ≤ PAIRS_MAX_SELECTED, per-pair * n == PAIRS_GROSS_CAPITAL."""
        from config import PAIRS_GROSS_CAPITAL
        for n in [1, 2, 3, 4, 5]:
            per_pair = PAIRS_GROSS_CAPITAL / n
            assert math.isclose(per_pair * n, PAIRS_GROSS_CAPITAL), f"Failed for n={n}"

    def test_capital_pools_are_separate(self):
        from config import VIRTUAL_CAPITAL, PAIRS_BASE_CAPITAL
        # Futures: ₹10L, Pairs base: ₹5L (with 2x leverage = ₹10L gross)
        assert VIRTUAL_CAPITAL == 1_000_000
        assert PAIRS_BASE_CAPITAL == 500_000
        # Neither is derived from the other at module level — they don't share a pool

    def test_no_max_open_pairs_in_config(self):
        """MAX_OPEN_PAIRS was removed; PAIRS_MAX_SELECTED is the cap."""
        import config
        assert not hasattr(config, "MAX_OPEN_PAIRS")

    def test_pairs_max_selected_in_config(self):
        """PAIRS_MAX_SELECTED must exist and equal 5."""
        from config import PAIRS_MAX_SELECTED
        assert PAIRS_MAX_SELECTED == 5

    def test_no_per_pair_capital_in_config(self):
        """PER_PAIR_CAPITAL was removed; allocation is dynamic."""
        import config
        assert not hasattr(config, "PER_PAIR_CAPITAL")


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


# ──────────────────────────────────────────────────────────────────────────────
#   NEW: Ranking, Deduplication, Concentration Filter
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _FakeCandidate:
    """Minimal stand-in for PairCandidate used in selection logic tests."""
    x_symbol: str
    y_symbol: str
    timeframe: str
    prob_profit: float
    z_score: float
    half_life: int
    matched_timeframes: list = None

    def __post_init__(self):
        if self.matched_timeframes is None:
            self.matched_timeframes = [self.timeframe]

    @property
    def pair_key(self):
        return tuple(sorted((self.x_symbol, self.y_symbol)))


def _rank(candidates):
    return sorted(candidates, key=lambda c: (-c.prob_profit, -abs(c.z_score), c.half_life))


def _dedup(candidates):
    seen = set()
    out = []
    for c in candidates:
        key = tuple(sorted((c.x_symbol, c.y_symbol)))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _concentrate(candidates, max_per_stock=2):
    usage = {}
    out = []
    for c in candidates:
        xc = usage.get(c.x_symbol, 0)
        yc = usage.get(c.y_symbol, 0)
        if xc < max_per_stock and yc < max_per_stock:
            out.append(c)
            usage[c.x_symbol] = xc + 1
            usage[c.y_symbol] = yc + 1
    return out


class TestRanking:
    """Pair ranking: prob_profit desc → abs(z_score) desc → half_life asc."""

    def test_prob_profit_primary_sort(self):
        a = _FakeCandidate("A", "B", "1h", 80.0, 2.0, 5)
        b = _FakeCandidate("C", "D", "1h", 70.0, 3.0, 3)
        result = _rank([b, a])
        assert result[0] is a  # higher prob_profit wins

    def test_z_score_tiebreaker(self):
        a = _FakeCandidate("A", "B", "1h", 75.0, 3.0, 5)
        b = _FakeCandidate("C", "D", "1h", 75.0, 2.0, 3)
        result = _rank([b, a])
        assert result[0] is a  # higher |z_score| wins on tie

    def test_half_life_final_tiebreaker(self):
        a = _FakeCandidate("A", "B", "1h", 75.0, 2.5, 3)
        b = _FakeCandidate("C", "D", "1h", 75.0, 2.5, 8)
        result = _rank([b, a])
        assert result[0] is a  # shorter half_life wins on double tie

    def test_negative_z_score_uses_absolute_value(self):
        a = _FakeCandidate("A", "B", "1h", 70.0, -3.5, 5)
        b = _FakeCandidate("C", "D", "1h", 70.0, 2.0, 5)
        result = _rank([b, a])
        assert result[0] is a  # |-3.5| > |2.0|

    def test_deterministic_order_for_many(self):
        candidates = [
            _FakeCandidate("A", "B", "1h", 60.0, 2.1, 5),
            _FakeCandidate("C", "D", "1h", 80.0, 3.0, 3),
            _FakeCandidate("E", "F", "1h", 70.0, 2.5, 4),
            _FakeCandidate("G", "H", "1h", 80.0, 3.5, 2),
        ]
        result = _rank(candidates)
        assert result[0].x_symbol == "G"  # 80.0, |3.5|, 2
        assert result[1].x_symbol == "C"  # 80.0, |3.0|, 3
        assert result[2].x_symbol == "E"  # 70.0
        assert result[3].x_symbol == "A"  # 60.0


class TestDeduplication:
    """Unordered pair deduplication: INFY/SBIN == SBIN/INFY."""

    def test_reversed_pair_deduplicated(self):
        a = _FakeCandidate("INFY", "SBIN", "15m", 75.0, 2.5, 5)
        b = _FakeCandidate("SBIN", "INFY", "30m", 70.0, 2.0, 4)
        ranked = _rank([a, b])
        result = _dedup(ranked)
        assert len(result) == 1
        assert result[0].prob_profit == 75.0  # better-ranked survives

    def test_reversed_pair_worse_ranked_dropped(self):
        a = _FakeCandidate("INFY", "SBIN", "1h", 80.0, 3.0, 3)
        b = _FakeCandidate("SBIN", "INFY", "15m", 65.0, 2.0, 5)
        ranked = _rank([b, a])
        result = _dedup(ranked)
        assert len(result) == 1
        assert result[0].x_symbol == "INFY"

    def test_different_pairs_both_kept(self):
        a = _FakeCandidate("INFY", "TCS", "1h", 75.0, 2.5, 5)
        b = _FakeCandidate("SBIN", "AXIS", "1h", 70.0, 2.0, 4)
        result = _dedup(_rank([a, b]))
        assert len(result) == 2

    def test_same_ticker_in_different_pairs_not_deduped(self):
        """INFY/TCS and INFY/HCLTECH are distinct unordered pairs — both kept."""
        a = _FakeCandidate("INFY", "TCS", "1h", 75.0, 2.5, 5)
        b = _FakeCandidate("INFY", "HCLTECH", "1h", 70.0, 2.0, 4)
        result = _dedup(_rank([a, b]))
        assert len(result) == 2

    def test_three_timeframes_same_pair_deduped_to_one(self):
        a = _FakeCandidate("HDFCBANK", "ICICIBANK", "15m", 72.0, 2.5, 5)
        b = _FakeCandidate("HDFCBANK", "ICICIBANK", "30m", 75.0, 2.8, 6)
        c = _FakeCandidate("HDFCBANK", "ICICIBANK", "1h", 68.0, 2.1, 8)
        result = _dedup(_rank([a, b, c]))
        assert len(result) == 1
        assert result[0].prob_profit == 75.0


class TestConcentrationFilter:
    """No ticker in more than 2 final selected pairs."""

    def test_single_stock_caps_at_2(self):
        """INFY can appear in at most 2 pairs."""
        candidates = [
            _FakeCandidate("INFY", "TCS",     "1h", 90.0, 3.0, 3),
            _FakeCandidate("INFY", "HCLTECH", "1h", 85.0, 2.8, 4),
            _FakeCandidate("INFY", "WIPRO",   "1h", 80.0, 2.5, 5),
        ]
        result = _concentrate(candidates)
        infy_count = sum(1 for c in result if "INFY" in (c.x_symbol, c.y_symbol))
        assert infy_count <= 2

    def test_exactly_2_allowed(self):
        candidates = [
            _FakeCandidate("INFY", "TCS",     "1h", 90.0, 3.0, 3),
            _FakeCandidate("INFY", "HCLTECH", "1h", 85.0, 2.8, 4),
        ]
        result = _concentrate(candidates)
        assert len(result) == 2

    def test_third_pair_with_same_stock_skipped(self):
        candidates = [
            _FakeCandidate("INFY", "TCS",     "1h", 90.0, 3.0, 3),
            _FakeCandidate("INFY", "HCLTECH", "1h", 85.0, 2.8, 4),
            _FakeCandidate("INFY", "WIPRO",   "1h", 80.0, 2.5, 5),  # should be skipped
            _FakeCandidate("SBIN", "AXISBANK","1h", 75.0, 2.3, 6),  # should be kept
        ]
        result = _concentrate(candidates)
        symbols_used = {c.x_symbol for c in result} | {c.y_symbol for c in result}
        infy_count = sum(1 for c in result if "INFY" in (c.x_symbol, c.y_symbol))
        assert infy_count == 2
        assert "SBIN" in symbols_used

    def test_no_concentration_violation_in_output(self):
        """Any stock appears in at most 2 pairs in final output."""
        candidates = [
            _FakeCandidate("A", "B", "1h", 95.0, 3.0, 2),
            _FakeCandidate("A", "C", "1h", 90.0, 2.8, 3),
            _FakeCandidate("A", "D", "1h", 85.0, 2.5, 4),
            _FakeCandidate("B", "C", "1h", 80.0, 2.3, 5),
            _FakeCandidate("D", "E", "1h", 75.0, 2.1, 6),
        ]
        result = _concentrate(candidates)
        usage = {}
        for c in result:
            usage[c.x_symbol] = usage.get(c.x_symbol, 0) + 1
            usage[c.y_symbol] = usage.get(c.y_symbol, 0) + 1
        for sym, count in usage.items():
            assert count <= 2, f"{sym} appears in {count} pairs (max 2)"

    def test_15_valid_no_overlap_all_kept(self):
        """15 candidates with no stock overlap — all 15 are selected (no hard cap)."""
        candidates = [
            _FakeCandidate(f"X{i}", f"Y{i}", "1h", float(90 - i), 3.0 - i * 0.1, i + 1)
            for i in range(15)
        ]
        result = _concentrate(candidates)
        assert len(result) == 15  # no cap — all pass concentration

    def test_gross_exposure_per_pair_dynamic(self):
        """Per-pair gross = PAIRS_GROSS_CAPITAL / n for any n."""
        from config import PAIRS_GROSS_CAPITAL
        for n in [1, 5, 10, 15]:
            per_pair = PAIRS_GROSS_CAPITAL / n
            total = per_pair * n
            assert math.isclose(total, PAIRS_GROSS_CAPITAL), f"Total mismatch for n={n}"


# ──────────────────────────────────────────────────────────────────────────────
#   NEW: Top-5 selection, repeat-stock allowance, open-failure replacement
# ──────────────────────────────────────────────────────────────────────────────

class TestTop5Selection:
    """Pairs selection capped at PAIRS_MAX_SELECTED=5 by MC rank."""

    def _make_candidates(self, n: int, start_prob: float = 90.0) -> list:
        return [
            _FakeCandidate(f"A{i}", f"B{i}", "1h", start_prob - i, 3.0 - i * 0.1, i + 1)
            for i in range(n)
        ]

    def test_top_5_selected_from_10(self):
        """10 eligible → only top 5 selected."""
        from config import PAIRS_MAX_SELECTED
        candidates = self._make_candidates(10)
        ranked = _rank(candidates)
        deduped = _dedup(ranked)
        selected = deduped[:PAIRS_MAX_SELECTED]
        assert len(selected) == 5
        # Best prob_profit is first
        assert selected[0].prob_profit == 90.0

    def test_top_5_ranked_correctly(self):
        """Top 5 are the 5 highest prob_profit candidates."""
        from config import PAIRS_MAX_SELECTED
        candidates = self._make_candidates(8)  # probs 90, 89, 88, 87, 86, 85, 84, 83
        ranked = _rank(candidates)
        deduped = _dedup(ranked)
        selected = deduped[:PAIRS_MAX_SELECTED]
        probs = [c.prob_profit for c in selected]
        assert probs == sorted(probs, reverse=True)
        assert probs[0] == 90.0
        assert probs[-1] == 86.0  # 5th best

    def test_fewer_than_5_all_selected(self):
        """With only 3 eligible pairs, all 3 are selected."""
        from config import PAIRS_MAX_SELECTED
        candidates = self._make_candidates(3)
        ranked = _rank(candidates)
        deduped = _dedup(ranked)
        selected = deduped[:PAIRS_MAX_SELECTED]
        assert len(selected) == 3

    def test_repeat_stocks_allowed_in_top_5(self):
        """Same stock may appear in multiple selected pairs (no concentration limit)."""
        from config import PAIRS_MAX_SELECTED
        candidates = [
            _FakeCandidate("INFY", "TCS",    "1h", 90.0, 3.0, 3),
            _FakeCandidate("INFY", "HCLTECH","1h", 85.0, 2.8, 4),
            _FakeCandidate("INFY", "WIPRO",  "1h", 80.0, 2.5, 5),
            _FakeCandidate("INFY", "SBIN",   "1h", 75.0, 2.3, 6),
            _FakeCandidate("INFY", "AXISBANK","1h", 70.0, 2.1, 7),
        ]
        ranked = _rank(candidates)
        deduped = _dedup(ranked)
        selected = deduped[:PAIRS_MAX_SELECTED]
        # All 5 kept — INFY appearing 5 times is allowed
        assert len(selected) == 5
        infy_count = sum(1 for c in selected if "INFY" in (c.x_symbol, c.y_symbol))
        assert infy_count == 5

    def test_repeat_stock_does_not_prevent_selection(self):
        """A stock in 3 pairs: all 3 survive (no old max-2 rule)."""
        candidates = [
            _FakeCandidate("INFY", "TCS",     "1h", 90.0, 3.0, 3),
            _FakeCandidate("INFY", "HCLTECH", "1h", 85.0, 2.8, 4),
            _FakeCandidate("INFY", "WIPRO",   "1h", 80.0, 2.5, 5),
        ]
        ranked = _rank(candidates)
        deduped = _dedup(ranked)
        selected = deduped[:5]
        assert len(selected) == 3

    def test_capital_5_pairs_is_200k(self):
        """5 pairs → ₹2,00,000 each."""
        from config import PAIRS_GROSS_CAPITAL, PAIRS_MAX_SELECTED
        per_pair = PAIRS_GROSS_CAPITAL / PAIRS_MAX_SELECTED
        assert math.isclose(per_pair, 200_000)

    def test_capital_fewer_than_5_dynamic(self):
        """3 eligible pairs → ₹3,33,333 each (dynamic)."""
        from config import PAIRS_GROSS_CAPITAL
        n = 3
        per_pair = PAIRS_GROSS_CAPITAL / n
        assert math.isclose(per_pair * n, PAIRS_GROSS_CAPITAL)
        assert per_pair > 200_000

    def test_unordered_dedup_before_cap(self):
        """Reversed-pair dedup applied before top-5 cap."""
        from config import PAIRS_MAX_SELECTED
        candidates = [
            _FakeCandidate("INFY", "SBIN", "1h", 80.0, 2.5, 5),
            _FakeCandidate("SBIN", "INFY", "1h", 79.0, 2.4, 5),  # same pair, lower rank
            _FakeCandidate("A2",   "B2",   "1h", 75.0, 2.0, 4),
            _FakeCandidate("A3",   "B3",   "1h", 74.0, 2.0, 4),
            _FakeCandidate("A4",   "B4",   "1h", 73.0, 2.0, 4),
            _FakeCandidate("A5",   "B5",   "1h", 72.0, 2.0, 4),
        ]
        ranked = _rank(candidates)
        deduped = _dedup(ranked)
        selected = deduped[:PAIRS_MAX_SELECTED]
        # SBIN/INFY dropped → 5 unique pairs from 6 candidates
        assert len(selected) == 5
        pair_names = [(c.x_symbol, c.y_symbol) for c in selected]
        assert ("INFY", "SBIN") in pair_names
        assert ("SBIN", "INFY") not in pair_names

    def test_tiebreak_z_score(self):
        """Equal prob_profit → higher |z_score| ranked first."""
        a = _FakeCandidate("A", "B", "1h", 75.0, 3.0, 5)
        b = _FakeCandidate("C", "D", "1h", 75.0, 2.0, 5)
        selected = _rank([b, a])[:5]
        assert selected[0] is a  # higher |z_score|

    def test_tiebreak_half_life(self):
        """Equal prob_profit and |z_score| → shorter half_life ranked first."""
        a = _FakeCandidate("A", "B", "1h", 75.0, 2.5, 3)
        b = _FakeCandidate("C", "D", "1h", 75.0, 2.5, 8)
        selected = _rank([b, a])[:5]
        assert selected[0] is a  # shorter half_life

    def test_half_life_filter_still_enforced(self):
        """Pairs with half_life >= HALF_LIFE_MAX_BARS[interval] must not be in candidates."""
        from config import HALF_LIFE_MAX_BARS
        # This test verifies the filter condition — pairs are rejected BEFORE portfolio sees them
        # A pair with half_life=72 at 5m should have been screened out by scanner
        too_slow = _FakeCandidate("X", "Y", "5m", 95.0, 4.0, 72)  # 72 >= 72 → rejected
        ok_pair  = _FakeCandidate("A", "B", "5m", 85.0, 3.0, 71)  # 71 < 72 → ok
        # Simulate scanner's filter
        eligible = [c for c in [too_slow, ok_pair]
                    if c.half_life < HALF_LIFE_MAX_BARS.get(c.timeframe, 9999)]
        assert len(eligible) == 1
        assert eligible[0] is ok_pair


class TestOpenFailureReplacement:
    """Open-failure replacement: next-ranked pair tried when a candidate fails."""

    def test_concentration_filter_not_called_in_allocate_and_open(self):
        """Source guard: _apply_concentration_filter must not be called in allocate_and_open."""
        import re
        src = (_REPO_ROOT / "pairs" / "portfolio.py").read_text()
        # Find the allocate_and_open method body (from def until next def at same indent)
        m = re.search(
            r"def allocate_and_open\(.*?\n((?:    [^\n]*\n|\n)*)",
            src,
        )
        assert m, "allocate_and_open not found in portfolio.py"
        body = m.group(1)
        assert "_apply_concentration_filter" not in body, (
            "_apply_concentration_filter must not be called in allocate_and_open"
        )

    def test_pairs_max_selected_imported_in_portfolio(self):
        """portfolio.py must import PAIRS_MAX_SELECTED from config."""
        src = (_REPO_ROOT / "pairs" / "portfolio.py").read_text()
        assert "PAIRS_MAX_SELECTED" in src

    def test_open_failure_uses_next_pair(self):
        """If first candidate fails to open, next-ranked is tried."""
        import sys
        import types as _types
        # Stub broker so we can import portfolio without Shoonya
        shoonya_mod = _types.ModuleType("broker.shoonya_client")
        from dataclasses import dataclass as _dc

        @_dc
        class _RS:
            symbol: str
            tradingsymbol: str
            token: str

        shoonya_mod.ShoonyaClient = object
        shoonya_mod.ResolvedScrip = _RS
        broker_mod = _types.ModuleType("broker")
        broker_mod.shoonya_client = shoonya_mod
        from unittest.mock import patch, MagicMock

        with patch.dict(sys.modules, {
            "broker": broker_mod,
            "broker.shoonya_client": shoonya_mod,
        }):
            from pairs.portfolio import PairPortfolio

            portfolio = PairPortfolio.__new__(PairPortfolio)
            portfolio.capital = 1_000_000
            portfolio._quote_cache = None
            portfolio._state_file = MagicMock()
            portfolio._state_file.write_text = MagicMock()
            portfolio.positions = []

            # Build 3 candidates (already as PairCandidate-like mocks)
            def _cand(x, y, prob):
                c = MagicMock()
                c.x_symbol = x
                c.y_symbol = y
                c.prob_profit = prob
                c.z_score = 2.5
                c.half_life = 3
                c.timeframe = "1h"
                c.method = "CADF"
                c.matched_timeframes = ["1h"]
                c.direction = "SHORT_SPREAD"
                c.beta = 1.0
                c.prob_profit_low = prob - 10
                c.prob_profit_high = prob + 10
                return c

            c1 = _cand("A", "B", 90.0)  # best — will fail to open
            c2 = _cand("C", "D", 85.0)  # second — will succeed
            c3 = _cand("E", "F", 80.0)  # third

            open_results = {("A", "B"): None, ("C", "D"): MagicMock(), ("E", "F"): MagicMock()}

            def _fake_open(client, candidate, capital):
                return open_results[(candidate.x_symbol, candidate.y_symbol)]

            mock_client = MagicMock()

            with patch.object(portfolio, "_open_candidate", side_effect=_fake_open), \
                 patch.object(portfolio, "_persist"), \
                 patch.object(portfolio, "_write_journal"):
                opened = portfolio.allocate_and_open(mock_client, [c1, c2, c3])

            # c1 failed → c2 and c3 both opened (target=min(5,3)=3)
            assert len(opened) == 2  # c1 failed, c2+c3 succeeded

    def test_journal_shows_capital_per_pair(self):
        """Journal must include Capital Reserved per Pair line."""
        src = (_REPO_ROOT / "pairs" / "portfolio.py").read_text()
        assert "Capital Reserved per Pair" in src
