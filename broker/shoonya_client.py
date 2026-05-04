"""
broker/shoonya_client.py — Shoonya API client for BlitzTrader.
OAuth2 authentication via NorenRestApiPy (post April 2026 migration).

Auth flow (fully autonomous — no manual auth code needed):
  1. QuickAuth  → POST credentials + TOTP → get jKey (susertoken)
  2. GetAuthCode → POST jKey + app_key → get one-time auth code
  3. GenAcsTok  → POST checksum(vendor|secret|code) → get access_token
  4. injectOAuthHeader() → sets Bearer token for all subsequent calls

All three steps are implemented in pure Python (no Selenium required).
Endpoint: https://trade.shoonya.com/NorenWClientAPI/
"""
import hashlib
import json
import logging
import time
import urllib.parse
import uuid
from typing import Optional

import pyotp
import requests

logger = logging.getLogger("BlitzTrader.ShoonyaClient")

BASE_URL = "https://trade.shoonya.com/NorenWClientAPI"
WS_URL = "wss://api.shoonya.com/NorenWS/"

NFO_EXCHANGE = "NFO"

# Internal web-app API secret (decoded from Shoonya OAuth portal JS)
# Xa = new Uint8Array([83,50,97,114,110,46,27,93]) → each char = byte + index
_K = [83, 50, 97, 114, 110, 46, 27, 93]
_INTERNAL_SECRET = "".join(chr(b + i) for i, b in enumerate(_K))  # "S3cur3!d"


