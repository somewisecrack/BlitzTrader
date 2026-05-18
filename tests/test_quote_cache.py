"""
tests/test_quote_cache.py
--------------------------
Tests for broker/quote_cache.py

Covers:
  - Same instrument requested twice in one loop → REST fetched once
  - Same stock in two pairs → quoted once, reused
  - Telegram/status summary → no extra broker calls when cached prices exist
  - Rate limiter → blocks calls beyond MAX_CALLS_PER_MINUTE
  - "exceeds Limit 120" response → handled gracefully, no crash
"""
import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker.quote_cache import QuoteCache, MAX_CALLS_PER_MINUTE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_ok_resp(ltp: float, bid: float = None, ask: float = None) -> dict:
    """Build a fake Shoonya GetQuotes response."""
    resp = {"stat": "Ok", "lp": str(ltp)}
    if bid is not None:
        resp["bp1"] = str(bid)
    if ask is not None:
        resp["sp1"] = str(ask)
    return resp


def _make_error_resp(msg: str) -> dict:
    return {"stat": "Not_Ok", "emsg": msg}


def _make_client(resp) -> MagicMock:
    """Return a mock ShoonyaClient whose get_quotes returns resp."""
    client = MagicMock()
    client.get_quotes.return_value = resp
    return client


def _make_cache(client=None, feed=None) -> QuoteCache:
    if client is None:
        client = _make_client(_make_ok_resp(100.0, 99.9, 100.1))
    return QuoteCache(shoonya_client=client, live_feed=feed)


# ---------------------------------------------------------------------------
# Basic fetch and caching
# ---------------------------------------------------------------------------

class TestBasicFetch:

    def test_get_ltp_returns_float(self):
        cache = _make_cache()
        ltp = cache.get_ltp("NFO", "12345", ttl=2.0)
        assert isinstance(ltp, float)
        assert ltp == 100.0

    def test_same_token_fetched_once_within_ttl(self):
        """Same (exchange, token) requested twice within TTL → REST called once."""
        client = _make_client(_make_ok_resp(200.0, 199.9, 200.1))
        cache = QuoteCache(shoonya_client=client, live_feed=None)
        cache.get_ltp("NFO", "99999", ttl=5.0)
        cache.get_ltp("NFO", "99999", ttl=5.0)
        assert client.get_quotes.call_count == 1

    def test_same_stock_two_callers_fetched_once(self):
        """Simulate pairs trading: two legs for same stock → only one REST call."""
        client = _make_client(_make_ok_resp(500.0, 499.5, 500.5))
        cache = QuoteCache(shoonya_client=client, live_feed=None)
        # First caller (e.g. long leg check)
        price_1 = cache.get_ltp("NSE", "88888", ttl=5.0)
        # Second caller (e.g. short leg check on same stock)
        price_2 = cache.get_ltp("NSE", "88888", ttl=5.0)
        assert client.get_quotes.call_count == 1
        assert price_1 == price_2 == 500.0

    def test_different_tokens_fetch_independently(self):
        """Two different tokens each get their own REST call."""
        client = _make_client(_make_ok_resp(300.0))
        cache = QuoteCache(shoonya_client=client, live_feed=None)
        cache.get_ltp("NFO", "11111", ttl=5.0)
        cache.get_ltp("NFO", "22222", ttl=5.0)
        assert client.get_quotes.call_count == 2

    def test_expired_cache_triggers_new_fetch(self):
        """After TTL expires a stale entry is re-fetched."""
        client = _make_client(_make_ok_resp(150.0))
        cache = QuoteCache(shoonya_client=client, live_feed=None)
        cache.get_ltp("NFO", "55555", ttl=0.01)
        time.sleep(0.05)  # let the TTL expire
        cache.get_ltp("NFO", "55555", ttl=0.01)
        assert client.get_quotes.call_count == 2


# ---------------------------------------------------------------------------
# Bid/ask
# ---------------------------------------------------------------------------

class TestBidAsk:

    def test_get_best_bid_ask_returns_tuple(self):
        cache = _make_cache()
        result = cache.get_best_bid_ask("NFO", "12345", ttl=2.0)
        assert result is not None
        bid, ask = result
        assert bid == 99.9
        assert ask == 100.1

    def test_bid_ask_uses_cache(self):
        """Second bid/ask call within TTL doesn't call REST again."""
        client = _make_client(_make_ok_resp(100.0, 99.9, 100.1))
        cache = QuoteCache(shoonya_client=client, live_feed=None)
        cache.get_best_bid_ask("NSE", "77777", ttl=5.0)
        cache.get_best_bid_ask("NSE", "77777", ttl=5.0)
        assert client.get_quotes.call_count == 1


# ---------------------------------------------------------------------------
# Telegram / status use case — no extra broker calls when cache is warm
# ---------------------------------------------------------------------------

