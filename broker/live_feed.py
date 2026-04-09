"""
broker/live_feed.py — WebSocket live feed manager for BlitzTrader.
Ported from SpreadTrader's websocket_worker.py (QThread → threading.Thread).

Runs in a daemon thread, maintains a real-time price cache with:
  LTP, best bid, best ask, bid qty, ask qty, timestamp.

All reads from the cache are thread-safe (via threading.Lock).
"""
import json
import logging
import threading
import time
from typing import Optional, Callable

logger = logging.getLogger("BlitzTrader.LiveFeed")


class LiveFeedManager:
    """
    Manages Shoonya WebSocket connection in a background thread.
    Maintains a real-time, thread-safe price cache.

    Usage:
        feed = LiveFeedManager(shoonya_client)
        feed.start()
        feed.subscribe([("NSE", "26000"), ("NSE", "26009")])
        ...
        bid, ask = feed.get_best_bid_ask("26000")
        quote = feed.get_live_quote("26000")
        ...
        feed.stop()
    """

    # Stale threshold — fall back to REST if data is older than this (seconds)
    STALE_THRESHOLD = 30.0

    def __init__(self, shoonya_client, on_tick_callback: Optional[Callable] = None, on_health_alert: Optional[Callable] = None):
        """
        :param shoonya_client: Authenticated ShoonyaClient instance
        :param on_tick_callback: Optional callback(token, quote_dict) on each tick
        :param on_health_alert: Optional callback(message) for health alerts
        """
        self._client = shoonya_client
        self._on_tick_callback = on_tick_callback
        self._on_health_alert = on_health_alert

        # Thread-safe price cache: {token: {ltp, best_bid, best_ask, ...}}
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

        # Live candle aggregator: {(token, interval_mins): deque of completed candles}
        # Each candle: {time, open, high, low, close, volume}
        # Current (in-progress) candle: {(token, interval_mins): dict}
        from collections import deque
        self._candles: dict[tuple, deque] = {}
        self._current_candle: dict[tuple, dict] = {}
        self._vol_at_start: dict[tuple, int] = {}
        self._MAX_CANDLES = 500  # keep last 500 candles per token+interval (enough for EMA100/ADX warm-up)

        # WebSocket thread
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False

        # Pending subscriptions (accumulated before WS opens)
        self._pending_subs: list[tuple[str, str]] = []
        self._active_subs: list[tuple[str, str]] = []

        # Reconnect control
        self._reconnect_delay = 5.0
        self._max_reconnect_delay = 60.0
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._last_successful_connection = time.time()

    # ──────────────────────────────────────────────────────────
    #   LIFECYCLE
    # ──────────────────────────────────────────────────────────

    def start(self):
        """Start the WebSocket connection in a daemon thread."""
        if self._running:
            logger.warning("LiveFeedManager already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._ws_loop,
            name="BlitzTrader-LiveFeed",
            daemon=True,
        )
        self._thread.start()
        logger.info("LiveFeedManager started")

    def stop(self):
        """Stop the WebSocket and background thread."""
        self._running = False
        self._connected = False
        try:
            self._client.close_websocket()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("LiveFeedManager stopped")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ──────────────────────────────────────────────────────────
    #   SUBSCRIPTIONS
    # ──────────────────────────────────────────────────────────

    def subscribe(self, exchange_token_pairs: list[tuple[str, str]]):
        """
        Subscribe to touchline feed for given (exchange, token) pairs.
        Can be called before or after WebSocket connects.
        """
        new_pairs = [
            p for p in exchange_token_pairs
            if p not in self._active_subs and p not in self._pending_subs
        ]
        if not new_pairs:
            return

        if self._connected:
            self._client.subscribe(new_pairs)
            self._active_subs.extend(new_pairs)
        else:
            self._pending_subs.extend(new_pairs)

    def unsubscribe(self, exchange_token_pairs: list[tuple[str, str]]):
        """Unsubscribe from given tokens."""
        if self._connected:
            self._client.unsubscribe(exchange_token_pairs)
        self._active_subs = [
            p for p in self._active_subs if p not in exchange_token_pairs
        ]
        self._pending_subs = [
            p for p in self._pending_subs if p not in exchange_token_pairs
        ]
        # Clean cache
        with self._lock:
            for _, token in exchange_token_pairs:
                self._cache.pop(token, None)

    # ──────────────────────────────────────────────────────────
    #   CACHE READS (Thread-Safe)
    # ──────────────────────────────────────────────────────────

    def get_live_quote(self, token: str) -> Optional[dict]:
        """
        Get cached live quote for a token.
        Returns dict: {ltp, best_bid, best_ask, bid_qty, ask_qty, timestamp}
        Returns None if no data for this token.
        """
        with self._lock:
            return self._cache.get(token, None)

    def get_best_bid_ask(self, token: str) -> Optional[tuple[float, float]]:
        """
        Get best (bid, ask) from cache.
        Returns None if no data or data is stale.
        """
        with self._lock:
            entry = self._cache.get(token)
            if not entry:
                return None
            # Check staleness
            age = time.time() - entry.get("timestamp", 0)
            if age > self.STALE_THRESHOLD:
                logger.warning(
                    f"Stale data for token {token} (age={age:.1f}s), "
                    "caller should use REST fallback"
                )
                return None
            bid = entry.get("best_bid")
            ask = entry.get("best_ask")
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                return bid, ask
        return None

    def get_ltp(self, token: str) -> Optional[float]:
        """Get cached LTP for a token."""
        with self._lock:
            entry = self._cache.get(token)
            if entry:
                age = time.time() - entry.get("timestamp", 0)
                if age <= self.STALE_THRESHOLD:
                    return entry.get("ltp")
        return None

    def get_all_quotes(self) -> dict[str, dict]:
        """Get a snapshot of all cached quotes."""
        with self._lock:
            return dict(self._cache)

    def get_candles(self, token: str, interval_mins: int, count: int = 20) -> Optional[list[dict]]:
        """
        Get last N completed OHLCV candles built from live ticks.

        :param token: Shoonya token (e.g. "26000" for NIFTY)
        :param interval_mins: Candle size in minutes (1, 5, 15, 60, etc.)
        :param count: Number of completed candles to return
        :returns: List of candle dicts [{time, open, high, low, close, volume}], newest last.
                  Returns None if not enough data yet.
        """
        key = (token, interval_mins)
        with self._lock:
            candles = self._candles.get(key)
            if not candles:
                return None
            result = list(candles)[-count:]
            return result if result else None

    def _update_candles(self, token: str, ltp: float, volume: int, ts: float) -> None:
        """
        Update live candle aggregator with a new tick.
        Called inside _on_tick while lock is held.
        Builds 1m, 3m, 5m, 15m, 30m, and 60m candles simultaneously.
        """
        from collections import deque
        import math

        for interval_mins in (1, 3, 5, 15, 30, 60):
            key = (token, interval_mins)
            # Candle boundary: floor tick timestamp to interval
            candle_ts = math.floor(ts / (interval_mins * 60)) * (interval_mins * 60)

            current = self._current_candle.get(key)

            if current is None or current["time"] != candle_ts:
                # Candle boundary crossed — finalise old candle
                if current is not None:
                    if key not in self._candles:
                        self._candles[key] = deque(maxlen=self._MAX_CANDLES)
                    # Remove internal vol_start before saving completed candle
                    saved = {k: v for k, v in current.items() if k != "vol_start"}
                    self._candles[key].append(saved)
                # Record cumulative volume at the start of the new candle
                vol_start = volume
                self._vol_at_start[key] = vol_start
                # Start new candle
                self._current_candle[key] = {
                    "time": candle_ts,
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume": 0,
                    "vol_start": vol_start,
                }
            else:
                # Update current candle
                current["high"] = max(current["high"], ltp)
                current["low"] = min(current["low"], ltp)
                current["close"] = ltp
                # Per-bar volume = current cumulative - baseline at candle start
                vol_start = current.get("vol_start", self._vol_at_start.get(key, volume))
                current["volume"] = max(0, volume - vol_start)

    # ──────────────────────────────────────────────────────────
    #   WEBSOCKET LOOP (runs in thread)
    # ──────────────────────────────────────────────────────────

    def _ws_loop(self):
        """Main loop: connect → process → reconnect on failure."""
        delay = self._reconnect_delay

        while self._running:
            try:
                logger.info("Connecting WebSocket...")
                self._reconnect_attempts += 1
                self._client.start_websocket(
                    on_open=self._on_open,
                    on_tick=self._on_tick,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
            except Exception as e:
                logger.exception("WebSocket connection failed")
                self._reconnect_attempts += 1

                # Check if stuck in reconnection loop
                if self._reconnect_attempts > self._max_reconnect_attempts:
                    msg = f"⚠️ WebSocket stuck in reconnection loop ({self._reconnect_attempts} attempts). Resetting..."
                    logger.error(msg)
                    if self._on_health_alert:
                        self._on_health_alert(msg)
                    # Force reset
                    self._reconnect_attempts = 0
                    delay = self._reconnect_delay

            self._connected = False

            if not self._running:
                break

            # Reconnect with backoff
            logger.info(f"Reconnecting in {delay:.0f}s... (attempt {self._reconnect_attempts}/{self._max_reconnect_attempts})")
            time.sleep(delay)
            delay = min(delay * 1.5, self._max_reconnect_delay)

        logger.info("WebSocket loop exited")

    # ──────────────────────────────────────────────────────────
    #   WEBSOCKET CALLBACKS
    # ──────────────────────────────────────────────────────────

    def _on_open(self, ws):
        """Called when WebSocket connects successfully."""
        self._connected = True
        self._reconnect_delay = 5.0  # Reset backoff
        self._reconnect_attempts = 0  # Reset attempt counter
        self._last_successful_connection = time.time()
        logger.info("WebSocket connected successfully")

        # Subscribe pending tokens
        all_subs = self._pending_subs + self._active_subs
        if all_subs:
            self._client.subscribe(all_subs)
            self._active_subs = list(set(all_subs))
            self._pending_subs = []
            logger.info(f"Subscribed to {len(self._active_subs)} tokens")

    def _on_tick(self, ws, message):
        """Parse touchline message and update cache."""
        try:
            if isinstance(message, str):
                data = json.loads(message)
            elif isinstance(message, dict):
                data = message
            else:
                return

            msg_type = data.get("t", "")
            if msg_type not in ("tf", "tk"):
                return

            token = data.get("tk", "")
            if not token:
                return

            # Build quote update — Shoonya sends partial updates,
            # so we merge with existing cache
            now = time.time()

            with self._lock:
                existing = self._cache.get(token, {})

                # Update only fields that are present in this tick
                if "lp" in data:
                    existing["ltp"] = float(data["lp"])
                if "bp1" in data:
                    existing["best_bid"] = float(data["bp1"])
                if "sp1" in data:
                    existing["best_ask"] = float(data["sp1"])
                if "bq1" in data:
                    existing["bid_qty"] = int(data["bq1"])
                if "sq1" in data:
                    existing["ask_qty"] = int(data["sq1"])
                if "oi" in data:
                    existing["oi"] = int(data["oi"])
                if "v" in data:
                    existing["volume"] = int(data["v"])
                if "h" in data:
                    existing["high"] = float(data["h"])
                if "l" in data:
                    existing["low"] = float(data["l"])
                if "o" in data:
                    existing["open"] = float(data["o"])
                if "c" in data:
                    existing["prev_close"] = float(data["c"])

                existing["timestamp"] = now
                existing["token"] = token

                self._cache[token] = existing

                # Build live candles from this tick
                ltp = existing.get("ltp")
                vol = existing.get("volume", 0)
                if ltp:
                    self._update_candles(token, ltp, vol, now)

            # External callback
            if self._on_tick_callback:
                try:
                    self._on_tick_callback(token, existing)
                except Exception:
                    logger.exception("Error in on_tick_callback")

        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        except Exception:
            logger.exception("Unexpected error parsing WebSocket message")

    def _on_error(self, ws, error):
        """Called on WebSocket error."""
        logger.error(f"WebSocket error: {error}")
        self._connected = False

    def _on_close(self, ws, code, msg):
        """Called when WebSocket closes."""
        self._connected = False
        logger.warning(f"WebSocket closed: code={code} msg={msg}")
