"""
tests/test_wiki_spread_reporting.py

Tests for PART B: wiki review correctly reports spread activity and
hypothesis validation allows "Gemini gatekeeper" in evidence text.
"""
import json
from datetime import date
from pathlib import Path
import pytest


# ── Tests: futures_hypothesis validate_hypothesis ────────────────────────────

class TestHypothesisValidation:
    def _validate(self, data):
        from tools.futures_hypothesis import validate_hypothesis
        return validate_hypothesis(data)

    def _base(self, **kw):
        h = {
            "scope": "futures",
            "symbol": "NIFTY",
            "strategy": "VP-05",
            "status": "proposed",
        }
        h.update(kw)
        return h

    # Evidence text may mention "Gemini gatekeeper" — must pass
    def test_gemini_gatekeeper_in_evidence_passes(self):
        h = self._base(evidence="Gemini gatekeeper rejected this signal due to RSI overbought.")
        ok, err = self._validate(h)
        assert ok, f"Expected pass, got: {err}"

    def test_gemini_gatekeeper_in_description_passes(self):
        h = self._base(
            description="Gemini gatekeeper approved entries even when RSI > 70 — block in Python."
        )
        ok, err = self._validate(h)
        assert ok, f"Expected pass, got: {err}"

    # "live approval" / "live rejection" still prohibited
    def test_live_approval_in_evidence_fails(self):
        h = self._base(evidence="Based on live approval from Gemini, this entry was taken.")
        ok, err = self._validate(h)
        assert not ok
        assert "prohibited" in err.lower()

    def test_live_rejection_in_evidence_fails(self):
        h = self._base(evidence="live rejection count was high.")
        ok, err = self._validate(h)
        assert not ok

    # Filter depending on LLM fields must fail
    def test_filter_with_gemini_rejected_field_fails(self):
        h = self._base(filter={"block_when": {"gemini_rejected": True}})
        ok, err = self._validate(h)
        assert not ok
        assert "LLM-dependent" in err or "llm" in err.lower() or "gatekeeper" in err.lower()

    def test_filter_with_gatekeeper_approved_field_fails(self):
        h = self._base(filter={"block_when": {"gatekeeper_approved": True}})
        ok, err = self._validate(h)
        assert not ok

    # Valid deterministic filter with evidence mentioning gatekeeper — must pass
    def test_deterministic_filter_with_gatekeeper_evidence_passes(self):
        h = self._base(
            evidence="Gemini gatekeeper approved but trade lost. RSI was > 70.",
            filter={"block_when": {"rsi14_gt": 70}},
        )
        ok, err = self._validate(h)
        assert ok, f"Expected pass, got: {err}"

    def test_hyp_20260604_001_validates(self):
        """HYP-20260604-001: block BANKNIFTY VP-09 BUY when RSI > 70."""
        h = {
            "scope": "futures",
            "symbol": "BANKNIFTY",
            "strategy": "VP-09",
            "direction": "BUY",
            "status": "proposed",
            "evidence": (
                "Gemini gatekeeper also flagged RSI overbought as a concern."
            ),
            "filter": {"block_when": {"rsi14_gt": 70}},
        }
        ok, err = self._validate(h)
        assert ok, f"Expected pass, got: {err}"

    def test_hyp_20260604_002_validates(self):
        """HYP-20260604-002: block NIFTY VP-15 SELL when EMA stack neutral."""
        h = {
            "scope": "futures",
            "symbol": "NIFTY",
            "strategy": "VP-15",
            "direction": "SELL",
            "status": "proposed",
            "evidence": "Gemini gatekeeper mentioned EMA directional confirmation missing.",
            "filter": {"block_when": {"ema_stacked_bear": False}},
        }
        ok, err = self._validate(h)
        assert ok, f"Expected pass, got: {err}"

    def test_hyp_files_load_and_validate(self):
        """Actual JSON files in wiki/hypotheses/ must pass validation."""
        from tools.futures_hypothesis import validate_hypothesis
        repo = Path(__file__).parent.parent
        hyp_dir = repo / "wiki" / "hypotheses"
        for hyp_file in [
            hyp_dir / "HYP-20260604-001.json",
            hyp_dir / "HYP-20260604-002.json",
        ]:
            if not hyp_file.exists():
                pytest.skip(f"Hypothesis file not found: {hyp_file}")
            data = json.loads(hyp_file.read_text())
            ok, err = validate_hypothesis(data)
            assert ok, f"{hyp_file.name}: {err}"


# ── Tests: evaluate_futures_day spread parsing ────────────────────────────────

