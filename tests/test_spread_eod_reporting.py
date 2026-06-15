"""
tests/test_spread_eod_reporting.py

Tests for PART A: EOD summary must correctly report option-spread activity.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_state_manager(tmp_path, virtual_capital=1_000_000):
    from tools.state_manager import StateManager
    sm = StateManager(tmp_path / "live_state.json", virtual_capital=virtual_capital)
    sm.init_state()
    return sm


def _make_closed_spread(
    spread_id="SPR-20260604-001",
    symbol="NIFTY",
    spread_type="BEAR_CALL",
    direction="BEARISH",
    realized_pnl=331.0,
    close_reason="take-profit",
    strategy="VP-05",
    signal_id="SIG-001",
    opened_at="2026-06-04T09:20:00+05:30",
    closed_at="2026-06-04T11:30:00+05:30",
):
    return {
        "spread_id": spread_id,
        "symbol": symbol,
        "spread_type": spread_type,
        "direction": direction,
        "strategy": strategy,
        "signal_id": signal_id,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "long_tsym": f"{symbol}26JUN2624500CE",
        "long_token": "111",
        "long_fill_price": 50.0,
        "long_close_price": 30.0,
        "short_tsym": f"{symbol}26JUN2624600CE",
        "short_token": "222",
        "short_fill_price": 80.0,
        "short_close_price": 47.0,
        "realized_pnl": realized_pnl,
        "close_reason": close_reason,
        "max_profit": 600.0,
        "max_loss": 400.0,
        "expiry": "26-JUN-2026",
        "lot_size": 50,
        "lots": 1,
        "underlying_at_entry": 24520.0,
        "quote_source": "shoonya",
    }


# ── Tests: StateManager spread ledger ────────────────────────────────────────

class TestStateManagerSpreadLedger:
    def test_initial_spreads_traded_is_empty(self, tmp_path):
        sm = _make_state_manager(tmp_path)
        assert sm.get_traded_spreads() == []

    def test_add_traded_spread_persists(self, tmp_path):
        sm = _make_state_manager(tmp_path)
        record = _make_closed_spread()
        sm.add_traded_spread(record)
        assert len(sm.get_traded_spreads()) == 1
        assert sm.get_traded_spreads()[0]["spread_id"] == "SPR-20260604-001"

    def test_multiple_spreads_appended(self, tmp_path):
        sm = _make_state_manager(tmp_path)
        sm.add_traded_spread(_make_closed_spread("SPR-001", realized_pnl=331.0))
        sm.add_traded_spread(_make_closed_spread("SPR-002", realized_pnl=331.0))
        assert len(sm.get_traded_spreads()) == 2

    def test_spreads_persisted_to_disk(self, tmp_path):
        sm = _make_state_manager(tmp_path)
        sm.add_traded_spread(_make_closed_spread())
        # Reload from disk — do NOT call init_state() which would wipe saved state
        from tools.state_manager import StateManager
        sm2 = StateManager(tmp_path / "live_state.json", virtual_capital=1_000_000)
        sm2.load_state()
        assert len(sm2.get_traded_spreads()) == 1

    def test_default_state_has_open_spreads_key(self, tmp_path):
        sm = _make_state_manager(tmp_path)
        state = sm.get_state()
        assert "open_spreads" in state
        assert "spreads_traded" in state
        assert state["open_spreads"] == []
        assert state["spreads_traded"] == []


# ── Tests: OrderExecutionTools spread tools ───────────────────────────────────

class TestOrderExecutionSpreadTools:
    def _make_order_exec(self, tmp_path):
        from tools.order_execution import OrderExecutionTools
        from tools.state_manager import StateManager
        from tools.virtual_ledger import VirtualLedger

        sm = StateManager(tmp_path / "live_state.json", virtual_capital=1_000_000)
        sm.init_state()
        ledger = VirtualLedger(lot_sizes={}, margin_per_lot={})
        oe = OrderExecutionTools(
            state_manager=sm,
            virtual_ledger=ledger,
            live_feed=None,
            shoonya_client=None,
        )
        return oe, sm

    def test_get_todays_trades_no_activity(self, tmp_path):
        oe, _ = self._make_order_exec(tmp_path)
        result = oe.get_todays_trades()
        assert result["futures_count"] == 0
        assert result["spread_count"] == 0

    def test_get_todays_spread_trades_with_closed_spread(self, tmp_path):
        oe, sm = self._make_order_exec(tmp_path)
        sm.add_traded_spread(_make_closed_spread(realized_pnl=331.0))
        sm.add_traded_spread(_make_closed_spread("SPR-002", realized_pnl=331.0))
        result = oe.get_todays_spread_trades()
        assert result["closed_count"] == 2
        assert abs(result["total_realized_pnl"] - 662.0) < 0.01

    def test_get_daily_pnl_includes_spread_breakdown(self, tmp_path):
        oe, sm = self._make_order_exec(tmp_path)
        sm.add_traded_spread(_make_closed_spread(realized_pnl=331.0))
        sm.update_daily_pnl(331.0)
        result = oe.get_daily_pnl()
        assert "spread_realized_pnl" in result
        assert "futures_realized_pnl" in result
        assert abs(result["spread_realized_pnl"] - 331.0) < 0.01

    def test_get_todays_trades_includes_spread_count(self, tmp_path):
        oe, sm = self._make_order_exec(tmp_path)
        sm.add_traded_spread(_make_closed_spread())
        result = oe.get_todays_trades()
        assert result["spread_count"] == 1
        assert result["futures_count"] == 0

    def test_eod_with_only_spreads_no_futures(self, tmp_path):
        """With spreads only and no futures, get_todays_trades must show spread_count > 0."""
        oe, sm = self._make_order_exec(tmp_path)
        sm.add_traded_spread(_make_closed_spread(realized_pnl=662.0))
        result = oe.get_todays_trades()
        # Key requirement: spread_count must be non-zero so EOD doesn't say "No trades"
        assert result["spread_count"] == 1
        assert result["futures_count"] == 0

    def test_june4_style_two_spreads_662_pnl(self, tmp_path):
        oe, sm = self._make_order_exec(tmp_path)
        sm.add_traded_spread(_make_closed_spread("SPR-001", realized_pnl=331.0))
        sm.add_traded_spread(_make_closed_spread("SPR-002", realized_pnl=331.0))
        sm.update_daily_pnl(662.0)
        spread_result = oe.get_todays_spread_trades()
        pnl_result = oe.get_daily_pnl()
        assert spread_result["closed_count"] == 2
        assert abs(spread_result["total_realized_pnl"] - 662.0) < 0.01
        assert abs(pnl_result["daily_pnl"] - 662.0) < 0.01


# ── Tests: Journal spread ground truth ──────────────────────────────────────

class TestJournalSpreadReporting:
    def test_stop_ground_truth_counts_and_lists_option_spreads(self, tmp_path):
        from tools.journal_writer import JournalWriter

        sm = _make_state_manager(tmp_path)
        sm.add_traded_spread(_make_closed_spread(realized_pnl=-331.0))
        sm.update_daily_pnl(-331.0)

        journal = JournalWriter(tmp_path / "journals", 1_000_000, state_manager=sm)
        result = journal.log_decision(action="STOP", reason="End of session")

        assert result["status"] == "logged"
        content = Path(result["journal_file"]).read_text(encoding="utf-8")
        assert "- Actual trades executed: 1" in content
        assert "- Futures trades: 0" in content
        assert "- Option spreads: 1" in content
        assert "Spread: NIFTY BEAR_CALL" in content
        assert "P&L: ₹-331.00" in content
        assert "No trades were executed this session" not in content

    def test_session_summary_includes_spread_count_and_win_rate(self, tmp_path):
        from tools.journal_writer import JournalWriter

        journal = JournalWriter(tmp_path / "journals", 1_000_000)
        journal.log_decision(action="ENTER_LONG", symbol="NIFTY")
        journal.update_session_summary(
            end_capital=1_000_100,
            net_pnl=100,
            total_trades=2,
            wins=1,
        )

        content = journal._get_journal_path().read_text(encoding="utf-8")
        assert "- **Total Trades:** 2" in content
        assert "- **Win Rate:** 50% (1/2)" in content

    def test_session_summary_repairs_already_finalized_values(self, tmp_path):
        from tools.journal_writer import JournalWriter

        journal = JournalWriter(tmp_path / "journals", 1_000_000)
        journal.log_decision(action="ENTER_LONG", symbol="NIFTY")
        path = journal._get_journal_path()
        content = path.read_text(encoding="utf-8")
        content = content.replace("- **End Capital:** —", "- **End Capital:** ₹998,694")
        content = content.replace(
            "- **Net P&L:** —", "- **Net P&L:** ₹-1,306 (-0.13%)"
        )
        content = content.replace("- **Total Trades:** 0", "- **Total Trades:** 0")
        content = content.replace("- **Win Rate:** —", "- **Win Rate:** N/A (0/0)")
        path.write_text(content, encoding="utf-8")

        journal.update_session_summary(
            end_capital=998_694.5,
            net_pnl=-1_305.5,
            total_trades=2,
            wins=0,
        )

        repaired = path.read_text(encoding="utf-8")
        assert "- **Total Trades:** 2" in repaired
        assert "- **Win Rate:** 0% (0/2)" in repaired


# ── Tests: CandidateAudit spread stages ──────────────────────────────────────

class TestCandidateAuditSpreadStages:
    def test_spread_order_placed_is_valid_stage(self, tmp_path):
        from tools.candidate_audit import CandidateAudit, _VALID_STAGES
        assert "SPREAD_ORDER_PLACED" in _VALID_STAGES

    def test_spread_build_rejected_is_valid_stage(self, tmp_path):
        from tools.candidate_audit import _VALID_STAGES
        assert "SPREAD_BUILD_REJECTED" in _VALID_STAGES

    def test_spread_build_success_is_valid_stage(self, tmp_path):
        from tools.candidate_audit import _VALID_STAGES
        assert "SPREAD_BUILD_SUCCESS" in _VALID_STAGES

    def test_spread_order_placed_records_without_warning(self, tmp_path, caplog):
        from tools.candidate_audit import CandidateAudit
        import logging
        audit = CandidateAudit(tmp_path)
        with caplog.at_level(logging.WARNING, logger="BlitzTrader.CandidateAudit"):
            audit.record(
                signal_id="SIG-001",
                stage="SPREAD_ORDER_PLACED",
                signal={"symbol": "NIFTY", "strategy": "VP-05", "direction": "SELL"}
            )
        # Should not log "unknown stage" warning
        assert "unknown stage" not in caplog.text

    def test_read_today_counts_spread_stages(self, tmp_path):
        from tools.candidate_audit import CandidateAudit
        audit = CandidateAudit(tmp_path)
        audit.record(signal_id="SIG-001", stage="SPREAD_ORDER_PLACED",
                     signal={"symbol": "NIFTY", "strategy": "VP-05", "direction": "SELL"})
        audit.record(signal_id="SIG-002", stage="SPREAD_BUILD_REJECTED",
                     signal={"symbol": "BANKNIFTY", "strategy": "VP-09", "direction": "BUY"})
        records = audit.read_today()
        stages = [r["stage"] for r in records]
        assert "SPREAD_ORDER_PLACED" in stages
        assert "SPREAD_BUILD_REJECTED" in stages
