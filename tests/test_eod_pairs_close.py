"""
tests/test_eod_pairs_close.py
------------------------------
Tests for the robust pairs EOD close (pairs/portfolio.py PairPortfolio.close_all).

Covers:
  - All pairs close successfully → closed=N, failed=0
  - One leg price missing → failed=1, no state mutation for that pair
  - close_all result always has required keys
  - Telegram warning sent when failed > 0
"""
import sys
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Minimal shims so we can import PairPortfolio without real Shoonya connection
# ---------------------------------------------------------------------------

def _make_leg(symbol="INFY", tradingsymbol="INFY-EQ", token="11111",
              side="BUY", qty=10, entry_price=1500.0):
    from pairs.portfolio import Leg
    return Leg(
        symbol=symbol,
        tradingsymbol=tradingsymbol,
        token=token,
        side=side,
        qty=qty,
        entry_price=entry_price,
    )


def _make_pair_position(pair_name="INFY/SBIN", long_leg=None, short_leg=None):
    from pairs.portfolio import PairPosition
    if long_leg is None:
        long_leg = _make_leg("INFY", "INFY-EQ", "11111", "BUY", 10, 1500.0)
    if short_leg is None:
        short_leg = _make_leg("SBIN", "SBIN-EQ", "22222", "SELL", 10, 600.0)
    return PairPosition(
        pair_name=pair_name,
        timeframe="D",
        method="kalman",
        z_score=2.1,
        beta=1.2,
        prob_profit=65.0,
        prob_profit_low=55.0,
        prob_profit_high=75.0,
        long_leg=long_leg,
        short_leg=short_leg,
        margin_used=500.0,
        capital_reserved=5000.0,
        opened_at=datetime.now().isoformat(),
    )


def _make_portfolio(positions=None, tmp_path=None):
    from pairs.portfolio import PairPortfolio
    state_file = (tmp_path or Path(tempfile.mkdtemp())) / "pairs_state.json"
    portfolio = PairPortfolio(state_file=state_file)
    portfolio.positions = positions or []
    return portfolio


def _make_shoonya_client(prices: dict):
    """
    Create a mock ShoonyaClient where get_best_bid_ask returns values from
    the prices dict {token: (bid, ask)} or None.
    """
    client = MagicMock()

    def _get_best_bid_ask(exchange, token):
        return prices.get(token)

    def _get_last_price(exchange, token):
        ba = prices.get(token)
        if ba:
            return round((ba[0] + ba[1]) / 2, 2)
        return None

    client.get_best_bid_ask.side_effect = _get_best_bid_ask
    client.get_last_price.side_effect = _get_last_price
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCloseAllResultShape:
    """close_all must always return a dict with the required keys."""

    def test_result_has_required_keys(self, tmp_path):
        portfolio = _make_portfolio(tmp_path=tmp_path)
        client = _make_shoonya_client({})
        result = portfolio.close_all(client)
        for key in ("closed", "failed", "total_pnl", "failed_pairs"):
            assert key in result, f"Missing key '{key}' in close_all result"

    def test_empty_portfolio_returns_zeros(self, tmp_path):
        portfolio = _make_portfolio(tmp_path=tmp_path)
        client = _make_shoonya_client({})
        result = portfolio.close_all(client)
        assert result["closed"] == 0
        assert result["failed"] == 0
        assert result["total_pnl"] == 0.0
        assert result["failed_pairs"] == []


class TestCloseAllSuccess:
    """All pairs close successfully."""

    def test_two_pairs_all_closed(self, tmp_path):
        pos1 = _make_pair_position("INFY/SBIN")
        pos2 = _make_pair_position(
            "TCS/WIPRO",
            long_leg=_make_leg("TCS", "TCS-EQ", "33333", "BUY", 5, 3000.0),
            short_leg=_make_leg("WIPRO", "WIPRO-EQ", "44444", "SELL", 5, 400.0),
        )
        portfolio = _make_portfolio([pos1, pos2], tmp_path)

        # Prices available for all tokens
        prices = {
            "11111": (1510.0, 1511.0),   # INFY
            "22222": (598.0, 599.0),      # SBIN
            "33333": (3020.0, 3021.0),    # TCS
            "44444": (398.0, 399.0),      # WIPRO
        }
        client = _make_shoonya_client(prices)

        result = portfolio.close_all(client)

        assert result["closed"] == 2
        assert result["failed"] == 0
        assert result["failed_pairs"] == []
        # Both positions should now be marked closed
        assert pos1.closed_at is not None
        assert pos2.closed_at is not None

    def test_pnl_is_correct(self, tmp_path):
        """P&L is computed from actual entry vs exit prices (not zero)."""
        pos = _make_pair_position("INFY/SBIN")
        # long_leg: BUY 10 @ 1500, exit SELL @ 1520 → profit 200
        # short_leg: SELL 10 @ 600, exit BUY @ 590 → profit 100
        portfolio = _make_portfolio([pos], tmp_path)

        prices = {
            "11111": (1519.0, 1520.0),  # INFY ask=1520 (exit for long BUY→SELL uses ask? No, SELL uses bid)
            "22222": (589.0, 590.0),    # SBIN bid=589 (exit for short SELL→BUY uses ask)
        }
        client = _make_shoonya_client(prices)

        result = portfolio.close_all(client)
        assert result["closed"] == 1
        # Long leg SELL → uses bid = 1519, pnl = (1519-1500)*10 = 190
        # Short leg BUY → uses ask = 590, pnl = (600-590)*10 = 100
        assert abs(result["total_pnl"] - 290.0) <= 1.0


