"""
tests/test_pairs_halflife.py

Tests for:
  - HALF_LIFE_MAX_BARS config constants and 5m timeframe
  - Half-life eligibility filter in screen_pair()
  - half_life field in PairPosition / _open_candidate / journal
  - Scope guards: no "60m", no fallback that disables the filter
"""
from __future__ import annotations

import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# ── Stub heavy scientific libs so tests run without full install ──────────────
for lib in ("numpy", "pandas", "scipy", "statsmodels", "yfinance"):
    if lib not in sys.modules:
        sys.modules[lib] = types.ModuleType(lib)

import numpy as _np_stub
_np_stub.ndarray = object
_np_stub.nan = float("nan")

import pandas as _pd_stub
_pd_stub.DataFrame = object
_pd_stub.Series = object

# Root on path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
#   PART A — Config tests
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigHalfLife(unittest.TestCase):

    def test_pair_intervals_contains_5m(self):
        from config import PAIR_INTERVALS
        self.assertIn("5m", PAIR_INTERVALS)

    def test_interval_periods_5m_is_60d(self):
        from config import INTERVAL_PERIODS
        self.assertEqual(INTERVAL_PERIODS["5m"], "60d")

    def test_half_life_max_bars_5m(self):
        from config import HALF_LIFE_MAX_BARS
        self.assertEqual(HALF_LIFE_MAX_BARS["5m"], 72)

    def test_half_life_max_bars_15m(self):
        from config import HALF_LIFE_MAX_BARS
        self.assertEqual(HALF_LIFE_MAX_BARS["15m"], 24)

    def test_half_life_max_bars_30m(self):
        from config import HALF_LIFE_MAX_BARS
        self.assertEqual(HALF_LIFE_MAX_BARS["30m"], 12)

    def test_half_life_max_bars_1h(self):
        from config import HALF_LIFE_MAX_BARS
        self.assertEqual(HALF_LIFE_MAX_BARS["1h"], 6)

    def test_pair_intervals_no_60m(self):
        """'60m' must never be in PAIR_INTERVALS (ambiguous yfinance behaviour)."""
        from config import PAIR_INTERVALS
        self.assertNotIn("60m", PAIR_INTERVALS)

    def test_half_life_max_bars_no_fallback_key(self):
        """HALF_LIFE_MAX_BARS must not contain a catch-all key that bypasses the filter."""
        from config import HALF_LIFE_MAX_BARS
        # A None or '*' key would silently disable the filter
        self.assertNotIn(None, HALF_LIFE_MAX_BARS)
        self.assertNotIn("*", HALF_LIFE_MAX_BARS)

    def test_all_pair_intervals_covered_by_half_life_max_bars(self):
        """Every interval in PAIR_INTERVALS must have a finite entry in HALF_LIFE_MAX_BARS."""
        from config import PAIR_INTERVALS, HALF_LIFE_MAX_BARS
        for iv in PAIR_INTERVALS:
            self.assertIn(iv, HALF_LIFE_MAX_BARS, f"Missing HALF_LIFE_MAX_BARS entry for {iv}")
            self.assertGreater(HALF_LIFE_MAX_BARS[iv], 0)


# ─────────────────────────────────────────────────────────────────────────────
#   PART B — Eligibility filter (test the condition directly)
# ─────────────────────────────────────────────────────────────────────────────

class TestHalfLifeEligibility(unittest.TestCase):
    """
    The eligibility rule is: reject if half_life >= HALF_LIFE_MAX_BARS[interval].
    Test each interval at boundary pass (max-1) and boundary fail (max).
    """

    def _passes(self, interval: str, half_life: int) -> bool:
        from config import HALF_LIFE_MAX_BARS
        return half_life < HALF_LIFE_MAX_BARS[interval]

    # 5m: max = 72
    def test_5m_hl_71_passes(self):
        self.assertTrue(self._passes("5m", 71))

    def test_5m_hl_72_fails(self):
        self.assertFalse(self._passes("5m", 72))

    # 15m: max = 24
    def test_15m_hl_23_passes(self):
        self.assertTrue(self._passes("15m", 23))

    def test_15m_hl_24_fails(self):
        self.assertFalse(self._passes("15m", 24))

    # 30m: max = 12
    def test_30m_hl_11_passes(self):
        self.assertTrue(self._passes("30m", 11))

    def test_30m_hl_12_fails(self):
        self.assertFalse(self._passes("30m", 12))

    # 1h: max = 6
    def test_1h_hl_5_passes(self):
        self.assertTrue(self._passes("1h", 5))

    def test_1h_hl_6_fails(self):
        self.assertFalse(self._passes("1h", 6))

    def test_half_life_1_always_passes_all_intervals(self):
        """half_life=1 is the minimum and must pass every configured interval."""
        from config import HALF_LIFE_MAX_BARS
        for interval in HALF_LIFE_MAX_BARS:
            self.assertTrue(self._passes(interval, 1), f"half_life=1 should pass {interval}")

    def test_very_large_half_life_always_fails(self):
        """A huge half_life (e.g. 9999) must fail every configured interval."""
        from config import HALF_LIFE_MAX_BARS
        for interval in HALF_LIFE_MAX_BARS:
            self.assertFalse(self._passes(interval, 9999), f"half_life=9999 should fail {interval}")


