"""
broker/shoonya_client.py — Shoonya API client for BlitzTrader.
QuickAuth authentication with appkey from secret_code.

Auth flow:
  1. Decode secret_code (base64) → K array
  2. Compute appkey: SHA256(user_id + "|" + chr(K[p]+p) for all p)
  3. SHA256 hash the password
  4. Generate TOTP code
  5. POST to /NorenWClientAPI/QuickAuth with jData
  6. Store susertoken for all subsequent calls

Endpoint: https://api.shoonya.com/NorenWClientAPI/ (working as of Apr 2026)
"""
import base64
import hashlib
import logging
import time
from typing import Optional

import pyotp
import requests

logger = logging.getLogger("BlitzTrader.ShoonyaClient")

# Shoonya API endpoints (working April 2026)
API_BASE = "https://api.shoonya.com"
API_PATH = "/NorenWClientAPI"
WS_URL = "wss://api.shoonya.com/NorenWSTP/"

# NFO exchange constant (avoid circular import from config)
NFO_EXCHANGE = "NFO"


class ShoonyaClient:
    """
    Shoonya API client with QuickAuth authentication.
    Appkey computed from secret_code (base64-encoded K array).
    """

    def __init__(self):
        self._api = None
        self._logged_in = False

    # ──────────────────────────────────────────────────────────
    #   LOGIN
    # ──────────────────────────────────────────────────────────

    def login(
        self,
        user_id: str,
        password: str,
        totp_secret: str,
        api_key: str,
        vendor_code: str,
        imei: str,
        secret_code: str = "",
        auth_code: str = "",  # Reserved for future OAuth support
    ) -> tuple[bool, str]:
        """
        Log in to Shoonya using QuickAuth.

        Appkey computation (April 2026):
          1. Decode secret_code from base64 → K array (48 bytes)
          2. Build string: user_id + "|" + chr(K[p]+p) for all p
          3. Appkey = SHA256(that string)

        Args:
            user_id: Shoonya user ID
            password: Login password (will be SHA256 hashed)
            totp_secret: TOTP secret for 2FA
            api_key: (Deprecated, not used)
            vendor_code: Shoonya vendor code
            imei: Device IMEI identifier
            secret_code: Base64-encoded K array for appkey computation
            auth_code: Reserved for future OAuth support

        Returns:
            (success: bool, message: str)
        """
        try:
            from NorenRestApiPy.NorenApi import NorenApi

            class _ShoonyaApi(NorenApi):
                def __init__(self):
                    super().__init__(
                        host=f"{API_BASE}{API_PATH}/",
                        websocket=WS_URL,
                    )

            self._api = _ShoonyaApi()

            # Compute appkey from secret_code (K array)
            if secret_code:
                try:
                    K = base64.b64decode(secret_code)
                    d = user_id + "|"
                    for p in range(len(K)):
                        d += chr(K[p] + p)
                    app_key = hashlib.sha256(d.encode()).hexdigest()
                    logger.info(f"Computed appkey from secret_code (K len={len(K)})")
                except Exception as e:
                    logger.error(f"Failed to decode secret_code: {e}")
                    return False, f"Invalid secret_code: {e}"
            else:
                # Fallback to old method (for compatibility)
                app_key = hashlib.sha256(f"{user_id}|{api_key}".encode()).hexdigest()
                logger.warning("No secret_code provided, using fallback appkey")

            pwd_hash = hashlib.sha256(password.encode()).hexdigest()
            totp_code = pyotp.TOTP(totp_secret).now()

            ret = self._api.login(
                userid=user_id,
                password=pwd_hash,
                twoFA=totp_code,
                vendor_code=vendor_code,
                api_secret=app_key,
                imei=imei,
            )

            if ret is None:
                return False, "Login returned None — check credentials/IP whitelist"

            if ret.get("stat") == "Ok":
                self._logged_in = True
                logger.info(f"Shoonya login SUCCESS for user: {user_id}")
                return True, "Login successful"

            err = ret.get("emsg", str(ret))
            logger.error(f"Shoonya login FAILED: {err}")
            return False, err

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
        endtime: float = None,
        interval: str = "1",
    ) -> Optional[list[dict]]:
        """
        Fetch OHLC data.
        interval: 1/5/15/60 (minutes)
        Returns list of [timestamp, open, high, low, close, volume, oi]
        """
        if not self._api:
            logger.error("Cannot get_time_price_series: not logged in")
            return None
        try:
            resp = self._api.get_time_price_series(
                exchange=exchange,
                token=token,
                starttime=int(starttime),
                endtime=int(endtime) if endtime else int(time.time()),
                interval=interval,
            )
            if resp and resp.get("stat") == "Ok":
                return resp.get("series", [])
            err = resp.get("emsg", "Unknown") if resp else "None response"
            logger.warning(f"get_time_price_series failed: {err}")
        except Exception:
            logger.exception("Exception in get_time_price_series")
        return None

    # ──────────────────────────────────────────────────────────
    #   WEBSOCKET
    # ──────────────────────────────────────────────────────────

    def start_websocket(self, callback=None, exchange_tokens: list[str] = None) -> bool:
        """
        Start WebSocket subscription for market data.
        callback(data): called on each tick
        exchange_tokens: list of "NSE|26000" style tokens
        """
        if not self._api:
            logger.error("Cannot start_websocket: not logged in")
            return False

        try:
            # Subscribe to tokens if provided
            if exchange_tokens:
                for et in exchange_tokens:
                    parts = et.split("|")
                    if len(parts) == 2:
                        exchange, token = parts
                        self._api.subscribe(exchange=exchange, token=token)
                        logger.info(f"Subscribed to {exchange}:{token}")

            # Set websocket callback
            if callback:
                self._api.set_websocket_callback(callback)

            logger.info("WebSocket started")
            return True

        except Exception:
            logger.exception("Failed to start WebSocket")
            return False

    def stop_websocket(self) -> bool:
        """Stop WebSocket connection."""
        try:
            if self._api:
                self._api.close_websocket()
                logger.info("WebSocket stopped")
            return True
        except Exception:
            logger.exception("Failed to stop WebSocket")
            return False
