"""
tests/test_futures_resolution.py
---------------------------------
Validates that get_front_month_futures_token() resolves the CORRECT front-month
FUTIDX contract and does NOT confuse NIFTY with NIFTYNXT50, FINNIFTY, etc.

Run: python -m pytest tests/test_futures_resolution.py -v
"""
import sys
import os
import datetime
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is on path when run directly or via pytest from project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_scrip(
    tsym: str,
    instname: str,
    exd: str,
    token: str = "99999",
    lot_size: int = 25,
) -> dict:
    """Helper to build a minimal scrip dict as returned by search_scrip."""
    return {
        "tsym": tsym,
        "instname": instname,
        "exd": exd,
        "token": token,
        "ls": str(lot_size),
        "symname": tsym.split("2")[0] if "2" in tsym else tsym,
    }


def _future_expiry(days_ahead: int = 18) -> str:
    """Return an expiry date string 'days_ahead' days from today."""
    d = datetime.date.today() + datetime.timedelta(days=days_ahead)
    return d.strftime("%d-%b-%Y").upper()   # e.g. "28-APR-2026"


def _past_expiry(days_ago: int = 5) -> str:
    """Return a past expiry date string."""
    d = datetime.date.today() - datetime.timedelta(days=days_ago)
    return d.strftime("%d-%b-%Y").upper()


def _build_client(scrips: list) -> "ShoonyaClient-mock":
    """
    Return a ShoonyaClient with search_scrip mocked to return scrips.

    search_scrip uses _post_private (raw OAuth REST path), so we mock
    _post_private directly rather than _api.searchscrip, which is the
    old NorenRestApiPy jKey path that broke after April-2026 OAuth migration.
    """
    from broker.shoonya_client import ShoonyaClient
    client = ShoonyaClient.__new__(ShoonyaClient)
    client._api = MagicMock()
    client._logged_in = True
    client._session = MagicMock()
    client._user_id = "TESTUSER"
    client._jkey = "testjkey"
    # Mock at the _post_private level — search_scrip calls _post_private("SearchScrip", ...)
    client._post_private = MagicMock(return_value={"stat": "Ok", "values": scrips})
    return client


class TestNiftyResolvesCorrectly(unittest.TestCase):
    """NIFTY must resolve to NIFTY front-month, not NIFTYNXT50 or FINNIFTY."""

    def test_nifty_resolves_to_nifty_not_niftynxt50(self):
        """When search returns NIFTYNXT50, NIFTY28APR26F, FINNIFTY — result must be NIFTY28APR26F."""
        expiry = _future_expiry(18)
        scrips = [
            _make_scrip("NIFTYNXT5028APR26F", "FUTIDX", expiry, token="11111"),
            _make_scrip("NIFTY28APR26F",       "FUTIDX", expiry, token="22222"),
            _make_scrip("FINNIFTY28APR26F",    "FUTIDX", expiry, token="33333"),
            _make_scrip("MIDCPNIFTY28APR26F",  "FUTIDX", expiry, token="44444"),
        ]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("NIFTY")

        self.assertIsNotNone(result, "Should resolve to NIFTY front-month futures")
        self.assertEqual(result["tsym"], "NIFTY28APR26F",
                         f"Expected NIFTY28APR26F but got {result['tsym']}")
        self.assertEqual(result["token"], "22222")
        self.assertEqual(result["name"], "NIFTY")
        self.assertEqual(result["exchange"], "NFO")

    def test_nifty_skips_non_futidx(self):
        """FUTSTK and FUTCUR instname contracts must be filtered out."""
        expiry = _future_expiry(18)
        scrips = [
            _make_scrip("NIFTY28APR26F", "FUTSTK", expiry, token="55555"),  # wrong instname
            _make_scrip("NIFTY28APR26F", "FUTIDX", expiry, token="66666"),  # correct
        ]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("NIFTY")
        self.assertIsNotNone(result)
        self.assertEqual(result["token"], "66666", "Should pick the FUTIDX entry")

    def test_nifty_resolves_when_only_correct_contract_present(self):
        """Baseline: single correct NIFTY FUTIDX is selected."""
        expiry = _future_expiry(18)
        scrips = [_make_scrip("NIFTY28APR26F", "FUTIDX", expiry, token="77777")]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("NIFTY")
        self.assertIsNotNone(result)
        self.assertEqual(result["tsym"], "NIFTY28APR26F")