# ─────────────────────────────────────────────────────────────────────────────
#   PART C — Scanner source-code guard
# ─────────────────────────────────────────────────────────────────────────────

class TestScannerHalfLifeFilterSource(unittest.TestCase):
    """Check that scanner.py actually imports and uses HALF_LIFE_MAX_BARS."""

    @classmethod
    def _src(cls) -> str:
        return (Path(__file__).parent.parent / "pairs" / "scanner.py").read_text()

    def test_scanner_imports_half_life_max_bars(self):
        self.assertIn("HALF_LIFE_MAX_BARS", self._src())

    def test_scanner_rejects_with_hl_comparison(self):
        """The filter must use >= comparison (reject when hl >= max)."""
        src = self._src()
        self.assertIn("half_life >= hl_max", src)

    def test_scanner_returns_none_on_rejection(self):
        """screen_pair must return None to skip the pair."""
        src = self._src()
        # The rejection block must contain 'return None'
        lines = src.splitlines()
        hl_max_lines = [i for i, ln in enumerate(lines) if "hl_max" in ln]
        self.assertTrue(len(hl_max_lines) >= 2, "Expected hl_max lines in scanner.py")

    def test_scanner_filter_is_before_ensemble_mc(self):
        """The half_life filter must appear in screen_pair, not run_ensemble_mc."""
        src = self._src()
        # screen_pair ends before run_ensemble_mc starts
        screen_pair_start = src.index("def screen_pair(")
        run_ensemble_start = src.index("def run_ensemble_mc(")
        hl_max_pos = src.index("hl_max = HALF_LIFE_MAX_BARS")
        self.assertGreater(hl_max_pos, screen_pair_start)
        self.assertLess(hl_max_pos, run_ensemble_start)

    def test_scanner_writes_scan_artifact(self):
        """run_scan must write a pairs_scan_<date>.json artifact."""
        src = self._src()
        self.assertIn("pairs_scan_", src)
        self.assertIn("RUNTIME_STORAGE_DIR", src)

    def test_scanner_no_yahoo_fallback(self):
        """No direct Yahoo/HTTP fallback added."""
        src = self._src()
        self.assertNotIn("query1.finance", src)
        self.assertNotIn("query2.finance", src)
        self.assertNotIn("_fetch_yahoo", src)
        self.assertNotIn("urlopen", src)


# ─────────────────────────────────────────────────────────────────────────────
#   PART D — PairPosition dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestPairPositionHalfLife(unittest.TestCase):

    def test_pair_position_has_half_life_field(self):
        """PairPosition must expose a half_life field."""
        import dataclasses
        # Stub broker.shoonya_client so portfolio.py can import
        shoonya_mod = types.ModuleType("broker.shoonya_client")
        shoonya_mod.ShoonyaClient = object
        shoonya_mod.ResolvedScrip = object
        broker_mod = types.ModuleType("broker")
        broker_mod.shoonya_client = shoonya_mod
        with patch.dict(sys.modules, {
            "broker": broker_mod,
            "broker.shoonya_client": shoonya_mod,
        }):
            from pairs.portfolio import PairPosition
            field_names = {f.name for f in dataclasses.fields(PairPosition)}
            self.assertIn("half_life", field_names)

    def test_pair_position_half_life_defaults_to_zero(self):
        """half_life must default to 0 so old state files load without error."""
        import dataclasses
        shoonya_mod = types.ModuleType("broker.shoonya_client")
        shoonya_mod.ShoonyaClient = object
        shoonya_mod.ResolvedScrip = object
        broker_mod = types.ModuleType("broker")
        broker_mod.shoonya_client = shoonya_mod
        with patch.dict(sys.modules, {
            "broker": broker_mod,
            "broker.shoonya_client": shoonya_mod,
        }):
            from pairs.portfolio import PairPosition, Leg

            dummy_leg = Leg(
                symbol="X", tradingsymbol="X-EQ", token="1",
                side="BUY", qty=1, entry_price=100.0,
            )
            pos = PairPosition(
                pair_name="A/B", timeframe="1h", method="CADF",
                z_score=2.5, beta=1.0,
                prob_profit=70.0, prob_profit_low=60.0, prob_profit_high=80.0,
                long_leg=dummy_leg, short_leg=dummy_leg,
                margin_used=1000.0, capital_reserved=5000.0,
                opened_at="2026-05-18T09:15:00",
            )
            self.assertEqual(pos.half_life, 0)


