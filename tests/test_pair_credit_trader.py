from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pytz

from tools import pair_credit_trader
from tools.pair_credit_trader import OmniSpreadReadOnlyAdapter, PairCreditConfig, PairCreditTrader


class FakeAdapter:
    def __init__(self, structures):
        self.structures = structures
        self.scan_calls = []

    def scan(self, preset, period, interval, top_n):
        self.scan_calls.append((preset, period, interval, top_n))
        return [
            {"pair": "AAA/BBB", "x": "AAA.NS", "y": "BBB.NS", "qty": 1.0, "direction": "SHORT_SPREAD", "method": "CADF", "z_score": 2.5, "hurst": 0.3, "prob_profit": 0.75, "half_life": 5},
            {"pair": "CCC/DDD", "x": "CCC.NS", "y": "DDD.NS", "qty": 1.0, "direction": "LONG_SPREAD", "method": "JOHANSEN", "z_score": -2.8, "hurst": 0.25, "prob_profit": 0.72, "half_life": 6},
        ]

    def build_credit_structure(self, candidate, strike_rule, sold_sd, hedge_sd):
        return self.structures[candidate["pair"]]

    def build_iv_expected_move_credit_structure(self, candidate, sell_iv_move, hedge_max_iv_move):
        structure = dict(self.structures[candidate["pair"]])
        structure["leg_selection"] = "iv_expected_move"
        structure["sell_iv_move"] = sell_iv_move
        structure["hedge_max_iv_move"] = hedge_max_iv_move
        return structure

    def latest_option_price(self, leg):
        return float(leg["price"])

    def evaluate_credit_volatility(self, structure, min_ratio, min_days, max_days, multiplier):
        result = structure.get("volatility_result")
        if result is not None:
            return result
        return {
            "ok": True,
            "preferred_structure": "CREDIT_SPREAD",
            "min_iv_hv_ratio": 1.2,
            "legs": [
                {"symbol": structure["pair"].split("/")[0], "iv": 24.0, "hv": 20.0, "iv_hv_ratio": 1.2}
            ],
        }


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send_telegram(self, message):
        self.messages.append(message)
        return {"status": "sent"}


def _structure(pair, margin):
    left, right = pair.split("/")
    return {
        "pair": pair,
        "x_lots": 1,
        "y_lots": 1,
        "actual_ratio": 1.0,
        "margin": {"estimated_margin": margin},
        "legs": [
            {"asset": "x", "symbol": left, "instrument": "PE", "side": "SELL", "lots": 1, "lot_size": 100, "strike": 100, "expiry": "31-Dec-2099", "spot": 110, "price": 10, "is_index": False},
            {"asset": "x", "symbol": left, "instrument": "PE", "side": "BUY", "lots": 1, "lot_size": 100, "strike": 90, "expiry": "31-Dec-2099", "spot": 110, "price": 4, "is_index": False},
            {"asset": "y", "symbol": right, "instrument": "CE", "side": "SELL", "lots": 1, "lot_size": 100, "strike": 200, "expiry": "31-Dec-2099", "spot": 190, "price": 8, "is_index": False},
            {"asset": "y", "symbol": right, "instrument": "CE", "side": "BUY", "lots": 1, "lot_size": 100, "strike": 210, "expiry": "31-Dec-2099", "spot": 190, "price": 3, "is_index": False},
        ],
    }


def _trader(tmp_path: Path, adapter, telegram=None, **config_overrides):
    cfg = PairCreditConfig(
        backend_path=tmp_path,
        state_file=tmp_path / "pair_credit_positions.json",
        ledger_file=tmp_path / "pair_credit_ledger.jsonl",
        capital=100_000,
        preset="nifty_50",
        period="1y",
        interval="1d",
        top_n=50,
    )
    for key, value in config_overrides.items():
        setattr(cfg, key, value)
    return PairCreditTrader(cfg, telegram=telegram, adapter=adapter)


