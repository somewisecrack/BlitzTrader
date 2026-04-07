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
            yesterday = now_ist - datetime.timedelta(seconds=86400)
            yesterday_date = yesterday.date()

            yesterday_9am = IST.localize(
                datetime.datetime(yesterday_date.year, yesterday_date.month, yesterday_date.day, 9, 15, 0)
            )
            yesterday_3pm = IST.localize(
                datetime.datetime(yesterday_date.year, yesterday_date.month, yesterday_date.day, 15, 30, 0)
            )

            result = self._client.get_time_price_series(
                exchange=exchange,
                token=token,
                starttime=int(yesterday_9am.timestamp()),
                endtime=int(yesterday_3pm.timestamp()),
                interval="60",
            )

            if not result or not isinstance(result, list) or len(result) == 0:
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

        exchange, _ = self._resolve_token(index)

        try:
            resp = self._client.get_option_chain(
                exchange=exchange,
                tradingsymbol=index.upper(),
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

        chain = []
        for item in values:
            try:
                chain.append({
                    "symbol": item.get("tsym", ""),
                    "strike": float(item.get("strprc", 0)),
                    "type": item.get("optt", ""),
                    "ltp": float(item.get("lp", 0)),
                    "bid": float(item.get("bp1", 0)),
                    "ask": float(item.get("sp1", 0)),
                    "oi": int(item.get("oi", 0)),
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

        :param symbol: Trading symbol
        :param interval: '1', '5', '15', '30', '60' (minutes)
        :param count: Number of candles to return
        :returns: {symbol, interval, candles: [{time, open, high, low, close, volume}]}
        """
        # Resolve symbol to (exchange, token)
        # For known indices use hardcoded tokens (searchscrip doesn't work for indices)
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

        # Primary: live candles built from WebSocket ticks
        if self._feed:
            live_candles = self._feed.get_candles(token, int(interval), count)
            if live_candles and len(live_candles) >= min(count, 2):
                return {
                    "symbol": symbol,
                    "interval": f"{interval}min",
                    "candles": live_candles[-count:],
                    "count": len(live_candles[-count:]),
                    "source": "live_feed",
                }

        # Fallback: REST historical data
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
            "source": "rest_api",
        }

    def get_indicators(self, symbol: str, interval: str = "5") -> dict:
        """
        Get all technical indicators needed for strategy analysis.
        Computed from live candle data (WebSocket feed).

        Returns:
            ema20, ema50, ema100       — trend EMAs (VP-05, VP-07, VP-15, etc.)
            rsi14                      — RSI 14-period (momentum_pinball, adx_gapper)
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
            "candles_used":    len(candles),
            "current_price":   ltp,
            "ema20":           e20,
            "ema50":           e50,
            "ema100":          e100,
            "ema_stacked_bull": bool(e20 and e50 and e100 and ltp > e20 > e50 > e100),
            "ema_stacked_bear": bool(e20 and e50 and e100 and ltp < e20 < e50 < e100),
            "rsi14":           rsi(closes),
            "lbr_rsi":         lbr_rsi(closes),
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
