"""
broker/quote_cache.py — Thread-safe quote cache with TTL and per-minute rate limiter.

This module prevents Shoonya's 120-calls/minute GetQuotes limit from being
exceeded when multiple components (futures monitoring, pairs monitoring,
Telegram status, context builders) all request prices in the same loop cycle.

Design:
  - Prefer live WebSocket tick data where available and fresh.
  - Fall back to REST get_quotes ONLY when the WebSocket data is missing/stale.
  - Deduplicate: same (exchange, token) is fetched at most once per TTL window.
  - Rate limiter: hard cap of MAX_CALLS_PER_MINUTE REST calls in any 60-second
    rolling window. When the cap is approached, stale WebSocket prices are used
    and a single throttle warning is logged (not one per token).

TTL policy (conservative defaults, overridable per call):
  - Futures position monitoring: 2 seconds (fast-moving, need fresh price)
  - Pairs leg monitoring: 5 seconds (equity, slower)
  - Telegram/status/context: reuse any cached snapshot — no forced fetch

Usage:
    cache = QuoteCache(shoonya_client, live_feed)
    price = cache.get_ltp("NFO", "66691", ttl=2.0)
    bid, ask = cache.get_best_bid_ask("NSE", "12345", ttl=5.0)
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("BlitzTrader.QuoteCache")

# Hard limit — never exceed this many REST GetQuotes calls per minute.
MAX_CALLS_PER_MINUTE = 110  # keep 10 calls headroom below Shoonya's 120 cap
# How often (seconds) to emit a throttle warning (avoid log spam).
THROTTLE_WARN_INTERVAL = 60.0


class QuoteCache:
    """
    Thread-safe quote cache with TTL and rolling rate limiter.

    Attributes
    ----------
    _cache : dict
        {(exchange, token): {"ltp": float, "bid": float, "ask": float,
                              "ts": float (epoch), "source": str}}
    _call_times : deque
        Epoch timestamps of recent REST get_quotes calls (within last 60 s).
    """

    def __init__(self, shoonya_client, live_feed=None):
        """
        Parameters
        ----------
        shoonya_client : ShoonyaClient
            Authenticated Shoonya REST client.
        live_feed : LiveFeedManager | None
            WebSocket feed manager; used to read tick data before REST fallback.
        """
        self._client = shoonya_client
        self._feed = live_feed
        self._cache: dict[tuple, dict] = {}
        self._lock = threading.Lock()
        self._call_times: deque = deque()  # epoch times of REST calls in last 60 s
        self._last_throttle_warn: float = 0.0

    # ──────────────────────────────────────────────────────────
    #   PUBLIC API
    # ──────────────────────────────────────────────────────────

    def get_ltp(
        self,
        exchange: str,
        token: str,
        ttl: float = 2.0,
    ) -> Optional[float]:
        """
        Return the last-traded price for (exchange, token).

        Parameters
        ----------
        ttl : float
            Maximum age (seconds) before a cached value is considered stale.
            Pass a large value (e.g. 3600) to accept any cached snapshot.
        """
        entry = self._get_or_fetch(exchange, token, ttl)
        if entry:
            return entry.get("ltp")
        return None

    def get_best_bid_ask(
        self,
        exchange: str,
        token: str,
        ttl: float = 2.0,
    ) -> Optional[tuple[float, float]]:
        """
        Return (best_bid, best_ask) for (exchange, token).

        Returns None if price is unavailable.
        """
        entry = self._get_or_fetch(exchange, token, ttl)
        if entry:
            bid = entry.get("bid")
            ask = entry.get("ask")
            if bid is not None and ask is not None:
                return float(bid), float(ask)
        return None

    def invalidate(self, exchange: str, token: str) -> None:
        """Remove a cached entry (force fresh fetch on next access)."""
        key = (exchange, token)
        with self._lock:
            self._cache.pop(key, None)

    def call_count_last_minute(self) -> int:
        """Return the number of REST calls made in the last 60 seconds."""
        with self._lock:
            self._prune_call_times()
            return len(self._call_times)

    # ──────────────────────────────────────────────────────────
    #   INTERNAL
    # ──────────────────────────────────────────────────────────

    def _get_or_fetch(
        self,
        exchange: str,
        token: str,
        ttl: float,
    ) -> Optional[dict]:
        """
        Return a cached entry if fresh enough, otherwise fetch from WebSocket
        then REST.
        """
        key = (exchange, token)
        now = time.monotonic()

        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and (now - entry["ts"]) <= ttl:
                return entry

        # Try WebSocket first (no REST cost)
        ws_entry = self._read_from_feed(exchange, token)
        if ws_entry is not None:
            with self._lock:
                self._cache[key] = ws_entry
            return ws_entry

        # Decide whether we can make a REST call
        with self._lock:
            self._prune_call_times()
            near_limit = len(self._call_times) >= MAX_CALLS_PER_MINUTE

        if near_limit:
            # Use stale cache if available
            with self._lock:
                stale = self._cache.get(key)
            now_wall = time.time()
            if now_wall - self._last_throttle_warn >= THROTTLE_WARN_INTERVAL:
                self._last_throttle_warn = now_wall
                logger.warning(
                    "QuoteCache: REST rate limit reached (%d calls in last 60 s). "
                    "Returning stale/cached price for %s:%s.",
                    len(self._call_times),
                    exchange,
                    token,
                )
            return stale  # may be None — callers must handle

        # REST fetch
        rest_entry = self._fetch_from_rest(exchange, token)
        if rest_entry is not None:
            with self._lock:
                rest_entry["ts"] = time.monotonic()
                self._cache[key] = rest_entry
                self._call_times.append(time.time())
            return rest_entry

        # REST returned nothing — return stale if we have it
        with self._lock:
            return self._cache.get(key)

    def _prune_call_times(self) -> None:
        """Remove call timestamps older than 60 seconds (must be called under lock)."""
        cutoff = time.time() - 60.0
        while self._call_times and self._call_times[0] < cutoff:
            self._call_times.popleft()

    def _read_from_feed(self, exchange: str, token: str) -> Optional[dict]:
        """Try to read a live tick from the WebSocket feed (zero REST cost)."""
        if not self._feed:
            return None
        try:
            quote = self._feed.get_live_quote(token)
            if not quote:
                return None
            ts_raw = quote.get("timestamp") or quote.get("ft") or 0
            try:
                tick_ts = float(ts_raw)
            except (TypeError, ValueError):
                tick_ts = 0.0
            # Accept tick if it arrived within the last 30 seconds
            if tick_ts and (time.time() - tick_ts) > 30.0:
                return None
            ltp_raw = quote.get("ltp") or quote.get("last_price")
            bid_raw = quote.get("best_bid")
            ask_raw = quote.get("best_ask")
            if ltp_raw is None and bid_raw is None:
                return None
            entry: dict = {"ts": time.monotonic(), "source": "websocket"}
            if ltp_raw is not None:
                try:
                    entry["ltp"] = float(ltp_raw)
                except (TypeError, ValueError):
                    pass
            if bid_raw is not None and ask_raw is not None:
                try:
                    entry["bid"] = float(bid_raw)
                    entry["ask"] = float(ask_raw)
                except (TypeError, ValueError):
                    pass
            # ltp fallback from bid/ask midpoint
            if "ltp" not in entry and "bid" in entry and "ask" in entry:
                entry["ltp"] = round((entry["bid"] + entry["ask"]) / 2, 2)
            return entry if "ltp" in entry or ("bid" in entry and "ask" in entry) else None
        except Exception:
            logger.debug("QuoteCache: exception reading from WebSocket feed", exc_info=True)
            return None

    def _fetch_from_rest(self, exchange: str, token: str) -> Optional[dict]:
        """Call Shoonya REST get_quotes and parse into a cache entry dict."""
        if not self._client:
            return None
        try:
            resp = self._client.get_quotes(exchange, token)
            if not resp or resp.get("stat") != "Ok":
                emsg = resp.get("emsg", "unknown") if resp else "None response"
                # Detect rate-limit error and log once, not per token
                if resp and "exceeds Limit" in str(emsg):
                    now_wall = time.time()
                    if now_wall - self._last_throttle_warn >= THROTTLE_WARN_INTERVAL:
                        self._last_throttle_warn = now_wall
                        logger.warning(
                            "QuoteCache: Shoonya rate limit hit: %s. "
                            "Consider reducing poll frequency.",
                            emsg,
                        )
                    return None
                return None
            entry: dict = {"source": "rest"}
            lp = resp.get("lp") or resp.get("c")
            if lp is not None:
                try:
                    entry["ltp"] = float(lp)
                except (TypeError, ValueError):
                    pass
            bp1 = resp.get("bp1")
            sp1 = resp.get("sp1")
            if bp1 is not None and sp1 is not None:
                try:
                    entry["bid"] = float(bp1)
                    entry["ask"] = float(sp1)
                except (TypeError, ValueError):
                    pass
            if "ltp" not in entry and "bid" in entry and "ask" in entry:
                entry["ltp"] = round((entry["bid"] + entry["ask"]) / 2, 2)
            # Store full response for callers that need depth data
            entry["_raw"] = resp
            return entry if "ltp" in entry or ("bid" in entry and "ask" in entry) else None
        except Exception:
            logger.debug(
                "QuoteCache: exception calling REST get_quotes(%s, %s)",
                exchange,
                token,
                exc_info=True,
            )
            return None