def test_scan_uses_nifty50_one_year_daily(tmp_path):
    adapter = FakeAdapter({"AAA/BBB": _structure("AAA/BBB", 10_000), "CCC/DDD": _structure("CCC/DDD", 10_000)})
    trader = _trader(tmp_path, adapter)
    trader.run_opening_allocation()
    assert adapter.scan_calls == [("nifty_50", "1y", "1d", 50)]


def test_vrp_feature_off_preserves_credit_builder_and_never_fetches_metrics(tmp_path):
    adapter = FakeAdapter({"AAA/BBB": _structure("AAA/BBB", 10_000), "CCC/DDD": _structure("CCC/DDD", 10_000)})
    trader = _trader(tmp_path, adapter, vrp_structure_selection_enabled=False)
    trader._vrp_provider.metrics = lambda ticker: (_ for _ in ()).throw(AssertionError("must not fetch metrics"))
    result = trader.run_opening_allocation()
    assert len(result["opened"]) == 2
    assert all(position["structure_type"] == "CREDIT_SPREAD" for position in result["opened"])


def test_vrp_cheap_signal_uses_futures_plus_options_builder(tmp_path):
    class FuturesOptionsAdapter(FakeAdapter):
        def build_iv_expected_move_futures_options_structure(self, candidate, protection_iv_move):
            structure = _structure(candidate["pair"], 10_000)
            structure["legs"] = [
                {"asset": "x", "symbol": "AAA", "instrument": "FUT", "side": "BUY", "lots": 1, "lot_size": 100, "expiry": "31-Dec-2099", "spot": 110, "price": 111, "is_index": False},
                {"asset": "x", "symbol": "AAA", "instrument": "PE", "side": "BUY", "lots": 1, "lot_size": 100, "strike": 100, "expiry": "31-Dec-2099", "spot": 110, "price": 4, "is_index": False},
            ]
            structure["structure_type"] = "FUTURES_PLUS_OPTIONS"
            return structure

    adapter = FuturesOptionsAdapter({"AAA/BBB": _structure("AAA/BBB", 10_000), "CCC/DDD": _structure("CCC/DDD", 10_000)})
    trader = _trader(tmp_path, adapter, vrp_structure_selection_enabled=True, vrp_buy_threshold=-0.03)
    from tools.pair_vrp_selector import PairVolatilityMetrics
    trader._vrp_provider.metrics = lambda ticker: PairVolatilityMetrics(ticker, 0.15, 50, 0.20, -0.05)
    result = trader.run_opening_allocation()
    assert result["opened"]
    assert all(position["structure_type"] == "FUTURES_PLUS_OPTIONS" for position in result["opened"])


def test_futures_plus_options_mark_uses_future_and_option_quotes(tmp_path):
    class MarkingAdapter(FakeAdapter):
        def latest_future_price(self, leg):
            return 115.0

        def latest_option_price(self, leg):
            return 6.0

    adapter = MarkingAdapter({"AAA/BBB": _structure("AAA/BBB", 10_000), "CCC/DDD": _structure("CCC/DDD", 10_000)})
    trader = _trader(tmp_path, adapter)
    position = trader._position_from(
        {"pair": "AAA/BBB", "x": "AAA.NS", "y": "BBB.NS"},
        {
            "pair": "AAA/BBB", "structure_type": "FUTURES_PLUS_OPTIONS", "margin": {"estimated_margin": 10_000},
            "legs": [
                {"asset": "x", "symbol": "AAA", "instrument": "FUT", "side": "BUY", "lots": 1, "lot_size": 100, "expiry": "31-Dec-2099", "spot": 110, "price": 110, "is_index": False},
                {"asset": "x", "symbol": "AAA", "instrument": "PE", "side": "BUY", "lots": 1, "lot_size": 100, "strike": 100, "expiry": "31-Dec-2099", "spot": 110, "price": 4, "is_index": False},
            ],
        },
        10_000,
    )
    mark = trader.mark_position(position)
    assert mark["data_ok"] is True
    assert mark["unrealized_pnl"] == 700.0  # +500 future and +200 protective option.


