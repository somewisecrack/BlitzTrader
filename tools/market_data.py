"""
tools/market_data.py — Market data tools for Claude.

Each function here is a tool the LLM can call.
All data comes from the Shoonya API (via live WebSocket feed or REST fallback).
"""
import logging
import time
from typing import Optional

logger = logging.getLogger("BlitzTrader.MarketData")


class MarketDataTools:
    """
    Market data tool implementations.
    Wraps the ShoonyaClient and LiveFeedManager for Claude's use.
    """

    def __init__(self, shoonya_client, live_feed, nse_tokens: dict):
        """
        :param shoonya_client: Authenticated ShoonyaClient
        :param live_feed: LiveFeedManager instance
        :param nse_tokens: Token map from config.NSE_TOKENS
        """
        self._client = shoonya_client
        self._feed = live_feed
        self._tokens = nse_tokens

    def _resolve_token(self, index: str) -> tuple[str, str]:
        """Resolve index name to (exchange, token)."""
        info = self._tokens.get(index.upper())
        if info:
            return info["exchange"], info["token"]
        raise ValueError(f"Unknown index: {index}. Valid: {list(self._tokens.keys())}")

    # ──────────────────────────────────────────────────────────
    #   TOOLS (callable by Claude)
    # ──────────────────────────────────────────────────────────

    def get_spot_price(self, index: str) -> dict:
        """
        Get current spot price for NIFTY or BANKNIFTY.

        :param index: 'NIFTY' or 'BANKNIFTY'
        :returns: {index, spot_price, change, change_pct, high, low, open}
        """
        exchange, token = self._resolve_token(index)

        # Try live feed first
        live = self._feed.get_live_quote(token) if self._feed else None

        if live and live.get("ltp"):
            prev = live.get("prev_close", 0)
            change = live["ltp"] - prev if prev else 0
            return {
                "index": index.upper(),
                "spot_price": live["ltp"],
                "change": round(change, 2),
                "change_pct": round((change / prev * 100) if prev else 0, 2),
                "high": live.get("high", 0),
                "low": live.get("low", 0),
                "open": live.get("open", 0),
                "source": "live_websocket",
            }

        # REST fallback
        resp = self._client.get_quotes(exchange, token)
        if not resp:
            return {"error": f"Failed to get spot price for {index}"}

        ltp = float(resp.get("lp", 0))
        prev = float(resp.get("c", 0))
        change = ltp - prev if prev else 0

        return {
            "index": index.upper(),
            "spot_price": ltp,
            "change": round(change, 2),
            "change_pct": round((change / prev * 100) if prev else 0, 2),
            "high": float(resp.get("h", 0)),
            "low": float(resp.get("l", 0)),
            "open": float(resp.get("o", 0)),
            "source": "rest_api",
        }

    def get_option_chain(self, index: str, expiry: str) -> dict:
        """
        Get full option chain with strikes, LTP, bid, ask, OI.

        :param index: 'NIFTY' or 'BANKNIFTY'
        :param expiry: Expiry prefix e.g. '27MAR' or '03APR'
        :returns: {index, expiry, chain: [{symbol, strike, type, ltp, bid, ask, oi, volume}]}
        """
        # Get spot price to determine strike range
        spot_data = self.get_spot_price(index)
        spot = spot_data.get("spot_price", 0)

        # Search ±10 strikes around ATM
        step = 50 if index.upper() == "NIFTY" else 100
        strike_range = (spot - step * 10, spot + step * 10)

        chain = self._client.get_option_chain(
            index=index.upper(),
            expiry_prefix=expiry.upper(),
            strike_range=strike_range,
        )

        return {
            "index": index.upper(),
            "expiry": expiry.upper(),
            "spot_price": spot,
            "atm_strike": round(spot / step) * step,
            "chain": chain,
            "count": len(chain),
        }

    def get_quote(self, symbol: str) -> dict:
        """
        Get LTP, best bid, best ask for a specific symbol.

        :param symbol: Trading symbol (e.g., 'NIFTY27MAR24500CE')
        :returns: {symbol, ltp, best_bid, best_ask, bid_qty, ask_qty}
        """
        # First, we need the token — search for it
        results = self._client.search_scrip("NFO", symbol)
        if not results:
            # Try NSE
            results = self._client.search_scrip("NSE", symbol)

        if not results:
            return {"error": f"Symbol not found: {symbol}"}

        scrip = results[0]
        token = scrip.get("token", "")
        exchange = scrip.get("exch", "NFO")

        # Try live feed
        live = self._feed.get_live_quote(token) if self._feed else None

        if live and live.get("ltp"):
            return {
                "symbol": symbol,
                "token": token,
                "ltp": live["ltp"],
                "best_bid": live.get("best_bid", 0),
                "best_ask": live.get("best_ask", 0),
                "bid_qty": live.get("bid_qty", 0),
                "ask_qty": live.get("ask_qty", 0),
                "oi": live.get("oi", 0),
                "volume": live.get("volume", 0),
                "source": "live_websocket",
            }

        # REST fallback
        resp = self._client.get_quotes(exchange, token)
        if not resp:
            return {"error": f"Failed to get quote for {symbol}"}

        return {
            "symbol": symbol,
            "token": token,
            "ltp": float(resp.get("lp", 0)),
            "best_bid": float(resp.get("bp1", 0)),
            "best_ask": float(resp.get("sp1", 0)),
            "bid_qty": int(resp.get("bq1", 0)),
            "ask_qty": int(resp.get("sq1", 0)),
            "oi": int(resp.get("oi", 0)),
            "volume": int(resp.get("v", 0)),
            "source": "rest_api",
        }

    def get_candles(
        self, symbol: str, interval: str = "5", count: int = 20
    ) -> dict:
        """
        Get last N candles at given interval.

        :param symbol: Trading symbol
        :param interval: '1', '5', '15', '30', '60' (minutes)
        :param count: Number of candles to return
        :returns: {symbol, interval, candles: [{time, open, high, low, close, volume}]}
        """
        # Resolve symbol to token
        results = self._client.search_scrip("NFO", symbol)
        if not results:
            results = self._client.search_scrip("NSE", symbol)
        if not results:
            return {"error": f"Symbol not found: {symbol}"}

        scrip = results[0]
        token = scrip.get("token", "")
        exchange = scrip.get("exch", "NFO")

        # Calculate start time (enough to get `count` candles)
        interval_secs = int(interval) * 60
        start_time = time.time() - (count + 10) * interval_secs

        raw_candles = self._client.get_time_price_series(
            exchange=exchange,
            token=token,
            starttime=start_time,
            interval=interval,
        )

        if not raw_candles:
            return {"error": f"No candle data for {symbol}"}

        # Parse and limit
        candles = []
        for c in raw_candles[:count]:
            candles.append({
                "time": c.get("time", c.get("ssboe", "")),
                "open": float(c.get("into", 0)),
                "high": float(c.get("inth", 0)),
                "low": float(c.get("intl", 0)),
                "close": float(c.get("intc", 0)),
                "volume": int(c.get("intv", 0)),
            })

        return {
            "symbol": symbol,
            "interval": f"{interval}min",
            "candles": candles,
            "count": len(candles),
        }

    def get_vix(self) -> dict:
        """
        Get current India VIX.

        :returns: {vix, change, change_pct}
        """
        return self.get_spot_price("INDIA VIX")

    def get_market_depth(self, symbol: str) -> dict:
        """
        Get full order book depth for a symbol.

        :param symbol: Trading symbol
        :returns: {symbol, bids: [{price, qty}], asks: [{price, qty}]}
        """
        # Resolve token
        results = self._client.search_scrip("NFO", symbol)
        if not results:
            results = self._client.search_scrip("NSE", symbol)
        if not results:
            return {"error": f"Symbol not found: {symbol}"}

        scrip = results[0]
        token = scrip.get("token", "")
        exchange = scrip.get("exch", "NFO")

        resp = self._client.get_quotes(exchange, token)
        if not resp:
            return {"error": f"Failed to get depth for {symbol}"}

        bids = []
        asks = []
        for i in range(1, 6):  # Shoonya provides up to 5 levels
            bp = resp.get(f"bp{i}")
            bq = resp.get(f"bq{i}")
            sp = resp.get(f"sp{i}")
            sq = resp.get(f"sq{i}")
            if bp and bq:
                bids.append({"price": float(bp), "qty": int(bq)})
            if sp and sq:
                asks.append({"price": float(sp), "qty": int(sq)})

        return {
            "symbol": symbol,
            "ltp": float(resp.get("lp", 0)),
            "bids": bids,
            "asks": asks,
            "total_bid_qty": sum(b["qty"] for b in bids),
            "total_ask_qty": sum(a["qty"] for a in asks),
        }