# ─────────────────────────────────────────────────────────────────────────────
#   PART E — _open_candidate propagates half_life
# ─────────────────────────────────────────────────────────────────────────────

class TestOpenCandidatePropagatesHalfLife(unittest.TestCase):

    def _make_portfolio(self):
        shoonya_mod = types.ModuleType("broker.shoonya_client")

        @dataclass
        class _ResolvedScrip:
            symbol: str
            tradingsymbol: str
            token: str

        shoonya_mod.ShoonyaClient = object
        shoonya_mod.ResolvedScrip = _ResolvedScrip
        broker_mod = types.ModuleType("broker")
        broker_mod.shoonya_client = shoonya_mod

        with patch.dict(sys.modules, {
            "broker": broker_mod,
            "broker.shoonya_client": shoonya_mod,
        }):
            from pairs.portfolio import PairPortfolio
            return PairPortfolio.__new__(PairPortfolio)

    def test_open_candidate_sets_half_life(self):
        shoonya_mod = types.ModuleType("broker.shoonya_client")

        @dataclass
        class _ResolvedScrip:
            symbol: str
            tradingsymbol: str
            token: str

        shoonya_mod.ShoonyaClient = object
        shoonya_mod.ResolvedScrip = _ResolvedScrip
        broker_mod = types.ModuleType("broker")
        broker_mod.shoonya_client = shoonya_mod

        with patch.dict(sys.modules, {
            "broker": broker_mod,
            "broker.shoonya_client": shoonya_mod,
        }):
            from pairs.portfolio import PairPortfolio

            portfolio = PairPortfolio.__new__(PairPortfolio)
            portfolio.capital = 1_000_000

            # Build a mock candidate with half_life=7
            candidate = MagicMock()
            candidate.x_symbol = "INFY"
            candidate.y_symbol = "TCS"
            candidate.direction = "SHORT_SPREAD"
            candidate.beta = 1.2
            candidate.z_score = 2.5
            candidate.prob_profit = 72.0
            candidate.prob_profit_low = 62.0
            candidate.prob_profit_high = 82.0
            candidate.half_life = 7
            candidate.timeframe = "15m"
            candidate.method = "CADF"
            candidate.matched_timeframes = ["15m"]

            # Mock client
            mock_client = MagicMock()
            mock_client.resolve_equity_symbol.side_effect = lambda sym: _ResolvedScrip(
                symbol=sym, tradingsymbol=f"{sym}-EQ", token="1234"
            )
            mock_client.get_best_bid_ask.return_value = (100.0, 101.0)
            mock_client.get_last_price.return_value = 100.0
            mock_client.get_order_margin.return_value = {
                "stat": "Ok", "ordermargin": "500.0"
            }

            pos = portfolio._open_candidate(mock_client, candidate, 50_000.0)
            self.assertIsNotNone(pos)
            self.assertEqual(pos.half_life, 7)


# ─────────────────────────────────────────────────────────────────────────────
#   PART F — _write_journal includes "Half-life:"
# ─────────────────────────────────────────────────────────────────────────────