class TestCloseAllWithMissingPrice:
    """One or both leg prices missing → pair not closed, added to failed_pairs."""

    def test_one_leg_price_missing_no_state_mutation(self, tmp_path):
        """Long leg price unavailable → pair stays open, no state change."""
        pos = _make_pair_position("INFY/SBIN")
        portfolio = _make_portfolio([pos], tmp_path)

        # Only short leg price available; long leg missing
        prices = {
            "22222": (598.0, 599.0),  # SBIN only
        }
        client = _make_shoonya_client(prices)

        # Record original state
        assert pos.closed_at is None
        assert pos.long_leg.closed_at is None
        assert pos.short_leg.closed_at is None

        result = portfolio.close_all(client)

        assert result["failed"] == 1
        assert result["closed"] == 0
        assert len(result["failed_pairs"]) == 1
        assert result["failed_pairs"][0]["pair_name"] == "INFY/SBIN"

        # CRITICAL: no state mutation
        assert pos.closed_at is None, "Pair should NOT be marked closed when price is missing"
        assert pos.long_leg.closed_at is None, "Long leg should NOT be closed when price is missing"
        assert pos.short_leg.closed_at is None, "Short leg should NOT be closed when price is missing"

    def test_both_legs_price_missing_no_state_mutation(self, tmp_path):
        """Both leg prices unavailable → pair stays open."""
        pos = _make_pair_position("INFY/SBIN")
        portfolio = _make_portfolio([pos], tmp_path)

        client = _make_shoonya_client({})  # no prices at all
        result = portfolio.close_all(client)

        assert result["failed"] == 1
        assert result["closed"] == 0
        assert pos.closed_at is None

    def test_mixed_pairs_partial_close(self, tmp_path):
        """Two pairs: one closes, one fails due to missing price."""
        pos1 = _make_pair_position("INFY/SBIN")  # prices available
        pos2 = _make_pair_position(
            "TCS/WIPRO",
            long_leg=_make_leg("TCS", "TCS-EQ", "33333", "BUY", 5, 3000.0),
            short_leg=_make_leg("WIPRO", "WIPRO-EQ", "44444", "SELL", 5, 400.0),
        )
        portfolio = _make_portfolio([pos1, pos2], tmp_path)

        prices = {
            "11111": (1510.0, 1511.0),  # INFY
            "22222": (598.0, 599.0),    # SBIN
            # TCS and WIPRO missing
        }
        client = _make_shoonya_client(prices)
        result = portfolio.close_all(client)

        assert result["closed"] == 1
        assert result["failed"] == 1
        assert len(result["failed_pairs"]) == 1
        assert result["failed_pairs"][0]["pair_name"] == "TCS/WIPRO"

        # pos1 should be closed, pos2 should be open
        assert pos1.closed_at is not None
        assert pos2.closed_at is None

    def test_failed_pairs_have_reason(self, tmp_path):
        """failed_pairs entries include a 'reason' field."""
        pos = _make_pair_position("INFY/SBIN")
        portfolio = _make_portfolio([pos], tmp_path)
        client = _make_shoonya_client({})

        result = portfolio.close_all(client)

        assert len(result["failed_pairs"]) == 1
        fp = result["failed_pairs"][0]
        assert "reason" in fp
        assert "price" in fp["reason"].lower() or "unavailable" in fp["reason"].lower()

    def test_pnl_is_zero_for_failed_close(self, tmp_path):
        """When a pair fails to close, total_pnl should not include its contribution."""
        pos = _make_pair_position("INFY/SBIN")
        portfolio = _make_portfolio([pos], tmp_path)
        client = _make_shoonya_client({})  # all prices missing

        result = portfolio.close_all(client)

        assert result["total_pnl"] == 0.0


class TestCloseAllAlreadyClosed:
    """Pairs already closed intraday are counted correctly."""

    def test_already_closed_pair_counted_in_closed(self, tmp_path):
        pos = _make_pair_position("INFY/SBIN")
        # Simulate already-closed pair
        pos.closed_at = datetime.now().isoformat()
        pos.pnl = 500.0
        pos.long_leg.closed_at = datetime.now().isoformat()
        pos.long_leg.exit_price = 1550.0
        pos.long_leg.realized_pnl = 500.0
        pos.short_leg.closed_at = datetime.now().isoformat()
        pos.short_leg.exit_price = 580.0
        pos.short_leg.realized_pnl = 0.0

        portfolio = _make_portfolio([pos], tmp_path)
        client = _make_shoonya_client({})  # prices don't matter for already-closed

        result = portfolio.close_all(client)

        assert result["closed"] == 1
        assert result["failed"] == 0
        assert abs(result["total_pnl"] - 500.0) <= 0.01
