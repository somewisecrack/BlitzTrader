"""
broker/shoonya_client.py — Shoonya API client for BlitzTrader.
OAuth-based authentication (April 2026 migration).

Auth flow:
  1. User logs in manually: https://trade.shoonya.com/OAuthlogin?client_id={USER_ID}
  2. Copy authorization code from redirect URL
  3. Store code in .env as SHOONYA_AUTH_CODE
  4. BlitzTrader exchanges code for access_token via GenAcsTok
  5. Inject token into NorenApi for all subsequent calls
"""
import hashlib
import json
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger("BlitzTrader.ShoonyaClient")

# Shoonya API endpoints (April 2026)
API_BASE = "https://trade.shoonya.com"
API_PATH = "/NorenWClientAPI"
WS_URL = "wss://trade.shoonya.com/NorenWSTP/"

# NFO exchange constant (avoid circular import from config)
NFO_EXCHANGE = "NFO"


class ShoonyaClient:
    """
    Shoonya API client with OAuth authentication.

    Requires one-time manual login:
      1. Visit: https://trade.shoonya.com/OAuthlogin?client_id={USER_ID}
      2. Log in with credentials + TOTP
      3. Copy authorization code from redirect URL
      4. Set SHOONYA_AUTH_CODE={code} in .env
      5. BlitzTrader handles token exchange automatically
    """

    def __init__(self):
        self._api = None
        self._logged_in = False
        self._access_token = None
        self._token_expires_at = 0

    # ──────────────────────────────────────────────────────────
    #   LOGIN VIA OAUTH
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
        auth_code: str = "",
    ) -> tuple[bool, str]:
        """
        Log in to Shoonya using OAuth (April 2026 migration).

        Requires SHOONYA_AUTH_CODE environment variable:
          1. User logs in manually at: https://trade.shoonya.com/OAuthlogin?client_id={USER_ID}
          2. Copy the authorization code from redirect URL
          3. Set it in .env as SHOONYA_AUTH_CODE={code}
          4. This method exchanges code for access_token

        Args:
            user_id: Shoonya user ID (e.g., "FA125387")
            password: Not used in OAuth flow (kept for compatibility)
            totp_secret: Not used in OAuth flow (kept for compatibility)
            api_key: Not used in OAuth flow (kept for compatibility)
            vendor_code: Not used in OAuth flow (kept for compatibility)
            imei: Not used in OAuth flow (kept for compatibility)
            secret_code: Not used in OAuth flow (kept for compatibility)
            auth_code: OAuth authorization code (from manual login redirect URL)

        Returns:
            (success: bool, message: str)
        """
        if not auth_code:
            msg = (
                "OAuth code required. Manual login needed:\n"
                f"1. Visit: https://trade.shoonya.com/OAuthlogin?client_id={user_id}\n"
                "2. Log in with credentials + TOTP\n"
                "3. Copy code from redirect URL\n"
                "4. Set SHOONYA_AUTH_CODE={code} in .env\n"
                "5. Restart BlitzTrader"
            )
            logger.error(msg)
            return False, msg

        try:
            # Exchange authorization code for access token
            success, token = self._exchange_code_for_token(user_id, auth_code, secret_code)
            if not success:
                return False, token

            self._access_token = token
            self._logged_in = True
            logger.info(f"Shoonya OAuth login SUCCESS for user: {user_id}")
            return True, "Login successful"

        except Exception as e:
            logger.exception("Unexpected error during Shoonya OAuth login")
            return False, str(e)

    def _exchange_code_for_token(self, user_id: str, auth_code: str, secret_code: str) -> tuple[bool, str]:
        """
        Exchange OAuth authorization code for access token.

        Calls GenAcsTok API:
          POST /NorenWClientAPI/GenAcsTok
          jData: { "code": auth_code, "checksum": SHA256(user_id + secret_code + auth_code) }

        Returns:
            (success: bool, access_token_or_error: str)
        """
        try:
            # Compute checksum
            checksum_input = f"{user_id}{secret_code}{auth_code}"
            checksum = hashlib.sha256(checksum_input.encode()).hexdigest()

            # Build request
            jdata = json.dumps({"code": auth_code, "checksum": checksum})

            url = f"{API_BASE}{API_PATH}/GenAcsTok"
            logger.info(f"Exchanging OAuth code for token at {url}")

            resp = requests.post(
                url,
                data={"jData": jdata},
                timeout=10,
                verify=True
            )

            if resp.status_code != 200:
                err = f"GenAcsTok returned {resp.status_code}: {resp.text[:200]}"
                logger.error(err)
                return False, err

            result = resp.json()

            if result.get("stat") != "Ok":
                err = result.get("emsg", f"GenAcsTok failed: {result}")
                logger.error(err)
                return False, err

            access_token = result.get("access_token")
            if not access_token:
                err = f"No access_token in response: {result}"
                logger.error(err)
                return False, err

            logger.info(f"Successfully obtained access_token (expires in {result.get('expires_in', '?')}s)")
            return True, access_token

        except requests.exceptions.RequestException as e:
            err = f"Network error exchanging code: {e}"
            logger.error(err)
            return False, err
        except json.JSONDecodeError as e:
            err = f"Invalid JSON response from GenAcsTok: {e}"
            logger.error(err)
            return False, err
        except Exception as e:
            err = f"Unexpected error in _exchange_code_for_token: {e}"
            logger.error(err)
            return False, err

    def _init_noren_api(self):
        """Initialize NorenApi with OAuth token injected."""
        try:
            from NorenRestApiPy.NorenApi import NorenApi

            class _ShoonyaApi(NorenApi):
                def __init__(self):
                    super().__init__(
                        host=f"{API_BASE}{API_PATH}/",
                        websocket=WS_URL,
                    )

            self._api = _ShoonyaApi()
            # Inject OAuth token as session token
            self._api._NorenApi__susertoken = self._access_token
            logger.info("NorenApi initialized with OAuth token")

        except Exception as e:
            logger.error(f"Failed to initialize NorenApi: {e}")
            return False

        return True

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in and self._access_token is not None

    @property
    def api(self):
        """Direct access to the underlying NorenApi for advanced use."""
        if not self._api and self._access_token:
            self._init_noren_api()
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
        if not self.api:
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
        if not self.api:
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
        if not self.api:
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
