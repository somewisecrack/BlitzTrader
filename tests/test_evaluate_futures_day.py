"""
tests/test_evaluate_futures_day.py
------------------------------------
Regression tests for scripts/evaluate_futures_day.py — Issue 2 fix.

Covers:
  - 7 NIFTY/BANKNIFTY trades counted from fixture live_state.json (not 123; FINNIFTY excluded)
  - Placeholder "—" rows ignored
  - emitted_signal_keys not parsed as trades
  - Rejected signal count = 3 (not polluted by placeholders)
  - P&L included when live_state.json available
  - "unknown reason" not in Patterns Observed
  - propose_futures_hypotheses receives clean compact review (no bogus rows)
"""
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_futures_day import (
    parse_live_state,
    parse_journal,
    compute_trade_stats,
    detect_patterns,
    build_review_markdown,
    is_futures_symbol,
    _epoch_to_date,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IST = pytz.timezone("Asia/Kolkata")
REVIEW_DATE = date(2026, 5, 18)


def _make_epoch(d: date, hour: int = 9, minute: int = 30) -> float:
    """Return an IST epoch float for the given date + time."""
    dt = IST.localize(datetime(d.year, d.month, d.day, hour, minute, 0))
    return dt.timestamp()


def _make_live_state(trades: list) -> dict:
    return {"trades": trades, "daily_pnl": -156, "virtual_capital": 1000000}


# Fixture: the 10 real trades from 2026-05-18 (includes 3 FINNIFTY historical trades;
# parse_live_state now filters these out since FINNIFTY is not in the active universe)
FIXTURE_TRADES = [
    {"symbol": "BANKNIFTY26MAY26F", "direction": "SELL", "quantity": 30,
     "entry_price": 53190.0, "exit_price": 53199.6, "pnl": -288.0,
     "entry_time": _make_epoch(REVIEW_DATE, 9, 20), "exit_time": _make_epoch(REVIEW_DATE, 10, 0)},
    {"symbol": "FINNIFTY26MAY26F", "direction": "BUY", "quantity": 60,
     "entry_price": 25055.7, "exit_price": 25194.2, "pnl": 8310.0,
     "entry_time": _make_epoch(REVIEW_DATE, 10, 36), "exit_time": _make_epoch(REVIEW_DATE, 11, 0)},
    {"symbol": "BANKNIFTY26MAY26F", "direction": "BUY", "quantity": 30,
     "entry_price": 53240.0, "exit_price": 53150.0, "pnl": -2700.0,
     "entry_time": _make_epoch(REVIEW_DATE, 11, 23), "exit_time": _make_epoch(REVIEW_DATE, 12, 0)},
    {"symbol": "BANKNIFTY26MAY26F", "direction": "BUY", "quantity": 30,
     "entry_price": 53217.4, "exit_price": 53163.2, "pnl": -1626.0,
     "entry_time": _make_epoch(REVIEW_DATE, 12, 18), "exit_time": _make_epoch(REVIEW_DATE, 12, 20)},
    {"symbol": "BANKNIFTY26MAY26F", "direction": "SELL", "quantity": 30,
     "entry_price": 53170.2, "exit_price": 53249.8, "pnl": -2394.0,
     "entry_time": _make_epoch(REVIEW_DATE, 12, 21), "exit_time": _make_epoch(REVIEW_DATE, 12, 30)},
    {"symbol": "FINNIFTY26MAY26F", "direction": "BUY", "quantity": 60,
     "entry_price": 25156.6, "exit_price": 25251.5, "pnl": 5694.0,
     "entry_time": _make_epoch(REVIEW_DATE, 12, 12), "exit_time": _make_epoch(REVIEW_DATE, 12, 40)},
    {"symbol": "FINNIFTY26MAY26F", "direction": "BUY", "quantity": 60,
     "entry_price": 25333.8, "exit_price": 25398.5, "pnl": 3882.0,
     "entry_time": _make_epoch(REVIEW_DATE, 12, 57), "exit_time": _make_epoch(REVIEW_DATE, 14, 0)},
    {"symbol": "NIFTY26MAY26F", "direction": "SELL", "quantity": 75,
     "entry_price": 23420.0, "exit_price": 23643.4, "pnl": -16705.0,
     "entry_time": _make_epoch(REVIEW_DATE, 9, 20), "exit_time": _make_epoch(REVIEW_DATE, 15, 2)},
    {"symbol": "BANKNIFTY26MAY26F", "direction": "BUY", "quantity": 30,
     "entry_price": 53440.0, "exit_price": 53746.0, "pnl": 9168.0,
     "entry_time": _make_epoch(REVIEW_DATE, 12, 39), "exit_time": _make_epoch(REVIEW_DATE, 15, 0)},
    {"symbol": "NIFTY26MAY26F", "direction": "BUY", "quantity": 75,
     "entry_price": 23684.8, "exit_price": 23638.2, "pnl": -3497.0,
     "entry_time": _make_epoch(REVIEW_DATE, 15, 2), "exit_time": _make_epoch(REVIEW_DATE, 15, 15)},
]


# ---------------------------------------------------------------------------
# Test: parse_live_state
# ---------------------------------------------------------------------------

class TestParseLiveState:

    def test_7_trades_from_fixture(self, tmp_path):
        """Exactly 7 NIFTY/BANKNIFTY trades should be parsed from the fixture.
        The fixture contains 3 FINNIFTY trades which are excluded since FINNIFTY
        was removed from the active futures universe."""
        ls_path = tmp_path / "live_state.json"
        ls_path.write_text(json.dumps(_make_live_state(FIXTURE_TRADES)))
        trades = parse_live_state(ls_path, REVIEW_DATE)
        assert len(trades) == 7, f"Expected 7 (NIFTY+BANKNIFTY only), got {len(trades)}"

    def test_no_placeholder_rows(self, tmp_path):
        """No trade should have '—' as symbol."""
        ls_path = tmp_path / "live_state.json"
        ls_path.write_text(json.dumps(_make_live_state(FIXTURE_TRADES)))
        trades = parse_live_state(ls_path, REVIEW_DATE)
        for t in trades:
            assert "—" not in t.get("symbol", ""), (
                f"Placeholder symbol in trade: {t}"
            )

    def test_only_futures_symbols_included(self, tmp_path):
        """Pairs/equity trades should not appear in parsed trades."""
        extra_trades = FIXTURE_TRADES + [
            {"symbol": "INFY", "direction": "BUY", "quantity": 100,
             "pnl": 1000.0, "entry_time": _make_epoch(REVIEW_DATE, 10, 0),
             "exit_time": _make_epoch(REVIEW_DATE, 11, 0)},
        ]
        ls_path = tmp_path / "live_state.json"
        ls_path.write_text(json.dumps(_make_live_state(extra_trades)))
        trades = parse_live_state(ls_path, REVIEW_DATE)
        assert len(trades) == 7  # INFY excluded; FINNIFTY excluded (removed from active universe)

    def test_different_date_trades_excluded(self, tmp_path):
        """Trades from a different date should not appear."""
        other_date = date(2026, 5, 17)
        other_trades = [
            {"symbol": "NIFTY26MAY26F", "direction": "BUY", "quantity": 75,
             "pnl": 5000.0, "entry_time": _make_epoch(other_date, 10, 0),
             "exit_time": _make_epoch(other_date, 11, 0)},
        ]
        ls_path = tmp_path / "live_state.json"
        ls_path.write_text(json.dumps(_make_live_state(other_trades)))
        trades = parse_live_state(ls_path, REVIEW_DATE)
        assert len(trades) == 0, "Trades from previous day should be excluded"

    def test_pnl_values_parsed(self, tmp_path):
        """P&L values from live_state.json should be numeric floats."""
        ls_path = tmp_path / "live_state.json"
        ls_path.write_text(json.dumps(_make_live_state(FIXTURE_TRADES)))
        trades = parse_live_state(ls_path, REVIEW_DATE)
        for t in trades:
            pnl = t.get("pnl")
            assert pnl is None or isinstance(pnl, float), (
                f"P&L should be float, got {type(pnl)}: {pnl}"
            )

    def test_missing_file_returns_empty(self, tmp_path):
        """Missing live_state.json should return empty list, not crash."""
        ls_path = tmp_path / "nonexistent_live_state.json"
        trades = parse_live_state(ls_path, REVIEW_DATE)
        assert trades == []

    def test_emitted_signal_keys_not_parsed_as_trades(self, tmp_path):
        """
        live_state.json may have emitted_signal_keys; they are not in the
        trades array and must never be parsed as trades.
        """
        state = _make_live_state(FIXTURE_TRADES)
        state["emitted_signal_keys"] = [
            "NIFTY_VP01_BUY_20260518",
            "BANKNIFTY_VP15_SELL_20260518",
            # ... 113 more fake keys that caused the 123-trade bug
        ] + [f"FAKE_KEY_{i}" for i in range(113)]
        ls_path = tmp_path / "live_state.json"
        ls_path.write_text(json.dumps(state))
        trades = parse_live_state(ls_path, REVIEW_DATE)
        assert len(trades) == 7, (
            f"emitted_signal_keys must not be counted as trades; "
            f"got {len(trades)} (expected 7 NIFTY+BANKNIFTY trades, FINNIFTY excluded)"
        )


# ---------------------------------------------------------------------------
# Test: parse_journal — rejected signals
# ---------------------------------------------------------------------------

JOURNAL_WITH_3_REJECTS = """# BlitzTrader Daily Journal — 18 May 2026

## Decision Log

### 09:35:29 — REJECT
**Market context:** FINNIFTY 15m | price ₹25070.00
**Instrument:** FINNIFTY26MAY26F
**Strategy applied:** VP-21 Extreme Candle Reversal
**Reasoning:** Rejected by Python: higher-timeframe EMA stack remains bearish.

### 11:03:01 — REJECT
**Market context:** BANKNIFTY 5m | price ₹53190.00
**Instrument:** BANKNIFTY26MAY26F
**Strategy applied:** VPA No Demand
**Reasoning:** Rejected by Python: latest candle volume 5460 is below avg_volume_20 14391.

### 14:32:35 — REJECT
**Market context:** FINNIFTY 3m | price ₹25372.20
**Instrument:** FINNIFTY26MAY26F
**Strategy applied:** ADX Gapper
**Reasoning:** Rejected by Python: higher-timeframe EMA stack remains bullish.

### 15:15:20 — EOD_REPORT
**Reasoning:** Total trades: 10
"""

JOURNAL_WITH_PLACEHOLDER_REJECT = """# BlitzTrader Daily Journal — 18 May 2026

## Decision Log

### 09:00:00 — REJECT
**Instrument:** —
**Strategy applied:** —
**Reasoning:** —

### 09:35:29 — REJECT
**Market context:** NIFTY 3m
**Instrument:** NIFTY26MAY26F
**Strategy applied:** VP-01 Counter Bull Trap
**Reasoning:** ADX too weak.
"""


class TestJournalRejectedSignals:

    def test_rejected_count_is_1(self, tmp_path):
        """Exactly 1 real rejected signal should be parsed.
        The fixture has 3 rejects but 2 are FINNIFTY — excluded since FINNIFTY
        was removed from the active futures universe. Only the BANKNIFTY reject survives."""
        p = tmp_path / "20260518.md"
        p.write_text(JOURNAL_WITH_3_REJECTS)
        result = parse_journal(p)
        assert len(result["rejected"]) == 1, (
            f"Expected 1 rejected signal (BANKNIFTY only), got {len(result['rejected'])}"
        )

    def test_placeholder_rows_ignored(self, tmp_path):
        """Rows with '—' symbol/strategy should be ignored."""
        p = tmp_path / "20260518.md"
        p.write_text(JOURNAL_WITH_PLACEHOLDER_REJECT)
        result = parse_journal(p)
        for r in result["rejected"]:
            assert "—" not in r.get("symbol", ""), f"Placeholder symbol in reject: {r}"
            assert "—" not in r.get("strategy", ""), f"Placeholder strategy in reject: {r}"
        # Only the real one should survive
        assert len(result["rejected"]) == 1

    def test_eod_report_not_in_rejected(self, tmp_path):
        """EOD_REPORT action must not appear in rejected list."""
        p = tmp_path / "20260518.md"
        p.write_text(JOURNAL_WITH_3_REJECTS)
        result = parse_journal(p)
        for r in result["rejected"]:
            assert r.get("action", "").upper() != "EOD_REPORT"


# ---------------------------------------------------------------------------
# Test: compute_trade_stats
# ---------------------------------------------------------------------------

class TestComputeTradeStats:

    def test_stats_from_fixture(self, tmp_path):
        """stats must show total=7, wins=1, losses=6, net_pnl=-18042.
        FINNIFTY trades are excluded from the active universe."""
        ls_path = tmp_path / "live_state.json"
        ls_path.write_text(json.dumps(_make_live_state(FIXTURE_TRADES)))
        trades = parse_live_state(ls_path, REVIEW_DATE)
        stats = compute_trade_stats(trades)
        assert stats["total"] == 7
        assert stats["wins"] == 1
        assert stats["losses"] == 6
        # Net P&L: -288-2700-1626-2394-16705+9168-3497 = -18042
        assert stats["net_pnl"] == pytest.approx(-18042.0, abs=1.0)
        assert stats["pnl_known"] is True

    def test_no_trades_returns_zero_stats(self):
        stats = compute_trade_stats([])
        assert stats["total"] == 0
        assert stats["net_pnl"] is None
        assert stats["pnl_known"] is False


# ---------------------------------------------------------------------------
# Test: detect_patterns — no "unknown reason"
# ---------------------------------------------------------------------------

class TestDetectPatterns:

    def test_no_unknown_reason_in_patterns(self):
        """'unknown reason' must never appear in detected patterns."""
        rejected = [
            {"symbol": "NIFTY26MAY26F", "reason": ""},
            {"symbol": "—", "reason": "some reason"},
        ]
        patterns = detect_patterns([], rejected)
        for p in patterns:
            assert "unknown reason" not in p.lower(), (
                f"'unknown reason' found in pattern: {p!r}"
            )

    def test_repeated_loss_pattern_detected(self, tmp_path):
        """Two losses on same symbol → detected as a pattern."""
        ls_path = tmp_path / "live_state.json"
        ls_path.write_text(json.dumps(_make_live_state(FIXTURE_TRADES)))
        trades = parse_live_state(ls_path, REVIEW_DATE)
        patterns = detect_patterns(trades, [])
        # BANKNIFTY has 4 trades with 3 losses
        banknifty_pattern = [p for p in patterns if "BANKNIFTY" in p]
        assert len(banknifty_pattern) >= 1, (
            f"Expected repeated-loss pattern for BANKNIFTY; patterns={patterns}"
        )


# ---------------------------------------------------------------------------
# Test: build_review_markdown — full integration
# ---------------------------------------------------------------------------

class TestBuildReviewMarkdown:

    def _get_trades_and_rejected(self, tmp_path):
        ls_path = tmp_path / "live_state.json"
        ls_path.write_text(json.dumps(_make_live_state(FIXTURE_TRADES)))
        executed = parse_live_state(ls_path, REVIEW_DATE)

        j_path = tmp_path / "20260518.md"
        j_path.write_text(JOURNAL_WITH_3_REJECTS)
        rejected = parse_journal(j_path)["rejected"]
        return executed, rejected

    def test_trade_count_in_summary(self, tmp_path):
        executed, rejected = self._get_trades_and_rejected(tmp_path)
        stats = compute_trade_stats(executed)
        patterns = detect_patterns(executed, rejected)
        md = build_review_markdown(REVIEW_DATE, executed, rejected, stats, patterns, [], [])
        assert "Futures trades executed: 7" in md, (
            "Review should show exactly 7 trades (NIFTY+BANKNIFTY only, FINNIFTY excluded)"
        )

    def test_no_placeholder_rows_in_table(self, tmp_path):
        executed, rejected = self._get_trades_and_rejected(tmp_path)
        stats = compute_trade_stats(executed)
        md = build_review_markdown(REVIEW_DATE, executed, rejected, stats, [], [], [])
        # A placeholder row would look like: | — | NIFTY | — | — | — |
        lines = md.splitlines()
        for line in lines:
            if line.startswith("|") and "—" in line:
                # It's acceptable to have "—" as a value only in pnl column
                # but not as the symbol (2nd column)
                cols = [c.strip() for c in line.split("|")]
                # cols[0]="" cols[1]=time cols[2]=symbol cols[3]=direction ...
                if len(cols) >= 3:
                    sym_col = cols[2]
                    assert sym_col != "—", (
                        f"Placeholder symbol '—' found in table row: {line!r}"
                    )

    def test_pnl_in_summary(self, tmp_path):
        executed, _ = self._get_trades_and_rejected(tmp_path)
        stats = compute_trade_stats(executed)
        md = build_review_markdown(REVIEW_DATE, executed, [], stats, [], [], [])
        assert "Net P&L:" in md
        assert "N/A (P&L data not available)" not in md, (
            "P&L should be shown when live_state.json provides it"
        )

    def test_wins_losses_in_summary(self, tmp_path):
        executed, _ = self._get_trades_and_rejected(tmp_path)
        stats = compute_trade_stats(executed)
        md = build_review_markdown(REVIEW_DATE, executed, [], stats, [], [], [])
        assert "Wins: 1" in md
        assert "Losses: 6" in md

    def test_rejected_count_in_summary(self, tmp_path):
        executed, rejected = self._get_trades_and_rejected(tmp_path)
        stats = compute_trade_stats(executed)
        md = build_review_markdown(REVIEW_DATE, executed, rejected, stats, [], [], [])
        assert "Rejected signals: 1" in md

    def test_no_unknown_reason_in_output(self, tmp_path):
        executed, rejected = self._get_trades_and_rejected(tmp_path)
        stats = compute_trade_stats(executed)
        patterns = detect_patterns(executed, rejected)
        md = build_review_markdown(REVIEW_DATE, executed, rejected, stats, patterns, [], [])
        assert "unknown reason" not in md.lower(), (
            "'unknown reason' must not appear in the generated review"
        )

    def test_review_is_compact_for_hypotheses(self, tmp_path):
        """
        The generated review should be a clean, compact markdown that
        propose_futures_hypotheses can parse without noise.
        No garbage rows, no emitted_signal_key lines, no bogus trade counts.
        """
        executed, rejected = self._get_trades_and_rejected(tmp_path)
        stats = compute_trade_stats(executed)
        patterns = detect_patterns(executed, rejected)
        md = build_review_markdown(REVIEW_DATE, executed, rejected, stats, patterns, [], [])

        # Must not contain strings that indicate polluted data
        assert "123" not in md, "Review must not show 123 trades"
        assert "emitted_signal" not in md.lower()
        # Must have the correct trade count
        assert "Futures trades executed: 7" in md
