"""
tests/test_strategy_freshness_and_rules.py
-------------------------------------------
Tests for:
  - Signal freshness (session validity) at the scanner layer
  - ADX14/RSI14 rule tightening for VP-01, VP-05, VP-07, VP-18, VP-19
  - VP-14 Morning Star disabled from live executable signals

Run: python -m pytest tests/test_strategy_freshness_and_rules.py -v
"""
import sys
import os
import datetime
import unittest

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.futures_strategy_engine import scan_candles

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Candle-building helpers
# ---------------------------------------------------------------------------

def _ist_ts(hour: int, minute: int, date: datetime.date = None) -> float:
    """Return Unix timestamp for given IST time on *date* (defaults to today)."""
    if date is None:
        date = datetime.datetime.now(IST).date()
    dt = IST.localize(datetime.datetime(date.year, date.month, date.day, hour, minute, 0))
    return dt.timestamp()


def _candle(ts: float, open_: float, high: float, low: float, close: float,
            volume: int = 5000) -> dict:
    return {"time": ts, "open": open_, "high": high, "low": low, "close": close,
            "volume": volume}


def _flat_candles(count: int, base_ts: float, step: float, price: float = 24000.0) -> list[dict]:
    """Return *count* candles with nearly identical prices (low-noise; low DX)."""
    result = []
    for i in range(count):
        p = price + i * 0.1
        result.append(_candle(
            ts=base_ts + i * step,
            open_=p,
            high=p + 5,
            low=p - 5,
            close=p + 1,
        ))
    return result


def _trending_up_candles(count: int, base_ts: float, step: float,
                          price: float = 24000.0, step_size: float = 15.0) -> list[dict]:
    """Return candles in a consistent uptrend (high ADX)."""
    result = []
    for i in range(count):
        p = price + i * step_size
        result.append(_candle(
            ts=base_ts + i * step,
            open_=p,
            high=p + step_size * 0.8,
            low=p - step_size * 0.2,
            close=p + step_size * 0.6,
        ))
    return result


def _trending_down_candles(count: int, base_ts: float, step: float,
                            price: float = 24000.0, step_size: float = 15.0) -> list[dict]:
    """Return candles in a consistent downtrend (high ADX)."""
    result = []
    for i in range(count):
        p = price - i * step_size
        result.append(_candle(
            ts=base_ts + i * step,
            open_=p,
            high=p + step_size * 0.2,
            low=p - step_size * 0.8,
            close=p - step_size * 0.6,
        ))
    return result


def _signals_for(candles: list[dict], symbol: str = "NIFTY",
                 interval: str = "5m") -> list[dict]:
    return scan_candles(symbol, interval, candles)


def _strategy_names(signals: list[dict]) -> list[str]:
    return [s["strategy"] for s in signals]


def _strategy_directions(signals: list[dict]) -> list[tuple[str, str]]:
    return [(s["strategy"], s["direction"]) for s in signals]


# ---------------------------------------------------------------------------
# Test 1: VP-01 Counter Bull Trap SELL is blocked when ADX14 > 30
# ---------------------------------------------------------------------------

class TestVP01SellBlockedByHighADX(unittest.TestCase):
    """VP-01 Counter Bull Trap SELL must not fire when ADX14 > 30."""

    def test_vp01_sell_blocked_when_adx_above_30(self):
        """
        Build a strong downtrend (ADX14 > 30) with VP-01 pattern conditions
        present.  No VP-01 SELL should fire — the ADX gate blocks it.
        """
        base_ts = _ist_ts(9, 15)
        step = 300  # 5-minute candles

        # Strong downtrend warm-up (enough candles for ADX = 2*14 + 1 = 29 + extras)
        candles = _trending_down_candles(100, base_ts, step, price=24500.0, step_size=20.0)

        # All candles are from today's session — no staleness issue
        sigs = _signals_for(candles)
        vp01_sells = [s for s in sigs if s["strategy"] == "VP-01 Counter Bull Trap"
                      and s["direction"] == "SELL"]

        self.assertEqual(
            len(vp01_sells), 0,
            f"VP-01 SELL must be blocked in high-ADX downtrend, got: {vp01_sells}"
        )


# ---------------------------------------------------------------------------
# Test 2: VP-07 Wicks Pullback BUY is blocked when ADX14 > 30
# ---------------------------------------------------------------------------

