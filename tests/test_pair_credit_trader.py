from __future__ import annotations

from pathlib import Path

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

    def latest_option_price(self, leg):
        return float(leg["price"])


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


def _trader(tmp_path: Path, adapter, telegram=None):
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
    return PairCreditTrader(cfg, telegram=telegram, adapter=adapter)


def test_scan_uses_nifty50_one_year_daily(tmp_path):
    adapter = FakeAdapter({"AAA/BBB": _structure("AAA/BBB", 10_000), "CCC/DDD": _structure("CCC/DDD", 10_000)})
    trader = _trader(tmp_path, adapter)
    trader.run_opening_allocation()
    assert adapter.scan_calls == [("nifty_50", "1y", "1d", 50)]


def test_allocates_only_affordable_margin_and_notifies(tmp_path):
    telegram = FakeTelegram()
    adapter = FakeAdapter({"AAA/BBB": _structure("AAA/BBB", 80_000), "CCC/DDD": _structure("CCC/DDD", 40_000)})
    trader = _trader(tmp_path, adapter, telegram)
    result = trader.run_opening_allocation()
    assert len(result["opened"]) == 1
    assert result["opened"][0]["pair"] == "AAA/BBB"
    assert result["insufficient"][0]["pair"] == "CCC/DDD"
    assert any("Insufficient margin" in msg for msg in telegram.messages)


def test_manual_exit_records_pnl_and_blocks_same_day_reallocation_until_next_scan(tmp_path):
    adapter = FakeAdapter({"AAA/BBB": _structure("AAA/BBB", 10_000), "CCC/DDD": _structure("CCC/DDD", 10_000)})
    trader = _trader(tmp_path, adapter)
    trader.run_opening_allocation()
    result = trader.close_by_serial(1)
    assert result["ok"] is True
    assert trader.ledger.state["manual_exit_dates"]
    second = trader.run_opening_allocation()
    assert second["status"] == "skipped"



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
        return {"lp": "1.25"}


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
