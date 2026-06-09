"""
tests/test_atm_option_recorder.py

Tests for PART C: ATM option recorder.
"""
import json
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")


def _make_recorder(tmp_path, shoonya_client=None, options_chain=None):
    from tools.atm_option_recorder import ATMOptionRecorder
    return ATMOptionRecorder(
        base_dir=tmp_path / "data_exports",
        shoonya_client=shoonya_client,
        options_chain=options_chain,
        sample_interval=0,  # immediate for tests
    )


def _fake_chain(symbol="NIFTY", expiry="26-JUN-2026", strike=24500):
    chain = MagicMock()
    chain.get_nearest_expiry.return_value = expiry
    chain.resolve_option.side_effect = lambda symbol, expiry, strike, option_type: {
        "token": f"TOK{strike}{option_type}",
        "tsym": f"{symbol}26JUN26{strike}{option_type}",
    }
    return chain


def _fake_shoonya_quote(**overrides):
    defaults = {
        "stat": "Ok",
        "lp": "150.5",
        "o": "140.0",
        "h": "160.0",
        "l": "135.0",
        "c": "148.0",
        "v": "12345",
        "oi": "50000",
        "poi": "48000",
        "bp1": "150.0", "bq1": "100", "bo1": "5",
        "bp2": "149.5", "bq2": "50",
        "bp3": "149.0", "bq3": "25",
        "bp4": "148.5", "bq4": "20",
        "bp5": "148.0", "bq5": "10",
        "sp1": "151.0", "sq1": "80", "so1": "4",
        "sp2": "151.5", "sq2": "40",
        "sp3": "152.0", "sq3": "20",
        "sp4": "152.5", "sq4": "15",
        "sp5": "153.0", "sq5": "5",
    }
    defaults.update(overrides)
    return defaults


# ── Tests: ATM detection and tracking ─────────────────────────────────────────