def test_allocates_only_affordable_margin_and_notifies(tmp_path):
    telegram = FakeTelegram()
    adapter = FakeAdapter({"AAA/BBB": _structure("AAA/BBB", 80_000), "CCC/DDD": _structure("CCC/DDD", 40_000)})
    trader = _trader(tmp_path, adapter, telegram)
    result = trader.run_opening_allocation()
    assert len(result["opened"]) == 1
    assert result["opened"][0]["pair"] == "CCC/DDD"
    assert result["insufficient"][0]["pair"] == "AAA/BBB"
    assert any("Insufficient margin" in msg for msg in telegram.messages)


def test_allocates_lowest_margin_pair_first_even_if_scanner_rank_is_lower(tmp_path):
    class MarginRankAdapter(FakeAdapter):
        def scan(self, preset, period, interval, top_n):
            self.scan_calls.append((preset, period, interval, top_n))
            return [
                {"pair": "AAA/BBB", "x": "AAA.NS", "y": "BBB.NS", "qty": 1.0, "direction": "SHORT_SPREAD", "method": "CADF", "z_score": 3.0, "hurst": 0.3, "prob_profit": 99.0, "half_life": 5},
                {"pair": "CCC/DDD", "x": "CCC.NS", "y": "DDD.NS", "qty": 1.0, "direction": "SHORT_SPREAD", "method": "CADF", "z_score": 2.1, "hurst": 0.3, "prob_profit": 70.0, "half_life": 5},
            ]

    structures = {
        "AAA/BBB": _structure("AAA/BBB", 50_000),
        "CCC/DDD": _structure("CCC/DDD", 10_000),
    }
    trader = _trader(tmp_path, MarginRankAdapter(structures), capital=100_000)
    result = trader.run_opening_allocation()
    assert [position["pair"] for position in result["opened"]] == ["CCC/DDD", "AAA/BBB"]


def test_rejects_candidates_that_reuse_a_stock_already_allocated(tmp_path):
    class RepeatedStockAdapter(FakeAdapter):
        def scan(self, preset, period, interval, top_n):
            self.scan_calls.append((preset, period, interval, top_n))
            return [
                {"pair": "AAA/BBB", "x": "AAA.NS", "y": "BBB.NS", "qty": 1.0, "direction": "SHORT_SPREAD", "method": "CADF", "z_score": 2.5, "hurst": 0.3, "prob_profit": 0.75, "half_life": 5},
                {"pair": "AAA/CCC", "x": "AAA.NS", "y": "CCC.NS", "qty": 1.0, "direction": "SHORT_SPREAD", "method": "CADF", "z_score": 2.4, "hurst": 0.3, "prob_profit": 0.74, "half_life": 5},
                {"pair": "DDD/EEE", "x": "DDD.NS", "y": "EEE.NS", "qty": 1.0, "direction": "SHORT_SPREAD", "method": "CADF", "z_score": 2.3, "hurst": 0.3, "prob_profit": 0.73, "half_life": 5},
            ]

    structures = {
        "AAA/BBB": _structure("AAA/BBB", 10_000),
        "AAA/CCC": _structure("AAA/CCC", 5_000),
        "DDD/EEE": _structure("DDD/EEE", 10_000),
    }
    trader = _trader(tmp_path, RepeatedStockAdapter(structures), capital=100_000)
    result = trader.run_opening_allocation()
    assert [position["pair"] for position in result["opened"]] == ["AAA/CCC", "DDD/EEE"]
    assert result["rejected"][0]["pair"] == "AAA/BBB"
    assert result["rejected"][0]["symbols"] == ["AAA"]


def test_manual_exit_records_pnl_and_blocks_same_day_reallocation_until_next_scan(tmp_path):
    adapter = FakeAdapter({"AAA/BBB": _structure("AAA/BBB", 10_000), "CCC/DDD": _structure("CCC/DDD", 10_000)})
    trader = _trader(tmp_path, adapter)
    trader.run_opening_allocation()
    result = trader.close_by_serial(1)
    assert result["ok"] is True
    assert trader.ledger.state["manual_exit_dates"]
    second = trader.run_opening_allocation()
    assert second["status"] == "skipped"


