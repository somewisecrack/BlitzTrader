"""
tests/test_options_spread.py — Full test suite for the options spread pipeline.

Covers all 23 required test cases:
  1.  round_to_strike rounds to nearest NIFTY step (50)
  2.  round_to_strike rounds to nearest BANKNIFTY step (100)
  3.  SpreadBuilder rejects unknown symbol
  4.  SpreadBuilder rejects unknown direction
  5.  SpreadBuilder selects BULL_CALL for bullish momentum signal (VP-05)
  6.  SpreadBuilder selects BEAR_PUT for bearish momentum signal (VP-07)
  7.  SpreadBuilder selects BULL_PUT (credit) for bullish mean-reversion (VP-14)
  8.  SpreadBuilder selects BEAR_CALL (credit) for bearish mean-reversion (VP-15)
  9.  SpreadBuilder returns None when quote validation fails (zero bid)
  10. SpreadBuilder returns None when max_loss exceeds budget
  11. SpreadBuilder returns None when expiry is None (no suitable expiry)
  12. SpreadCandidate.legs[0] is always BUY (long leg first invariant)
  13. SpreadCandidate.legs[1] is always SELL (short leg second invariant)
  14. Economics: BULL_CALL debit spread net_debit, max_profit, max_loss computed correctly
  15. Economics: BULL_PUT credit spread net_credit, max_profit, max_loss computed correctly
  16. SpreadExecutionEngine: guardrail blocks when trading is paused
  17. SpreadExecutionEngine: guardrail blocks after NO_ENTRY_AFTER time
  18. SpreadExecutionEngine: short leg placed only after long leg fill confirmed
  19. SpreadExecutionEngine: emergency long close triggered when short fails
  20. SpreadExecutionEngine: returns ok=True with OpenSpread on full success
  21. SpreadPortfolio: should_exit triggers on max-loss breach
  22. SpreadPortfolio: should_exit triggers on credit take-profit
  23. SpreadPortfolio: force=True triggers EOD close regardless of P&L
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import date
from unittest.mock import MagicMock, patch, call

from tools.options_chain import round_to_strike, OptionsChain, _is_quote_valid
from tools.options_spread_builder import (
    SpreadBuilder,
    SpreadCandidate,
    SpreadLeg,
    _select_spread_type,
    _build_strikes,
    _compute_economics,
)
from tools.options_spread_execution import SpreadExecutionEngine, OpenSpread
from tools.options_spread_portfolio import (
    SpreadPortfolio,
    should_exit,
    compute_spread_pnl,
)


# ──────────────────────────────────────────────────────────────────────────────
#   HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _make_quote(ltp=50.0, bid=49.0, ask=51.0) -> dict:
    return {"lp": str(ltp), "bp1": str(bid), "sp1": str(ask)}


def _make_token_info(symbol="NIFTY", strike=24500, otype="CE",
                     expiry=None, lot_size=75) -> dict:
    expiry = expiry or date(2026, 5, 28)
    tsym = f"{symbol}{expiry.strftime('%d%b%y').upper()}{strike}{otype}"
    return {
        "token": f"T{strike}",
        "tsym": tsym,
        "exchange": "NFO",
        "symbol": symbol,
        "strike": strike,
        "option_type": otype,
        "expiry": expiry,
        "expiry_str": expiry.strftime("%d-%b-%Y").upper(),
        "lot_size": lot_size,
    }


def _make_open_spread(
    spread_id="SPR-TEST",
    symbol="NIFTY",
    spread_type="BULL_CALL",
    direction="BULLISH",
    long_fill=50.0,
    short_fill=20.0,
    long_token="T1",
    short_token="T2",
    long_tsym="NIFTY28MAY2624500CE",
    short_tsym="NIFTY28MAY2624600CE",
    lot_size=75,
    max_loss=2250.0,
    max_profit=5250.0,
    net_dc=30.0,
) -> OpenSpread:
    """Build a minimal OpenSpread for testing."""
    return OpenSpread(
        spread_id=spread_id,
        symbol=symbol,
        spread_type=spread_type,
        direction=direction,
        expiry="28-MAY-2026",
        lot_size=lot_size,
        lots=1,
        long_tsym=long_tsym,
        long_token=long_token,
        long_action="BUY",
        long_strike=24500,
        long_option_type="CE",
        long_fill_price=long_fill,
        long_order_id="LONG-ORD-001",
        short_tsym=short_tsym,
        short_token=short_token,
        short_action="SELL",
        short_strike=24600,
        short_option_type="CE",
        short_fill_price=short_fill,
        short_order_id="SHORT-ORD-001",
        net_debit_or_credit=net_dc,
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven=24530.0,
        opened_at="2026-05-26T10:00:00+05:30",
        signal_id="TEST-SIG-001",
        strategy="VP-05",
        underlying_at_entry=24530.0,
    )


def _mock_options_chain(
    expiry=None,
    long_quote=None,
    short_quote=None,
    long_token_info=None,
    short_token_info=None,
) -> MagicMock:
    """Create a mock OptionsChain with preset return values."""
    expiry = expiry or date(2026, 5, 28)
    long_q = long_quote or _make_quote(50.0, 49.0, 51.0)
    short_q = short_quote or _make_quote(20.0, 19.5, 20.5)
    long_ti = long_token_info or _make_token_info(strike=24500, otype="CE")
    short_ti = short_token_info or _make_token_info(strike=24600, otype="CE")

    chain = MagicMock(spec=OptionsChain)
    chain.select_expiry.return_value = expiry
    chain.atm_strike.return_value = 24500
    chain.width_to_steps.return_value = 2   # 2 steps = 100 pts for NIFTY
    chain.resolve_option_token.side_effect = lambda sym, exp, strike, ot: (
        long_ti if strike == long_ti["strike"] else short_ti
    )
    chain.get_validated_quote.side_effect = lambda token, side, **kw: (
        (long_q, "") if token == long_ti["token"] else (short_q, "")
    )
    return chain


def _mock_state_manager(paused=False, stopped=False, open_spreads=None) -> MagicMock:
    sm = MagicMock()
    sm.get_state.return_value = {
        "is_paused": paused,
        "is_stopped": stopped,
        "open_spreads": open_spreads or [],
        "daily_pnl": 0,
    }
    sm.update_state = MagicMock()
    return sm


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 1 & 2: round_to_strike
# ──────────────────────────────────────────────────────────────────────────────

class TestRoundToStrike(unittest.TestCase):
    """Tests 1 & 2: Strike rounding for NIFTY and BANKNIFTY."""

    def test_nifty_rounds_to_50(self):
        """Test 1: NIFTY strike rounds to nearest 50."""
        self.assertEqual(round_to_strike(24512, "NIFTY"), 24500)
        self.assertEqual(round_to_strike(24526, "NIFTY"), 24550)
        self.assertEqual(round_to_strike(24550, "NIFTY"), 24550)
        self.assertEqual(round_to_strike(24501, "NIFTY"), 24500)

    def test_banknifty_rounds_to_100(self):
        """Test 2: BANKNIFTY strike rounds to nearest 100."""
        self.assertEqual(round_to_strike(56049, "BANKNIFTY"), 56000)
        self.assertEqual(round_to_strike(56051, "BANKNIFTY"), 56100)
        self.assertEqual(round_to_strike(56200, "BANKNIFTY"), 56200)


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 3 & 4: SpreadBuilder input validation
# ──────────────────────────────────────────────────────────────────────────────

class TestSpreadBuilderInputValidation(unittest.TestCase):
    """Tests 3 & 4: Symbol and direction validation."""

    def setUp(self):
        self.chain = _mock_options_chain()
        self.builder = SpreadBuilder(self.chain, max_risk_rupees=10000)

    def test_rejects_unknown_symbol(self):
        """Test 3: Builder returns None for non-NIFTY/BANKNIFTY symbol."""
        result = self.builder.build(
            {"symbol": "FINNIFTY", "direction": "BUY", "strategy": "VP-05"},
            underlying_price=21000,
        )
        self.assertIsNone(result)

    def test_rejects_unknown_direction(self):
        """Test 4: Builder returns None for unrecognised direction."""
        result = self.builder.build(
            {"symbol": "NIFTY", "direction": "HOLD", "strategy": "VP-05"},
            underlying_price=24500,
        )
        self.assertIsNone(result)


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 5–8: Spread type selection
# ──────────────────────────────────────────────────────────────────────────────

class TestSpreadTypeSelection(unittest.TestCase):
    """Tests 5–8: Spread type selected from direction × strategy."""

    def test_bullish_momentum_is_bull_call(self):
        """Test 5: BUY + VP-05 (momentum) → BULL_CALL debit spread."""
        self.assertEqual(_select_spread_type("BULLISH", "VP-05"), "BULL_CALL")

    def test_bearish_momentum_is_bear_put(self):
        """Test 6: SELL + VP-07 (momentum) → BEAR_PUT debit spread."""
        self.assertEqual(_select_spread_type("BEARISH", "VP-07"), "BEAR_PUT")

    def test_bullish_mean_reversion_is_bull_put(self):
        """Test 7: BUY + VP-14 (mean-reversion) → BULL_PUT credit spread."""
        self.assertEqual(_select_spread_type("BULLISH", "VP-14"), "BULL_PUT")

    def test_bearish_mean_reversion_is_bear_call(self):
        """Test 8: SELL + VP-15 (mean-reversion) → BEAR_CALL credit spread."""
        self.assertEqual(_select_spread_type("BEARISH", "VP-15"), "BEAR_CALL")


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 9: Bad quote rejects candidate
# ──────────────────────────────────────────────────────────────────────────────

class TestSpreadBuilderBadQuote(unittest.TestCase):
    """Test 9: Builder returns None when quote validation fails."""

    def test_zero_bid_on_long_leg_rejected(self):
        """Test 9: Zero bid/LTP on long leg → candidate rejected."""
        # Return invalid quote (bid=0, ltp=0) for long leg
        zero_quote = {"lp": "0", "bp1": "0", "sp1": "0.1"}
        chain = _mock_options_chain(long_quote=zero_quote)
        # Override get_validated_quote to return None for long leg
        chain.get_validated_quote.side_effect = lambda token, side, **kw: (
            (None, "LTP 0.00 and bid 0.00 below min_ltp 2.0")
            if token == "T24500"
            else (_make_quote(), "")
        )
        builder = SpreadBuilder(chain, max_risk_rupees=10000)
        result = builder.build(
            {"symbol": "NIFTY", "direction": "BUY", "strategy": "VP-05"},
            underlying_price=24500,
        )
        self.assertIsNone(result)


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 10: Budget guard
# ──────────────────────────────────────────────────────────────────────────────

class TestSpreadBuilderBudgetGuard(unittest.TestCase):
    """Test 10: Builder returns None when max_loss exceeds budget."""

    def test_max_loss_exceeds_budget(self):
        """Test 10: Spread whose max_loss > max_risk_rupees is rejected."""
        # long @ 200, short @ 10, lot_size=75, width=1*50=50
        # net_debit = 200-10=190, max_loss = 190*75=14250
        long_q = _make_quote(ltp=200, bid=199, ask=200)
        short_q = _make_quote(ltp=10, bid=9.5, ask=10.5)
        long_ti = _make_token_info(strike=24500, otype="CE", lot_size=75)
        short_ti = _make_token_info(strike=24550, otype="CE", lot_size=75)

        chain = MagicMock(spec=OptionsChain)
        chain.select_expiry.return_value = date(2026, 5, 28)
        chain.atm_strike.return_value = 24500
        chain.width_to_steps.return_value = 1
        chain.resolve_option_token.side_effect = lambda sym, exp, s, ot: (
            long_ti if s == 24500 else short_ti
        )
        chain.get_validated_quote.side_effect = lambda token, side, **kw: (
            (long_q, "") if token == long_ti["token"] else (short_q, "")
        )

        builder = SpreadBuilder(chain, max_risk_rupees=10000)
        result = builder.build(
            {"symbol": "NIFTY", "direction": "BUY", "strategy": "VP-05"},
            underlying_price=24500,
        )
        self.assertIsNone(result)


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 11: No expiry returns None
# ──────────────────────────────────────────────────────────────────────────────

class TestSpreadBuilderNoExpiry(unittest.TestCase):
    """Test 11: Builder returns None when select_expiry returns None."""

    def test_no_expiry_returns_none(self):
        """Test 11: No suitable expiry → candidate is None."""
        chain = _mock_options_chain()
        chain.select_expiry.return_value = None
        builder = SpreadBuilder(chain, max_risk_rupees=10000)
        result = builder.build(
            {"symbol": "NIFTY", "direction": "BUY", "strategy": "VP-05"},
            underlying_price=24500,
        )
        self.assertIsNone(result)


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 12 & 13: Leg order invariant
# ──────────────────────────────────────────────────────────────────────────────

class TestLegOrderInvariant(unittest.TestCase):
    """Tests 12 & 13: legs[0] is BUY, legs[1] is SELL."""

    def setUp(self):
        self.chain = _mock_options_chain()
        self.builder = SpreadBuilder(self.chain, max_risk_rupees=10000)

    def test_legs_0_is_buy(self):
        """Test 12: legs[0].action is always BUY (long leg placed first)."""
        result = self.builder.build(
            {"symbol": "NIFTY", "direction": "BUY", "strategy": "VP-05"},
            underlying_price=24500,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.legs[0].action, "BUY")

    def test_legs_1_is_sell(self):
        """Test 13: legs[1].action is always SELL (short leg placed second)."""
        result = self.builder.build(
            {"symbol": "NIFTY", "direction": "BUY", "strategy": "VP-05"},
            underlying_price=24500,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.legs[1].action, "SELL")


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 14 & 15: Economics
# ──────────────────────────────────────────────────────────────────────────────

class TestSpreadEconomics(unittest.TestCase):
    """Tests 14 & 15: P&L math for debit and credit spreads."""

    def test_bull_call_debit_economics(self):
        """
        Test 14: BULL_CALL debit spread economics.
        long=50, short=20, width=100pts, lot_size=75
        net_debit = 50-20 = 30
        max_profit = (100-30)*75 = 5250
        max_loss   = 30*75 = 2250
        breakeven  = 24500+30 = 24530
        """
        nd, mp, ml, be, rr = _compute_economics(
            "BULL_CALL",
            long_fill=50.0,
            short_fill=20.0,
            long_strike=24500,
            short_strike=24600,
            lot_size=75,
        )
        self.assertAlmostEqual(nd, 30.0)
        self.assertAlmostEqual(mp, 5250.0)
        self.assertAlmostEqual(ml, 2250.0)
        self.assertAlmostEqual(be, 24530.0)
        self.assertGreater(rr, 0)

    def test_bull_put_credit_economics(self):
        """
        Test 15: BULL_PUT credit spread economics.
        long PE (OTM, lower strike) fill=10, short PE (ATM, higher strike) fill=30
        net_credit = 30-10 = 20
        max_profit = 20*75 = 1500
        max_loss   = (100-20)*75 = 6000
        breakeven  = short_strike - net_credit = 24500-20 = 24480
        """
        nd, mp, ml, be, rr = _compute_economics(
            "BULL_PUT",
            long_fill=10.0,
            short_fill=30.0,
            long_strike=24400,    # OTM PE (lower)
            short_strike=24500,   # ATM PE (higher) — the one we sold
            lot_size=75,
        )
        self.assertAlmostEqual(nd, -20.0)  # net credit = inflow, stored negative
        self.assertAlmostEqual(mp, 1500.0)
        self.assertAlmostEqual(ml, 6000.0)
        self.assertAlmostEqual(be, 24480.0)


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 16 & 17: Execution guardrails
# ──────────────────────────────────────────────────────────────────────────────

class TestExecutionGuardrails(unittest.TestCase):
    """Tests 16 & 17: Trading paused and after no-entry-time guardrails."""

    def _make_candidate(self) -> SpreadCandidate:
        expiry = date(2026, 5, 28)
        long_leg = SpreadLeg("BUY", "CE", 24500, expiry, "T1", "NIFTY28MAY2624500CE", "NFO", 75, {}, 51.0)
        short_leg = SpreadLeg("SELL", "CE", 24600, expiry, "T2", "NIFTY28MAY2624600CE", "NFO", 75, {}, 20.0)
        return SpreadCandidate(
            symbol="NIFTY",
            spread_type="BULL_CALL",
            direction="BULLISH",
            expiry=expiry,
            expiry_str="28-MAY-2026",
            legs=[long_leg, short_leg],
            lot_size=75,
            lots=1,
            net_debit_or_credit=31.0,
            max_profit=5175.0,
            max_loss=2325.0,
            breakeven=24531.0,
            risk_reward=2.2,
        )

    def test_paused_blocks_entry(self):
        """Test 16: Engine returns error when trading is paused."""
        sm = _mock_state_manager(paused=True)
        engine = SpreadExecutionEngine(None, sm)
        result = engine.place_spread(self._make_candidate())
        self.assertFalse(result["ok"])
        self.assertIn("paused", result["error"].lower())

    def test_after_time_blocks_entry(self):
        """Test 17: Engine returns error after no-entry time."""
        from unittest.mock import patch
        from datetime import datetime as _real_dt
        import pytz as _pytz
        _IST = _pytz.timezone("Asia/Kolkata")
        # Simulate 15:30 IST — well past no_entry_after="09:00"
        _late = _real_dt(2026, 5, 26, 15, 30, 0, tzinfo=_IST)

        sm = _mock_state_manager()
        engine = SpreadExecutionEngine(None, sm, no_entry_after="09:00")
        with patch("tools.options_spread_execution.datetime") as mock_dt:
            mock_dt.now.return_value = _late
            result = engine.place_spread(self._make_candidate())
        self.assertFalse(result["ok"])
        self.assertIn("09:00", result["error"])


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 18: Short placed after long fill
# ──────────────────────────────────────────────────────────────────────────────

class TestShortAfterLongFill(unittest.TestCase):
    """Test 18: Short leg placed only after long leg fill confirmed."""

    def _make_candidate(self) -> SpreadCandidate:
        expiry = date(2026, 5, 28)
        long_leg = SpreadLeg("BUY", "CE", 24500, expiry, "T1", "NIFTY28MAY2624500CE", "NFO", 75, {}, 51.0)
        short_leg = SpreadLeg("SELL", "CE", 24600, expiry, "T2", "NIFTY28MAY2624600CE", "NFO", 75, {}, 20.0)
        return SpreadCandidate(
            symbol="NIFTY",
            spread_type="BULL_CALL",
            direction="BULLISH",
            expiry=expiry,
            expiry_str="28-MAY-2026",
            legs=[long_leg, short_leg],
            lot_size=75,
            lots=1,
            net_debit_or_credit=31.0,
            max_profit=5175.0,
            max_loss=2325.0,
            breakeven=24531.0,
            risk_reward=2.2,
        )

    def test_short_placed_after_long(self):
        """Test 18: place_order calls happen in long-first, short-second order."""
        sm = _mock_state_manager()

        order_ids = iter(["LONG-ORD-001", "SHORT-ORD-002"])
        mock_client = MagicMock()
        mock_client.place_order.side_effect = lambda **kw: {
            "stat": "Ok",
            "norenordno": next(order_ids),
        }

        fill_results = {
            "LONG-ORD-001": [{"norenordno": "LONG-ORD-001", "status": "COMPLETE", "avgprc": "51.0"}],
            "SHORT-ORD-002": [{"norenordno": "SHORT-ORD-002", "status": "COMPLETE", "avgprc": "20.0"}],
        }
        call_counter = {"n": 0}

        def fake_order_book():
            n = call_counter["n"]
            call_counter["n"] += 1
            # Alternate returning long fill, then short fill
            if n < 2:
                return fill_results["LONG-ORD-001"]
            return fill_results["SHORT-ORD-002"]

        mock_client.get_order_book.side_effect = fake_order_book

        engine = SpreadExecutionEngine(mock_client, sm, no_entry_after="15:05")

        from datetime import datetime as _real_dt
        import pytz as _pytz
        _IST = _pytz.timezone("Asia/Kolkata")
        _market_open = _real_dt(2026, 5, 26, 10, 30, 0, tzinfo=_IST)

        with patch("tools.options_spread_execution.datetime") as mock_dt, patch("time.sleep"):
            mock_dt.now.return_value = _market_open
            result = engine.place_spread(self._make_candidate())

        self.assertTrue(result["ok"], f"Expected ok=True, got: {result}")

        # Verify call order: first call must be BUY, second must be SELL
        calls = mock_client.place_order.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["buy_or_sell"], "BUY")
        self.assertEqual(calls[1].kwargs["buy_or_sell"], "SELL")


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 19: Emergency close on short failure
# ──────────────────────────────────────────────────────────────────────────────

class TestEmergencyClose(unittest.TestCase):
    """Test 19: Emergency long close triggered when short leg fails."""

    def _make_candidate(self) -> SpreadCandidate:
        expiry = date(2026, 5, 28)
        long_leg = SpreadLeg("BUY", "CE", 24500, expiry, "T1", "NIFTY28MAY2624500CE", "NFO", 75, {}, 51.0)
        short_leg = SpreadLeg("SELL", "CE", 24600, expiry, "T2", "NIFTY28MAY2624600CE", "NFO", 75, {}, 20.0)
        return SpreadCandidate(
            symbol="NIFTY", spread_type="BULL_CALL", direction="BULLISH",
            expiry=expiry, expiry_str="28-MAY-2026",
            legs=[long_leg, short_leg], lot_size=75, lots=1,
            net_debit_or_credit=31.0, max_profit=5175.0, max_loss=2325.0,
            breakeven=24531.0, risk_reward=2.2,
        )

    def test_emergency_close_on_short_failure(self):
        """Test 19: If short leg placement fails, long leg is immediately closed."""
        sm = _mock_state_manager()
        mock_client = MagicMock()

        long_order_id = "LONG-ORD-001"
        # Long order succeeds
        mock_client.place_order.side_effect = [
            {"stat": "Ok", "norenordno": long_order_id},   # long leg placed
            {"stat": "Not_Ok", "emsg": "order rejected"},   # short leg fails
            {"stat": "Ok", "norenordno": "EMRG-001"},       # emergency MKT sell
        ]
        # Long leg fills
        mock_client.get_order_book.return_value = [
            {"norenordno": long_order_id, "status": "COMPLETE", "avgprc": "51.0"}
        ]

        engine = SpreadExecutionEngine(mock_client, sm, no_entry_after="15:05")

        from datetime import datetime as _real_dt
        import pytz as _pytz
        _IST = _pytz.timezone("Asia/Kolkata")
        _market_open = _real_dt(2026, 5, 26, 10, 30, 0, tzinfo=_IST)

        with patch("tools.options_spread_execution.datetime") as mock_dt, patch("time.sleep"):
            mock_dt.now.return_value = _market_open
            result = engine.place_spread(self._make_candidate())

        self.assertFalse(result["ok"])
        self.assertTrue(result.get("emergency"))

        # Verify emergency market sell was placed for long leg
        all_calls = mock_client.place_order.call_args_list
        emergency_call = all_calls[-1]
        self.assertEqual(emergency_call.kwargs["buy_or_sell"], "SELL")
        self.assertEqual(emergency_call.kwargs["price_type"], "MKT")
        self.assertEqual(emergency_call.kwargs["tradingsymbol"], "NIFTY28MAY2624500CE")


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 20: Full success returns OpenSpread
# ──────────────────────────────────────────────────────────────────────────────

class TestFullSuccessReturnsOpenSpread(unittest.TestCase):
    """Test 20: Successful execution returns ok=True with OpenSpread."""

    def _make_candidate(self) -> SpreadCandidate:
        expiry = date(2026, 5, 28)
        long_leg = SpreadLeg("BUY", "CE", 24500, expiry, "T1", "NIFTY28MAY2624500CE", "NFO", 75, {}, 51.0)
        short_leg = SpreadLeg("SELL", "CE", 24600, expiry, "T2", "NIFTY28MAY2624600CE", "NFO", 75, {}, 20.0)
        return SpreadCandidate(
            symbol="NIFTY", spread_type="BULL_CALL", direction="BULLISH",
            expiry=expiry, expiry_str="28-MAY-2026",
            legs=[long_leg, short_leg], lot_size=75, lots=1,
            net_debit_or_credit=31.0, max_profit=5175.0, max_loss=2325.0,
            breakeven=24531.0, risk_reward=2.2,
        )

    def test_full_success(self):
        """Test 20: ok=True, spread is OpenSpread, spread_id set."""
        sm = _mock_state_manager()
        mock_client = MagicMock()
        mock_client.place_order.side_effect = [
            {"stat": "Ok", "norenordno": "ORD-LONG-001"},
            {"stat": "Ok", "norenordno": "ORD-SHORT-002"},
        ]
        mock_client.get_order_book.side_effect = [
            [{"norenordno": "ORD-LONG-001", "status": "COMPLETE", "avgprc": "51.5"}],
            [{"norenordno": "ORD-SHORT-002", "status": "COMPLETE", "avgprc": "19.5"}],
        ]
        engine = SpreadExecutionEngine(mock_client, sm, no_entry_after="15:05")

        from datetime import datetime as _real_dt
        import pytz as _pytz
        _IST = _pytz.timezone("Asia/Kolkata")
        _market_open = _real_dt(2026, 5, 26, 10, 30, 0, tzinfo=_IST)

        with patch("tools.options_spread_execution.datetime") as mock_dt, patch("time.sleep"):
            mock_dt.now.return_value = _market_open
            result = engine.place_spread(self._make_candidate())

        self.assertTrue(result["ok"])
        spread = result["spread"]
        self.assertIsInstance(spread, OpenSpread)
        self.assertTrue(spread.spread_id.startswith("SPR-"))
        self.assertEqual(spread.symbol, "NIFTY")
        self.assertEqual(spread.long_fill_price, 51.5)
        self.assertEqual(spread.short_fill_price, 19.5)


# ──────────────────────────────────────────────────────────────────────────────
#   TEST 21–23: Portfolio exit conditions
# ──────────────────────────────────────────────────────────────────────────────

class TestPortfolioExitConditions(unittest.TestCase):
    """Tests 21–23: should_exit logic for P&L thresholds and EOD force."""

    def _pnl_data(self, pnl: float) -> dict:
        return {"long_ltp": 50.0, "short_ltp": 20.0, "unrealized_pnl": pnl, "data_ok": True}

    def test_max_loss_breach_triggers_exit(self):
        """Test 21: Loss >= 60% of max_loss triggers exit."""
        spread = _make_open_spread(max_loss=2250.0, max_profit=5250.0, net_dc=30.0)
        # Loss of 1350 = 60% of 2250 — threshold met
        pnl_data = self._pnl_data(-1350.0)
        do_exit, reason = should_exit(spread, pnl_data, max_loss_exit_fraction=0.60)
        self.assertTrue(do_exit)
        self.assertIn("max-loss", reason)

    def test_credit_tp_triggers_exit(self):
        """Test 22: Profit >= 60% of max_profit triggers take-profit exit (credit spread)."""
        # Credit spread: net_dc < 0
        spread = _make_open_spread(
            spread_type="BULL_PUT",
            max_profit=1500.0, max_loss=6000.0, net_dc=-20.0
        )
        pnl_data = self._pnl_data(900.0)   # 60% of 1500
        do_exit, reason = should_exit(spread, pnl_data, credit_tp_fraction=0.60)
        self.assertTrue(do_exit)
        self.assertIn("take-profit", reason)

    def test_force_eod_exits_regardless_of_pnl(self):
        """Test 23: force=True triggers exit even when P&L is flat."""
        spread = _make_open_spread()
        pnl_data = self._pnl_data(0.0)
        do_exit, reason = should_exit(spread, pnl_data, force=True)
        self.assertTrue(do_exit)
        self.assertIn("EOD", reason)

    def test_no_exit_when_pnl_data_missing(self):
        """Extra: should_exit returns False when data_ok=False."""
        spread = _make_open_spread()
        pnl_data = {"data_ok": False, "unrealized_pnl": None}
        do_exit, _ = should_exit(spread, pnl_data)
        self.assertFalse(do_exit)

    def test_no_exit_under_thresholds(self):
        """Extra: No exit when loss and profit are both under thresholds."""
        spread = _make_open_spread(max_loss=2250.0, max_profit=5250.0, net_dc=30.0)
        pnl_data = self._pnl_data(100.0)  # small profit, well under TP
        do_exit, _ = should_exit(spread, pnl_data)
        self.assertFalse(do_exit)


# ──────────────────────────────────────────────────────────────────────────────
#   EXTRAS: _is_quote_valid, fill_price_estimate, OptionsChain helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestQuoteValidation(unittest.TestCase):
    """Validate _is_quote_valid covers all branches."""

    def test_valid_sell_quote(self):
        ok, reason = _is_quote_valid(_make_quote(50, 49, 51), "SELL", min_sell_bid=5.0)
        self.assertTrue(ok)

    def test_missing_quote(self):
        ok, reason = _is_quote_valid({}, "SELL")
        self.assertFalse(ok)

    def test_zero_bid_sell_rejected(self):
        ok, reason = _is_quote_valid({"lp": "10", "bp1": "0", "sp1": "12"}, "SELL", min_sell_bid=5.0)
        self.assertFalse(ok)
        self.assertIn("bid", reason)

    def test_wide_spread_rejected(self):
        # bid=10, ask=20 → spread = 10/20 = 50% > 30%
        ok, reason = _is_quote_valid({"lp": "15", "bp1": "10", "sp1": "20"}, "BUY",
                                      max_spread_fraction=0.30)
        self.assertFalse(ok)
        self.assertIn("wide", reason)

    def test_below_min_ltp(self):
        ok, reason = _is_quote_valid({"lp": "1", "bp1": "0.5", "sp1": "1.5"}, "BUY",
                                      min_ltp=2.0)
        self.assertFalse(ok)


class TestBuildStrikes(unittest.TestCase):
    """Verify _build_strikes returns correct (long, short) pairs."""

    def test_bull_call(self):
        long_s, short_s = _build_strikes("BULL_CALL", 24500, "NIFTY", 2)
        self.assertEqual(long_s, 24500)
        self.assertEqual(short_s, 24600)

    def test_bear_put(self):
        long_s, short_s = _build_strikes("BEAR_PUT", 24500, "NIFTY", 2)
        self.assertEqual(long_s, 24500)
        self.assertEqual(short_s, 24400)

    def test_bull_put(self):
        long_s, short_s = _build_strikes("BULL_PUT", 24500, "NIFTY", 2)
        self.assertEqual(long_s, 24400)   # OTM PE (lower)
        self.assertEqual(short_s, 24500)  # ATM PE (higher) — the sold leg

    def test_bear_call(self):
        long_s, short_s = _build_strikes("BEAR_CALL", 24500, "NIFTY", 2)
        self.assertEqual(long_s, 24600)   # OTM CE (higher, protective)
        self.assertEqual(short_s, 24500)  # ATM CE (lower) — the sold leg


if __name__ == "__main__":
    unittest.main()