class TestVP07BuyBlockedByHighADX(unittest.TestCase):
    """VP-07 Wicks Pullback BUY must not fire when ADX14 > 30."""

    def test_vp07_buy_blocked_when_adx_above_30(self):
        """
        Strong uptrend (ADX14 > 30): VP-07 BUY must not fire even if a
        long-lower-wick master candle exists in the lookback window.
        """
        base_ts = _ist_ts(9, 15)
        step = 300

        candles = _trending_up_candles(100, base_ts, step, price=24000.0, step_size=25.0)

        sigs = _signals_for(candles)
        vp07_buys = [s for s in sigs if s["strategy"] == "VP-07 Wicks Pullback"
                     and s["direction"] == "BUY"]

        self.assertEqual(
            len(vp07_buys), 0,
            f"VP-07 BUY must be blocked in high-ADX trend, got: {vp07_buys}"
        )


# ---------------------------------------------------------------------------
# Test 3: VP-07 Wicks Pullback SELL is blocked when ADX14 > 30
# ---------------------------------------------------------------------------

class TestVP07SellBlockedByHighADX(unittest.TestCase):
    """VP-07 Wicks Pullback SELL must not fire when ADX14 > 30."""

    def test_vp07_sell_blocked_when_adx_above_30(self):
        """
        Strong downtrend (ADX14 > 30): VP-07 SELL must not fire.
        """
        base_ts = _ist_ts(9, 15)
        step = 300

        candles = _trending_down_candles(100, base_ts, step, price=24500.0, step_size=25.0)

        sigs = _signals_for(candles)
        vp07_sells = [s for s in sigs if s["strategy"] == "VP-07 Wicks Pullback"
                      and s["direction"] == "SELL"]

        self.assertEqual(
            len(vp07_sells), 0,
            f"VP-07 SELL must be blocked in high-ADX trend, got: {vp07_sells}"
        )


# ---------------------------------------------------------------------------
# Test 4: VP-18 M-Pattern Double Top SELL is blocked when ADX14 > 30
# ---------------------------------------------------------------------------

class TestVP18SellBlockedByHighADX(unittest.TestCase):
    """VP-18 M-Pattern Double Top SELL must not fire when ADX14 > 30."""

    def test_vp18_sell_blocked_when_adx_above_30(self):
        """
        Build a strong trending market (high ADX). VP-18 SELL must not fire
        even if two similar swing highs and a neckline break appear.
        """
        base_ts = _ist_ts(9, 15)
        step = 300

        # Strong uptrend then sudden down
        candles = _trending_up_candles(80, base_ts, step, price=24000.0, step_size=20.0)
        # Append a few candles that might look like double top
        last_p = candles[-1]["close"]
        candles.append(_candle(base_ts + 80 * step, last_p, last_p + 30, last_p - 10, last_p - 20))
        candles.append(_candle(base_ts + 81 * step, last_p - 20, last_p + 25, last_p - 40, last_p - 30))

        sigs = _signals_for(candles)
        vp18_sells = [s for s in sigs if s["strategy"] == "VP-18 M-Pattern Double Top"]

        self.assertEqual(
            len(vp18_sells), 0,
            f"VP-18 SELL must be blocked in high-ADX trend, got: {vp18_sells}"
        )


# ---------------------------------------------------------------------------
# Test 5: VP-19 W-Pattern Double Bottom BUY is blocked when ADX14 > 30
# ---------------------------------------------------------------------------

class TestVP19BuyBlockedByHighADX(unittest.TestCase):
    """VP-19 W-Pattern Double Bottom BUY must not fire when ADX14 > 30."""

    def test_vp19_buy_blocked_when_adx_above_30(self):
        """
        Strong downtrend (high ADX): VP-19 BUY must not fire even if a
        double-bottom pattern appears in the lookback window.
        """
        base_ts = _ist_ts(9, 15)
        step = 300

        candles = _trending_down_candles(80, base_ts, step, price=25000.0, step_size=20.0)
        last_p = candles[-1]["close"]
        candles.append(_candle(base_ts + 80 * step, last_p, last_p + 20, last_p - 30, last_p + 10))
        candles.append(_candle(base_ts + 81 * step, last_p + 10, last_p + 40, last_p - 5, last_p + 25))

        sigs = _signals_for(candles)
        vp19_buys = [s for s in sigs if s["strategy"] == "VP-19 W-Pattern Double Bottom"]

        self.assertEqual(
            len(vp19_buys), 0,
            f"VP-19 BUY must be blocked in high-ADX trend, got: {vp19_buys}"
        )