def test_same_day_expiry_does_not_close_before_market_close(tmp_path, monkeypatch):
    structure = _structure("AAA/BBB", 10_000)
    for leg in structure["legs"]:
        leg["expiry"] = "28-Jul-2026"
    adapter = FakeAdapter({"AAA/BBB": structure, "CCC/DDD": _structure("CCC/DDD", 10_000)})
    trader = _trader(tmp_path, adapter)
    trader.run_opening_allocation()

    ist = pytz.timezone("Asia/Kolkata")
    monkeypatch.setattr(
        pair_credit_trader,
        "_now_ist",
        lambda: ist.localize(datetime(2026, 7, 28, 9, 20)),
    )

    assert trader.close_expired_positions() == []
    expiring = [p for p in trader.ledger.open_positions() if p["earliest_expiry"] == "2026-07-28"]
    assert len(expiring) == 1


def test_same_day_expiry_closes_after_market_close(tmp_path, monkeypatch):
    structure = _structure("AAA/BBB", 10_000)
    for leg in structure["legs"]:
        leg["expiry"] = "28-Jul-2026"
    adapter = FakeAdapter({"AAA/BBB": structure, "CCC/DDD": _structure("CCC/DDD", 10_000)})
    trader = _trader(tmp_path, adapter)
    trader.run_opening_allocation()

    ist = pytz.timezone("Asia/Kolkata")
    monkeypatch.setattr(
        pair_credit_trader,
        "_now_ist",
        lambda: ist.localize(datetime(2026, 7, 28, 15, 16)),
    )

    results = trader.close_expired_positions()
    assert len(results) == 1
    assert results[0]["ok"] is True
    expiring = [p for p in trader.ledger.open_positions() if p["earliest_expiry"] == "2026-07-28"]
    assert expiring == []



def test_open_message_formats_percentage_when_probability_is_already_percent():
    position = {
        "position_id": "PCR-1",
        "pair": "AAA/BBB",
        "direction": "LONG_SPREAD",
        "prob_profit": 97.8,
        "z_score": -2.2,
        "hurst": 0.21,
        "entry_margin": 10000,
        "entry_net_credit": 500,
        "legs": [],
    }
    message = PairCreditTrader._format_open_message(position, remaining=90000)
    assert "Prob profit: 97.8%" in message
    assert "9780.0%" not in message


def test_open_message_formats_fractional_probability_as_percent():
    position = {
        "position_id": "PCR-1",
        "pair": "AAA/BBB",
        "direction": "LONG_SPREAD",
        "prob_profit": 0.978,
        "z_score": -2.2,
        "hurst": 0.21,
        "entry_margin": 10000,
        "entry_net_credit": 500,
        "legs": [],
    }
    message = PairCreditTrader._format_open_message(position, remaining=90000)
    assert "Prob profit: 97.8%" in message


class FakeShoonya:
    def __init__(self):
        self.searches = []
        self.quotes = []

    def search_scrip(self, exchange, query):
        self.searches.append((exchange, query))
        return [
            {
                "exch": "NFO",
                "token": "37010",
                "tsym": "ITC28JUL26C287.5",
                "optt": "CE",
                "instname": "OPTSTK",
                "symname": "ITC",
                "exd": "28-JUL-2026",
            }
        ]

    def get_quotes(self, exchange, token):
        self.quotes.append((exchange, token))
        return {
            "exch": "NFO",
            "token": "37010",
            "tsym": "ITC28JUL26C287.5",
            "lp": "1.25",
            "l": "1.00",
            "h": "2.00",
            "ti": "0.05",
            "sptprc": "290.00",
        }


def test_latest_option_price_uses_exact_shoonya_contract_and_ltp(tmp_path):
    adapter = OmniSpreadReadOnlyAdapter(tmp_path)
    adapter._shoonya_client = FakeShoonya()
    price = adapter.latest_option_price({
        "symbol": "ITC",
        "instrument": "CE",
        "expiry": "28-Jul-2026",
        "strike": 287.5,
    })
    assert price == 1.25
    assert adapter._shoonya_client.searches == [("NFO", "ITC28JUL26C287.5")]
    assert adapter._shoonya_client.quotes == [("NFO", "37010")]