class TestBankniftyResolvesCorrectly(unittest.TestCase):
    """BANKNIFTY must resolve to BANKNIFTY front-month, not other derivatives."""

    def test_banknifty_resolves_correctly(self):
        """When search returns multiple, BANKNIFTY28APR26F must be selected."""
        expiry = _future_expiry(18)
        scrips = [
            _make_scrip("BANKNIFTY28APR26F", "FUTIDX", expiry, token="88888"),
            _make_scrip("NIFTY28APR26F",     "FUTIDX", expiry, token="99999"),  # wrong symbol
        ]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("BANKNIFTY")
        self.assertIsNotNone(result)
        self.assertEqual(result["tsym"], "BANKNIFTY28APR26F")
        self.assertEqual(result["token"], "88888")
        self.assertEqual(result["name"], "BANKNIFTY")

    def test_banknifty_not_confused_with_nifty(self):
        """BANKNIFTY search must not accidentally return NIFTY contracts."""
        expiry = _future_expiry(18)
        # Both BANKNIFTY and NIFTY contracts in results (search_scrip may return both)
        scrips = [
            _make_scrip("NIFTY28APR26F",     "FUTIDX", expiry, token="11111"),
            _make_scrip("BANKNIFTY28APR26F", "FUTIDX", expiry, token="22222"),
        ]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("BANKNIFTY")
        self.assertIsNotNone(result)
        self.assertEqual(result["tsym"], "BANKNIFTY28APR26F")


class TestFinniftyResolvesCorrectly(unittest.TestCase):
    """FINNIFTY is a supported front-month FUTIDX instrument."""

    def test_finnifty_resolves_to_finnifty_not_nifty(self):
        """FINNIFTY query must select FINNIFTY and preserve Shoonya lot size."""
        expiry = _future_expiry(18)
        scrips = [
            _make_scrip("NIFTY28APR26F", "FUTIDX", expiry, token="11111", lot_size=25),
            _make_scrip("FINNIFTY28APR26F", "FUTIDX", expiry, token="33333", lot_size=60),
        ]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("FINNIFTY")

        self.assertIsNotNone(result, "Should resolve to FINNIFTY front-month futures")
        self.assertEqual(result["tsym"], "FINNIFTY28APR26F")
        self.assertEqual(result["token"], "33333")
        self.assertEqual(result["name"], "FINNIFTY")
        self.assertEqual(result["exchange"], "NFO")
        self.assertEqual(result["lot_size"], 60)


class TestNoMatchReturnsNone(unittest.TestCase):
    """When only wrong contracts are present, None should be returned."""

    def test_no_match_returns_none_when_only_niftynxt50(self):
        """If search returns only NIFTYNXT50 for NIFTY query, result must be None."""
        expiry = _future_expiry(18)
        scrips = [_make_scrip("NIFTYNXT5028APR26F", "FUTIDX", expiry, token="11111")]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("NIFTY")
        self.assertIsNone(result,
                          "NIFTYNXT50 should NOT match ^NIFTY\\d — result must be None")

    def test_no_match_returns_none_when_only_finnifty(self):
        """FINNIFTY contracts must not match a NIFTY query."""
        expiry = _future_expiry(18)
        scrips = [_make_scrip("FINNIFTY28APR26F", "FUTIDX", expiry, token="22222")]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("NIFTY")
        self.assertIsNone(result, "FINNIFTY should NOT match NIFTY prefix")

    def test_no_match_returns_none_for_empty_results(self):
        """Empty search result must return None gracefully."""
        from broker.shoonya_client import ShoonyaClient
        client = ShoonyaClient.__new__(ShoonyaClient)
        client._api = MagicMock()
        client._logged_in = True
        client._session = MagicMock()
        client._api.searchscrip.return_value = {"stat": "Ok", "values": []}
        result = client.get_front_month_futures_token("NIFTY")
        self.assertIsNone(result)


class TestExpiredContractSkipped(unittest.TestCase):
    """Expired contracts (expiry < today) must be filtered out."""

    def test_expired_contract_skipped(self):
        """If the only NIFTY FUTIDX has a past expiry, result must be None."""
        scrips = [_make_scrip("NIFTY10APR26F", "FUTIDX", _past_expiry(5), token="11111")]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("NIFTY")
        self.assertIsNone(result, "Expired contracts must be excluded")

    def test_nearest_valid_expiry_chosen(self):
        """When multiple valid contracts exist, the nearest expiry must be chosen."""
        near_expiry = _future_expiry(18)
        far_expiry  = _future_expiry(46)
        scrips = [
            _make_scrip("NIFTY28MAY26F", "FUTIDX", far_expiry,  token="FAR11"),
            _make_scrip("NIFTY28APR26F", "FUTIDX", near_expiry, token="NEAR11"),
        ]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("NIFTY")
        self.assertIsNotNone(result)
        self.assertEqual(result["token"], "NEAR11", "Front-month (nearest) should be selected")

    def test_expired_filtered_nearest_valid_chosen(self):
        """Expired + valid contracts: expired must be dropped, nearest valid returned."""
        near_expiry = _future_expiry(18)
        scrips = [
            _make_scrip("NIFTY10APR26F", "FUTIDX", _past_expiry(5), token="EXPIRED"),
            _make_scrip("NIFTY28APR26F", "FUTIDX", near_expiry,     token="VALID11"),
        ]
        client = _build_client(scrips)
        result = client.get_front_month_futures_token("NIFTY")
        self.assertIsNotNone(result)
        self.assertEqual(result["token"], "VALID11")


if __name__ == "__main__":
    unittest.main(verbosity=2)
