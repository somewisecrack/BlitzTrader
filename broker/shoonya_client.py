"""
broker/shoonya_client.py — Shoonya API client for BlitzTrader.
Ported from SpreadTrader's auth.py + shoonya_client.py.

Handles: authentication (TOTP), REST quotes, historical data,
scrip search, option chain, and WebSocket lifecycle.
"""
import logging
import time
from typing import Optional

import pyotp

logger = logging.getLogger("BlitzTrader.ShoonyaClient")

# NFO exchange constant (avoid circular import from config)
NFO_EXCHANGE = "NFO"


class ShoonyaClient:
    """
    Thin wrapper around the authenticated NorenApi object.
    Provides REST market data + WebSocket lifecycle management.
    """

    def __init__(self):
        self._api = None
        self._logged_in = False

    # ──────────────────────────────────────────────────────────
    #   AUTHENTICATION
    # ──────────────────────────────────────────────────────────

    def login(
        self,
        user_id: str,
        password: str,
        totp_secret: str,
        api_key: str,
        vendor_code: str,
        imei: str,
    ) -> tuple[bool, str]:
        """
        Log in to Shoonya API with TOTP.
        Returns (success: bool, message: str).
        """
        try:
            from NorenRestApiPy.NorenApi import NorenApi

            class _ShoonyaApi(NorenApi):
                def __init__(self):
                    super().__init__(
                        host="https://api.shoonya.com/NorenWClientTP/",
                        websocket="wss://api.shoonya.com/NorenWSTP/",
                    )

            totp = pyotp.TOTP(totp_secret)
            totp_code = totp.now()

            self._api = _ShoonyaApi()
            ret = self._api.login(
                userid=user_id,
                password=password,
                twoFA=totp_code,
                vendor_code=vendor_code,
                api_secret=api_key,
                imei=imei,
            )

            if ret is None or (isinstance(ret, dict) and ret.get("stat") == "Not_Ok"):
                msg = (
                    ret.get("emsg", "Unknown login error")
                    if isinstance(ret, dict)
                    else "Login returned None"
                )
                logger.error(f"Shoonya login failed: {msg}")
                return False, msg

            self._logged_in = True
            logger.info(f"Shoonya login SUCCESS for user: {user_id}")
            return True, "Login successful"

        except ImportError:
            msg = "NorenRestApiPy not installed. Run: pip install NorenRestApiPy"
            logger.error(msg)
            return False, msg
        except Exception as e:
            logger.exception("Unexpected error during Shoonya login")
            return False, str(e)

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    @property
    def api(self):
        """Direct access to the underlying NorenApi for advanced use."""
        return self._api

    # ──────────────────────────────────────────────────────────
    #   REST QUOTES
    # ──────────────────────────────────────────────────────────

    def get_quotes(self, exchange: str, token: str) -> Optional[dict]:
        """
        Fetch full quote via REST: LTP, bid, ask, open, high, low, volume.
        Returns raw response dict on success, None on failure.

        Key fields in response:
          lp  = last traded price
          bp1 = best bid price,  bq1 = best bid qty
          sp1 = best ask price,  sq1 = best ask qty
          o   = open, h = high, l = low, c = close
          v   = volume, oi = open interest
        """
        if not self._api:
            logger.error("Cannot get_quotes: not logged in")
            return None
        try:
            resp = self._api.get_quotes(exchange=exchange, token=token)
            if resp and resp.get("stat") == "Ok":
                return resp
            err = resp.get("emsg", "Unknown") if resp else "None response"
            logger.warning(f"get_quotes failed [{exchange}:{token}]: {err}")
        except Exception:
            logger.exception(f"Exception in get_quotes({exchange}, {token})")
        return None

    def get_ltp(self, exchange: str, token: str) -> Optional[float]:
        """Fetch current LTP via REST."""
        resp = self.get_quotes(exchange, token)
        if resp:
            raw = resp.get("lp", resp.get("c"))
            if raw is not None:
                return float(raw)
        return None

    def get_best_bid_ask_rest(
        self, exchange: str, token: str
    ) -> Optional[tuple[float, float]]:
        """Fetch best bid/ask via REST. Returns (bid, ask) or None."""
        resp = self.get_quotes(exchange, token)
        if resp:
            bid = resp.get("bp1")
            ask = resp.get("sp1")
            if bid is not None and ask is not None:
                return float(bid), float(ask)
        return None

    # ──────────────────────────────────────────────────────────
    #   HISTORICAL DATA
    # ──────────────────────────────────────────────────────────

    def get_time_price_series(
        self,
        exchange: str,
        token: str,
        starttime: float,
        interval: str = "5",
        endtime: Optional[float] = None,
    ) -> Optional[list[dict]]:
        """
        Fetch OHLCV candles.

        :param exchange:  e.g. 'NSE', 'NFO'
        :param token:     Shoonya numeric scrip token
        :param starttime: Unix timestamp for start
        :param interval:  '1', '3', '5', '10', '15', '30', '60', '120', '240'
        :param endtime:   Unix timestamp for end (optional)
        :returns: list of candle dicts with 'into', 'inth', 'intl', 'intc', 'intv', 'ssboe'
        """
        if not self._api:
            return None
        try:
            kwargs = {
                "exchange": exchange,
                "token": token,
                "starttime": starttime,
                "interval": interval,
            }
            if endtime:
                kwargs["endtime"] = endtime

            resp = self._api.get_time_price_series(**kwargs)
            if resp and isinstance(resp, list):
                return resp
            logger.warning(f"get_time_price_series empty for {exchange}:{token}")
        except Exception:
            logger.exception(f"Exception in get_time_price_series({exchange}, {token})")
        return None

    # ──────────────────────────────────────────────────────────
    #   SCRIP SEARCH
    # ──────────────────────────────────────────────────────────

    def search_scrip(self, exchange: str, searchtext: str) -> list[dict]:
        """
        Search for scrip by name/symbol.
        Returns list of dicts with 'token', 'tsym' (trading symbol), 'cname'.
        """
        if not self._api:
            return []
        try:
            resp = self._api.searchscrip(exchange=exchange, searchtext=searchtext)
            if resp and resp.get("stat") == "Ok":
                return resp.get("values", [])
            logger.warning(f"searchscrip no results for '{searchtext}' on {exchange}")
        except Exception:
            logger.exception(f"Exception in search_scrip({exchange}, {searchtext})")
        return []

    # ──────────────────────────────────────────────────────────
    #   OPTION CHAIN
    # ──────────────────────────────────────────────────────────

    def get_option_chain(
        self,
        index: str,
        expiry_prefix: str,
        strike_range: tuple[float, float] = None,
    ) -> list[dict]:
        """
        Build option chain by searching NFO for CE/PE at nearby strikes.

        :param index: 'NIFTY' or 'BANKNIFTY'
        :param expiry_prefix: e.g. '27MAR' or '03APR' (Shoonya format)
        :param strike_range: (low, high) to filter strikes
        :returns: list of dicts with strike, type (CE/PE), and quote data
        """
        chain = []

        for opt_type in ["CE", "PE"]:
            search = f"{index} {expiry_prefix}"
            results = self.search_scrip(NFO_EXCHANGE, search)

            for scrip in results:
                tsym = scrip.get("tsym", "")
                if opt_type not in tsym:
                    continue

                # Extract strike from trading symbol
                try:
                    # Format: NIFTY27MAR24500CE
                    strike_str = tsym.replace(index, "").replace(expiry_prefix, "")
                    strike_str = strike_str.replace("CE", "").replace("PE", "")
                    strike = float(strike_str)
                except (ValueError, IndexError):
                    continue

                if strike_range and not (strike_range[0] <= strike <= strike_range[1]):
                    continue

                # Fetch quote for this strike
                token = scrip.get("token", "")
                quote = self.get_quotes(NFO_EXCHANGE, token)

                chain.append({
                    "symbol": tsym,
                    "token": token,
                    "strike": strike,
                    "type": opt_type,
                    "ltp": float(quote.get("lp", 0)) if quote else 0,
                    "bid": float(quote.get("bp1", 0)) if quote else 0,
                    "ask": float(quote.get("sp1", 0)) if quote else 0,
                    "oi": int(quote.get("oi", 0)) if quote else 0,
                    "volume": int(quote.get("v", 0)) if quote else 0,
                })

                # Rate limiting — Shoonya has API limits
                time.sleep(0.1)

        # Sort by strike
        chain.sort(key=lambda x: (x["strike"], x["type"]))
        return chain

    # ──────────────────────────────────────────────────────────
    #   WEBSOCKET LIFECYCLE
    # ──────────────────────────────────────────────────────────

    def start_websocket(self, on_open, on_tick, on_error, on_close):
        """
        Start the Shoonya WebSocket connection.
        Callbacks:
          on_open(ws)
          on_tick(ws, message)  — receives raw message dict/str
          on_error(ws, error)
          on_close(ws, code, msg)
        """
        if not self._api:
            logger.error("Cannot start WebSocket: not logged in")
            return

        # SSL fix for certain environments
        import ssl
        import websocket as _ws
        _ws.enableTrace(False)
        
        try:
            ssl._create_default_https_context = ssl._create_unverified_context
        except AttributeError:
            pass
            
        _orig_run_forever = _ws.WebSocketApp.run_forever
        def _patched_run_forever(self, **kwargs):
            if "sslopt" not in kwargs:
                kwargs["sslopt"] = {"cert_reqs": ssl.CERT_NONE}
            elif "cert_reqs" not in kwargs["sslopt"]:
                kwargs["sslopt"]["cert_reqs"] = ssl.CERT_NONE
            return _orig_run_forever(self, **kwargs)
        _ws.WebSocketApp.run_forever = _patched_run_forever

        def _on_open(*args):
            logger.info("WebSocket connected")
            on_open(args[0] if args else None)

        def _on_message(*args):
            msg = args[-1] if args else None
            on_tick(args[0] if len(args) > 1 else None, msg)

        def _on_error(*args):
            err = args[-1] if args else "unknown error"
            logger.error(f"WebSocket error: {err}")
            on_error(args[0] if len(args) > 1 else None, err)

        def _on_close(*args):
            code = args[1] if len(args) > 1 else None
            msg = args[2] if len(args) > 2 else None
            logger.warning(f"WebSocket closed: {code} {msg}")
            on_close(args[0] if args else None, code, msg)

        self._api.start_websocket(
            order_update_callback=lambda *a: None,
            subscribe_callback=_on_message,
            socket_open_callback=_on_open,
            socket_error_callback=_on_error,
            socket_close_callback=_on_close,
        )

    def subscribe(self, exchange_token_pairs: list[tuple[str, str]]):
        """Subscribe to touchline feed for given (exchange, token) pairs."""
        if not self._api or not exchange_token_pairs:
            return
        scrip_list = [f"{ex}|{tok}" for ex, tok in exchange_token_pairs]
        self._api.subscribe(scrip_list)
        logger.info(f"Subscribed: {scrip_list}")

    def unsubscribe(self, exchange_token_pairs: list[tuple[str, str]]):
        """Unsubscribe from touchline feed."""
        if not self._api or not exchange_token_pairs:
            return
        scrip_list = [f"{ex}|{tok}" for ex, tok in exchange_token_pairs]
        self._api.unsubscribe(scrip_list)
        logger.info(f"Unsubscribed: {scrip_list}")

    def close_websocket(self):
        """Cleanly close WebSocket connection."""
        try:
            if self._api:
                self._api.close_websocket()
                logger.info("WebSocket closed cleanly")
        except Exception:
            pass