def test_latest_option_price_returns_none_when_exact_contract_not_resolved(tmp_path):
    adapter = OmniSpreadReadOnlyAdapter(tmp_path)
    adapter._shoonya_client = FakeShoonya()
    adapter._shoonya_client.search_scrip = lambda exchange, query: []
    price = adapter.latest_option_price({
        "symbol": "ITC",
        "instrument": "CE",
        "expiry": "28-Jul-2026",
        "strike": 287.5,
    })
    assert price is None


def test_latest_option_price_rejects_underlying_value_as_put_premium(tmp_path):
    adapter = OmniSpreadReadOnlyAdapter(tmp_path)
    adapter._shoonya_client = FakeShoonya()
    adapter._shoonya_client.search_scrip = lambda exchange, query: [{
        "exch": "NFO",
        "token": "100534",
        "tsym": "HDFCLIFE25AUG26P500",
        "optt": "PE",
        "instname": "OPTSTK",
        "symname": "HDFCLIFE",
        "exd": "25-AUG-2026",
    }]
    adapter._shoonya_client.get_quotes = lambda exchange, token: {
        "exch": "NFO",
        "token": "100534",
        "tsym": "HDFCLIFE25AUG26P500",
        "lp": "533.30",
        "l": "0.45",
        "h": "0.70",
        "ti": "0.05",
        "sptprc": "533.30",
    }

    price = adapter.latest_option_price({
        "symbol": "HDFCLIFE",
        "instrument": "PE",
        "expiry": "25-Aug-2026",
        "strike": 500,
    })

    assert price is None



def test_rejects_credit_candidate_when_iv_hv_below_threshold(tmp_path):
    structures = {
        "AAA/BBB": {
            **_structure("AAA/BBB", 10_000),
            "volatility_result": {
                "ok": False,
                "preferred_structure": "LONG_VOL",
                "reason": "IV/HV below credit threshold: 0.80 < 1.00",
                "min_iv_hv_ratio": 0.8,
                "legs": [{"symbol": "AAA", "iv": 12.0, "hv": 15.0, "iv_hv_ratio": 0.8}],
            },
        },
        "CCC/DDD": _structure("CCC/DDD", 10_000),
    }
    trader = _trader(tmp_path, FakeAdapter(structures), vol_gate_enabled=True)
    result = trader.run_opening_allocation()
    assert [p["pair"] for p in result["opened"]] == ["CCC/DDD"]
    assert result["rejected"][0]["pair"] == "AAA/BBB"
    assert result["rejected"][0]["preferred_structure"] == "LONG_VOL"


def test_position_records_volatility_gate_metrics(tmp_path):
    structures = {"AAA/BBB": _structure("AAA/BBB", 10_000), "CCC/DDD": _structure("CCC/DDD", 95_000)}
    trader = _trader(tmp_path, FakeAdapter(structures), vol_gate_enabled=True)
    result = trader.run_opening_allocation()
    position = result["opened"][0]
    assert position["volatility"]["preferred_structure"] == "CREDIT_SPREAD"
    assert position["volatility"]["min_iv_hv_ratio"] == 1.2


def test_default_pair_credit_ignores_iv_hv_gate_and_uses_iv_leg_selection(tmp_path):
    structures = {
        "AAA/BBB": {
            **_structure("AAA/BBB", 10_000),
            "volatility_result": {
                "ok": False,
                "preferred_structure": "LONG_VOL",
                "reason": "IV/HV below credit threshold: 0.80 < 1.00",
                "min_iv_hv_ratio": 0.8,
                "legs": [{"symbol": "AAA", "iv": 12.0, "hv": 15.0, "iv_hv_ratio": 0.8}],
            },
        },
        "CCC/DDD": _structure("CCC/DDD", 95_000),
    }
    trader = _trader(tmp_path, FakeAdapter(structures))
    result = trader.run_opening_allocation()
    assert [p["pair"] for p in result["opened"]] == ["AAA/BBB"]
    assert result["opened"][0]["leg_selection"] == "iv_expected_move"
    assert result["opened"][0]["sell_iv_move"] == 1.0
    assert result["opened"][0]["hedge_max_iv_move"] == 2.5
    assert result["rejected"] == []


