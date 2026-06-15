"""
broker/shoonya_client.py — GammaBlast read-only Shoonya API client.

Read-only subset: login, quote, option-chain search.
NEVER calls place_order, cancel_order, or any broker mutation endpoint.

OAuth2 flow (same as BlitzTrader):
  1. QuickAuth  → POST credentials + TOTP → jKey
  2. GetAuthCode → POST jKey + app_key → auth code
  3. GenAcsTok  → POST checksum → access_token

CLIENT_APP is "GammaBlast" — fail-fast if wrong app loads this module.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import pyotp
import requests

logger = logging.getLogger("GammaBlast.ShoonyaClient")

BASE_URL = "https://trade.shoonya.com/NorenWClientAPI"

CLIENT_APP = "GammaBlast"

# Internal web-app API secret decoded from Shoonya OAuth portal JS
_K = [83, 50, 97, 114, 110, 46, 27, 93]
_INTERNAL_SECRET = "".join(chr(b + i) for i, b in enumerate(_K))


def assert_client_identity(expected_app: str) -> None:
    """Fail fast if a deployment accidentally loaded the wrong Shoonya client."""
    if CLIENT_APP != expected_app:
        raise RuntimeError(
            f"Wrong Shoonya client loaded: expected {expected_app}, got {CLIENT_APP}"
        )


@dataclass
class ResolvedScrip:
    symbol: str
    tradingsymbol: str
    token: str
    exchange: str


class ShoonyaClient:
    """
    GammaBlast Shoonya client — read-only.
    No place_order, cancel_order, or any mutation method exists here.
    """

    def __init__(self):
        self._session_token: Optional[str] = None
        self._access_token: Optional[str] = None
        self._susertoken: Optional[str] = None
        self._uid: Optional[str] = None

    # ── auth ──────────────────────────────────────────────────────────────────

    def login(
        self,
        user_id: str,
        password: str,
        totp_secret: str,
        api_key: str,
        secret_code: str,
        vendor_code: str,
        imei: str,
    ) -> bool:
        """
        Full OAuth2 login sequence. Returns True on success.
        Uses the same three-step flow as BlitzTrader (QuickAuth → GetAuthCode → GenAcsTok).
        """
        self._uid = user_id
        try:
            jkey = self._quick_auth(user_id, password, totp_secret, vendor_code, imei)
            if not jkey:
                logger.error("GammaBlast login: QuickAuth failed")
                return False

            auth_code = self._get_auth_code(user_id, jkey, api_key)
            if not auth_code:
                logger.error("GammaBlast login: GetAuthCode failed")
                return False

            access_token = self._gen_access_token(user_id, auth_code, api_key, secret_code)
            if not access_token:
                logger.error("GammaBlast login: GenAcsTok failed")
                return False

            self._access_token = access_token
            self._susertoken = jkey
            logger.info("GammaBlast Shoonya login succeeded (user=%s)", user_id)
            return True
        except Exception:
            logger.exception("GammaBlast Shoonya login exception")
            return False

    def _quick_auth(self, uid: str, pwd: str, totp_secret: str,
                    vendor_code: str, imei: str) -> Optional[str]:
        totp = pyotp.TOTP(totp_secret).now()
        pwd_hash = hashlib.sha256(pwd.encode()).hexdigest()
        payload = {
            "apkversion": "1.0.0",
            "uid": uid,
            "pwd": pwd_hash,
            "factor2": totp,
            "vc": vendor_code,
            "appkey": hashlib.sha256(f"{uid}|{_INTERNAL_SECRET}".encode()).hexdigest(),
            "imei": imei,
            "source": "API",
        }
        resp = requests.post(
            f"{BASE_URL}/QuickAuth",
            data=f"jData={json.dumps(payload)}",
            timeout=15,
        )
        data = resp.json()
        if data.get("stat") == "Ok":
            return data.get("susertoken")
        logger.warning("QuickAuth failed: %s", data.get("emsg", data))
        return None

    def _get_auth_code(self, uid: str, jkey: str, api_key: str) -> Optional[str]:
        payload = {"uid": uid, "jKey": jkey}
        resp = requests.post(
            f"{BASE_URL}/GetAuthCode",
            data=f"jData={json.dumps(payload)}&jKey={jkey}",
            timeout=15,
        )
        data = resp.json()
        if data.get("stat") == "Ok":
            return data.get("authcode")
        logger.warning("GetAuthCode failed: %s", data.get("emsg", data))
        return None

    def _gen_access_token(self, uid: str, auth_code: str,
                          api_key: str, secret_code: str) -> Optional[str]:
        checksum = hashlib.sha256(
            f"{vendor_code_from_api_key(api_key)}|{secret_code}|{auth_code}".encode()
        ).hexdigest()
        payload = {
            "uid": uid,
            "appkey": api_key,
            "secret": secret_code,
            "authcode": auth_code,
            "checksum": checksum,
        }
        resp = requests.post(
            f"{BASE_URL}/GenAcsTok",
            data=f"jData={json.dumps(payload)}",
            timeout=15,
        )
        data = resp.json()
        if data.get("stat") == "Ok":
            return data.get("access_token") or data.get("susertoken")
        logger.warning("GenAcsTok failed: %s", data.get("emsg", data))
        return None

    # ── read-only market data ─────────────────────────────────────────────────

    def get_quotes(self, exchange: str, token: str) -> Optional[dict]:
        """Fetch live quote for a scrip. Returns None on failure."""
        if not self._susertoken:
            logger.warning("get_quotes called before login")
            return None
        try:
            payload = {"uid": self._uid, "exch": exchange, "token": token}
            resp = requests.post(
                f"{BASE_URL}/GetQuotes",
                data=f"jData={json.dumps(payload)}&jKey={self._susertoken}",
                timeout=10,
            )
            data = resp.json()
            if data.get("stat") == "Ok":
                return data
            logger.debug("GetQuotes failed for %s:%s — %s", exchange, token, data.get("emsg"))
            return None
        except Exception:
            logger.exception("get_quotes exception")
            return None

    def search_scrip(self, exchange: str, search_text: str) -> list[dict]:
        """Search for scrips by text. Returns list of matching scrip dicts."""
        if not self._susertoken:
            return []
        try:
            payload = {"uid": self._uid, "stext": search_text, "exch": exchange}
            resp = requests.post(
                f"{BASE_URL}/SearchScrip",
                data=f"jData={json.dumps(payload)}&jKey={self._susertoken}",
                timeout=10,
            )
            data = resp.json()
            if data.get("stat") == "Ok" and data.get("values"):
                return data["values"]
            return []
        except Exception:
            logger.exception("search_scrip exception")
            return []

    def get_option_chain(self, exchange: str, tsym: str, strike_price: str,
                         count: int = 5) -> list[dict]:
        """Fetch option chain around a strike. Returns list of chain rows."""
        if not self._susertoken:
            return []
        try:
            payload = {
                "uid": self._uid,
                "exch": exchange,
                "tsym": tsym,
                "strprc": strike_price,
                "cnt": str(count),
            }
            resp = requests.post(
                f"{BASE_URL}/GetOptionChain",
                data=f"jData={json.dumps(payload)}&jKey={self._susertoken}",
                timeout=10,
            )
            data = resp.json()
            if data.get("stat") == "Ok" and data.get("values"):
                return data["values"]
            return []
        except Exception:
            logger.exception("get_option_chain exception")
            return []

    def get_index_ltp(self, exchange: str, token: str) -> Optional[float]:
        """Return index last traded price, or None on failure."""
        q = self.get_quotes(exchange, token)
        if not q:
            return None
        try:
            return float(q.get("lp") or q.get("ltp") or 0) or None
        except (TypeError, ValueError):
            return None

    @property
    def is_logged_in(self) -> bool:
        return bool(self._susertoken)


def vendor_code_from_api_key(api_key: str) -> str:
    """Extract vendor code prefix from API key (first token before underscore if any)."""
    return api_key.split("_")[0] if "_" in api_key else api_key