class ShoonyaClient:
    """
    Shoonya API client using OAuth2 Bearer token authentication.
    Fully autonomous — derives auth code internally via QuickAuth + GetAuthCode.
    """

    def __init__(self):
        self._api = None
        self._logged_in = False
        self._session = requests.Session()

    # ──────────────────────────────────────────────────────────
    #   LOGIN (autonomous OAuth flow)
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
        auth_code: str = "",   # ignored — kept for interface compatibility
    ) -> tuple[bool, str]:
        """
        Authenticate with Shoonya using the full autonomous OAuth flow:
          1. QuickAuth (user/pass/TOTP)  → jKey
          2. GetAuthCode (jKey + vendor)  → one-time code
          3. GenAcsTok (code + checksum)  → access_token

        auth_code parameter is ignored — the code is obtained automatically.
        Returns (success: bool, message: str).
        """
        try:
            from NorenRestApiPy.NorenApi import NorenApi

            class _ShoonyaApi(NorenApi):
                def __init__(self):
                    super().__init__(
                        host=BASE_URL,
                        websocket=WS_URL,
                    )

            self._api = _ShoonyaApi()

            # ── Step 1: QuickAuth ──
            logger.info(f"Step 1: QuickAuth for {user_id}...")
            jkey, err = self._quickauth(user_id, password, totp_secret, vendor_code)
            if not jkey:
                return False, f"QuickAuth failed: {err}"
            logger.info("QuickAuth success — got jKey")
            # Save jKey for WebSocket auth (server requires old-style t:c format)
            self._jkey = jkey

            # ── Step 2: GetAuthCode ──
            logger.info("Step 2: GetAuthCode...")
            code, err = self._get_auth_code(jkey, vendor_code)
            if not code:
                return False, f"GetAuthCode failed: {err}"
            logger.info(f"Got auth code: {code[:8]}...")

            # NorenRestApiPy differs across environments. Some builds expose
            # getAccessToken() for the OAuth completion step; others only
            # support the classic jKey path, which Shoonya now rejects for
            # private REST endpoints. In that case we complete GenAcsTok
            # ourselves and inject Bearer headers.
            if hasattr(self._api, "getAccessToken"):
                # ── Step 3: GenAcsTok ──
                logger.info("Step 3: GenAcsTok...")
                result = self._api.getAccessToken(
                    authcode=code,
                    Secret_Code=secret_code,
                    client_id=vendor_code,
                    UID=user_id,
                )

                if result is None:
                    return False, "GenAcsTok returned None — auth code may be invalid"

                access_token, userid, refresh_token, actid = result
                logger.info(f"OAuth login SUCCESS for user: {userid}, actid: {actid}")
                self._account_id = actid or user_id
            else:
                logger.warning(
                    "NorenRestApiPy has no getAccessToken(); using raw OAuth GenAcsTok fallback"
                )
                result = self._get_access_token(
                    auth_code=code,
                    secret_code=secret_code,
                    client_id=vendor_code,
                    user_id=user_id,
                )
                if result is None:
                    return False, "GenAcsTok returned None — auth code may be invalid"
                access_token, userid, refresh_token, actid, susertoken = result
                self._inject_oauth_session(
                    access_token=access_token,
                    user_id=userid or user_id,
                    account_id=actid or userid or user_id,
                    susertoken=susertoken or jkey,
                )
                logger.info(
                    "Raw OAuth login SUCCESS for user: %s, actid: %s",
                    userid,
                    actid,
                )
                self._account_id = actid or userid or user_id

            self._logged_in = True
            self._user_id = getattr(self, "_user_id", user_id) or user_id
            return True, "Login successful"

        except Exception as e:
            logger.exception("Unexpected error during Shoonya OAuth login")
            return False, str(e)

    def _quickauth(
        self,
        user_id: str,
        password: str,
        totp_secret: str,
        vendor_code: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        POST to QuickAuth with credentials.
        Returns (jKey, None) on success or (None, error_message).
        """
        last_err = "unknown error"
        for attempt in range(1, 4):
            try:
                # Wait for a fresh TOTP window (avoid using code with < 3s remaining)
                remaining = 30 - (int(time.time()) % 30)
                if remaining < 3:
                    logger.debug(f"Waiting {remaining + 1}s for fresh TOTP window...")
                    time.sleep(remaining + 1)

                pwd = hashlib.sha256(password.encode()).hexdigest()
                appkey = hashlib.sha256(
                    (user_id + "|" + _INTERNAL_SECRET).encode()
                ).hexdigest()
                totp = pyotp.TOTP(totp_secret).now()

                payload = {
                    "apkversion": "W2_20250926",
                    "uid": user_id,
                    "pwd": pwd,
                    "factor2": totp,
                    "appkey": appkey,
                    "imei": str(uuid.uuid4()),
                    "addldivinf": "BlitzTrader/1.0",
                    "source": "API",
                    "vc": "NOREN_API",
                    "app_key": vendor_code,
                }

                resp = self._session.post(
                    f"{BASE_URL}/QuickAuth",
                    data="jData=" + json.dumps(payload),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=15,
                )

                if resp.status_code >= 500:
                    last_err = f"HTTP {resp.status_code} from QuickAuth"
                    logger.warning("QuickAuth attempt %s failed: %s", attempt, last_err)
                    time.sleep(attempt)
                    continue

                try:
                    result = json.loads(resp.text)
                except Exception:
                    body = (resp.text or "").strip().replace("\n", " ")
                    last_err = f"Non-JSON QuickAuth response HTTP {resp.status_code}: {body[:160]}"
                    logger.warning("QuickAuth attempt %s failed: %s", attempt, last_err)
                    time.sleep(attempt)
                    continue

                if result.get("stat") == "Ok":
                    return result.get("susertoken"), None
                return None, result.get("emsg", "unknown error")

            except Exception as e:
                last_err = str(e)
                logger.warning("QuickAuth attempt %s exception: %s", attempt, last_err)
                time.sleep(attempt)

        return None, last_err

    def _get_auth_code(
        self, jkey: str, vendor_code: str
    ) -> tuple[Optional[str], Optional[str]]:
        """
        POST to GetAuthCode using jKey from QuickAuth.
        Returns (auth_code, None) on success or (None, error_message).
        """
        try:
            from urllib.parse import urlparse, parse_qs

            auth_payload = json.dumps({"app_key": vendor_code}) + "&jKey=" + jkey
            resp = self._session.post(
                f"{BASE_URL}/GetAuthCode",
                data="jData=" + auth_payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
                allow_redirects=False,
            )

            if resp.status_code >= 500:
                return None, f"HTTP {resp.status_code} from GetAuthCode"

            # Response body contains the auth code as JSON
            try:
                data = json.loads(resp.text)
                if "code" in data:
                    return data["code"], None
            except Exception:
                pass

            # Also check Location header as fallback
            location = resp.headers.get("Location", "")
            if "code=" in location:
                code = parse_qs(urlparse(location).query).get("code", [None])[0]
                if code:
                    return code, None

            body = (resp.text or "").strip().replace("\n", " ")
            return None, f"Could not extract auth code from response HTTP {resp.status_code}: {body[:200]}"

        except Exception as e:
            return None, str(e)

    def _get_access_token(
        self,
        auth_code: str,
        secret_code: str,
        client_id: str,
        user_id: str,
    ) -> Optional[tuple[str, str, str, str, str]]:
        """Complete Shoonya OAuth without relying on NorenRestApiPy.getAccessToken()."""
        try:
            checksum = hashlib.sha256(
                (client_id + secret_code + auth_code).encode("utf-8")
            ).hexdigest()
            payload = {
                "code": auth_code,
                "checksum": checksum,
                "uid": user_id,
            }
            resp = self._session.post(
                f"{BASE_URL}/GenAcsTok",
                data="jData=" + json.dumps(payload),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            result = json.loads(resp.text)
            if "access_token" not in result:
                logger.error("GenAcsTok failed: %s", result)
                return None
            return (
                result.get("access_token", ""),
                result.get("USERID", user_id),
                result.get("refresh_token", ""),
                result.get("actid", user_id),
                result.get("susertoken", ""),
            )
        except Exception:
            logger.exception("Exception during raw GenAcsTok")
        return None

    def _inject_oauth_session(
        self,
        access_token: str,
        user_id: str,
        account_id: str,
        susertoken: str = "",
    ) -> None:
        """Populate private NorenApi fields so raw REST and WS share one session."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        setattr(self._api, "_NorenApi__OAuthHeaders", headers)
        setattr(self._api, "_NorenApi__access_token", access_token)
        setattr(self._api, "_NorenApi__username", user_id)
        setattr(self._api, "_NorenApi__accountid", account_id)
        if susertoken:
            setattr(self._api, "_NorenApi__susertoken", susertoken)
            self._jkey = susertoken
        self._user_id = user_id
        self._account_id = account_id

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    @property
    def api(self):
        """Direct access to the underlying NorenApi for advanced use."""
        return self._api

    # ──────────────────────────────────────────────────────────
    #   RMS / MARGIN
    # ──────────────────────────────────────────────────────────

    def get_limits(
        self,
        product_type: str = None,
        segment: str = None,
        exchange: str = None,
    ) -> Optional[dict]:
        """
        Fetch Shoonya RMS limits via /NorenWClientAPI/Limits.

        The API response includes cash, marginused, span/expo breakup, MTM,
        collateral, and related risk fields. This is broker/RMS truth and is
        used for audit/context rather than approximating available margin.
        """
        if not self._api:
            logger.error("Cannot get_limits: not logged in")
            return None
        try:
            payload = {
                "uid": self._user_id,
                "actid": getattr(self, "_account_id", self._user_id),
            }
            if product_type is not None:
                payload["prd"] = product_type
            if segment is not None:
                payload["seg"] = segment
            if exchange is not None:
                payload["exch"] = exchange
            resp = self._post_private("Limits", payload)
            if resp and resp.get("stat") == "Ok":
                return resp
            logger.warning(f"get_limits failed: {resp}")
            return resp
        except Exception:
            logger.exception("Exception in get_limits")
        return None

    def get_order_margin(
        self,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        price: float,
        transaction_type: str,
        product: str = "M",
        price_type: str = "LMT",
        trigger_price: float = None,
    ) -> Optional[dict]:
        """
        Fetch actual Shoonya RMS margin for a proposed order.

        API doc: POST /NorenWClientAPI/GetOrderMargin with uid, actid, exch,
        tsym, qty, prc, prd, trantype, prctyp, etc. For virtual MARKET entries
        we intentionally ask as a LMT order at the intended entry price because
        the doc's margin endpoint formally lists LMT / SL-LMT.
        """
        payload = {
            "uid": self._user_id,
            "actid": getattr(self, "_account_id", self._user_id),
            "exch": exchange,
            "tsym": tradingsymbol,
            "qty": str(int(quantity)),
            "prc": str(round(float(price or 0), 2)),
            "prd": product,
            "trantype": transaction_type,
            "prctyp": price_type,
        }
        if trigger_price is not None:
            payload["trgprc"] = str(round(float(trigger_price), 2))
        return self._post_private("GetOrderMargin", payload)

    def _post_private(self, endpoint: str, payload: dict) -> Optional[dict]:
        """POST a private Shoonya Noren endpoint using OAuth headers when available."""
        if not self._api:
            logger.error("Cannot call %s: not logged in", endpoint)
            return None

        headers = getattr(self._api, "_NorenApi__OAuthHeaders", None)
        jkey = getattr(self, "_jkey", None) or getattr(self._api, "_NorenApi__susertoken", None)
        if not headers and not jkey:
            logger.error("Cannot call %s: missing OAuth headers and jKey/susertoken", endpoint)
            return None

        try:
            data = "jData=" + json.dumps(payload)
            if not headers:
                data += "&jKey=" + jkey
            resp = self._session.post(
                f"{BASE_URL}/{endpoint}",
                data=data,
                headers=headers,
                timeout=15,
            )
            result = json.loads(resp.text)
            if result.get("stat") != "Ok":
                logger.warning("%s returned %s", endpoint, result)
            return result
        except Exception:
            logger.exception("Exception calling %s", endpoint)
        return None

    # ──────────────────────────────────────────────────────────
    #   REST QUOTES
    # ──────────────────────────────────────────────────────────

    def get_quotes(self, exchange: str, token: str) -> Optional[dict]:
        """
        Fetch full quote via REST: LTP, bid, ask, open, high, low, volume.
        Returns raw response dict on success, None on failure.
        """
        if not self._api:
            logger.error("Cannot get_quotes: not logged in")
            return None
        try:
            resp = self._post_private(
                "GetQuotes",
                {"uid": self._user_id, "exch": exchange, "token": token},
            )
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
        Fetch OHLC candle data. Returns list of candle dicts or None.
        interval: 1/3/5/10/15/30/60/120/240
        """
        if not self._api:
            logger.error("Cannot get_time_price_series: not logged in")
            return None
        try:
            payload = {
                "ordersource": "API",
                "uid": self._user_id,
                "exch": exchange,
                "token": token,
                "st": str(int(starttime)),
                "intrv": str(interval),
            }
            if endtime is not None:
                payload["et"] = str(int(endtime))
            resp = self._post_private("TPSeries", payload)
            if isinstance(resp, list):
                return resp
            logger.warning(f"get_time_price_series returned non-list: {resp}")
        except Exception:
            logger.exception("Exception in get_time_price_series")
        return None

    def get_front_month_futures_token(self, symbol: str) -> Optional[dict]:
        """
        Find the front-month FUTIDX contract for an index.

        Searches NFO for all FUTIDX contracts matching the symbol, picks the
        nearest expiry that has not yet expired, and returns:
            {"exchange": "NFO", "token": "66691", "tsym": "NIFTY28APR26F",
             "expiry": "28-APR-2026", "name": "NIFTY", "lot_size": 25}

        Strict filtering rules (to avoid NIFTYNXT50, FINNIFTY, MIDCPNIFTY, etc.):
          - instname must be exactly "FUTIDX"
          - tsym must start with exactly the requested symbol prefix followed
            immediately by a digit (month-year or day pattern), NOT by letters.
            e.g. for "NIFTY": NIFTY28APR26F matches (tsym[5] is a digit),
            but NIFTYNXT50..., FINNIFTY..., MIDCPNIFTY... do NOT match.
          - symname field (if present) must also match the exact symbol.

        Returns None if login not done or no contract found.
        """
        import datetime as _dt
        import re as _re

        sym_upper = symbol.upper()
        # Pattern: symbol prefix immediately followed by a digit
        # e.g. NIFTY28APR26F → "NIFTY" + "2" (digit) ✓
        #       NIFTYNXT50...  → "NIFTY" + "N" (letter) ✗
        prefix_digit_re = _re.compile(r"^" + _re.escape(sym_upper) + r"\d")

        results = self.search_scrip("NFO", sym_upper)
        raw_count = len(results) if results else 0
        logger.info(
            f"get_front_month_futures_token({sym_upper}): "
            f"search_scrip returned {raw_count} raw candidate(s)"
        )

        if not results:
            logger.error(
                f"get_front_month_futures_token: no NFO results for {sym_upper}"
            )
            return None

        today = _dt.date.today()
        candidates = []
        for r in results:
            tsym = r.get("tsym", "")

            # 1. Must be a futures contract
            if r.get("instname") != "FUTIDX":
                continue

            # 2. tsym must start with EXACTLY the symbol followed by a digit
            #    (rejects NIFTYNXT50, FINNIFTY, MIDCPNIFTY, BANKNIFTY when asking for NIFTY, etc.)
            if not prefix_digit_re.match(tsym):
                logger.debug(
                    f"  Skipping {tsym!r}: tsym prefix does not match "
                    f"^{sym_upper}\\d pattern"
                )
                continue

            # 3. Parse expiry date
            exd = r.get("exd", "")          # e.g. "28-APR-2026"
            try:
                expiry = _dt.datetime.strptime(exd, "%d-%b-%Y").date()
            except ValueError:
                logger.debug(f"  Skipping {tsym!r}: cannot parse expiry {exd!r}")
                continue

            if expiry >= today:
                candidates.append((expiry, r))

        filtered_count = len(candidates)
        logger.info(
            f"get_front_month_futures_token({sym_upper}): "
            f"{filtered_count} candidate(s) after instname+prefix filter"
        )

        if not candidates:
            logger.error(
                f"get_front_month_futures_token: no live FUTIDX for {sym_upper} "
                f"after strict filtering. Raw candidates were:"
            )
            for r in (results or []):
                logger.error(
                    f"  tsym={r.get('tsym')!r} instname={r.get('instname')!r} "
                    f"exd={r.get('exd')!r} symname={r.get('symname')!r}"
                )
            return None

        candidates.sort(key=lambda x: x[0])
        expiry, scrip = candidates[0]

        lot_size = None
        for key in ("ls", "lotsize", "lot_size"):
            raw_lot_size = scrip.get(key)
            if raw_lot_size is None:
                continue
            try:
                lot_size = int(float(raw_lot_size))
                break
            except (TypeError, ValueError):
                logger.debug(
                    f"  Ignoring invalid lot size {raw_lot_size!r} from key {key!r}"
                )

        info = {
            "exchange": "NFO",
            "token":    scrip["token"],
            "tsym":     scrip.get("tsym", ""),
            "expiry":   scrip.get("exd", ""),
            "name":     sym_upper,
        }
        if lot_size:
            info["lot_size"] = lot_size
        logger.info(
            f"Front-month futures: {sym_upper} → {info['tsym']} "
            f"(token {info['token']}, expiry {info['expiry']}, "
            f"lot_size {info.get('lot_size', 'unknown')})"
        )
        return info

    def search_scrip(self, exchange: str, searchtext: str) -> Optional[list]:
        """
        Search for a scrip by name. Returns list of scrip dicts, or None.
        """
        if not self._api:
            logger.error("Cannot search_scrip: not logged in")
            return None
        try:
            resp = self._post_private(
                "SearchScrip",
                {
                    "uid": self._user_id,
                    "exch": exchange,
                    "stext": urllib.parse.quote_plus(searchtext),
                },
            )
            if resp and resp.get("stat") == "Ok":
                return resp.get("values", [])
            logger.warning(f"search_scrip({exchange}, {searchtext}): {resp}")
        except Exception:
            logger.exception(f"Exception in search_scrip({exchange}, {searchtext})")
        return None

    def get_option_chain(
        self, exchange: str, tradingsymbol: str, strikeprice: float, count: int = 2
    ) -> Optional[dict]:
        """
        Fetch option chain around a strike price.
        Returns raw dict with 'values' list on success, None on failure.
        """
        if not self._api:
            logger.error("Cannot get_option_chain: not logged in")
            return None
        try:
            resp = self._post_private(
                "GetOptionChain",
                {
                    "uid": self._user_id,
                    "exch": exchange,
                    "tsym": urllib.parse.quote_plus(tradingsymbol),
                    "strprc": str(strikeprice),
                    "cnt": str(count),
                },
            )
            if resp and resp.get("stat") == "Ok":
                return resp
            logger.warning(f"get_option_chain({tradingsymbol}): {resp}")
        except Exception:
            logger.exception("Exception in get_option_chain")
        return None

    # ──────────────────────────────────────────────────────────
    #   WEBSOCKET
    # ──────────────────────────────────────────────────────────

    def start_websocket(
        self,
        on_open=None,
        on_tick=None,
        on_error=None,
        on_close=None,
        callback=None,
        exchange_tokens: list[str] = None,
    ) -> bool:
        """
        Start WebSocket connection for live market data. BLOCKS until disconnected.

        Callback signature adapters:
          NorenApi on_open  → called with ()      live_feed expects (ws)
          NorenApi on_tick  → called with (msg)   live_feed expects (ws, msg)
          NorenApi on_error → called with (error) live_feed expects (ws, error)
          NorenApi on_close → called with ()      live_feed expects (ws, code, msg)
        """
        if not self._api:
            logger.error("Cannot start_websocket: not logged in")
            return False

        import threading as _threading

        tick_cb = on_tick or callback
        disconnect_event = _threading.Event()

        def _open_adapter():
            if on_open:
                try:
                    on_open(None)
                except TypeError:
                    on_open()

        def _tick_adapter(message):
            if tick_cb:
                try:
                    tick_cb(None, message)
                except TypeError:
                    tick_cb(message)

        def _error_adapter(error):
            if on_error:
                try:
                    on_error(None, error)
                except TypeError:
                    on_error(error)

        def _close_adapter():
            disconnect_event.set()
            if on_close:
                try:
                    on_close(None, None, None)
                except TypeError:
                    on_close()

        try:
            import websocket as _ws_lib
            import json as _json

            uid = getattr(self, '_user_id', None) or (
                self._api._NorenApi__username if self._api else ""
            )
            jkey = getattr(self, '_jkey', None) or getattr(
                self._api, '_NorenApi__susertoken', None
            )

            raw_ws: list = [None]  # container so inner functions can reference it

            def _raw_on_open(ws):
                raw_ws[0] = ws
                # Old-style Shoonya auth (t:c + susertoken/jKey).
                # The newer t:a (OAuth accesstoken) format gets no server response.
                auth = _json.dumps({
                    "t": "c",
                    "uid": uid,
                    "actid": uid,
                    "susertoken": jkey,
                    "source": "API",
                })
                ws.send(auth)
                logger.info("WebSocket TCP open — sent auth message")

            def _raw_on_message(ws, message):
                try:
                    msg = _json.loads(message)
                except Exception:
                    msg = {}
                # Connection acknowledgement
                if msg.get("t") == "ck":
                    if msg.get("s") == "OK":
                        logger.info("WebSocket auth OK — connection established")
                        _open_adapter()
                    else:
                        logger.error(f"WebSocket auth rejected: {msg}")
                    return
                # Market data tick
                _tick_adapter(msg)

            def _raw_on_error(ws, error):
                _error_adapter(error)

            def _raw_on_close(ws, code, msg):
                _close_adapter()

            ws_app = _ws_lib.WebSocketApp(
                WS_URL,
                on_open=_raw_on_open,
                on_message=_raw_on_message,
                on_error=_raw_on_error,
                on_close=_raw_on_close,
            )
            self._ws_app = ws_app

            logger.info("WebSocket started — waiting for connection")
            ws_app.run_forever(ping_interval=30, ping_payload='{"t":"h"}')
            logger.info("WebSocket disconnected")
            return True

        except Exception:
            logger.exception("Failed to start WebSocket")
            return False

    def close_websocket(self) -> bool:
        """Close the WebSocket connection."""
        try:
            ws_app = getattr(self, '_ws_app', None)
            if ws_app:
                ws_app.close()
                logger.info("WebSocket closed")
            return True
        except Exception:
            logger.exception("Failed to close WebSocket")
            return False

    def subscribe(self, exchange_token_pairs: list[tuple[str, str]]) -> None:
        """Subscribe to touchline feed for (exchange, token) pairs."""
        ws_app = getattr(self, '_ws_app', None)
        if not ws_app:
            logger.warning("subscribe() called before WebSocket is open — ignored")
            return
        instruments = "#".join(f"{ex}|{tok}" for ex, tok in exchange_token_pairs)
        try:
            import json as _json
            ws_app.send(_json.dumps({"t": "t", "k": instruments}))
            logger.info(f"Subscribed: {instruments}")
        except Exception:
            logger.exception(f"Failed to subscribe {instruments}")

    def unsubscribe(self, exchange_token_pairs: list[tuple[str, str]]) -> None:
        """Unsubscribe from touchline feed for (exchange, token) pairs."""
        ws_app = getattr(self, '_ws_app', None)
        if not ws_app:
            return
        instruments = "#".join(f"{ex}|{tok}" for ex, tok in exchange_token_pairs)
        try:
            import json as _json
            ws_app.send(_json.dumps({"t": "u", "k": instruments}))
            logger.info(f"Unsubscribed: {instruments}")
        except Exception:
            logger.exception(f"Failed to unsubscribe {instruments}")
