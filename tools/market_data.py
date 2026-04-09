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

    def __init__(self, shoonya_client, live_feed, nse_tokens: dict, data_recorder=None):
        """
        :param shoonya_client: Authenticated ShoonyaClient
        :param live_feed: LiveFeedManager instance
        :param nse_tokens: Token map from config.NSE_TOKENS
        """
        self._client = shoonya_client
        self._feed = live_feed
        self._tokens = nse_tokens
        self._recorder = data_recorder
        self._candle_cache: dict[tuple, tuple[float, dict]] = {}
        self._daily_ohlc_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._emitted_signals: set[tuple] = set()

    def _resolve_token(self, index: str) -> tuple[str, str]:
        """Resolve index name to (exchange, token)."""
        info = self._tokens.get(index.upper())
        if info:
            return info["exchange"], info["token"]
        raise ValueError(f"Unknown index: {index}. Valid: {list(self._tokens.keys())}")

    def _get_prev_day_ohlc(self, symbol: str) -> Optional[dict]:
        """
        Fetch previous trading day OHLC for a symbol using hourly candles.

        :param symbol: Index name (NIFTY/BANKNIFTY) or trading symbol
        :returns: {high, low, close, open} or None on failure
        """
        import datetime
        import pytz

        IST = pytz.timezone("Asia/Kolkata")

        try:
            token_info = self._tokens.get(symbol.upper())
            if token_info:
                exchange = token_info["exchange"]
                token = token_info["token"]
            else:
                results = self._client.search_scrip("NFO", symbol)
                if not results:
                    results = self._client.search_scrip("NSE", symbol)
                if not results:
                    return None
                exchange = results[0].get("exch", "NSE")
                token = results[0].get("token", "")

            now_ist = datetime.datetime.now(IST)

            # Walk back to find last trading day: skip weekends (Mon=0 … Sun=6)
            # and retry up to 5 days back to handle market holidays.
            result = None
            for days_back in range(1, 6):
                candidate = (now_ist - datetime.timedelta(days=days_back)).date()
                if candidate.weekday() >= 5:   # Saturday or Sunday
                    continue
                day_9am = IST.localize(
                    datetime.datetime(candidate.year, candidate.month, candidate.day, 9, 15, 0)
                )
                day_3pm = IST.localize(
                    datetime.datetime(candidate.year, candidate.month, candidate.day, 15, 30, 0)
                )
                result = self._client.get_time_price_series(
                    exchange=exchange,
                    token=token,
                    starttime=int(day_9am.timestamp()),
                    endtime=int(day_3pm.timestamp()),
                    interval="60",
                )
                if result and isinstance(result, list) and len(result) > 0:
                    break   # found a day with actual data
                result = None

            if not result:
                return None

            highs  = [float(c["inth"]) for c in result if "inth" in c]
            lows   = [float(c["intl"]) for c in result if "intl" in c]
            closes = [float(c["intc"]) for c in result if "intc" in c]
            opens  = [float(c["into"]) for c in result if "into" in c]

            if not highs or not lows or not closes or not opens:
                return None

            return {
                "high":  max(highs),
                "low":   min(lows),
                "close": closes[-1],
                "open":  opens[0],
            }

        except Exception as e:
            logger.warning(f"_get_prev_day_ohlc({symbol}) failed: {e}")
            return None

    def _get_avg_cpr_width(self, symbol: str) -> Optional[float]:
        """
        Compute average CPR width over last 10 trading days.

        :param symbol: Index name
        :returns: Average CPR width as float, or None on failure
        """
        import datetime
        import pytz

        IST = pytz.timezone("Asia/Kolkata")

        try:
            token_info = self._tokens.get(symbol.upper())
            if token_info:
                exchange = token_info["exchange"]
                token = token_info["token"]
            else:
                return None

            widths = []
            now_ist = datetime.datetime.now(IST)

            for days_back in range(1, 15):
                day = now_ist - datetime.timedelta(days=days_back)
                # Skip weekends
                if day.weekday() >= 5:
                    continue

                day_date = day.date()
                day_9am = IST.localize(
                    datetime.datetime(day_date.year, day_date.month, day_date.day, 9, 15, 0)
                )
                day_3pm = IST.localize(
                    datetime.datetime(day_date.year, day_date.month, day_date.day, 15, 30, 0)
                )

                result = self._client.get_time_price_series(
                    exchange=exchange,
                    token=token,
                    starttime=int(day_9am.timestamp()),
                    endtime=int(day_3pm.timestamp()),
                    interval="60",
                )

                if not result or not isinstance(result, list):
                    continue

                highs  = [float(c["inth"]) for c in result if "inth" in c]
                lows   = [float(c["intl"]) for c in result if "intl" in c]
                closes = [float(c["intc"]) for c in result if "intc" in c]

                if not highs or not lows or not closes:
                    continue

                pH = max(highs)
                pL = min(lows)
                pC = closes[-1]
                pivot  = (pH + pL + pC) / 3
                cpr_tc = (pivot + pH) / 2
                cpr_bc = (pivot + pL) / 2
                widths.append(abs(cpr_tc - cpr_bc))

                if len(widths) >= 10:
                    break

            if not widths:
                return None

            return round(sum(widths) / len(widths), 2)

        except Exception as e:
            logger.warning(f"_get_avg_cpr_width({symbol}) failed: {e}")
            return None

    def _get_recent_daily_ohlc(self, symbol: str, days: int = 6) -> list[dict]:
        """
        Build recent daily OHLC rows from hourly Shoonya candles.

        :param symbol: Index name
        :param days: Number of recent trading days to fetch
        :returns: Oldest-to-newest list of {date, open, high, low, close}
        """
        import datetime
        import pytz

        IST = pytz.timezone("Asia/Kolkata")
        cache_key = (symbol.upper(), int(days))
        cached = self._daily_ohlc_cache.get(cache_key)
        if cached and time.time() - cached[0] < 300:
            return cached[1]

        token_info = self._tokens.get(symbol.upper())
        if not token_info:
            return []

        rows = []
        now_ist = datetime.datetime.now(IST)
        for days_back in range(1, 20):
            day = now_ist - datetime.timedelta(days=days_back)
            if day.weekday() >= 5:
                continue
            day_date = day.date()
            day_9am = IST.localize(
                datetime.datetime(day_date.year, day_date.month, day_date.day, 9, 15, 0)
            )
            day_3pm = IST.localize(
                datetime.datetime(day_date.year, day_date.month, day_date.day, 15, 30, 0)
            )
            result = self._client.get_time_price_series(
                exchange=token_info["exchange"],
                token=token_info["token"],
                starttime=int(day_9am.timestamp()),
                endtime=int(day_3pm.timestamp()),
                interval="60",
            )
            if not result or not isinstance(result, list):
                continue
            highs = [float(c["inth"]) for c in result if "inth" in c]
            lows = [float(c["intl"]) for c in result if "intl" in c]
            closes = [float(c["intc"]) for c in result if "intc" in c]
            opens = [float(c["into"]) for c in result if "into" in c]
            if not highs or not lows or not closes or not opens:
                continue
            rows.append({
                "date": day_date.isoformat(),
                "open": opens[0],
                "high": max(highs),
                "low": min(lows),
                "close": closes[-1],
            })
            if len(rows) >= days:
                break

        rows = list(reversed(rows))
        self._daily_ohlc_cache[cache_key] = (time.time(), rows)
        return rows

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
        # Get spot price to determine ATM strike
        spot_data = self.get_spot_price(index)
        spot = spot_data.get("spot_price", 0)

        step = 50 if index.upper() == "NIFTY" else 100
        atm_strike = round(spot / step) * step

        # Resolve an actual NFO trading symbol so GetOptionChain gets a
        # confirmed-valid tsym (bare index name may not be accepted by Shoonya).
        # Strategy: search NFO for "<INDEX><EXPIRY>" to find a futures/options
        # contract, then use its tsym.  Fall back to bare index name if search
        # yields nothing (e.g. market closed / expiry not yet listed).
        nfo_tsym = index.upper()   # conservative default
        search_term = index.upper() + expiry.upper()   # e.g. "NIFTY27APR"
        try:
            hits = self._client.search_scrip("NFO", search_term) or []
            # Prefer a FUT contract as the anchor tsym for option-chain lookup
            fut_hits = [h for h in hits if "FUT" in h.get("tsym", "").upper()]
            if fut_hits:
                nfo_tsym = fut_hits[0]["tsym"]
            elif hits:
                nfo_tsym = hits[0]["tsym"]
        except Exception:
            pass   # fall through with bare index name

        try:
            resp = self._client.get_option_chain(
                exchange="NFO",
                tradingsymbol=nfo_tsym,
                strikeprice=atm_strike,
                count=10,
            )
        except Exception as e:
            return {"error": f"option_chain call failed: {e}"}

        if not resp or not isinstance(resp, dict):
            return {"error": "No response from option chain API"}

        values = resp.get("values", [])
        if not values:
            return {"error": "Empty option chain response"}

        expiry_upper = expiry.upper()
        chain = []
        for item in values:
            try:
                sym = item.get("tsym", "")
                # Filter by expiry substring if provided (e.g. '27MAR', '03APR')
                if expiry_upper and expiry_upper not in sym.upper():
                    continue
                chain.append({
                    "symbol": sym,
                    "strike": float(item.get("strprc", 0)),
                    "type":   item.get("optt", ""),
                    "ltp":    float(item.get("lp", 0)),
                    "bid":    float(item.get("bp1", 0)),
                    "ask":    float(item.get("sp1", 0)),
                    "oi":     int(item.get("oi", 0)),
                    "volume": int(item.get("v", 0)),
                })
            except (ValueError, TypeError):
                continue

        return {
            "index": index.upper(),
            "expiry": expiry.upper(),
            "spot_price": spot,
            "atm_strike": atm_strike,
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

        Source priority (REST-first architecture):
          1. Exact cache hit (55 s TTL)
          2. Larger-count cache hit (same TTL)
          3. Shoonya REST get_time_price_series  ← PRIMARY
          4. LiveFeedManager.get_candles()       ← FALLBACK
             (used when REST returns nothing, e.g. outage)

        :param symbol: Trading symbol (e.g. 'NIFTY')
        :param interval: Candle width in minutes: '1', '3', '5', '15', '30', '60'
        :param count: Number of candles to return
        :returns: dict with keys: symbol, interval, candles, count, source
        """
        import datetime as _dt
        import pytz as _pytz
        _IST = _pytz.timezone("Asia/Kolkata")

        cache_key = (symbol.upper(), str(interval), int(count))
        cached = self._candle_cache.get(cache_key)
        if cached and time.time() - cached[0] < 55:
            return cached[1]
        for (cached_symbol, cached_interval, cached_count), cached in self._candle_cache.items():
            if cached_symbol != symbol.upper() or cached_interval != str(interval):
                continue
            if cached_count < int(count) or time.time() - cached[0] >= 55:
                continue
            cached_result = cached[1]
            result = dict(cached_result)
            candles = cached_result.get("candles", [])[-int(count):]
            result["candles"] = candles
            result["count"] = len(candles)
            result["source"] = f"{cached_result.get('source', 'unknown')}_cache"
            return result

        # Resolve symbol to (exchange, token)
        token_info = self._tokens.get(symbol.upper())
        if token_info:
            token = token_info["token"]
            exchange = token_info["exchange"]
        else:
            results = self._client.search_scrip("NFO", symbol)
            if not results:
                results = self._client.search_scrip("NSE", symbol)
            if not results:
                return {"error": f"Symbol not found: {symbol}"}
            scrip = results[0]
            token = scrip.get("token", "")
            exchange = scrip.get("exch", "NFO")

        # ── PRIMARY: Shoonya REST get_time_price_series ───────────────────────
        interval_secs = int(interval) * 60
        start_time = time.time() - (count + 10) * interval_secs

        raw_candles = self._client.get_time_price_series(
            exchange=exchange,
            token=token,
            starttime=start_time,
            interval=interval,
        )

        if raw_candles:
            candles = []
            for c in raw_candles[:count]:
                # Shoonya REST time is "HH:MM:SS DD-MM-YYYY"; normalise to epoch float.
                raw_time = c.get("time", c.get("ssboe", ""))
                try:
                    ts = _IST.localize(
                        _dt.datetime.strptime(raw_time, "%H:%M:%S %d-%m-%Y")
                    ).timestamp()
                except Exception:
                    ts = float(raw_time) if str(raw_time).isdigit() else 0.0
                candles.append({
                    "time":   ts,
                    "open":   float(c.get("into", 0)),
                    "high":   float(c.get("inth", 0)),
                    "low":    float(c.get("intl", 0)),
                    "close":  float(c.get("intc", 0)),
                    "volume": int(c.get("intv", 0)),
                })
            candles.sort(key=lambda c: c["time"])

            if len(candles) >= min(count, 2):
                result = {
                    "symbol":   symbol,
                    "interval": f"{interval}min",
                    "candles":  candles,
                    "count":    len(candles),
                    "source":   "rest_api",
                }
                self._candle_cache[cache_key] = (time.time(), result)
                logger.debug(
                    "[candles] %s %sm → rest_api (%d bars)", symbol, interval, len(candles)
                )
                return result

        # ── FALLBACK: live-feed candles (WebSocket-aggregated ticks) ─────────
        # Triggered when REST returns nothing (market-hours quirk or
        # connectivity issue).  Strategy logic still runs; the LLM
        # receives a clear candle_source so it can weigh the data accordingly.
        if self._feed:
            live_candles = self._feed.get_candles(token, int(interval), count)
            if live_candles and len(live_candles) >= min(count, 2):
                logger.info(
                    "[candles] %s %sm → live_feed (%d bars) — REST returned no data",
                    symbol, interval, len(live_candles),
                )
                result = {
                    "symbol":   symbol,
                    "interval": f"{interval}min",
                    "candles":  live_candles[-count:],
                    "count":    len(live_candles[-count:]),
                    "source":   "live_feed",
                }
                self._candle_cache[cache_key] = (time.time(), result)
                return result

        logger.warning(
            "[candles] %s %sm → no data (REST empty, live feed empty/disconnected)",
            symbol, interval,
        )
        return {"error": f"No candle data for {symbol} {interval}m", "candles": []}

    def get_indicators(self, symbol: str, interval: str = "5") -> dict:
        """
        Get all technical indicators needed for strategy analysis.
        Computed from REST candles (primary) with live-feed candle fallback.

        Returns:
            candle_source              — 'rest_api' | 'live_feed' | '*_cache'
            ema20, ema50, ema100       — trend EMAs (VP-05, VP-07, VP-15, etc.)
            rsi14                      — RSI 14-period (momentum_pinball, adx_gapper)
            lbr_rsi/daily_lbr_rsi      — daily 3-period RSI of 1-period ROC (Momentum Pinball)
            atr14                      — ATR 14-period for SL sizing
            adx14                      — ADX 14-period trend strength filter
            vwap                       — intraday VWAP (VSA strategies)
            avg_volume_20              — 20-bar avg volume (VSA setups)
            pivot, r1, r2, s1, s2     — daily pivot levels (VP-24)
            cpr_tc, cpr_bc, cpr_width — CPR levels (VP-20)
            current_price              — latest LTP
            ema_stacked_bull           — True if Close > EMA20 > EMA50 > EMA100
            ema_stacked_bear           — True if Close < EMA20 < EMA50 < EMA100
        """
        # Get candles — need at least 100 bars for EMA100
        candle_data = self.get_candles(symbol, interval, count=120)
        if "error" in candle_data or not candle_data.get("candles"):
            return {"error": f"Cannot compute indicators — no candle data for {symbol}"}

        candles = candle_data["candles"]
        candle_source = candle_data.get("source", "unknown")
        if len(candles) < 20:
            return {"error": f"Not enough candles ({len(candles)}) to compute indicators for {symbol}"}

        closes  = [c["close"]  for c in candles]
        highs   = [c["high"]   for c in candles]
        lows    = [c["low"]    for c in candles]
        volumes = [c["volume"] for c in candles]

        def ema(values, period):
            if len(values) < period:
                return None
            k = 2 / (period + 1)
            e = sum(values[:period]) / period
            for v in values[period:]:
                e = v * k + e * (1 - k)
            return round(e, 2)

        def rsi(values, period=14):
            if len(values) < period + 1:
                return None
            gains, losses = [], []
            for i in range(1, len(values)):
                d = values[i] - values[i - 1]
                gains.append(max(d, 0))
                losses.append(max(-d, 0))
            ag = sum(gains[-period:]) / period
            al = sum(losses[-period:]) / period
            if al == 0:
                return 100.0
            rs = ag / al
            return round(100 - 100 / (1 + rs), 2)

        def atr(highs, lows, closes, period=14):
            if len(highs) < period + 1:
                return None
            trs = []
            for i in range(1, len(highs)):
                tr = max(highs[i] - lows[i],
                         abs(highs[i] - closes[i - 1]),
                         abs(lows[i] - closes[i - 1]))
                trs.append(tr)
            return round(sum(trs[-period:]) / period, 2)

        def adx(highs, lows, closes, period=14):
            if len(highs) < period * 2:
                return None
            dms_plus, dms_minus, trs = [], [], []
            for i in range(1, len(highs)):
                up   = highs[i] - highs[i - 1]
                down = lows[i - 1] - lows[i]
                dms_plus.append(up if up > down and up > 0 else 0)
                dms_minus.append(down if down > up and down > 0 else 0)
                trs.append(max(highs[i] - lows[i],
                               abs(highs[i] - closes[i - 1]),
                               abs(lows[i] - closes[i - 1])))
            def wilders(vals, p):
                s = sum(vals[:p])
                result = [s]
                for v in vals[p:]:
                    s = s - s / p + v
                    result.append(s)
                return result
            atr_w = wilders(trs, period)
            dip_w = wilders(dms_plus, period)
            dim_w = wilders(dms_minus, period)
            dxs = []
            for a, p, m in zip(atr_w, dip_w, dim_w):
                if a == 0:
                    continue
                di_plus  = 100 * p / a
                di_minus = 100 * m / a
                denom = di_plus + di_minus
                dxs.append(100 * abs(di_plus - di_minus) / denom if denom else 0)
            if len(dxs) < period:
                return None
            return round(sum(dxs[-period:]) / period, 2)

        def vwap(candles):
            cum_tp_vol = cum_vol = 0.0
            for c in candles:
                tp = (c["high"] + c["low"] + c["close"]) / 3
                cum_tp_vol += tp * c["volume"]
                cum_vol    += c["volume"]
            return round(cum_tp_vol / cum_vol, 2) if cum_vol else None

        # Previous day OHLC for pivot levels — fetch actual previous day data
        prev_day = self._get_prev_day_ohlc(symbol)
        pivot = r1 = r2 = s1 = s2 = cpr_tc = cpr_bc = None
        if prev_day:
            pH, pL, pC = prev_day["high"], prev_day["low"], prev_day["close"]
            pivot  = round((pH + pL + pC) / 3, 2)
            r1     = round(2 * pivot - pL, 2)
            r2     = round(pivot + (pH - pL), 2)
            s1     = round(2 * pivot - pH, 2)
            s2     = round(pivot - (pH - pL), 2)
            cpr_tc = round((pivot + pH) / 2, 2)
            cpr_bc = round((pivot + pL) / 2, 2)
        else:
            pivot = r1 = r2 = s1 = s2 = cpr_tc = cpr_bc = None

        e20  = ema(closes, 20)
        e50  = ema(closes, 50)
        e100 = ema(closes, 100)
        ltp  = closes[-1]

        # LBR/RSI: 3-period RSI of 1-period ROC of closes
        def lbr_rsi(closes, rsi_period=3):
            if len(closes) < rsi_period + 2:
                return None
            rocs = [(closes[i] - closes[i - 1]) / closes[i - 1] * 100 for i in range(1, len(closes))]
            return rsi(rocs, rsi_period)

        daily_lbr_rsi = None
        daily_rows = self._get_recent_daily_ohlc(symbol, days=6)
        if len(daily_rows) >= 5:
            daily_closes = [row["close"] for row in daily_rows]
            daily_lbr_rsi = lbr_rsi(daily_closes)

        # First-hour range (09:15 – 10:15 IST today)
        import datetime
        import pytz as _pytz
        _IST = _pytz.timezone("Asia/Kolkata")
        _today = datetime.datetime.now(_IST).date()
        fh_start = _IST.localize(datetime.datetime(_today.year, _today.month, _today.day, 9, 15)).timestamp()
        fh_end   = _IST.localize(datetime.datetime(_today.year, _today.month, _today.day, 10, 15)).timestamp()
        fh_candles = [c for c in candles if fh_start <= c.get("time", 0) < fh_end]
        fh_high = max(c["high"] for c in fh_candles) if fh_candles else None
        fh_low  = min(c["low"]  for c in fh_candles) if fh_candles else None

        # 80-20 setup fields (uses prev_day already fetched)
        if prev_day:
            pd_range = prev_day["high"] - prev_day["low"]
            pd_open_pct  = (prev_day["open"]  - prev_day["low"]) / pd_range * 100 if pd_range else 0
            pd_close_pct = (prev_day["close"] - prev_day["low"]) / pd_range * 100 if pd_range else 0
            eighty_twenty_long_setup  = pd_open_pct >= 80 and pd_close_pct <= 20
            eighty_twenty_short_setup = pd_open_pct <= 20 and pd_close_pct >= 80
        else:
            eighty_twenty_long_setup = eighty_twenty_short_setup = False

        # CPR width and 10-day average
        cpr_width = round(abs(cpr_tc - cpr_bc), 2) if cpr_tc is not None and cpr_bc is not None else None
        cpr_avg = self._get_avg_cpr_width(symbol)
        cpr_is_narrow = bool(cpr_width and cpr_avg and cpr_width < 0.5 * cpr_avg)

        result = {
            "symbol":          symbol,
            "interval":        f"{interval}min",
            "candle_source":   candle_source,
            "candles_used":    len(candles),
            "current_price":   ltp,
            "ema20":           e20,
            "ema50":           e50,
            "ema100":          e100,
            "ema_stacked_bull": bool(e20 and e50 and e100 and ltp > e20 > e50 > e100),
            "ema_stacked_bear": bool(e20 and e50 and e100 and ltp < e20 < e50 < e100),
            "rsi14":           rsi(closes),
            "lbr_rsi":         daily_lbr_rsi,
            "daily_lbr_rsi":   daily_lbr_rsi,
            "atr14":           atr(highs, lows, closes),
            "adx14":           adx(highs, lows, closes),
            "vwap":            vwap(candles),
            "avg_volume_20":   round(sum(volumes[-20:]) / min(len(volumes), 20), 0),
            "pivot":           pivot,
            "r1":              r1,
            "r2":              r2,
            "s1":              s1,
            "s2":              s2,
            "cpr_tc":          cpr_tc,
            "cpr_bc":          cpr_bc,
            "cpr_width":       cpr_width,
            "cpr_avg_width_10d": cpr_avg,
            "cpr_is_narrow":   cpr_is_narrow,
            "prev_day_high":   prev_day["high"]  if prev_day else None,
            "prev_day_low":    prev_day["low"]   if prev_day else None,
            "prev_day_close":  prev_day["close"] if prev_day else None,
            "prev_day_open":   prev_day["open"]  if prev_day else None,
            "first_hour_high": fh_high,
            "first_hour_low":  fh_low,
            "eighty_twenty_long_setup":  eighty_twenty_long_setup,
            "eighty_twenty_short_setup": eighty_twenty_short_setup,
        }
        if self._recorder:
            self._recorder.record_indicators(symbol, interval, result)
        return result

    def get_strategy_signals(
        self,
        symbol: str = "BOTH",
        lookback_bars: int = 5,
    ) -> dict:
        """
        Deterministically scan recent NIFTY/BANKNIFTY candles for approved
        price-action strategy setups. This is a guardrail against the LLM
        overlooking candle-pattern rules in raw OHLC data.

        Volume-dependent VSA/VPA strategies still require real broker volume;
        index feeds with zero volume are not treated as valid volume signals.
        """
        import datetime
        import math
        import pytz

        symbols = ["NIFTY", "BANKNIFTY"] if symbol.upper() == "BOTH" else [symbol.upper()]
        intervals = ["3", "5", "15"]
        signals = []
        notes = []
        IST = pytz.timezone("Asia/Kolkata")

        def ema_series(values, period):
            result = [None] * len(values)
            if len(values) < period:
                return result
            k = 2 / (period + 1)
            e = sum(values[:period]) / period
            result[period - 1] = e
            for i in range(period, len(values)):
                e = values[i] * k + e * (1 - k)
                result[i] = e
            return result

        def candle_stats(c):
            body = abs(c["close"] - c["open"])
            rng = c["high"] - c["low"]
            upper = c["high"] - max(c["open"], c["close"])
            lower = min(c["open"], c["close"]) - c["low"]
            return body, rng, upper, lower

        def is_power_candle(c, recent_ranges):
            body, rng, _, _ = candle_stats(c)
            avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else rng
            return rng > 0 and body > 0.75 * rng and rng > 1.3 * avg_range

        def target_for(c, direction, stop_loss):
            if stop_loss is None:
                return None
            if direction == "BUY":
                return c["close"] + 2 * abs(c["close"] - stop_loss)
            return c["close"] - 2 * abs(stop_loss - c["close"])

        def pct_close(c):
            _, rng, _, _ = candle_stats(c)
            return (c["close"] - c["low"]) / rng if rng else 0.5

        def swing_highs(candles):
            highs = []
            for idx in range(1, len(candles) - 1):
                if candles[idx]["high"] > candles[idx - 1]["high"] and candles[idx]["high"] > candles[idx + 1]["high"]:
                    highs.append((idx, candles[idx]))
            return highs

        def swing_lows(candles):
            lows = []
            for idx in range(1, len(candles) - 1):
                if candles[idx]["low"] < candles[idx - 1]["low"] and candles[idx]["low"] < candles[idx + 1]["low"]:
                    lows.append((idx, candles[idx]))
            return lows

        # Mutable container so add_signal always picks up the current interval's source
        # without needing it passed explicitly through 34+ call sites.
        _active_src: list = ["unknown"]

        def add_signal(sym, interval, candle, strategy, direction, reason, stop_loss, target,
                       requires_volume_confirmation=False, candle_source=None):
            # Daily-first-hour signals must use a date-based key (not candle time) because
            # latest["time"] changes every 3 min, which would re-emit the same signal ~104×/day.
            if interval == "daily-first-hour":
                sig_key = (sym, "daily-first-hour", strategy, direction, today_date)
            else:
                sig_key = (sym, str(interval), strategy, direction, candle["time"])
            if sig_key in self._emitted_signals:
                return
            self._emitted_signals.add(sig_key)
            tool_interval = "3" if interval == "daily-first-hour" else str(interval)
            signal_timeframe = "daily-first-hour" if interval == "daily-first-hour" else f"{interval}min"
            signals.append({
                "symbol": sym,
                "interval": tool_interval,
                "signal_timeframe": signal_timeframe,
                "time": datetime.datetime.fromtimestamp(candle["time"], IST).strftime("%H:%M:%S"),
                "strategy": strategy,
                "direction": direction,
                "entry_reference": round(candle["close"], 2),
                "stop_loss": round(stop_loss, 2) if stop_loss is not None else None,
                "target": round(target, 2) if target is not None else None,
                "requires_volume_confirmation": requires_volume_confirmation,
                "candle_source": candle_source if candle_source is not None else _active_src[0],
                "reason": reason,
            })

        today_date = datetime.datetime.now(IST).strftime("%Y-%m-%d")

        for sym in symbols:
            # Use the cached _get_recent_daily_ohlc (5-min TTL) instead of the
            # uncached _get_prev_day_ohlc so we avoid 2 raw REST calls per minute.
            daily_rows_for_pivot = self._get_recent_daily_ohlc(sym, days=6)
            prev_day = daily_rows_for_pivot[-1] if daily_rows_for_pivot else None
            pivot_levels = {}
            cpr_tc = cpr_bc = cpr_width = None
            if prev_day:
                pH, pL, pC = prev_day["high"], prev_day["low"], prev_day["close"]
                pivot = (pH + pL + pC) / 3
                pivot_levels = {
                    "P": pivot,
                    "R1": 2 * pivot - pL,
                    "S1": 2 * pivot - pH,
                    "R2": pivot + (pH - pL),
                    "S2": pivot - (pH - pL),
                }
                cpr_tc = (pivot + pH) / 2
                cpr_bc = (pivot + pL) / 2
                cpr_width = abs(cpr_tc - cpr_bc)
            cpr_avg = self._get_avg_cpr_width(sym)
            cpr_is_narrow = bool(cpr_width and cpr_avg and cpr_width < 0.5 * cpr_avg)

            # Daily-level first-hour strategies: Momentum Pinball, 80-20, ADX Gapper.
            daily_rows = self._get_recent_daily_ohlc(sym, days=6)
            daily_lbr = None
            if len(daily_rows) >= 5:
                daily_closes = [row["close"] for row in daily_rows]
                rocs = [
                    (daily_closes[i] - daily_closes[i - 1]) / daily_closes[i - 1] * 100
                    for i in range(1, len(daily_closes))
                    if daily_closes[i - 1]
                ]
                if len(rocs) >= 4:
                    gains, losses = [], []
                    for i in range(1, len(rocs)):
                        d = rocs[i] - rocs[i - 1]
                        gains.append(max(d, 0))
                        losses.append(max(-d, 0))
                    avg_gain = sum(gains[-3:]) / 3
                    avg_loss = sum(losses[-3:]) / 3
                    daily_lbr = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

            daily_candles_data = self.get_candles(sym, "3", count=220)
            daily_candles_src = daily_candles_data.get("source", "unknown") if isinstance(daily_candles_data, dict) else "unknown"
            _active_src[0] = daily_candles_src  # used by daily-first-hour add_signal calls below
            daily_candles = daily_candles_data.get("candles", []) if isinstance(daily_candles_data, dict) else []
            if prev_day and daily_candles:
                first_hour = []
                latest = daily_candles[-1]
                for dc in daily_candles:
                    dt = datetime.datetime.fromtimestamp(dc["time"], IST)
                    if (dt.hour == 9 and 15 <= dt.minute) or (dt.hour == 10 and dt.minute < 15):
                        first_hour.append(dc)
                today_open = daily_candles[0]["open"]
                today_low = min(dc["low"] for dc in daily_candles)
                today_high = max(dc["high"] for dc in daily_candles)
                first_hour_done = bool(first_hour) and datetime.datetime.fromtimestamp(latest["time"], IST).time() >= datetime.time(10, 15)
                if first_hour and first_hour_done:
                    fh_high = max(dc["high"] for dc in first_hour)
                    fh_low = min(dc["low"] for dc in first_hour)
                    if daily_lbr is not None and daily_lbr < 30 and latest["close"] > fh_high:
                        add_signal(sym, "daily-first-hour", latest, "Momentum Pinball", "BUY",
                                   f"Daily LBR/RSI {daily_lbr:.1f} < 30 and price broke first-hour high", fh_low, latest["close"] + 2 * (latest["close"] - fh_low),
                                   candle_source=daily_candles_src)
                    if daily_lbr is not None and daily_lbr > 70 and latest["close"] < fh_low:
                        add_signal(sym, "daily-first-hour", latest, "Momentum Pinball", "SELL",
                                   f"Daily LBR/RSI {daily_lbr:.1f} > 70 and price broke first-hour low", fh_high, latest["close"] - 2 * (fh_high - latest["close"]),
                                   candle_source=daily_candles_src)

                pd_range = prev_day["high"] - prev_day["low"]
                if pd_range:
                    pd_open_pct = (prev_day["open"] - prev_day["low"]) / pd_range * 100
                    pd_close_pct = (prev_day["close"] - prev_day["low"]) / pd_range * 100
                    test_buffer = 15 if sym == "BANKNIFTY" else 5
                    if pd_open_pct >= 80 and pd_close_pct <= 20 and today_low <= prev_day["low"] - test_buffer and latest["close"] > prev_day["low"]:
                        add_signal(sym, "daily-first-hour", latest, "80-20 Reversal", "BUY",
                                   "Previous day opened in top 20%, closed in bottom 20%, today tested below prior low and reclaimed it",
                                   today_low, latest["close"] + 2 * (latest["close"] - today_low),
                                   candle_source=daily_candles_src)
                    if pd_open_pct <= 20 and pd_close_pct >= 80 and today_high >= prev_day["high"] + test_buffer and latest["close"] < prev_day["high"]:
                        add_signal(sym, "daily-first-hour", latest, "80-20 Reversal", "SELL",
                                   "Previous day opened in bottom 20%, closed in top 20%, today tested above prior high and rejected it",
                                   today_high, latest["close"] - 2 * (today_high - latest["close"]),
                                   candle_source=daily_candles_src)

                if today_open < prev_day["low"] and latest["close"] > prev_day["low"]:
                    add_signal(sym, "daily-first-hour", latest, "ADX Gapper", "BUY",
                               "Gap below previous low reclaimed; confirm ADX>30 and +DI>-DI before trading",
                               today_low, latest["close"] + 2 * (latest["close"] - today_low),
                               candle_source=daily_candles_src)
                if today_open > prev_day["high"] and latest["close"] < prev_day["high"]:
                    add_signal(sym, "daily-first-hour", latest, "ADX Gapper", "SELL",
                               "Gap above previous high rejected; confirm ADX>30 and -DI>+DI before trading",
                               today_high, latest["close"] - 2 * (today_high - latest["close"]),
                               candle_source=daily_candles_src)

            for interval in intervals:
                candle_count = 220 if interval == "3" else 140
                data = self.get_candles(sym, interval, count=candle_count)
                intraday_src = data.get("source", "unknown") if isinstance(data, dict) else "unknown"
                _active_src[0] = intraday_src  # picked up by add_signal() closure
                candles = data.get("candles", []) if isinstance(data, dict) else []
                if len(candles) < 25:
                    notes.append(f"{sym} {interval}m: insufficient candles ({len(candles)}) [{intraday_src}]")
                    continue

                closes = [c["close"] for c in candles]
                ranges = [c["high"] - c["low"] for c in candles]
                volumes = [c.get("volume", 0) for c in candles]
                e20_all = ema_series(closes, 20)
                e50_all = ema_series(closes, 50)
                e100_all = ema_series(closes, 100)

                start = max(1, len(candles) - max(1, lookback_bars))
                for i in range(start, len(candles)):
                    c = candles[i]
                    body, rng, upper, lower = candle_stats(c)
                    if rng <= 0:
                        continue
                    bull = c["close"] > c["open"]
                    bear = c["close"] < c["open"]
                    e20, e50, e100 = e20_all[i], e50_all[i], e100_all[i]
                    avg_vol20 = sum(volumes[max(0, i - 20):i]) / min(i, 20) if i else 0
                    has_volume = avg_vol20 > 0
                    tp_vol = sum(
                        ((vc["high"] + vc["low"] + vc["close"]) / 3) * vc.get("volume", 0)
                        for vc in candles[:i + 1]
                    )
                    cum_vol = sum(vc.get("volume", 0) for vc in candles[:i + 1])
                    intraday_vwap = tp_vol / cum_vol if cum_vol else None

                    # VP-05: 3-EMA trend pullback.
                    if e20 and e50 and e100:
                        bull_stack = c["close"] > e20 > e50 > e100
                        bear_stack = c["close"] < e20 < e50 < e100
                        touched_ema20_long = c["low"] <= e20 <= max(c["open"], c["close"]) and c["close"] > e20
                        touched_ema50_long = c["low"] <= e50 <= max(c["open"], c["close"]) and c["close"] > e50
                        touched_ema20_short = min(c["open"], c["close"]) <= e20 <= c["high"] and c["close"] < e20
                        touched_ema50_short = min(c["open"], c["close"]) <= e50 <= c["high"] and c["close"] < e50
                        if bull_stack and lower > body and (touched_ema20_long or touched_ema50_long):
                            sl = e50 if touched_ema20_long else e100
                            add_signal(sym, interval, c, "VP-05 3EMA Trend", "BUY",
                                       "EMA stack bullish and pin bar rejected EMA20/EMA50", sl, c["close"] + 2 * abs(c["close"] - sl))
                        if bear_stack and upper > body and (touched_ema20_short or touched_ema50_short):
                            sl = e50 if touched_ema20_short else e100
                            add_signal(sym, interval, c, "VP-05 3EMA Trend", "SELL",
                                       "EMA stack bearish and pin bar rejected EMA20/EMA50", sl, c["close"] - 2 * abs(sl - c["close"]))

                    # VP-07: wicks pullback in EMA20 direction.
                    if i >= 10 and e20:
                        masters = candles[i - 10:i]
                        if bull and c["close"] > e20:
                            for m in masters:
                                m_body, _, _, m_lower = candle_stats(m)
                                if m["close"] > m["open"] and m_body > 0 and m_lower > 2 * m_body and c["close"] > m["close"]:
                                    add_signal(sym, interval, c, "VP-07 Wicks Pullback", "BUY",
                                               "Bullish follow-through above lower-wick master candle and EMA20; confirm volume before trading", m["low"], c["close"] + 2 * (c["close"] - m["low"]), True)
                                    break
                        if bear and c["close"] < e20:
                            for m in masters:
                                m_body, _, m_upper, _ = candle_stats(m)
                                if m["close"] < m["open"] and m_body > 0 and m_upper > 2 * m_body and c["close"] < m["close"]:
                                    add_signal(sym, interval, c, "VP-07 Wicks Pullback", "SELL",
                                               "Bearish follow-through below upper-wick master candle and EMA20; confirm volume before trading", m["high"], c["close"] - 2 * (m["high"] - c["close"]), True)
                                    break

                    # VP-24: BANKNIFTY pivot bounce/rejection on preferred timeframes.
                    if sym == "BANKNIFTY" and interval in ("3", "5"):
                        for level_name, level in pivot_levels.items():
                            if not level or math.isnan(level):
                                continue
                            near = abs(c["close"] - level) / level <= 0.001
                            if near and level_name in ("P", "S1", "S2") and bull and lower > body:
                                sl = c["low"] * 0.998
                                add_signal(sym, interval, c, f"VP-24 Pivot Bounce {level_name}", "BUY",
                                           f"Bullish lower-wick bounce within 0.1% of {level_name}", sl, c["close"] + 2 * (c["close"] - sl))
                            if near and level_name in ("P", "R1", "R2") and bear and upper > body:
                                sl = c["high"] * 1.002
                                add_signal(sym, interval, c, f"VP-24 Pivot Rejection {level_name}", "SELL",
                                           f"Bearish upper-wick rejection within 0.1% of {level_name}", sl, c["close"] - 2 * (sl - c["close"]))

                    # VP-20: BANKNIFTY narrow-CPR reversal.
                    if sym == "BANKNIFTY" and interval == "3" and cpr_is_narrow and cpr_tc and cpr_bc and cpr_width:
                        if abs(c["close"] - cpr_bc) <= 0.5 * cpr_width and bull and lower > body:
                            sl = cpr_bc - cpr_width
                            add_signal(sym, interval, c, "VP-20 CPR Reversal", "BUY",
                                       "Narrow CPR day; bullish lower-wick bounce near BC", sl, c["close"] + 2 * (c["close"] - sl))
                        if abs(c["close"] - cpr_tc) <= 0.5 * cpr_width and bear and upper > body:
                            sl = cpr_tc + cpr_width
                            add_signal(sym, interval, c, "VP-20 CPR Reversal", "SELL",
                                       "Narrow CPR day; bearish upper-wick rejection near TC", sl, c["close"] - 2 * (sl - c["close"]))

                    # VP-01/02: counter trap against the largest recent opposite-color candle.
                    if i >= 10 and e20:
                        recent = candles[i - 10:i]
                        green_bodies = [(abs(rc["close"] - rc["open"]), rc) for rc in recent if rc["close"] > rc["open"]]
                        red_bodies = [(abs(rc["close"] - rc["open"]), rc) for rc in recent if rc["close"] < rc["open"]]
                        if green_bodies and c["close"] < e20 and bear:
                            _, trap = max(green_bodies, key=lambda item: item[0])
                            if c["close"] < trap["close"]:
                                add_signal(sym, interval, c, "VP-01 Counter Bull Trap", "SELL",
                                           "Price below EMA20; bearish candle closed below largest recent green candle close",
                                           c["high"], target_for(c, "SELL", c["high"]))
                        if sym == "NIFTY" and interval == "3" and red_bodies and c["close"] > e20 and bull:
                            _, trap = max(red_bodies, key=lambda item: item[0])
                            if c["close"] > trap["close"]:
                                add_signal(sym, interval, c, "VP-02 Counter Bear Trap", "BUY",
                                           "NIFTY 3m only; price above EMA20 and green candle reclaimed largest recent red candle close",
                                           c["low"], target_for(c, "BUY", c["low"]))

                    # VP-08: V-reversal after 5+ bearish candles. Volume remains a required confirmation.
                    if bull and i >= 5:
                        run = 0
                        for j in range(i - 1, -1, -1):
                            if candles[j]["close"] < candles[j]["open"]:
                                run += 1
                            else:
                                break
                        if run >= 5:
                            last_red = candles[i - 1]
                            move = candles[i - run:i]
                            if c["high"] > last_red["high"]:
                                sl = min(mc["low"] for mc in move)
                                add_signal(sym, interval, c, "VP-08 V-Reversal", "BUY",
                                           "5+ bearish candles followed by green candle breaking the last red high; confirm capitulation volume",
                                           sl, target_for(c, "BUY", sl), True)

                    # VP-09/16/17: power-candle base and 50% retracement setups.
                    if i >= 15:
                        power_window = candles[i - 15:i]
                        for pc_idx, pc in enumerate(power_window):
                            pc_global_idx = i - 15 + pc_idx
                            pc_recent_ranges = ranges[max(0, pc_global_idx - 5):pc_global_idx]
                            if not is_power_candle(pc, pc_recent_ranges):
                                continue
                            pc_bull = pc["close"] > pc["open"]
                            pc_bear = pc["close"] < pc["open"]
                            midpoint = (pc["open"] + pc["close"]) / 2
                            if pc_bull and c["low"] <= pc["low"] <= c["close"] and lower > body:
                                add_signal(sym, interval, c, "VP-09 Power Candle Pullback", "BUY",
                                           "Pullback rejected the low of a recent bullish power candle",
                                           c["low"], target_for(c, "BUY", c["low"]))
                            if pc_bear and c["close"] <= pc["high"] <= c["high"] and upper > body:
                                add_signal(sym, interval, c, "VP-09 Power Candle Pullback", "SELL",
                                           "Rally rejected the high of a recent bearish power candle",
                                           c["high"], target_for(c, "SELL", c["high"]))
                            if pc_bull and interval == "3" and e20 and c["low"] <= midpoint <= c["close"] and bull and c["close"] > e20:
                                add_signal(sym, interval, c, "VP-16 GCR Green Candle Retracement", "BUY",
                                           "3m bullish power candle 50% body retracement reclaimed above EMA20",
                                           pc["low"], target_for(c, "BUY", pc["low"]))
                            if pc_bear and sym == "NIFTY" and interval in ("3", "5") and e20 and c["close"] <= midpoint <= c["high"] and bear and c["close"] < e20:
                                add_signal(sym, interval, c, "VP-17 RCR Red Candle Retracement", "SELL",
                                           "NIFTY 3m/5m bearish power candle 50% body retracement rejected below EMA20",
                                           pc["high"], target_for(c, "SELL", pc["high"]))

                    # VP-10: first 09:15 candle open breakout, best in first 45 minutes.
                    first_candle = candles[0]
                    first_dt = datetime.datetime.fromtimestamp(first_candle["time"], IST).time()
                    current_dt = datetime.datetime.fromtimestamp(c["time"], IST).time()
                    if first_dt.hour == 9 and first_dt.minute == 15 and current_dt <= datetime.time(10, 0):
                        recent_ranges = ranges[max(0, i - 5):i]
                        if i > 0 and is_power_candle(c, recent_ranges):
                            first_open = first_candle["open"]
                            if first_candle["close"] > first_candle["open"] and bull and c["close"] > first_open:
                                add_signal(sym, interval, c, "VP-10 First Candle Open", "BUY",
                                           "Bullish power candle closed above the 09:15 candle open",
                                           c["low"], target_for(c, "BUY", c["low"]))
                            if bear and c["close"] < first_open:
                                add_signal(sym, interval, c, "VP-10 First Candle Open", "SELL",
                                           "Bearish power candle closed below the 09:15 candle open",
                                           c["high"], target_for(c, "SELL", c["high"]))

                    # VP-14/15: Morning/Evening Star 3-candle reversals.
                    if i >= 2 and e20:
                        c1, c2, c3 = candles[i - 2], candles[i - 1], c
                        c1_body, c1_rng, _, _ = candle_stats(c1)
                        c2_body, c2_rng, _, _ = candle_stats(c2)
                        c3_body, _, _, _ = candle_stats(c3)
                        c2_small = c2_rng > 0 and c2_body < 0.3 * c2_rng
                        if c1["close"] < c1["open"] and c2_small and bull and c3_body > 0.5 * c1_body and c["close"] > e20:
                            sl = min(c1["low"], c2["low"])
                            add_signal(sym, interval, c, "VP-14 Morning Star", "BUY",
                                       "3-candle Morning Star; use as support confluence, not standalone",
                                       sl, target_for(c, "BUY", sl))
                        if c1["close"] > c1["open"] and c2_small and bear and c3_body > 0.5 * c1_body and c["close"] < e20:
                            sl = max(c1["high"], c2["high"])
                            add_signal(sym, interval, c, "VP-15 Evening Star", "SELL",
                                       "3-candle Evening Star closed below EMA20",
                                       sl, target_for(c, "SELL", sl))

                    # VP-18/19: double top / double bottom neckline breaks.
                    if i >= 20:
                        window = candles[i - 20:i + 1]
                        prior = window[:-1]
                        highs_found = swing_highs(prior)
                        lows_found = swing_lows(prior)
                        for (idx1, h1), (idx2, h2) in zip(highs_found, highs_found[1:]):
                            if abs(h1["high"] - h2["high"]) / max(h1["high"], h2["high"]) <= 0.005:
                                neckline = min(w["low"] for w in prior[idx1:idx2 + 1])
                                if bear and c["close"] < neckline:
                                    sl = max(h1["high"], h2["high"])
                                    add_signal(sym, interval, c, "VP-18 M-Pattern Double Top", "SELL",
                                               "Two swing highs within 0.5% followed by bearish neckline break",
                                               sl, target_for(c, "SELL", sl))
                                    break
                        for (idx1, l1), (idx2, l2) in zip(lows_found, lows_found[1:]):
                            if abs(l1["low"] - l2["low"]) / min(l1["low"], l2["low"]) <= 0.005:
                                neckline = max(w["high"] for w in prior[idx1:idx2 + 1])
                                if bull and c["close"] > neckline:
                                    sl = min(l1["low"], l2["low"])
                                    add_signal(sym, interval, c, "VP-19 W-Pattern Double Bottom", "BUY",
                                               "Two swing lows within 0.5% followed by bullish neckline break",
                                               sl, target_for(c, "BUY", sl))
                                    break

                    # VP-21: extreme candle reversal, systematic mainly on BANKNIFTY 15m.
                    if i >= 21 and interval == "15":
                        prev = candles[i - 1]
                        prev_range = prev["high"] - prev["low"]
                        avg20 = sum(ranges[i - 21:i - 1]) / 20
                        if avg20 and prev_range > 2.5 * avg20 and prev["close"] < prev["open"] and bull and c["close"] > prev["close"]:
                            add_signal(sym, interval, c, "VP-21 Extreme Candle Reversal", "BUY",
                                       "Previous 15m bearish candle range > 2.5x average; current bullish candle reclaimed its close",
                                       c["low"], target_for(c, "BUY", c["low"]))
                        if avg20 and prev_range > 2.5 * avg20 and prev["close"] > prev["open"] and bear and c["close"] < prev["close"]:
                            add_signal(sym, interval, c, "VP-21 Extreme Candle Reversal", "SELL",
                                       "Previous 15m bullish candle range > 2.5x average; current bearish candle lost its close",
                                       c["high"], target_for(c, "SELL", c["high"]))

                    # VP-22: NIFTY supply-zone rejection.
                    if sym == "NIFTY" and i >= 45 and interval in ("3", "15"):
                        zone_source = candles[max(0, i - 45):i - 5]
                        prior_swings = swing_highs(zone_source)
                        if prior_swings:
                            _, zone_bar = max(prior_swings, key=lambda item: item[1]["high"])
                            zone_top = zone_bar["high"]
                            zone_bottom = zone_top * 0.998
                            if zone_bottom <= c["close"] <= zone_top and bear and upper > body:
                                sl = zone_top * 1.002
                                add_signal(sym, interval, c, "VP-22 Supply Zone Reversal", "SELL",
                                           "NIFTY returned to highest prior swing-high supply zone and printed upper-wick rejection",
                                           sl, target_for(c, "SELL", sl))

                    # VPA/VSA strategies: only surface when broker volume is present.
                    if has_volume:
                        high_vol = c.get("volume", 0) >= 1.5 * avg_vol20
                        ultra_high_vol = c.get("volume", 0) >= 2.0 * avg_vol20
                        avg_range20 = sum(ranges[max(0, i - 20):i]) / min(i, 20) if i else rng
                        wide_spread = rng > 1.3 * avg_range20
                        narrow_spread = rng < 0.7 * avg_range20

                        if interval == "5" and intraday_vwap and i >= 1:
                            for hm in candles[max(0, i - 3):i]:
                                hm_body, hm_rng, _, hm_lower = candle_stats(hm)
                                hm_avg_vol = avg_vol20
                                hm_high_vol = hm.get("volume", 0) >= 1.5 * hm_avg_vol
                                if hm_rng > 0 and hm_lower > 0.4 * hm_rng and hm_body < 0.4 * hm_rng and hm_high_vol:
                                    if c["close"] < hm["low"] and c["close"] > intraday_vwap:
                                        add_signal(sym, interval, c, "VPA Hanging Man", "SELL",
                                                   "Confirmed break below high-volume Hanging Man low; verify broker volume and VWAP context",
                                                   hm["high"], target_for(c, "SELL", hm["high"]), True)
                                        break

                            nd = candles[i - 1]
                            _, nd_rng, _, _ = candle_stats(nd)
                            if intraday_vwap and c["close"] < intraday_vwap and nd["close"] > nd["open"]:
                                if nd_rng < avg_range20 and nd.get("volume", 0) <= 0.7 * avg_vol20 and bear and c["close"] < nd["close"]:
                                    add_signal(sym, interval, c, "VPA No Demand", "SELL",
                                               "Low-volume rally/no-demand bar followed by bearish confirmation below VWAP",
                                               max(nd["high"], c["high"]), target_for(c, "SELL", max(nd["high"], c["high"])), True)

                        if interval in ("5", "15") and wide_spread and ultra_high_vol:
                            close_pct = pct_close(c)
                            if bull and 0.35 <= close_pct <= 0.65:
                                add_signal(sym, interval, c, "VSA Buying Climax", "SELL",
                                           "Wide-spread up bar on ultra-high volume with middle close; confirm distribution before trading",
                                           c["high"], target_for(c, "SELL", c["high"]), True)

                        if interval == "15":
                            close_pct = pct_close(c)
                            if bear and narrow_spread and ultra_high_vol and e20 and c["close"] < e20:
                                add_signal(sym, interval, c, "VSA Bag Holding", "BUY",
                                           "Narrow-spread down bar on ultra-high volume in downtrend; confirm institutional absorption",
                                           c["low"], target_for(c, "BUY", c["low"]), True)
                            if c["high"] > candles[i - 1]["high"] and high_vol and close_pct <= 0.3:
                                add_signal(sym, interval, c, "VSA Upthrust", "SELL",
                                           "New high on high volume closed in bottom 30%; confirm upthrust supply",
                                           c["high"], target_for(c, "SELL", c["high"]), True)
                            if c["high"] > candles[i - 1]["high"] and high_vol and c["close"] < candles[i - 1]["close"]:
                                add_signal(sym, interval, c, "VSA Hidden Upthrust", "SELL",
                                           "New high on high volume collapsed below previous close; confirm hidden upthrust",
                                           c["high"], target_for(c, "SELL", c["high"]), True)
                            if bear and wide_spread and ultra_high_vol and close_pct >= 0.7:
                                add_signal(sym, interval, c, "VSA Shakeout Intraday", "BUY",
                                           "Wide-spread down bar on ultra-high volume closed near high; confirm next-bar strength",
                                           c["low"], target_for(c, "BUY", c["low"]), True)

                # VP-13 open drive is valid only from first 3 candles on 5m/15m.
                if interval in ("5", "15") and len(candles) >= 3:
                    first3 = candles[:3]
                    first3_dt = [datetime.datetime.fromtimestamp(c["time"], IST).time() for c in first3]
                    if first3_dt[0].hour == 9 and first3_dt[0].minute == 15:
                        power = []
                        for j, fc in enumerate(first3):
                            f_body, f_rng, _, _ = candle_stats(fc)
                            prev_ranges = ranges[max(0, j - 5):j]
                            avg_range = sum(prev_ranges) / len(prev_ranges) if prev_ranges else f_rng
                            power.append(f_rng > 0 and f_body > 0.75 * f_rng and f_rng > 1.3 * avg_range)
                        if all(fc["close"] > fc["open"] for fc in first3) and any(power):
                            c = first3[-1]
                            sl = min(fc["low"] for fc in first3)
                            add_signal(sym, interval, c, "VP-13 Open Drive", "BUY",
                                       "First three candles bullish and at least one power candle", sl, c["close"] + 2 * (c["close"] - sl))
                        if all(fc["close"] < fc["open"] for fc in first3) and any(power):
                            c = first3[-1]
                            sl = max(fc["high"] for fc in first3)
                            add_signal(sym, interval, c, "VP-13 Open Drive", "SELL",
                                       "First three candles bearish and at least one power candle", sl, c["close"] - 2 * (sl - c["close"]))

        result = {
            "signals": signals,
            "count": len(signals),
            "lookback_bars": lookback_bars,
            "notes": notes,
            "volume_dependent_strategies": "VSA/VPA strategies require real broker volume; do not validate them from zero-volume index feeds.",
        }
        if self._recorder:
            self._recorder.record_strategy_signals(result)
        return result

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