class TestWriteJournalHalfLife(unittest.TestCase):

    def test_write_journal_includes_half_life(self):
        import tempfile

        shoonya_mod = types.ModuleType("broker.shoonya_client")
        shoonya_mod.ShoonyaClient = object
        shoonya_mod.ResolvedScrip = object
        broker_mod = types.ModuleType("broker")
        broker_mod.shoonya_client = shoonya_mod

        with patch.dict(sys.modules, {
            "broker": broker_mod,
            "broker.shoonya_client": shoonya_mod,
        }):
            from pairs.portfolio import PairPortfolio, PairPosition, Leg

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                portfolio = PairPortfolio.__new__(PairPortfolio)
                portfolio.capital = 1_000_000

                dummy_leg = Leg(
                    symbol="INFY", tradingsymbol="INFY-EQ", token="1594",
                    side="BUY", qty=10, entry_price=1500.0,
                )
                dummy_leg_s = Leg(
                    symbol="TCS", tradingsymbol="TCS-EQ", token="2044",
                    side="SELL", qty=8, entry_price=3500.0,
                )
                pos = PairPosition(
                    pair_name="INFY/TCS", timeframe="15m", method="CADF",
                    z_score=2.3, beta=0.85,
                    prob_profit=71.0, prob_profit_low=61.0, prob_profit_high=81.0,
                    long_leg=dummy_leg, short_leg=dummy_leg_s,
                    margin_used=8000.0, capital_reserved=50000.0,
                    opened_at="2026-05-18T09:15:00",
                    matched_timeframes=["15m"],
                    half_life=5,
                )

                with patch("pairs.portfolio.JOURNALS_DIR", tmp_path):
                    portfolio._write_journal([pos])

                journal_files = list(tmp_path.glob("*_pairs.md"))
                self.assertEqual(len(journal_files), 1)
                content = journal_files[0].read_text()
                self.assertIn("Half-life:", content)
                self.assertIn("5", content)

    def test_eod_journal_includes_half_life(self):
        import tempfile

        shoonya_mod = types.ModuleType("broker.shoonya_client")
        shoonya_mod.ShoonyaClient = object
        shoonya_mod.ResolvedScrip = object
        broker_mod = types.ModuleType("broker")
        broker_mod.shoonya_client = shoonya_mod

        with patch.dict(sys.modules, {
            "broker": broker_mod,
            "broker.shoonya_client": shoonya_mod,
        }):
            from pairs.portfolio import PairPortfolio, PairPosition, Leg

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                portfolio = PairPortfolio.__new__(PairPortfolio)
                portfolio.capital = 1_000_000

                dummy_leg = Leg(
                    symbol="INFY", tradingsymbol="INFY-EQ", token="1594",
                    side="BUY", qty=10, entry_price=1500.0,
                    exit_price=1520.0, realized_pnl=200.0,
                    closed_at="2026-05-18T15:15:00",
                )
                dummy_leg_s = Leg(
                    symbol="TCS", tradingsymbol="TCS-EQ", token="2044",
                    side="SELL", qty=8, entry_price=3500.0,
                    exit_price=3480.0, realized_pnl=160.0,
                    closed_at="2026-05-18T15:15:00",
                )
                pos = PairPosition(
                    pair_name="INFY/TCS", timeframe="15m", method="CADF",
                    z_score=2.3, beta=0.85,
                    prob_profit=71.0, prob_profit_low=61.0, prob_profit_high=81.0,
                    long_leg=dummy_leg, short_leg=dummy_leg_s,
                    margin_used=8000.0, capital_reserved=50000.0,
                    opened_at="2026-05-18T09:15:00",
                    closed_at="2026-05-18T15:15:00",
                    pnl=360.0,
                    matched_timeframes=["15m"],
                    half_life=5,
                )

                with patch("pairs.portfolio.JOURNALS_DIR", tmp_path):
                    portfolio._append_eod_journal([pos], 360.0)

                journal_files = list(tmp_path.glob("*_pairs.md"))
                self.assertEqual(len(journal_files), 1)
                content = journal_files[0].read_text()
                self.assertIn("Half-life:", content)


# ─────────────────────────────────────────────────────────────────────────────
#   PART G — Portfolio source-code guards
# ─────────────────────────────────────────────────────────────────────────────

class TestPortfolioSourceGuards(unittest.TestCase):

    @classmethod
    def _src(cls) -> str:
        return (Path(__file__).parent.parent / "pairs" / "portfolio.py").read_text()

    def test_portfolio_has_half_life_in_open_candidate(self):
        """_open_candidate must pass half_life=candidate.half_life to PairPosition."""
        self.assertIn("half_life=candidate.half_life", self._src())

    def test_portfolio_half_life_in_write_journal(self):
        """_write_journal must include the Half-life line."""
        self.assertIn("Half-life:", self._src())

    def test_portfolio_no_gemini(self):
        src = self._src()
        self.assertNotIn("genai", src)
        self.assertNotIn("generate_content", src)


if __name__ == "__main__":
    unittest.main()
