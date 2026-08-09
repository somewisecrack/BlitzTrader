import sys
import types as module_types
from datetime import datetime

if "google.genai" not in sys.modules:
    google_mod = module_types.ModuleType("google")
    genai_mod = module_types.ModuleType("google.genai")
    genai_types = module_types.SimpleNamespace(
        Tool=object,
        Schema=lambda **kwargs: kwargs,
        FunctionDeclaration=lambda **kwargs: kwargs,
    )
    genai_mod.types = genai_types
    google_mod.genai = genai_mod
    sys.modules.setdefault("google", google_mod)
    sys.modules.setdefault("google.genai", genai_mod)

import main as main_module
from main import BlitzTrader
from tools.nifty_first_hour_momentum import (
    IST,
    NiftyFirstHourMomentumConfig,
    NiftyFirstHourMomentumTrader,
)


class DummyClient:
    def get_quotes(self, exchange, token):
        return {"stat": "Ok", "lp": "100.0"}


class MissingQuoteClient:
    def get_quotes(self, exchange, token):
        return None


class SequenceQuoteClient:
    def __init__(self, prices):
        self.prices = list(prices)

    def get_quotes(self, exchange, token):
        price = self.prices.pop(0)
        return {"stat": "Ok", "lp": str(price)}


class NoRankingTrader(NiftyFirstHourMomentumTrader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rank_calls = 0

    def _rank_first_hour(self, now):
        self.rank_calls += 1
        return []


def test_momentum_exit_parser_requires_momentum_keyword():
    assert BlitzTrader._extract_momentum_exit_serial("exit momentum #2") == 2
    assert BlitzTrader._extract_momentum_exit_serial("momentum exit leg 3") == 3
    assert BlitzTrader._extract_momentum_exit_serial("close momentum position 4") == 4
    assert BlitzTrader._extract_momentum_exit_serial("exit pair #2") is None
    assert BlitzTrader._extract_momentum_exit_serial("exit #2") is None


def test_momentum_status_and_exit_all_text():
    assert BlitzTrader._is_momentum_status_text("momentum pnl")
    assert BlitzTrader._is_momentum_status_text("show momentum positions")
    assert BlitzTrader._is_momentum_exit_all_text("exit momentum")
    assert BlitzTrader._is_momentum_exit_all_text("/exit_momentum", "/exit_momentum")
    assert not BlitzTrader._is_momentum_exit_all_text("exit momentum #1")


def test_replacement_monitor_stays_alive_before_momentum_entry(monkeypatch):
    monkeypatch.setattr(main_module, "NIFTY_FIRST_HOUR_MOMENTUM_ENABLED", True)
    monkeypatch.setattr(
        BlitzTrader,
        "_state_file_has_open_positions",
        staticmethod(lambda path: False),
    )
    trader = BlitzTrader.__new__(BlitzTrader)
    now = IST.localize(datetime(2026, 8, 10, 9, 30))

    assert trader._replacement_monitor_needed(now)


def test_momentum_status_message_contains_capital_and_positions(tmp_path):
    cfg = NiftyFirstHourMomentumConfig(
        state_file=tmp_path / "momentum.json",
        capital=100000,
        leverage=3,
        basket_size=4,
        live_orders=False,
    )
    trader = NiftyFirstHourMomentumTrader(cfg, shoonya_client=DummyClient())
    trader.state["open_positions"] = [
        {
            "position_id": "P1",
            "status": "OPEN",
            "direction": "LONG",
            "tradingsymbol": "INFY-EQ",
            "token": "123",
            "quantity": 10,
            "entry_price": 95.0,
            "trailing_stop": 99.0,
        }
    ]
    msg = trader.status_message()
    assert "Capital: Rs 100,000.00" in msg
    assert "Gross 3.0x" in msg
    assert "1. LONG INFY-EQ x10" in msg
    assert "P&L Rs +50.00" in msg


def test_momentum_status_does_not_print_numeric_total_when_quote_missing(tmp_path):
    cfg = NiftyFirstHourMomentumConfig(
        state_file=tmp_path / "momentum.json",
        capital=100000,
        leverage=3,
        basket_size=4,
        live_orders=False,
    )
    trader = NiftyFirstHourMomentumTrader(cfg, shoonya_client=MissingQuoteClient())
    trader.state["open_positions"] = [
        {
            "position_id": "P1",
            "status": "OPEN",
            "direction": "LONG",
            "tradingsymbol": "INFY-EQ",
            "token": "123",
            "quantity": 10,
            "entry_price": 95.0,
            "trailing_stop": 99.0,
        }
    ]
    msg = trader.status_message()
    assert "P&L unavailable" in msg
    assert "Total unrealized P&L unavailable" in msg
    assert "Total unrealized P&L: Rs +0.00" not in msg


def test_momentum_entry_skips_after_eod_time(tmp_path):
    cfg = NiftyFirstHourMomentumConfig(
        state_file=tmp_path / "momentum.json",
        entry_time="10:16",
        eod_exit_time="15:15",
    )
    trader = NiftyFirstHourMomentumTrader(cfg, shoonya_client=DummyClient())
    now = IST.localize(datetime(2026, 8, 10, 15, 16))
    result = trader.run_entry_if_due(now)
    assert result == {"status": "skipped", "reason": "after EOD exit time"}


def test_momentum_entry_throttles_insufficient_data_retries(tmp_path):
    cfg = NiftyFirstHourMomentumConfig(
        state_file=tmp_path / "momentum.json",
        entry_time="10:16",
        eod_exit_time="15:15",
    )
    trader = NoRankingTrader(cfg, shoonya_client=DummyClient())
    first = IST.localize(datetime(2026, 8, 10, 10, 16))
    second = IST.localize(datetime(2026, 8, 10, 10, 16, 30))

    assert trader.run_entry_if_due(first)["reason"] == "insufficient ranked symbols"
    assert trader.run_entry_if_due(second) == {"status": "skipped", "reason": "waiting for retry"}
    assert trader.rank_calls == 1


def test_first_candle_sorts_shoonya_times():
    candle = NiftyFirstHourMomentumTrader._first_candle([
        {"time": "10-08-2026 10:15:00", "into": "105", "intc": "106"},
        {"time": "10-08-2026 09:15:00", "into": "100", "intc": "101"},
    ])
    assert candle["into"] == "100"


def test_trailing_stop_update_is_saved_without_exit(tmp_path):
    cfg = NiftyFirstHourMomentumConfig(
        state_file=tmp_path / "momentum.json",
        trailing_stop_pct=0.01,
    )
    trader = NiftyFirstHourMomentumTrader(cfg, shoonya_client=SequenceQuoteClient([110.0]))
    trader.state["open_positions"] = [
        {
            "position_id": "P1",
            "status": "OPEN",
            "direction": "LONG",
            "tradingsymbol": "INFY-EQ",
            "exchange": "NSE",
            "token": "123",
            "quantity": 10,
            "entry_price": 100.0,
            "best_price": 100.0,
            "trailing_stop": 99.0,
        }
    ]
    exits = trader.check_trailing_stops()
    assert exits == []
    assert trader.state["open_positions"][0]["best_price"] == 110.0
    assert trader.state["open_positions"][0]["trailing_stop"] == 108.9
    reloaded = NiftyFirstHourMomentumTrader(cfg, shoonya_client=DummyClient())
    assert reloaded.state["open_positions"][0]["best_price"] == 110.0


def test_momentum_exit_summary_reports_failures():
    msg = BlitzTrader._format_momentum_exit_results(
        "NIFTY first-hour EOD exit",
        [
            {"ok": True, "realized_pnl": 125.5},
            {"ok": False, "error": "quote unavailable", "position": {"tradingsymbol": "INFY-EQ"}},
        ],
    )
    assert "closed 1/2 leg(s)" in msg
    assert "Failed legs still need attention" in msg
    assert "INFY-EQ: quote unavailable" in msg