def test_hv_lookback_matches_expiry_with_floor_and_cap():
    from datetime import date
    adapter = OmniSpreadReadOnlyAdapter(Path("/tmp"))
    assert adapter._hv_lookback_days(date(2026, 7, 29), 5, 30, 2, today=date(2026, 7, 28)) == 5
    assert adapter._hv_lookback_days(date(2026, 8, 11), 5, 30, 2, today=date(2026, 7, 28)) == 20
    assert adapter._hv_lookback_days(date(2026, 9, 30), 5, 30, 2, today=date(2026, 7, 28)) == 30


def test_open_message_includes_iv_hv_line():
    position = {
        "position_id": "PCR-1",
        "pair": "AAA/BBB",
        "direction": "LONG_SPREAD",
        "prob_profit": 97.8,
        "z_score": -2.2,
        "hurst": 0.21,
        "entry_margin": 10000,
        "entry_net_credit": 500,
        "volatility": {
            "legs": [{"symbol": "AAA", "iv": 24.0, "hv": 20.0, "iv_hv_ratio": 1.2}]
        },
        "legs": [],
    }
    message = PairCreditTrader._format_open_message(position, remaining=90000)
    assert "IV/HV: AAA 24.0/20.0 (1.20x)" in message



def test_build_credit_structure_rejects_legacy_strike_fallback(tmp_path):
    class BackendFallbackAdapter(OmniSpreadReadOnlyAdapter):
        def _ensure_loaded(self):
            self._loaded = True
            self._fetch_future = lambda **kwargs: None
            self._fetch_option = lambda **kwargs: None
            self._build_credit_spread_structure = self._fake_build

        def _fake_build(self, **kwargs):
            logging.getLogger("derivatives_backtest").warning(
                "Volatility-scaled strikes unavailable for PE 31-Dec-2099 (test); using the legacy rule."
            )
            return _structure("AAA/BBB", 10_000)

    adapter = BackendFallbackAdapter(tmp_path)
    try:
        adapter.build_credit_structure(
            {"x": "AAA.NS", "y": "BBB.NS", "qty": 1.0, "direction": "LONG_SPREAD"},
            strike_rule="vol", sold_sd=1.0, hedge_sd=2.5,
        )
    except RuntimeError as exc:
        assert "fell back to legacy rule" in str(exc)
    else:
        raise AssertionError("expected legacy fallback to be rejected")


def test_trading_days_to_expiry_uses_nse_holidays():
    adapter = OmniSpreadReadOnlyAdapter(Path("/tmp"))
    # 2026-09-14 is a configured NSE holiday; it must not count toward expiry lookback.
    assert adapter._trading_days_to_expiry(date(2026, 9, 15), today=date(2026, 9, 11)) == 1



def test_factory_can_reuse_existing_shoonya_client(tmp_path, monkeypatch):
    import tools.pair_credit_trader as pct

    fake_client = object()
    monkeypatch.setattr(pct, "OMNISPREAD_BACKEND_PATH", tmp_path, raising=False)
    monkeypatch.setattr(pct, "PAIR_CREDIT_STATE_FILE", tmp_path / "state.json", raising=False)
    monkeypatch.setattr(pct, "PAIR_CREDIT_LEDGER_FILE", tmp_path / "ledger.jsonl", raising=False)
    trader = pct.PairCreditTrader(
        pct.PairCreditConfig(
            backend_path=tmp_path,
            state_file=tmp_path / "state.json",
            ledger_file=tmp_path / "ledger.jsonl",
            capital=100_000,
        ),
        shoonya_client=fake_client,
    )
    assert trader.adapter._shoonya_client is fake_client