class TestEvaluateFuturesDaySpreadParsing:
    def _make_live_state(self, tmp_path, spreads_traded=None, open_spreads=None):
        state = {
            "session_id": "test",
            "daily_pnl": 662.0,
            "trades": [],
            "open_spreads": open_spreads or [],
            "spreads_traded": spreads_traded or [],
        }
        p = tmp_path / "live_state.json"
        p.write_text(json.dumps(state))
        return p

    def _closed_spread(self, spread_id="SPR-001", pnl=331.0):
        return {
            "spread_id": spread_id,
            "symbol": "NIFTY",
            "spread_type": "BEAR_CALL",
            "direction": "BEARISH",
            "strategy": "VP-05",
            "signal_id": "SIG-001",
            "opened_at": "2026-06-04T09:20:00+05:30",
            "closed_at": "2026-06-04T11:30:00+05:30",
            "realized_pnl": pnl,
            "close_reason": "take-profit",
        }

    def test_parse_spreads_finds_closed_spreads(self, tmp_path):
        from scripts.evaluate_futures_day import parse_spreads_from_live_state
        p = self._make_live_state(tmp_path, spreads_traded=[self._closed_spread()])
        closed, open_ = parse_spreads_from_live_state(p, date(2026, 6, 4))
        assert len(closed) == 1
        assert closed[0]["spread_id"] == "SPR-001"

    def test_parse_spreads_filters_by_date(self, tmp_path):
        from scripts.evaluate_futures_day import parse_spreads_from_live_state
        # Spread opened on June 3, reviewing June 4 — should be excluded
        spread = self._closed_spread()
        spread["opened_at"] = "2026-06-03T09:20:00+05:30"
        p = self._make_live_state(tmp_path, spreads_traded=[spread])
        closed, _ = parse_spreads_from_live_state(p, date(2026, 6, 4))
        assert len(closed) == 0

    def test_parse_spreads_no_file_returns_empty(self, tmp_path):
        from scripts.evaluate_futures_day import parse_spreads_from_live_state
        closed, open_ = parse_spreads_from_live_state(tmp_path / "missing.json", date(2026, 6, 4))
        assert closed == []
        assert open_ == []

    def test_build_review_markdown_with_spreads_no_futures(self):
        from scripts.evaluate_futures_day import build_review_markdown, compute_trade_stats
        stats = compute_trade_stats([])
        md = build_review_markdown(
            review_date=date(2026, 6, 4),
            executed=[],
            rejected=[],
            stats=stats,
            patterns=[],
            log_messages=[],
            indicator_notes=[],
            closed_spreads=[
                self._closed_spread("SPR-001", pnl=331.0),
                self._closed_spread("SPR-002", pnl=331.0),
            ],
            open_spreads=[],
        )
        # Must NOT say "No trades executed" or "Futures trades: 0" as primary message
        assert "Option spreads" in md
        assert "662" in md  # total pnl 662

    def test_build_review_markdown_no_spread_no_futures_says_zero(self):
        from scripts.evaluate_futures_day import build_review_markdown, compute_trade_stats
        stats = compute_trade_stats([])
        md = build_review_markdown(
            review_date=date(2026, 6, 4),
            executed=[],
            rejected=[],
            stats=stats,
            patterns=[],
            log_messages=[],
            indicator_notes=[],
            closed_spreads=[],
            open_spreads=[],
        )
        assert "Option spreads opened: 0" in md

    def test_candidate_audit_counts_spread_order_placed(self, tmp_path):
        from scripts.evaluate_futures_day import parse_candidate_audit
        audit_file = tmp_path / "20260604.jsonl"
        records = [
            {"ts": "T", "signal_id": "S1", "stage": "SPREAD_ORDER_PLACED",
             "symbol": "NIFTY", "strategy": "VP-05", "direction": "SELL",
             "interval": "5"},
            {"ts": "T", "signal_id": "S2", "stage": "GATEKEEPER_APPROVED",
             "symbol": "NIFTY", "strategy": "VP-05", "direction": "SELL",
             "interval": "5"},
        ]
        audit_file.write_text("\n".join(json.dumps(r) for r in records))
        result = parse_candidate_audit(audit_file)
        assert result["spread_order_placed"] == 1
        assert result["gatekeeper_approved"] == 1

    def test_wiki_review_uses_spread_not_futures_label(self):
        from scripts.evaluate_futures_day import build_review_markdown, compute_trade_stats
        stats = compute_trade_stats([])
        md = build_review_markdown(
            review_date=date(2026, 6, 4),
            executed=[],
            rejected=[],
            stats=stats,
            patterns=[],
            log_messages=[],
            indicator_notes=[],
            closed_spreads=[self._closed_spread()],
            open_spreads=[],
        )
        # Primary heading should not say "Futures trades executed: 1"
        assert "Futures trades executed: 1" not in md
        # Must include spread section
        assert "Option Spreads" in md
