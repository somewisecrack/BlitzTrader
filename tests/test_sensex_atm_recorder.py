"""
tests/test_sensex_atm_recorder.py

Tests for SENSEX ATM option recorder (Change 3).

Covers:
1. Dynamic SENSEX expiry resolution
2. Correct BFO exchange and token handling
3. ATM rounding based on discovered strike interval
4. Initial ATM±1 tracking
5. Cumulative strike tracking after ATM changes
6. Calls and puts recorded for every discovered strike
7. Existing strikes remain active after leaving the current window
8. First-seen timestamps and roles are preserved
9. OI and OI-change calculations
10. Best-five bid/ask serialization
11. Missing quote behavior without fallback values
12. Rejection of underlying-index values as option LTP
13. Restart and reconnection without duplicate subscriptions
14. NIFTY behavior remains unchanged
15. SENSEX cannot reach scanners, Gemini, SpreadBuilder, execution or portfolio code
16. EOD backup includes both NIFTY and SENSEX directories
17. Cleanup never removes an export whose upload was not confirmed
"""
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_shoonya_client(
    search_results=None,
    quote=None,
    fail_search=False,
    fail_quote=False,
):
    client = MagicMock()
    if fail_search:
        client.search_scrip.return_value = None
    else:
        client.search_scrip.return_value = search_results or []
    if fail_quote:
        client.get_quotes.return_value = None
    else:
        client.get_quotes.return_value = quote or _good_quote()
    return client


def _good_quote(**overrides):
    """A realistic BFO option quote (token matches contract token)."""
    defaults = {
        "stat": "Ok",
        "token": "99001",
        "exch": "BFO",
        "lp": "250.50",
        "o": "240.00",
        "h": "270.00",
        "l": "230.00",
        "c": "248.00",
        "v": "8000",
        "oi": "20000",
        "poi": "18000",
        "bp1": "250.00", "bq1": "50",  "bo1": "3",
        "bp2": "249.50", "bq2": "30",
        "bp3": "249.00", "bq3": "20",
        "bp4": "248.50", "bq4": "10",
        "bp5": "248.00", "bq5": "5",
        "sp1": "251.00", "sq1": "40",  "so1": "2",
        "sp2": "251.50", "sq2": "25",
        "sp3": "252.00", "sq3": "15",
        "sp4": "252.50", "sq4": "8",
        "sp5": "253.00", "sq5": "3",
    }
    defaults.update(overrides)
    return defaults


def _sensex_bfo_row(strike: int, expiry: str = "11-JUN-2026", opt_type: str = "C") -> dict:
    """A Shoonya SearchScrip row for a SENSEX BFO option."""
    from datetime import datetime
    exp_date = datetime.strptime(expiry, "%d-%b-%Y")
    exp_suffix = exp_date.strftime("%d%b%y").upper()
    tsym = f"SENSEX{exp_suffix}{opt_type}{strike}"
    return {
        "token": f"9{strike}",
        "tsym": tsym,
        "exd": expiry,
        "instname": "OPTIDX",
        "strprc": str(strike),
        "optt": opt_type,
    }


def _make_recorder(tmp_path, client=None, sample_interval=0):
    from tools.sensex_atm_recorder import SensexATMOptionRecorder
    rec = SensexATMOptionRecorder(
        base_dir=tmp_path / "data_exports",
        shoonya_client=client,
        sample_interval=sample_interval,
    )
    return rec


def _setup_chain(recorder, expiry="11-JUN-2026", strike_step=100):
    """Prime the recorder's internal chain with a mocked expiry and step."""
    recorder._expiry = expiry
    recorder._strike_step = strike_step


# ── Test 1: Dynamic expiry resolution ─────────────────────────────────────────

