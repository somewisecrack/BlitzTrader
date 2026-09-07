import inspect
import json

import pyotp

from broker import shoonya_client
from broker.shoonya_client import ShoonyaClient


def test_blitz_shoonya_client_identity_guard():
    assert shoonya_client.CLIENT_APP == "BlitzTrader"
    assert shoonya_client.SUPPORTS_BLITZ_LOGIN_AUTH_CODE is True
    shoonya_client.assert_client_identity("BlitzTrader")


def test_blitz_login_signature_keeps_auth_code_compatibility():
    signature = inspect.signature(ShoonyaClient.login)
    assert "auth_code" in signature.parameters


def test_quickauth_uses_one_oauth_payload_without_credential_fallback(monkeypatch):
    class Response:
        status_code = 200
        text = json.dumps({"stat": "Ok", "susertoken": "token"})

    client = ShoonyaClient()
    requests = []

    def post(*args, **kwargs):
        requests.append((args, kwargs))
        return Response()

    monkeypatch.setattr(client._session, "post", post)
    monkeypatch.setattr(pyotp.TOTP, "now", lambda _: "123456")

    token, error = client._quickauth(
        "FA125387", "password", "JBSWY3DPEHPK3PXP", "FA125387_U", "ignored", "imei"
    )

    assert (token, error) == ("token", None)
    assert len(requests) == 1
    payload = json.loads(requests[0][1]["data"].removeprefix("jData="))
    assert payload["vc"] == "NOREN_API"
    assert payload["app_key"] == "FA125387_U"
    assert "appkey" in payload
    assert "api_key" not in payload


def test_quickauth_account_block_is_not_retried(monkeypatch):
    class Response:
        status_code = 200
        text = json.dumps({"stat": "Not_Ok", "emsg": "Invalid Input : User Blocked due to multiple wrong attempts"})

    client = ShoonyaClient()
    requests = []

    def post(*args, **kwargs):
        requests.append((args, kwargs))
        return Response()

    monkeypatch.setattr(client._session, "post", post)
    monkeypatch.setattr(pyotp.TOTP, "now", lambda _: "123456")

    token, error = client._quickauth(
        "FA125387", "password", "JBSWY3DPEHPK3PXP", "FA125387_U", "ignored", "imei"
    )

    assert token is None
    assert "User Blocked" in error
    assert len(requests) == 1
    assert shoonya_client.is_non_retryable_login_error(error)