class TestTelegramStatusNoExtraFetch:

    def test_status_query_reuses_cache(self):
        """
        After monitoring loop has already fetched a price, a status/Telegram
        query with a large TTL should reuse the cached value without any
        additional REST call.
        """
        client = _make_client(_make_ok_resp(23500.0, 23499.0, 23501.0))
        cache = QuoteCache(shoonya_client=client, live_feed=None)

        # Monitoring loop fetches fresh data (short TTL)
        cache.get_ltp("NFO", "66666", ttl=2.0)
        assert client.get_quotes.call_count == 1

        # Telegram status query — large TTL, should reuse cached price
        price = cache.get_ltp("NFO", "66666", ttl=3600.0)
        assert client.get_quotes.call_count == 1  # no new REST call
        assert price == 23500.0


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class TestRateLimiter:

    def test_rate_limiter_blocks_beyond_max(self):
        """
        After MAX_CALLS_PER_MINUTE REST calls, further calls return cached/None
        without making another REST call.
        """
        call_count = {"n": 0}

        def _get_quotes(exchange, token):
            call_count["n"] += 1
            return _make_ok_resp(100.0 + call_count["n"])

        client = MagicMock()
        client.get_quotes.side_effect = _get_quotes
        cache = QuoteCache(shoonya_client=client, live_feed=None)

        # Exhaust the rate limit by injecting fake call timestamps
        import time as _time
        now = _time.time()
        with cache._lock:
            for _ in range(MAX_CALLS_PER_MINUTE):
                cache._call_times.append(now)

        # This call should be throttled — REST must NOT be called
        result = cache.get_ltp("NFO", "99991", ttl=0.0)
        assert client.get_quotes.call_count == 0  # no REST calls since rate-limited
        # result may be None (no cached entry) — that's acceptable
        assert result is None or isinstance(result, float)

    def test_call_count_last_minute_reflects_real_calls(self):
        client = _make_client(_make_ok_resp(200.0))
        cache = QuoteCache(shoonya_client=client, live_feed=None)
        assert cache.call_count_last_minute() == 0
        cache.get_ltp("NFO", "11111", ttl=0.0)
        assert cache.call_count_last_minute() == 1
        cache.get_ltp("NFO", "22222", ttl=0.0)
        assert cache.call_count_last_minute() == 2


# ---------------------------------------------------------------------------
# "exceeds Limit 120" response → graceful handling, no crash
# ---------------------------------------------------------------------------

class TestShoonyaRateLimitResponse:

    def test_exceeds_limit_response_does_not_crash(self):
        """Shoonya 'exceeds Limit 120' error is handled; no exception raised."""
        client = _make_client(
            _make_error_resp(
                "GetQuotes returned Invalid Input : Order Recieved 121 in a "
                "current minute exceeds Limit 120 for user"
            )
        )
        cache = QuoteCache(shoonya_client=client, live_feed=None)
        result = cache.get_ltp("NFO", "12345", ttl=0.0)
        assert result is None  # gracefully returns None

    def test_exceeds_limit_logs_once_not_per_call(self, caplog):
        """Throttle warning is emitted at most once per THROTTLE_WARN_INTERVAL."""
        import logging
        client = _make_client(
            _make_error_resp(
                "Order Recieved 121 in a current minute exceeds Limit 120 for user"
            )
        )
        cache = QuoteCache(shoonya_client=client, live_feed=None)
        cache._last_throttle_warn = 0.0  # ensure warning can fire

        with caplog.at_level(logging.WARNING, logger="BlitzTrader.QuoteCache"):
            cache.get_ltp("NFO", "T001", ttl=0.0)
            cache.get_ltp("NFO", "T002", ttl=0.0)
            cache.get_ltp("NFO", "T003", ttl=0.0)

        # Count WARNING messages containing "rate limit" or "exceeds Limit"
        warn_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING
            and ("rate limit" in r.message.lower() or "exceeds" in r.message.lower()
                 or "120" in r.message)
        ]
        assert len(warn_msgs) <= 1, (
            f"Expected at most 1 throttle warning, got {len(warn_msgs)}: {warn_msgs}"
        )


# ---------------------------------------------------------------------------
# WebSocket feed preference
# ---------------------------------------------------------------------------

class TestWebSocketPreference:

    def _make_feed_with_tick(self, ltp: float, bid: float, ask: float) -> MagicMock:
        feed = MagicMock()
        tick_ts = time.time()  # fresh tick
        feed.get_live_quote.return_value = {
            "ltp": ltp,
            "best_bid": bid,
            "best_ask": ask,
            "timestamp": tick_ts,
        }
        return feed

    def test_websocket_tick_used_before_rest(self):
        """If WebSocket has a fresh tick, REST get_quotes is NOT called."""
        client = _make_client(_make_ok_resp(999.0))
        feed = self._make_feed_with_tick(500.0, 499.5, 500.5)
        cache = QuoteCache(shoonya_client=client, live_feed=feed)
        ltp = cache.get_ltp("NFO", "33333", ttl=2.0)
        assert ltp == 500.0
        assert client.get_quotes.call_count == 0  # REST not called

    def test_rest_fallback_when_feed_returns_none(self):
        """If WebSocket returns no tick, REST is used as fallback."""
        client = _make_client(_make_ok_resp(400.0))
        feed = MagicMock()
        feed.get_live_quote.return_value = None
        cache = QuoteCache(shoonya_client=client, live_feed=feed)
        ltp = cache.get_ltp("NFO", "44444", ttl=2.0)
        assert ltp == 400.0
        assert client.get_quotes.call_count == 1