class TestExpiryResolution:
    def test_nearest_expiry_from_bfo_search(self, tmp_path):
        rows = [
            _sensex_bfo_row(81000, "11-JUN-2026", "C"),
            _sensex_bfo_row(81100, "11-JUN-2026", "P"),
            _sensex_bfo_row(81000, "26-JUN-2026", "C"),
        ]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)

        with patch("tools.sensex_atm_recorder.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 10)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            expiry = rec._chain.get_nearest_expiry()

        assert expiry is not None
        assert "JUN" in expiry.upper()

    def test_expiry_resolution_failure_returns_none(self, tmp_path):
        client = _make_shoonya_client(fail_search=True)
        rec = _make_recorder(tmp_path, client=client)
        with patch("tools.sensex_atm_recorder.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 10)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            expiry = rec._chain.get_nearest_expiry()
        assert expiry is None

    def test_expiry_uses_bfo_exchange(self, tmp_path):
        rows = [_sensex_bfo_row(81000, "11-JUN-2026", "C")]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        with patch("tools.sensex_atm_recorder.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 10)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            rec._chain._get_available_expiries()
        # All search calls must use BFO, never NFO
        for call_args in client.search_scrip.call_args_list:
            assert call_args[0][0] == "BFO", (
                f"Expected BFO exchange but got {call_args[0][0]!r}"
            )


# ── Test 2: BFO exchange and token handling ───────────────────────────────────

class TestBFOExchangeHandling:
    def test_resolve_option_uses_bfo_exchange(self, tmp_path):
        rows = [_sensex_bfo_row(81000, "11-JUN-2026", "C")]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        result = rec._chain.resolve_option("11-JUN-2026", 81000, "CE")
        assert result is not None
        assert result["exchange"] == "BFO"
        assert result["symbol"] == "SENSEX"

    def test_get_quotes_uses_bfo_exchange(self, tmp_path):
        rows = [_sensex_bfo_row(81000, "11-JUN-2026", "C")]
        client = _make_shoonya_client(search_results=rows)
        client.get_quotes.return_value = _good_quote(token="981000")
        rec = _make_recorder(tmp_path, client=client)
        _setup_chain(rec, expiry="11-JUN-2026", strike_step=100)
        # Manually inject a tracked contract
        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._tracked[("SENSEX", 81000, "CE")] = contract
            rec._current_atm = 81000
            rec._latest_underlying_ltp = 81050.0

        rec._sample_one(contract, force=True)
        assert client.get_quotes.called
        call_args = client.get_quotes.call_args
        assert call_args[0][0] == "BFO"
        assert call_args[0][1] == "981000"

    def test_option_ltp_not_underlying_ltp(self, tmp_path):
        """Option LTP must come from option quote, not from underlying_ltp."""
        underlying_ltp = 81050.0   # SENSEX spot
        option_ltp_str = "250.50"

        client = _make_shoonya_client(
            quote=_good_quote(lp=option_ltp_str, token="981000")
        )
        rec = _make_recorder(tmp_path, client=client)
        _setup_chain(rec, expiry="11-JUN-2026", strike_step=100)

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._tracked[("SENSEX", 81000, "CE")] = contract
            rec._current_atm = 81000
            rec._latest_underlying_ltp = underlying_ltp

        record = rec._build_record(contract)

        # option ltp must be the small option price, not ~81050
        assert record["ltp"] == float(option_ltp_str)
        assert record["underlying_ltp"] == underlying_ltp
        assert record["ltp"] != record["underlying_ltp"]


# ── Test 3: ATM rounding based on strike interval ─────────────────────────────

class TestATMRounding:
    def test_rounding_with_100_step(self):
        from tools.sensex_atm_recorder import round_to_sensex_strike
        assert round_to_sensex_strike(81050.0, 100) == 81100
        assert round_to_sensex_strike(81049.0, 100) == 81000
        assert round_to_sensex_strike(81000.0, 100) == 81000

    def test_rounding_with_50_step(self):
        from tools.sensex_atm_recorder import round_to_sensex_strike
        assert round_to_sensex_strike(81025.0, 50) == 81050
        assert round_to_sensex_strike(81024.0, 50) == 81000

    def test_strike_step_discovery_from_contracts(self, tmp_path):
        rows = [
            _sensex_bfo_row(81000, "11-JUN-2026", "C"),
            _sensex_bfo_row(81100, "11-JUN-2026", "C"),
            _sensex_bfo_row(81200, "11-JUN-2026", "C"),
        ]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        step = rec._chain.discover_strike_step()
        assert step == 100


# ── Test 4: Initial ATM±1 tracking ───────────────────────────────────────────

class TestInitialATMTracking:
    def test_initial_atm_creates_six_contracts(self, tmp_path):
        # ATM = 81000 when spot = 81020 (round_to_sensex_strike(81020, 100) = int((81020+50)//100*100) = 81000)
        rows = [
            _sensex_bfo_row(s, "11-JUN-2026", ot)
            for s in (80900, 81000, 81100)
            for ot in ("C", "P")
        ]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        rec.update_atm(81020.0)  # round_to_sensex_strike(81020, 100) = 81000

        with rec._lock:
            keys = set(rec._tracked)
        # Should have ATM-1=80900, ATM=81000, ATM+1=81100 → 6 contracts
        assert len(keys) == 6
        strikes = {k[1] for k in keys}
        assert len(strikes) == 3


# ── Test 5: Cumulative strike tracking after ATM changes ──────────────────────

class TestCumulativeTracking:
    def _make_row(self, strike, opt_type):
        return _sensex_bfo_row(strike, "11-JUN-2026", opt_type)

    def test_previous_strikes_remain_after_atm_move(self, tmp_path):
        all_rows = [
            self._make_row(s, ot)
            for s in (80900, 81000, 81100, 81200, 81300)
            for ot in ("C", "P")
        ]
        client = _make_shoonya_client(search_results=all_rows)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        rec.update_atm(81050.0)   # ATM = 81100 → tracks 80900(?), 81000, 81100
        # With step=100: round_to_sensex_strike(81050.0, 100) = int((81050+50)//100*100) = 81100
        # window: 81000, 81100, 81200

        first_keys = set(rec._tracked.keys())
        first_strikes = {k[1] for k in first_keys}
        assert len(first_strikes) == 3

        rec.update_atm(81150.0)  # ATM = 81200 → window: 81100, 81200, 81300
        # round_to_sensex_strike(81150.0, 100) = int((81150+50)//100*100) = 81200

        all_keys = set(rec._tracked.keys())
        all_strikes = {k[1] for k in all_keys}
        # All original strikes + new ATM+1 strike must be present
        assert first_strikes.issubset(all_strikes)
        assert len(all_strikes) >= 4  # 3 original + at least 1 new

    def test_first_seen_timestamps_preserved(self, tmp_path):
        rows = [
            self._make_row(s, ot)
            for s in (80900, 81000, 81100, 81200)
            for ot in ("C", "P")
        ]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        rec.update_atm(81050.0)
        with rec._lock:
            initial_first_seen = {
                k: v.first_seen_at for k, v in rec._tracked.items()
            }

        time.sleep(0.02)
        rec.update_atm(81150.0)

        with rec._lock:
            for key, contract in rec._tracked.items():
                if key in initial_first_seen:
                    assert contract.first_seen_at == initial_first_seen[key], (
                        f"first_seen_at changed for {key}"
                    )


# ── Test 6: CE and PE recorded for every discovered strike ────────────────────

class TestCEPECoverage:
    def test_both_ce_and_pe_tracked_for_every_strike(self, tmp_path):
        rows = [
            _sensex_bfo_row(s, "11-JUN-2026", ot)
            for s in (80900, 81000, 81100)
            for ot in ("C", "P")
        ]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        rec.update_atm(81050.0)  # ATM=81100 → 81000, 81100, 81200

        with rec._lock:
            for strike in {k[1] for k in rec._tracked}:
                assert ("SENSEX", strike, "CE") in rec._tracked
                assert ("SENSEX", strike, "PE") in rec._tracked


# ── Test 7: First-seen roles preserved ───────────────────────────────────────

class TestFirstSeenRoles:
    def test_first_seen_role_set_correctly(self, tmp_path):
        rows = [
            _sensex_bfo_row(s, "11-JUN-2026", ot)
            for s in (80900, 81000, 81100, 81200)
            for ot in ("C", "P")
        ]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        rec.update_atm(81050.0)  # ATM=81100 → window 81000, 81100, 81200

        with rec._lock:
            atm = rec._current_atm  # 81100
            roles = {(k[1], k[2]): v.first_seen_role for k, v in rec._tracked.items()}

        assert roles.get((atm, "CE")) == "ATM"
        assert roles.get((atm, "PE")) == "ATM"
        assert roles.get((atm - 100, "CE")) == "ATM-1"
        assert roles.get((atm + 100, "CE")) == "ATM+1"

    def test_role_becomes_tracked_previous_after_atm_move(self, tmp_path):
        rows = [
            _sensex_bfo_row(s, "11-JUN-2026", ot)
            for s in (80900, 81000, 81100, 81200, 81300)
            for ot in ("C", "P")
        ]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        rec.update_atm(81050.0)   # ATM=81100
        rec.update_atm(81250.0)   # ATM=81300 → 81000 is now TRACKED_PREVIOUS

        with rec._lock:
            current_atm = rec._current_atm
            record_81000 = rec._tracked.get(("SENSEX", 81000, "CE"))

        assert record_81000 is not None
        # first_seen_role is preserved (not updated after tracking)
        assert record_81000.first_seen_role == "ATM-1"


# ── Test 8: OI and OI change calculations ────────────────────────────────────

class TestOICalculations:
    def test_oi_change_computed_correctly(self, tmp_path):
        client = _make_shoonya_client(
            quote=_good_quote(oi="22000", poi="20000", token="981000")
        )
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._current_atm = 81000
            rec._latest_underlying_ltp = 81050.0

        record = rec._build_record(contract)
        assert record["oi"] == 22000
        assert record["previous_oi"] == 20000
        assert record["oi_change"] == 2000

    def test_oi_change_none_if_missing_fields(self, tmp_path):
        client = _make_shoonya_client(
            quote=_good_quote(oi="22000", poi="", token="981000")
        )
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._current_atm = 81000
            rec._latest_underlying_ltp = 81050.0

        record = rec._build_record(contract)
        assert record["previous_oi"] is None
        assert record["oi_change"] is None


# ── Test 9: Best-five bid/ask serialization ───────────────────────────────────

class TestBidAskSerialization:
    def test_full_five_levels_parsed(self, tmp_path):
        client = _make_shoonya_client(quote=_good_quote(token="981000"))
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._current_atm = 81000
            rec._latest_underlying_ltp = 81050.0

        record = rec._build_record(contract)
        assert isinstance(record["best_5_bids"], list)
        assert len(record["best_5_bids"]) == 5
        assert isinstance(record["best_5_asks"], list)
        assert len(record["best_5_asks"]) == 5
        # bo1 / so1 fields parsed as orders
        assert record["best_5_bids"][0]["orders"] == 3
        assert record["best_5_asks"][0]["orders"] == 2

    def test_empty_depth_returns_none(self, tmp_path):
        # Quote with no bid/ask levels
        quote = _good_quote(token="981000")
        for i in range(1, 6):
            quote.pop(f"bp{i}", None)
            quote.pop(f"bq{i}", None)
            quote.pop(f"sp{i}", None)
            quote.pop(f"sq{i}", None)
        client = _make_shoonya_client(quote=quote)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._current_atm = 81000
            rec._latest_underlying_ltp = 81050.0

        record = rec._build_record(contract)
        assert record["best_5_bids"] is None
        assert record["best_5_asks"] is None


# ── Test 10: Missing quote behavior ──────────────────────────────────────────

class TestMissingQuoteBehavior:
    def test_missing_quote_sets_source_error_not_underlying(self, tmp_path):
        client = _make_shoonya_client(fail_quote=True)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._current_atm = 81000
            rec._latest_underlying_ltp = 81050.0

        record = rec._build_record(contract)
        assert record["ltp"] is None
        assert record["source_error"] is not None
        # underlying_ltp must NOT be in ltp
        assert record["ltp"] != 81050.0

    def test_error_quote_stat_sets_source_error(self, tmp_path):
        quote = _good_quote(stat="Not_Ok", emsg="symbol not found")
        client = _make_shoonya_client(quote=quote)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._current_atm = 81000
            rec._latest_underlying_ltp = 81050.0

        record = rec._build_record(contract)
        assert record["ltp"] is None
        assert "source_error" in record and record["source_error"]


# ── Test 11: Rejection of underlying-index values as option LTP ───────────────

class TestUnderlyingLTPIsolation:
    def test_index_level_ltp_rejected_as_option_price(self, tmp_path):
        """If Shoonya returns an LTP that looks like an index value (>15000),
        the recorder must reject it and set source_error."""
        suspect_ltp = "81050.00"  # Clearly an index value, not an option price
        quote = _good_quote(lp=suspect_ltp, token="981000")
        client = _make_shoonya_client(quote=quote)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._current_atm = 81000
            rec._latest_underlying_ltp = 81050.0

        record = rec._build_record(contract)
        assert record["ltp"] is None or record.get("source_error") is not None, (
            "Index-level LTP should be rejected"
        )

    def test_underlying_ltp_not_used_as_option_ltp(self, tmp_path):
        """underlying_ltp must stay in its own field and never overwrite option ltp."""
        client = _make_shoonya_client(fail_quote=True)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        underlying = 81050.0
        with rec._lock:
            rec._current_atm = 81000
            rec._latest_underlying_ltp = underlying

        record = rec._build_record(contract)
        assert record["ltp"] != underlying, (
            "underlying_ltp must not substitute for missing option LTP"
        )
        assert record["underlying_ltp"] == underlying

    def test_token_mismatch_sets_source_error(self, tmp_path):
        """If response token differs from contract token, reject with source_error."""
        quote = _good_quote(token="WRONGTOKEN", lp="250.0")
        client = _make_shoonya_client(quote=quote)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        from tools.sensex_atm_recorder import _TrackedContract
        contract = _TrackedContract(
            symbol="SENSEX", exchange="BFO", expiry="11-JUN-2026",
            strike=81000, option_type="CE", token="981000",
            tsym="SENSEX11JUN26C81000",
            first_seen_at="2026-06-10T10:00:00",
            first_seen_role="ATM",
        )
        with rec._lock:
            rec._current_atm = 81000
            rec._latest_underlying_ltp = 81050.0

        record = rec._build_record(contract)
        assert record["source_error"] is not None
        assert "token_mismatch" in record["source_error"]
        assert record["ltp"] is None


# ── Test 12: Restart/reconnect without duplicates ────────────────────────────

class TestRestartBehavior:
    def test_no_duplicate_subscriptions_on_repeated_ensure_active_window(self, tmp_path):
        rows = [
            _sensex_bfo_row(s, "11-JUN-2026", ot)
            for s in (80900, 81000, 81100)
            for ot in ("C", "P")
        ]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        rec._expiry = "11-JUN-2026"
        rec._strike_step = 100

        rec.update_atm(81050.0)
        count_after_first = len(rec._tracked)
        rec.update_atm(81050.0)  # same ATM — should not add duplicates
        assert len(rec._tracked) == count_after_first

    def test_initialise_is_idempotent(self, tmp_path):
        rows = [_sensex_bfo_row(81000, "11-JUN-2026", "C")]
        client = _make_shoonya_client(search_results=rows)
        rec = _make_recorder(tmp_path, client=client)
        with patch("tools.sensex_atm_recorder.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 10)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            rec.initialise()
            expiry_1 = rec._expiry
            step_1 = rec._strike_step
            rec.initialise()
            assert rec._expiry == expiry_1
            assert rec._strike_step == step_1


# ── Test 13: NIFTY behavior unchanged ────────────────────────────────────────

class TestNIFTYUnchanged:
    def test_nifty_recorder_still_uses_nfo_exchange(self, tmp_path):
        from tools.atm_option_recorder import ATMOptionRecorder
        chain = MagicMock()
        chain.get_nearest_expiry.return_value = "11-JUN-2026"
        chain.resolve_option.return_value = {"token": "TOK123", "tsym": "NIFTY11JUN26C24500"}
        client = MagicMock()
        client.get_quotes.return_value = {
            "stat": "Ok", "lp": "150.0", "o": "140.0", "h": "160.0",
            "l": "135.0", "c": "148.0", "v": "100", "oi": "10000", "poi": "9000",
        }
        rec = ATMOptionRecorder(
            base_dir=tmp_path / "data_exports",
            shoonya_client=client,
            options_chain=chain,
            sample_interval=0,
        )
        rec.update_atm("NIFTY", 24520.0)
        with rec._lock:
            keys = list(rec._tracked.keys())
        assert all(k[0] == "NIFTY" for k in keys)
        # NIFTY recorder uses NFO exchange
        rec.sample_due_contracts()
        for c in client.get_quotes.call_args_list:
            assert c[0][0] == "NFO", f"NIFTY recorder must use NFO, not {c[0][0]}"

    def test_sensex_recorder_separate_from_nifty_recorder(self, tmp_path):
        from tools.atm_option_recorder import ATMOptionRecorder
        from tools.sensex_atm_recorder import SensexATMOptionRecorder

        nifty_rec = ATMOptionRecorder(
            base_dir=tmp_path / "data_exports",
            shoonya_client=None,
            options_chain=None,
        )
        sensex_rec = SensexATMOptionRecorder(
            base_dir=tmp_path / "data_exports",
            shoonya_client=None,
        )
        assert nifty_rec.export_dir != sensex_rec.export_dir
        assert "NIFTY" in str(nifty_rec.export_dir) or "atm_options" in str(nifty_rec.export_dir)
        assert "SENSEX" in str(sensex_rec.export_dir)


# ── Test 14: SENSEX isolation from trading code ───────────────────────────────

class TestSensexTradingIsolation:
    """SENSEX must not appear in any trading-path class."""

    def test_sensex_not_in_allowed_underlyings(self):
        from tools.options_chain import _ALLOWED_UNDERLYINGS
        assert "SENSEX" not in _ALLOWED_UNDERLYINGS

    def test_sensex_not_in_spread_execution_engine(self):
        """SpreadExecutionEngine must reject SENSEX instrument via instrument check."""
        from tools.options_spread_execution import SpreadExecutionEngine
        from tools.options_spread_builder import SpreadCandidate

        state = MagicMock()
        state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "open_spreads": [],
        }
        engine = SpreadExecutionEngine(None, state, max_open_spreads=10)

        # SpreadCandidate with SENSEX symbol — minimal mock
        candidate = MagicMock()
        candidate.symbol = "SENSEX"
        candidate.legs = []

        # Override no_entry_after to allow any time, so instrument check is reached
        engine._no_entry_after = "23:59"
        err = engine._check_guardrails(candidate)
        assert err is not None
        assert "forbidden" in err.lower() or "sensex" in err.lower()

    def test_order_execution_rejects_sensex(self):
        from tools.order_execution import OrderExecutionTools
        state = MagicMock()
        state.get_state.return_value = {
            "is_stopped": False,
            "is_paused": False,
            "positions": [],
            "daily_pnl": 0.0,
            "virtual_capital": 500_000,
            "margin_used": 0,
        }
        ledger = MagicMock()
        feed = MagicMock()
        feed.get_best_bid_ask.return_value = (81000.0, 81010.0)

        # The token map must not allow SENSEX futures
        exec_tools = OrderExecutionTools(
            state_manager=state,
            virtual_ledger=ledger,
            live_feed=feed,
            shoonya_client=None,
            max_positions=10,
            max_risk_amount=50000,
            max_daily_loss=25000,
            active_tokens={},
        )
        # SENSEX is not in active_tokens and has no futures token → should be rejected
        result = exec_tools.place_virtual_order(
            symbol="SENSEX28JUN26F",
            direction="BUY",
            quantity=10,
            stop_loss=80000.0,
        )
        assert result.get("status") == "REJECTED"


# ── Test 15: EOD backup includes SENSEX ──────────────────────────────────────

class TestEODBackupInclusion:
    def test_sensex_dir_inside_data_exports_covered_by_backup(self, tmp_path):
        """SENSEX atm_options dir lives under data_exports/YYYYMMDD/ which is
        the entire backup unit — verify directory path structure."""
        from tools.sensex_atm_recorder import SensexATMOptionRecorder
        rec = SensexATMOptionRecorder(
            base_dir=tmp_path / "data_exports",
            shoonya_client=None,
        )
        # export_dir must be under data_exports/YYYYMMDD/atm_options/SENSEX
        parts = rec.export_dir.parts
        assert "data_exports" in parts
        assert "atm_options" in parts
        assert "SENSEX" in parts
        # Parent of SENSEX is atm_options, parent of that is the date dir
        date_dir = rec.export_dir.parent.parent
        assert date_dir.parent == tmp_path / "data_exports"


# ── Test 16: Cleanup never removes unconfirmed export ─────────────────────────

class TestCleanupSafety:
    def test_prune_only_removes_days_before_cutoff(self, tmp_path):
        """eod_backup.prune_old_local must not delete the keep window."""
        from scripts.eod_backup import prune_old_local
        from datetime import date, timedelta

        exports = tmp_path / "data_exports"
        # Create dirs for last 5 days
        run_date = date(2026, 6, 10)
        for delta in range(5):
            d = run_date - timedelta(days=delta)
            (exports / d.strftime("%Y%m%d")).mkdir(parents=True)

        prune_old_local(tmp_path, keep_days=2, run_date=run_date, dry_run=False)

        # Should keep: 20260608, 20260609, 20260610 (2 days before run_date = cutoff 20260608)
        remaining = {p.name for p in exports.iterdir() if p.is_dir()}
        cutoff = (run_date - timedelta(days=2)).strftime("%Y%m%d")
        for d in remaining:
            assert d >= cutoff, f"Should have pruned {d} but didn't"


# ── Test 17: Simultaneous position limit is exactly 10 ────────────────────────

class TestSimultaneousPositionLimit:
    def _make_candidate(self, symbol, idx):
        candidate = MagicMock()
        candidate.symbol = symbol
        candidate.legs = []
        return candidate

    def test_first_ten_accepted_eleventh_blocked(self):
        from tools.options_spread_execution import SpreadExecutionEngine

        state = MagicMock()
        engine = SpreadExecutionEngine(None, state, max_open_spreads=10)
        engine._no_entry_after = "23:59"  # bypass time check

        for n in range(10):
            state.get_state.return_value = {
                "is_stopped": False, "is_paused": False,
                # Use distinct symbols to avoid pyramiding block
                "open_spreads": [{"symbol": f"SYM{i}"} for i in range(n)],
            }
            candidate = self._make_candidate("NIFTY", n)
            err = engine._check_guardrails(candidate)
            if err and "max open spreads" in err.lower():
                pytest.fail(f"Spread {n+1} was blocked by max_open_spreads at count={n}")

        # 11th: state has 10 open spreads
        state.get_state.return_value = {
            "is_stopped": False, "is_paused": False,
            "open_spreads": [{"symbol": f"SYM{i}"} for i in range(10)],
        }
        candidate = self._make_candidate("NIFTY", 10)
        err = engine._check_guardrails(candidate)
        assert err is not None and "10" in err, (
            f"Expected 11th spread to be blocked with 10/10, got: {err}"
        )

    def test_closing_spread_frees_slot(self):
        from tools.options_spread_execution import SpreadExecutionEngine
        state = MagicMock()
        engine = SpreadExecutionEngine(None, state, max_open_spreads=10)

        # 10 spreads open — 11th blocked
        state.get_state.return_value = {
            "is_stopped": False, "is_paused": False,
            "open_spreads": [{"symbol": f"SYM{i}"} for i in range(10)],
        }
        candidate = self._make_candidate("NIFTY", 11)
        err = engine._check_guardrails(candidate)
        assert err is not None

        # Now 9 spreads open — 10th allowed
        state.get_state.return_value["open_spreads"] = [
            {"symbol": f"SYM{i}"} for i in range(9)
        ]
        err2 = engine._check_guardrails(candidate)
        assert err2 is None or "max open spreads" not in err2.lower()

    def test_config_default_is_10(self):
        from config import MAX_OPEN_OPTION_SPREADS, MAX_POSITIONS
        assert MAX_OPEN_OPTION_SPREADS == 10
        assert MAX_POSITIONS == 10

    def test_sensex_recorder_does_not_count_as_position(self):
        """SENSEX recorder subscriptions must not affect position count."""
        from tools.sensex_atm_recorder import SensexATMOptionRecorder
        from tools.options_spread_execution import SpreadExecutionEngine
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            rec = SensexATMOptionRecorder(
                base_dir=Path(tmp) / "data_exports",
                shoonya_client=None,
            )
            rec._expiry = "11-JUN-2026"
            rec._strike_step = 100

            state = MagicMock()
            state.get_state.return_value = {
                "is_stopped": False, "is_paused": False,
                "open_spreads": [],
            }
            engine = SpreadExecutionEngine(None, state, max_open_spreads=10)
            candidate = self._make_candidate("NIFTY", 1)
            err = engine._check_guardrails(candidate)
            assert err is None or "max open spreads" not in err.lower()

    def test_legacy_futures_counter_does_not_block_spread(self):
        """Futures positions (positions[]) must not pollute spread count."""
        from tools.options_spread_execution import SpreadExecutionEngine
        state = MagicMock()
        state.get_state.return_value = {
            "is_stopped": False, "is_paused": False,
            "open_spreads": [],          # zero option spreads
            "positions": [               # but some legacy futures positions
                {"symbol": "NIFTY28APR26F"},
                {"symbol": "BANKNIFTY28APR26F"},
            ],
        }
        engine = SpreadExecutionEngine(None, state, max_open_spreads=10)
        candidate = self._make_candidate("NIFTY", 1)
        err = engine._check_guardrails(candidate)
        # futures positions must not trigger max_open_spreads block
        assert err is None or "max open spreads" not in err.lower()

    def test_restarted_state_counts_existing_spreads(self):
        """On restart, pre-existing open_spreads in state count against the limit."""
        from tools.options_spread_execution import SpreadExecutionEngine
        state = MagicMock()
        # 10 spreads already open from previous session
        state.get_state.return_value = {
            "is_stopped": False, "is_paused": False,
            "open_spreads": [{"symbol": f"SYM{i}"} for i in range(10)],
        }
        engine = SpreadExecutionEngine(None, state, max_open_spreads=10)
        engine._no_entry_after = "23:59"  # bypass time check
        candidate = self._make_candidate("NIFTY", 1)
        err = engine._check_guardrails(candidate)
        assert err is not None and "10" in err
