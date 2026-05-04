import inspect

from broker import shoonya_client
from broker.shoonya_client import ShoonyaClient


def test_blitz_shoonya_client_identity_guard():
    assert shoonya_client.CLIENT_APP == "BlitzTrader"
    assert shoonya_client.SUPPORTS_BLITZ_LOGIN_AUTH_CODE is True
    shoonya_client.assert_client_identity("BlitzTrader")


def test_blitz_login_signature_keeps_auth_code_compatibility():
    signature = inspect.signature(ShoonyaClient.login)
    assert "auth_code" in signature.parameters