class TestATMDetection:
    def test_initial_atm_creates_atm_plus_minus_one_ce_and_pe(self, tmp_path):
        chain = _fake_chain(symbol="NIFTY", strike=24500)
        recorder = _make_recorder(tmp_path, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)  # rounds to 24500
        with recorder._lock:
            keys = set(recorder._tracked)
        assert keys == {
            ("NIFTY", strike, option_type)
            for strike in (24450, 24500, 24550)
            for option_type in ("CE", "PE")
        }

    def test_atm_change_adds_new_contracts_preserves_old(self, tmp_path):
        chain = _fake_chain(symbol="NIFTY")
        chain.resolve_option.side_effect = lambda symbol, expiry, strike, option_type: {
            "token": f"TOK{strike}{option_type}",
            "tsym": f"{symbol}26JUN26{strike}{option_type}",
        }
        recorder = _make_recorder(tmp_path, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)   # ATM = 24500
        recorder.update_atm("NIFTY", 24570.0)   # ATM = 24550

        with recorder._lock:
            keys = list(recorder._tracked.keys())
        assert {
            strike for symbol, strike, option_type in keys if symbol == "NIFTY"
        } == {24450, 24500, 24550, 24600}
        assert len(keys) == 8

    def test_same_atm_does_not_add_duplicate(self, tmp_path):
        chain = _fake_chain(symbol="NIFTY")
        recorder = _make_recorder(tmp_path, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.update_atm("NIFTY", 24510.0)  # 24510/50=490.2 → rounds to 24500
        with recorder._lock:
            count = sum(1 for k in recorder._tracked if k[0] == "NIFTY")
        assert count == 6  # three strikes, CE + PE

    def test_banknifty_is_ignored(self, tmp_path):
        recorder = _make_recorder(tmp_path, options_chain=_fake_chain())
        recorder.update_atm("BANKNIFTY", 54000.0)
        with recorder._lock:
            assert recorder._tracked == {}

    def test_unresolved_contract_is_retried_without_atm_change(self, tmp_path):
        chain = _fake_chain()
        attempts = {}

        def resolve(symbol, expiry, strike, option_type):
            key = (strike, option_type)
            attempts[key] = attempts.get(key, 0) + 1
            if key == (24550, "CE") and attempts[key] == 1:
                return None
            return {
                "token": f"TOK{strike}{option_type}",
                "tsym": f"{symbol}26JUN26{strike}{option_type}",
            }

        chain.resolve_option.side_effect = resolve
        recorder = _make_recorder(tmp_path, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        assert ("NIFTY", 24550, "CE") not in recorder._tracked

        recorder.update_atm("NIFTY", 24521.0)
        assert ("NIFTY", 24550, "CE") in recorder._tracked
        assert attempts[(24550, "CE")] == 2

    def test_unknown_symbol_is_ignored(self, tmp_path):
        recorder = _make_recorder(tmp_path)
        recorder.update_atm("SENSEX", 80000.0)
        with recorder._lock:
            assert len(recorder._tracked) == 0

    def test_zero_ltp_is_ignored(self, tmp_path):
        recorder = _make_recorder(tmp_path)
        recorder.update_atm("NIFTY", 0.0)
        with recorder._lock:
            assert len(recorder._tracked) == 0


# ── Tests: Record writing ─────────────────────────────────────────────────────

class TestRecordWriting:
    def test_sample_writes_ohlcv_jsonl(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        ohlcv_files = list((tmp_path / "data_exports").rglob("*_ohlcv.jsonl"))
        assert len(ohlcv_files) >= 2  # CE and PE

        row = json.loads(ohlcv_files[0].read_text().splitlines()[0])
        assert "timestamp_ist" in row
        assert "ltp" in row
        assert "oi" in row
        assert row["source"] == "shoonya"

    def test_sample_writes_depth_jsonl_when_bids_present(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        depth_files = list((tmp_path / "data_exports").rglob("*_depth.jsonl"))
        assert len(depth_files) >= 1
        row = json.loads(depth_files[0].read_text().splitlines()[0])
        assert "best_5_bids" in row
        assert "best_5_asks" in row
        bids = row["best_5_bids"]
        assert isinstance(bids, list)
        assert len(bids) == 5
        assert len(row["best_5_asks"]) == 5
        assert "price" in bids[0]
        assert "qty" in bids[0]

    def test_required_ohlcv_fields_present(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        ohlcv_files = list((tmp_path / "data_exports").rglob("*_ohlcv.jsonl"))
        row = json.loads(ohlcv_files[0].read_text().splitlines()[0])
        required = [
            "timestamp_ist", "symbol", "expiry", "strike", "option_type",
            "token", "tsym", "source", "atm_strike_at_time",
            "first_seen_at", "first_seen_role", "strike_role_at_time",
            "strike_offset_steps_at_time", "is_current_atm_at_time",
            "open", "high", "low", "close", "ltp", "volume", "oi",
        ]
        for f in required:
            assert f in row, f"Missing required field: {f}"

    def test_records_spot_ltp_and_strike_role(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        recorder = _make_recorder(
            tmp_path, shoonya_client=client, options_chain=_fake_chain()
        )
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        atm_file = next(
            p for p in (tmp_path / "data_exports").rglob("*24500CE_ohlcv.jsonl")
        )
        row = json.loads(atm_file.read_text().splitlines()[0])
        assert row["underlying_ltp"] == 24520.0
        assert row["first_seen_role"] == "ATM"
        assert row["strike_role_at_time"] == "ATM"
        assert row["strike_offset_steps_at_time"] == 0

    def test_old_strike_role_changes_but_recording_continues(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        recorder = _make_recorder(
            tmp_path, shoonya_client=client, options_chain=_fake_chain()
        )
        recorder.update_atm("NIFTY", 25000.0)
        recorder.sample_due_contracts()
        recorder.update_atm("NIFTY", 25050.0)
        recorder.sample_due_contracts()

        strikes = {
            contract.strike for contract in recorder._tracked.values()
        }
        assert strikes == {24950, 25000, 25050, 25100}

        previous_file = next(
            p for p in (tmp_path / "data_exports").rglob("*24950CE_ohlcv.jsonl")
        )
        latest = json.loads(previous_file.read_text().splitlines()[-1])
        assert latest["atm_strike_at_time"] == 25050
        assert latest["strike_role_at_time"] == "TRACKED_PREVIOUS"

    def test_missing_oi_results_in_null(self, tmp_path):
        client = MagicMock()
        quote = _fake_shoonya_quote()
        del quote["oi"]  # remove OI field
        client.get_quotes.return_value = quote
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        ohlcv_files = list((tmp_path / "data_exports").rglob("*_ohlcv.jsonl"))
        row = json.loads(ohlcv_files[0].read_text().splitlines()[0])
        assert row["oi"] is None  # null, not a crash or invented value

    def test_no_shoonya_client_records_source_error(self, tmp_path):
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=None, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        ohlcv_files = list((tmp_path / "data_exports").rglob("*_ohlcv.jsonl"))
        assert len(ohlcv_files) >= 1
        row = json.loads(ohlcv_files[0].read_text().splitlines()[0])
        assert row["source_error"] is not None
        # No fabricated data
        assert row["ltp"] is None

    def test_shoonya_returns_none_records_source_error(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = None
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        ohlcv_files = list((tmp_path / "data_exports").rglob("*_ohlcv.jsonl"))
        row = json.loads(ohlcv_files[0].read_text().splitlines()[0])
        assert row["source_error"] is not None
        assert row["ltp"] is None

    def test_export_path_under_atm_options(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        export_root = tmp_path / "data_exports" / recorder._date_str / "atm_options"
        assert export_root.exists()
        nifty_dir = export_root / "NIFTY"
        assert nifty_dir.exists()


# ── Tests: Rate limiting / sample interval ────────────────────────────────────

class TestSampleInterval:
    def test_sample_interval_prevents_duplicate_calls(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        chain = _fake_chain()

        from tools.atm_option_recorder import ATMOptionRecorder
        recorder = ATMOptionRecorder(
            base_dir=tmp_path / "data_exports",
            shoonya_client=client,
            options_chain=chain,
            sample_interval=9999,  # very long interval
        )
        recorder.update_atm("NIFTY", 24520.0)
        # Force first sample
        recorder.flush()
        call_count_after_first = client.get_quotes.call_count

        # sample_due_contracts should NOT call again (interval not elapsed)
        recorder.sample_due_contracts()
        assert client.get_quotes.call_count == call_count_after_first

    def test_flush_always_samples_regardless_of_interval(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        chain = _fake_chain()

        from tools.atm_option_recorder import ATMOptionRecorder
        recorder = ATMOptionRecorder(
            base_dir=tmp_path / "data_exports",
            shoonya_client=client,
            options_chain=chain,
            sample_interval=9999,
        )
        recorder.update_atm("NIFTY", 24520.0)
        recorder.flush()
        # flush() forces all contracts regardless of interval
        assert client.get_quotes.call_count >= 2  # CE and PE


# ── Tests: OI change calculation ──────────────────────────────────────────────

class TestOIChange:
    def test_oi_change_computed_when_both_present(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote(oi="50000", poi="48000")
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        ohlcv_files = list((tmp_path / "data_exports").rglob("*_ohlcv.jsonl"))
        row = json.loads(ohlcv_files[0].read_text().splitlines()[0])
        assert row["oi"] == 50000
        assert row["previous_oi"] == 48000
        assert row["oi_change"] == 2000

    def test_oi_change_null_when_poi_missing(self, tmp_path):
        client = MagicMock()
        quote = _fake_shoonya_quote(oi="50000")
        quote.pop("poi", None)
        client.get_quotes.return_value = quote
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        ohlcv_files = list((tmp_path / "data_exports").rglob("*_ohlcv.jsonl"))
        row = json.loads(ohlcv_files[0].read_text().splitlines()[0])
        assert row["oi_change"] is None


# ── Tests: ATM strikes log ────────────────────────────────────────────────────

class TestATMStrikesLog:
    def test_strikes_log_written(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()

        strikes_files = list((tmp_path / "data_exports").rglob("*_ATM_STRIKES.jsonl"))
        assert len(strikes_files) >= 1

    def test_strikes_log_not_duplicated(self, tmp_path):
        client = MagicMock()
        client.get_quotes.return_value = _fake_shoonya_quote()
        chain = _fake_chain()
        recorder = _make_recorder(tmp_path, shoonya_client=client, options_chain=chain)
        recorder.update_atm("NIFTY", 24520.0)
        recorder.sample_due_contracts()
        recorder.sample_due_contracts()  # second sample — same contract

        strikes_files = list((tmp_path / "data_exports").rglob("*_ATM_STRIKES.jsonl"))
        # Each contract should appear only once in the strikes log
        for f in strikes_files:
            tsyms = [json.loads(l)["tsym"] for l in f.read_text().splitlines() if l]
            assert len(tsyms) == len(set(tsyms)), "Duplicate tsym entries in strikes log"