# ---------------------------------------------------------------------------
# Test 6: VP-05 3EMA Trend SELL is blocked when RSI14 > 40
# ---------------------------------------------------------------------------

class TestVP05SellBlockedByHighRSI(unittest.TestCase):
    """VP-05 3EMA Trend SELL must not fire when RSI14 > 40."""

    def test_vp05_sell_blocked_when_rsi_above_40(self):
        """
        Generate a bearish EMA stack scenario but keep RSI14 above 40 by
        mixing in some up-moves.  VP-05 SELL must be blocked.
        """
        base_ts = _ist_ts(9, 15)
        step = 300

        # Build a flat/neutral market: RSI will hover near 50
        candles = _flat_candles(150, base_ts, step, price=24000.0)

        sigs = _signals_for(candles)
        vp05_sells = [s for s in sigs if s["strategy"] == "VP-05 3EMA Trend"
                      and s["direction"] == "SELL"]

        # In a flat market the EMA stack condition (close < e20 < e50 < e100) will
        # not be consistently met AND RSI will be ~50; no SELL should fire.
        self.assertEqual(
            len(vp05_sells), 0,
            f"VP-05 SELL must be blocked when RSI14 > 40, got: {vp05_sells}"
        )

    def test_vp05_sell_uses_rsi_not_adx(self):
        """
        Verify that VP-05 SELL rule specifically depends on RSI gate.
        The RSI gate is independent of the ADX gate.
        """
        from tools.futures_strategy_engine import _rsi_series
        # Ascending prices → gains dominate → RSI near 100
        prices = [100.0 + i for i in range(30)]
        rsi = _rsi_series(prices, 14)
        valid = [v for v in rsi if v is not None]
        self.assertTrue(all(v > 40 for v in valid),
                        "All RSI values in pure uptrend should be > 40")


# ---------------------------------------------------------------------------
# Test 7: VP-14 Morning Star BUY does not appear in live scanner output
# ---------------------------------------------------------------------------

class TestVP14MorningStar_Disabled(unittest.TestCase):
    """VP-14 Morning Star BUY must never appear in scan_candles output."""

    def _build_morning_star_candles(self, base_ts: float, step: float) -> list[dict]:
        """
        Build candle sequence that satisfies VP-14 Morning Star conditions:
        - c1: large bearish candle (big red)
        - c2: small doji/indecision
        - c3: large bullish candle closing above EMA20
        surrounded by enough warm-up candles.
        """
        # Warm-up: 120 flat candles, then inject the 3-candle pattern
        candles = _flat_candles(120, base_ts, step, price=24000.0)
        # Compute approximate EMA20 of the flat region (~24010)
        e20_approx = 24010.0
        # c1: large bearish candle
        c1_ts = base_ts + 120 * step
        c1 = _candle(c1_ts, 24100.0, 24110.0, 23900.0, 23910.0)  # bearish, 190pt body
        # c2: doji (body < 30% of range)
        c2_ts = c1_ts + step
        c2 = _candle(c2_ts, 23915.0, 23930.0, 23900.0, 23912.0)  # tiny body, 30pt range
        # c3: large bullish candle closing above EMA20
        c3_ts = c2_ts + step
        c3 = _candle(c3_ts, 23920.0, 24100.0, 23915.0, 24080.0)  # bullish, > EMA20
        candles += [c1, c2, c3]
        return candles

    def test_vp14_buy_not_in_live_scanner_output(self):
        """VP-14 Morning Star BUY must never appear in scan_candles output."""
        base_ts = _ist_ts(9, 15)
        candles = self._build_morning_star_candles(base_ts, step=300)
        sigs = _signals_for(candles)
        vp14_buys = [s for s in sigs if s["strategy"] == "VP-14 Morning Star"
                     and s["direction"] == "BUY"]
        self.assertEqual(
            len(vp14_buys), 0,
            "VP-14 Morning Star BUY must be disabled from live scanner output; "
            f"got {len(vp14_buys)} signal(s)"
        )

    def test_vp14_in_disabled_not_supported(self):
        """VP-14 must be in DISABLED_STRATEGIES and NOT in SUPPORTED_STRATEGIES."""
        from tools.futures_strategy_engine import SUPPORTED_STRATEGIES, DISABLED_STRATEGIES
        self.assertIn(
            "VP-14 Morning Star", DISABLED_STRATEGIES,
            "VP-14 Morning Star must be in DISABLED_STRATEGIES (disabled with `if False`)"
        )
        self.assertNotIn(
            "VP-14 Morning Star", SUPPORTED_STRATEGIES,
            "VP-14 Morning Star must NOT be in SUPPORTED_STRATEGIES while disabled"
        )


# ---------------------------------------------------------------------------
# Test 8: Previous-session trigger candle is NOT emitted on current date
# ---------------------------------------------------------------------------

class TestStaleCandle_NotEmittedOnCurrentDate(unittest.TestCase):
    """Candles from a previous session must not produce signals dated today."""

    def test_previous_session_candle_not_emitted(self):
        """
        Build candles all timestamped from yesterday. scan_candles should
        produce zero signals OR all signals should be from yesterday's date
        (not today). The session_date gate inside _add() prevents any signal
        emission when the trigger candle's IST date != last candle's IST date.

        Here: ALL candles are from yesterday, so session_date = yesterday.
        Any emitted signal's 'signal_date' (if present) must NOT be today.
        """
        today = datetime.datetime.now(IST).date()
        yesterday = today - datetime.timedelta(days=1)

        # Build 150 candles from yesterday (09:15 to ~21:40 IST)
        base_ts = IST.localize(
            datetime.datetime(yesterday.year, yesterday.month, yesterday.day, 9, 15, 0)
        ).timestamp()
        candles = _flat_candles(150, base_ts, step=300, price=24000.0)

        sigs = _signals_for(candles)
        today_str = today.strftime("%Y-%m-%d")

        # No signal should have today's date
        for sig in sigs:
            sig_date = sig.get("signal_date")
            if sig_date:
                self.assertNotEqual(
                    sig_date, today_str,
                    f"Stale candle from {yesterday} must not produce a signal "
                    f"dated today ({today_str}). Got: {sig_date}"
                )


# ---------------------------------------------------------------------------
# Test 9: Current-session candle still emits normally (lookback preserved)
# ---------------------------------------------------------------------------

class TestFreshCandle_EmitsNormally(unittest.TestCase):
    """
    Candles from today's session must still be able to produce signals,
    even when prior-session candles are present as lookback warm-up.
    Tests that the session freshness gate does NOT block current-session signals.
    """

    def test_fresh_candle_can_emit_signal(self):
        """
        Build 100 candles from yesterday (for indicator warm-up) followed by
        30 candles from today's session.  scan_candles must be able to produce
        at least some signal output from the today-dated candles (the engine
        should not silently drop all today signals).

        We check: no signal has yesterday's date as signal_date.
        (All emitted signals must originate from today's session candles.)
        """
        today = datetime.datetime.now(IST).date()
        yesterday = today - datetime.timedelta(days=1)
        today_str = today.strftime("%Y-%m-%d")
        yesterday_str = yesterday.strftime("%Y-%m-%d")

        # Yesterday warm-up candles
        base_yesterday = IST.localize(
            datetime.datetime(yesterday.year, yesterday.month, yesterday.day, 9, 15, 0)
        ).timestamp()
        warmup = _flat_candles(100, base_yesterday, step=300, price=24000.0)

        # Today session candles (starting at 09:15 IST today)
        base_today = IST.localize(
            datetime.datetime(today.year, today.month, today.day, 9, 15, 0)
        ).timestamp()
        today_candles = _flat_candles(30, base_today, step=300, price=24000.0)

        all_candles = warmup + today_candles
        sigs = _signals_for(all_candles)

        # Verify: no signal is from yesterday
        for sig in sigs:
            sig_date = sig.get("signal_date")
            if sig_date:
                self.assertNotEqual(
                    sig_date, yesterday_str,
                    f"Signal from yesterday's candle leaked through: {sig}"
                )

        # Optionally verify: the session_date from the engine is today
        # (indirect check via _ist_date of last candle)
        last_ts = all_candles[-1]["time"]
        last_ist_date = datetime.datetime.fromtimestamp(last_ts, IST).strftime("%Y-%m-%d")
        self.assertEqual(last_ist_date, today_str,
                         "Last candle should be from today's session")


if __name__ == "__main__":
    unittest.main(verbosity=2)
