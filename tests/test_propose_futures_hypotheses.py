"""
tests/test_propose_futures_hypotheses.py
-----------------------------------------
Unit tests for scripts/propose_futures_hypotheses.py.

All tests mock Gemini API calls — no network access required.
Tests validate:
  - Gemini success path (google-genai SDK): valid hypotheses written with created_by=gemini
  - Gemini error/unavailable: no fake hypotheses; no-proposals audit artifact written
  - Invalid Gemini JSON: rejected cleanly; audit artifact written
  - All candidates rejected: audit artifact written
  - Strategy must be both seen in review AND in SUPPORTED_STRATEGIES
  - Review with no extractable strategies: fail closed, audit artifact written
  - --max-hypotheses caps output
  - Pairs-related hypotheses are rejected
  - --no-llm mode uses deterministic parse without touching Gemini
"""
from __future__ import annotations

import json
import sys
import textwrap
import types
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
import importlib.util as _ilu

_SCRIPT_PATH = _REPO_ROOT / "scripts" / "propose_futures_hypotheses.py"
_spec = _ilu.spec_from_file_location("propose_futures_hypotheses", _SCRIPT_PATH)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

call_gemini = _mod.call_gemini
_validate_gemini_hypothesis = _mod._validate_gemini_hypothesis
_reject_pairs_content = _mod._reject_pairs_content
assemble_hypothesis = _mod.assemble_hypothesis
mode_a_parse = _mod.mode_a_parse
write_hypothesis_json = _mod.write_hypothesis_json
_strip_json_fences = _mod._strip_json_fences
compact_review = _mod.compact_review
extract_strategies_from_review = _mod.extract_strategies_from_review
write_no_proposals_artifact = _mod.write_no_proposals_artifact
_extract_response_text = _mod._extract_response_text
_extract_loss_tuples = _mod._extract_loss_tuples


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_REVIEW_DATE_ISO = "2026-05-09"
_DATE_COMPACT = "20260509"

# Review that contains two SUPPORTED_STRATEGIES names verbatim
_SAMPLE_REVIEW = textwrap.dedent("""\
    # Futures Daily Review — 2026-05-09

    ## Signal Summary
    - BANKNIFTY VP-01 Counter Bull Trap: 4 SELL signals, 1 winner (25% win rate)
    - Losing entries had RSI14 < 25 at entry (extreme oversold, snap-back risk)
    - NIFTY VP-07 Wicks Pullback SELL: 3 signals, 1 winner (33% win rate)
    - Weak ADX14 readings at entry correlate with losing trades

    ## Possible Hypotheses
    - Block BANKNIFTY VP-01 SELL when RSI14 < 25 (extreme oversold caused snap-backs)
    - Block NIFTY VP-07 Wicks Pullback SELL when ADX14 < 18 (weak trend, false wick)
""")

# Strategies that appear verbatim in _SAMPLE_REVIEW and are in SUPPORTED_STRATEGIES
_STRATEGIES_IN_REVIEW = {"VP-01 Counter Bull Trap", "VP-07 Wicks Pullback"}

_VALID_GEMINI_CANDIDATE = {
    "scope": "futures",
    "symbol": "BANKNIFTY",
    "strategy": "VP-01 Counter Bull Trap",
    "claim": "Block VP-01 SELL signals on BANKNIFTY when RSI14 < 25",
    "direction": "SELL",
    "filter": {"block_when": {"rsi14_lt": 25.0}},
    "rationale": "3/4 VP-01 SELL signals failed when RSI14 < 25 in today's session.",
    "source_review_date": _REVIEW_DATE_ISO,
    "created_by": "gemini",
    "status": "proposed",
}


def _make_gemini_response(payload) -> MagicMock:
    """Build a mock compatible with the google-genai SDK response structure.

    Mirrors agent_loop.py's access pattern: response.candidates[0].content.parts[i].text
    Also sets response.text as a fallback for _extract_response_text.
    """
    text = json.dumps(payload)
    part = MagicMock()
    part.text = text
    candidate = MagicMock()
    candidate.content.parts = [part]
    resp = MagicMock()
    resp.candidates = [candidate]
    resp.text = text  # fallback path in _extract_response_text
    return resp


def _make_fake_genai(mock_client: MagicMock) -> MagicMock:
    """Build a fake google.genai module whose Client() returns mock_client."""
    fake = MagicMock(name="google.genai")
    fake.Client.return_value = mock_client
    return fake


def _patch_gemini_client(mock_response=None, side_effect=None):
    """Return a context manager that injects a fake google.genai into sys.modules.

    google-genai may not be installed in the dev environment; injecting via
    sys.modules makes `from google import genai` pick up our fake module,
    matching the production import path in call_gemini().
    """
    mock_client = MagicMock()
    if side_effect is not None:
        mock_client.models.generate_content.side_effect = side_effect
    else:
        mock_client.models.generate_content.return_value = mock_response
    return patch.dict(sys.modules, {"google.genai": _make_fake_genai(mock_client)})


# ---------------------------------------------------------------------------
# _extract_response_text
# ---------------------------------------------------------------------------

class TestExtractResponseText:
    def test_reads_from_candidates_parts(self):
        resp = _make_gemini_response([{"a": 1}])
        text = _extract_response_text(resp)
        assert "[" in text and "a" in text

    def test_falls_back_to_text_attribute(self):
        resp = MagicMock()
        resp.candidates = []
        resp.text = "fallback text"
        assert _extract_response_text(resp) == "fallback text"

    def test_returns_empty_on_broken_response(self):
        resp = MagicMock()
        resp.candidates = None
        resp.text = None
        assert _extract_response_text(resp) == ""


# ---------------------------------------------------------------------------
# _strip_json_fences
# ---------------------------------------------------------------------------

class TestStripJsonFences:
    def test_strips_json_fence(self):
        assert _strip_json_fences("```json\n[]\n```") == "[]"

    def test_strips_plain_fence(self):
        assert _strip_json_fences("```\n[]\n```") == "[]"

    def test_passes_through_plain_json(self):
        raw = '[{"a": 1}]'
        assert _strip_json_fences(raw) == raw


# ---------------------------------------------------------------------------
# extract_strategies_from_review
# ---------------------------------------------------------------------------

class TestExtractStrategiesFromReview:
    def test_finds_strategies_present_in_review(self):
        result = extract_strategies_from_review(_SAMPLE_REVIEW)
        assert "VP-01 Counter Bull Trap" in result
        assert "VP-07 Wicks Pullback" in result

    def test_does_not_invent_strategies(self):
        result = extract_strategies_from_review(_SAMPLE_REVIEW)
        # VP-14 Morning Star is supported but not in this review
        assert "VP-14 Morning Star" not in result

    def test_returns_empty_on_review_with_no_strategies(self):
        empty_review = "# Daily Review\n\nNo trades today.\n"
        assert extract_strategies_from_review(empty_review) == set()

    def test_only_returns_supported_strategies(self):
        # Inject an unsupported strategy name in the review text
        review_with_unsupported = _SAMPLE_REVIEW + "\nVP-99 Fake Strategy: 1 signal"
        result = extract_strategies_from_review(review_with_unsupported)
        assert "VP-99 Fake Strategy" not in result

    def test_returns_subset_of_supported_strategies(self):
        from tools.futures_strategy_engine import SUPPORTED_STRATEGIES
        result = extract_strategies_from_review(_SAMPLE_REVIEW)
        assert result.issubset(SUPPORTED_STRATEGIES)


# ---------------------------------------------------------------------------
# _reject_pairs_content
# ---------------------------------------------------------------------------

class TestRejectPairsContent:
    def test_accepts_clean_hypothesis(self):
        assert _reject_pairs_content(_VALID_GEMINI_CANDIDATE) is None

    def test_rejects_cointegration_key(self):
        hyp = {**_VALID_GEMINI_CANDIDATE, "cointegration": 0.05}
        result = _reject_pairs_content(hyp)
        assert result is not None and "cointegration" in result

    def test_rejects_pairs_keyword_in_value(self):
        hyp = {**_VALID_GEMINI_CANDIDATE, "claim": "pairs spread divergence on BANKNIFTY"}
        assert _reject_pairs_content(hyp) is not None

    def test_rejects_z_score_key(self):
        hyp = {**_VALID_GEMINI_CANDIDATE, "z_score": 2.1}
        assert _reject_pairs_content(hyp) is not None


# ---------------------------------------------------------------------------
# _validate_gemini_hypothesis
# ---------------------------------------------------------------------------

class TestValidateGeminiHypothesis:
    def test_valid_candidate_passes(self):
        ok, reason = _validate_gemini_hypothesis(
            _VALID_GEMINI_CANDIDATE, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW
        )
        assert ok, reason

    def test_rejects_missing_created_by(self):
        bad = {**_VALID_GEMINI_CANDIDATE, "created_by": "human"}
        ok, reason = _validate_gemini_hypothesis(bad, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW)
        assert not ok
        assert "created_by" in reason

    def test_rejects_wrong_scope(self):
        bad = {**_VALID_GEMINI_CANDIDATE, "scope": "equity"}
        ok, reason = _validate_gemini_hypothesis(bad, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW)
        assert not ok

    def test_rejects_ns_symbol(self):
        bad = {**_VALID_GEMINI_CANDIDATE, "symbol": "INFY.NS"}
        ok, reason = _validate_gemini_hypothesis(bad, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW)
        assert not ok

    def test_rejects_unsupported_filter_field(self):
        bad = {
            **_VALID_GEMINI_CANDIDATE,
            "filter": {"block_when": {"mystery_indicator_lt": 10}},
        }
        ok, reason = _validate_gemini_hypothesis(bad, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW)
        assert not ok
        assert "unsupported filter fields" in reason

    def test_rejects_pairs_content_in_body(self):
        bad = {**_VALID_GEMINI_CANDIDATE, "rationale": "cointegration spread z_score signal"}
        ok, reason = _validate_gemini_hypothesis(bad, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW)
        assert not ok

    def test_rejects_non_dict(self):
        ok, reason = _validate_gemini_hypothesis("not a dict", _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW)
        assert not ok


class TestValidateGeminiHypothesisStrategy:
    """Strategy must be both in SUPPORTED_STRATEGIES AND seen in the review."""

    def test_rejects_strategy_not_in_review(self):
        # VP-14 Morning Star is supported but absent from _SAMPLE_REVIEW
        bad = {**_VALID_GEMINI_CANDIDATE, "strategy": "VP-14 Morning Star"}
        ok, reason = _validate_gemini_hypothesis(bad, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW)
        assert not ok
        assert "not present in today's review" in reason

    def test_rejects_strategy_not_supported(self):
        bad = {**_VALID_GEMINI_CANDIDATE, "strategy": "VP-99 Invented"}
        ok, reason = _validate_gemini_hypothesis(bad, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW)
        assert not ok
        assert "SUPPORTED_STRATEGIES" in reason

    def test_accepts_strategy_in_both_review_and_supported(self):
        ok, reason = _validate_gemini_hypothesis(
            _VALID_GEMINI_CANDIDATE, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW
        )
        assert ok, reason

    def test_rejects_when_strategies_in_review_is_empty(self):
        # Empty set means the review had no recognisable strategies — fail closed
        ok, reason = _validate_gemini_hypothesis(
            _VALID_GEMINI_CANDIDATE, _REVIEW_DATE_ISO, set()
        )
        assert not ok
        assert "not present in today's review" in reason


# ---------------------------------------------------------------------------
# direction vs strategy validation
# ---------------------------------------------------------------------------

class TestValidateGeminiHypothesisDirectionMismatch:
    """Proposals whose direction can never be emitted by the strategy must be rejected."""

    def _strategies_with(self, *names) -> set:
        return set(names)

    def test_rejects_vp01_buy(self):
        """VP-01 Counter Bull Trap only emits SELL — BUY proposal must be rejected."""
        bad = {
            **_VALID_GEMINI_CANDIDATE,
            "direction": "BUY",
            "claim": "Block VP-01 BUY on BANKNIFTY when RSI14 < 25",
        }
        ok, reason = _validate_gemini_hypothesis(
            bad, _REVIEW_DATE_ISO, {"VP-01 Counter Bull Trap"}
        )
        assert not ok
        assert "only emits" in reason or "can never produce" in reason

    def test_rejects_vp02_sell(self):
        """VP-02 Counter Bear Trap only emits BUY — SELL proposal must be rejected."""
        bad = {
            **_VALID_GEMINI_CANDIDATE,
            "strategy": "VP-02 Counter Bear Trap",
            "direction": "SELL",
            "claim": "Block VP-02 SELL on NIFTY when RSI14 > 80",
        }
        ok, reason = _validate_gemini_hypothesis(
            bad, _REVIEW_DATE_ISO, {"VP-02 Counter Bear Trap"}
        )
        assert not ok
        assert "only emits" in reason or "can never produce" in reason

    def test_rejects_morning_star_sell(self):
        """VP-14 Morning Star only emits BUY — SELL proposal must be rejected."""
        bad = {
            **_VALID_GEMINI_CANDIDATE,
            "strategy": "VP-14 Morning Star",
            "direction": "SELL",
            "claim": "Block VP-14 SELL on BANKNIFTY when ADX14 < 18",
        }
        ok, reason = _validate_gemini_hypothesis(
            bad, _REVIEW_DATE_ISO, {"VP-14 Morning Star"}
        )
        assert not ok
        assert "only emits" in reason or "can never produce" in reason

    def test_accepts_vp01_sell(self):
        """VP-01 SELL is the correct direction — must pass."""
        ok, reason = _validate_gemini_hypothesis(
            _VALID_GEMINI_CANDIDATE, _REVIEW_DATE_ISO, {"VP-01 Counter Bull Trap"}
        )
        assert ok, reason

    def test_accepts_bidirectional_buy(self):
        """VP-05 3EMA Trend can emit both directions — BUY proposal must pass."""
        candidate = {
            **_VALID_GEMINI_CANDIDATE,
            "strategy": "VP-05 3EMA Trend",
            "direction": "BUY",
            "claim": "Block VP-05 BUY on BANKNIFTY when ADX14 < 18",
        }
        ok, reason = _validate_gemini_hypothesis(
            candidate, _REVIEW_DATE_ISO, {"VP-05 3EMA Trend"}
        )
        assert ok, reason

    def test_accepts_bidirectional_sell(self):
        """VP-05 3EMA Trend can emit both directions — SELL proposal must pass."""
        candidate = {
            **_VALID_GEMINI_CANDIDATE,
            "strategy": "VP-05 3EMA Trend",
            "direction": "SELL",
            "claim": "Block VP-05 SELL on BANKNIFTY when ADX14 < 18",
        }
        ok, reason = _validate_gemini_hypothesis(
            candidate, _REVIEW_DATE_ISO, {"VP-05 3EMA Trend"}
        )
        assert ok, reason


# ---------------------------------------------------------------------------
# write_no_proposals_artifact
# ---------------------------------------------------------------------------

class TestWriteNoProposalsArtifact:
    def test_writes_artifact_with_correct_schema(self, tmp_path):
        write_no_proposals_artifact(tmp_path, _REVIEW_DATE_ISO, "test reason", llm_attempted=True)
        artifact_path = tmp_path / "hypotheses" / "no_proposals" / f"{_REVIEW_DATE_ISO}.json"
        assert artifact_path.exists()
        data = json.loads(artifact_path.read_text())
        assert data["date"] == _REVIEW_DATE_ISO
        assert data["scope"] == "futures"
        assert data["status"] == "no_proposals_generated"
        assert data["reason"] == "test reason"
        assert data["created_by"] == "python"
        assert data["llm_attempted"] is True
        assert data["hypotheses_written"] == 0

    def test_creates_directory_if_missing(self, tmp_path):
        write_no_proposals_artifact(tmp_path, _REVIEW_DATE_ISO, "reason", llm_attempted=False)
        assert (tmp_path / "hypotheses" / "no_proposals").is_dir()

    def test_does_not_include_raw_review_content(self, tmp_path):
        write_no_proposals_artifact(tmp_path, _REVIEW_DATE_ISO, "reason", llm_attempted=True)
        artifact_path = tmp_path / "hypotheses" / "no_proposals" / f"{_REVIEW_DATE_ISO}.json"
        text = artifact_path.read_text()
        assert "Signal Summary" not in text
        assert "Possible Hypotheses" not in text
        assert "RSI" not in text

    def test_llm_attempted_false_recorded(self, tmp_path):
        write_no_proposals_artifact(tmp_path, _REVIEW_DATE_ISO, "no key", llm_attempted=False)
        artifact_path = tmp_path / "hypotheses" / "no_proposals" / f"{_REVIEW_DATE_ISO}.json"
        data = json.loads(artifact_path.read_text())
        assert data["llm_attempted"] is False


# ---------------------------------------------------------------------------
# call_gemini: success path
# ---------------------------------------------------------------------------

class TestCallGeminiSuccess:
    def test_returns_validated_hypotheses(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        with _patch_gemini_client(_make_gemini_response([_VALID_GEMINI_CANDIDATE])):
            results, failure = call_gemini(
                _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
                max_hypotheses=3, strategies_in_review=_STRATEGIES_IN_REVIEW,
            )
        assert failure is None
        assert len(results) == 1
        assert results[0]["created_by"] == "gemini"
        assert results[0]["symbol"] == "BANKNIFTY"

    def test_max_hypotheses_caps_output(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        two_candidates = [
            _VALID_GEMINI_CANDIDATE,
            {
                **_VALID_GEMINI_CANDIDATE,
                "symbol": "NIFTY",
                "claim": "Block NIFTY VP-07 SELL when ADX14 < 18",
                "strategy": "VP-07 Wicks Pullback",
                "filter": {"block_when": {"adx14_lt": 18.0}},
            },
        ]
        with _patch_gemini_client(_make_gemini_response(two_candidates)):
            results, failure = call_gemini(
                _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
                max_hypotheses=1, strategies_in_review=_STRATEGIES_IN_REVIEW,
            )
        assert failure is None
        assert len(results) == 1  # capped to 1

    def test_rejects_pairs_hypothesis_in_batch(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        pairs_candidate = {
            **_VALID_GEMINI_CANDIDATE,
            "claim": "pairs spread cointegration z_score divergence",
        }
        with _patch_gemini_client(_make_gemini_response([pairs_candidate])):
            results, failure = call_gemini(
                _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
                max_hypotheses=3, strategies_in_review=_STRATEGIES_IN_REVIEW,
            )
        assert results == []
        assert failure is not None
        assert "none passed validation" in failure

    def test_rejects_strategy_unseen_in_review(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        # VP-14 Morning Star is supported but not in _SAMPLE_REVIEW
        unseen = {**_VALID_GEMINI_CANDIDATE, "strategy": "VP-14 Morning Star"}
        with _patch_gemini_client(_make_gemini_response([unseen])):
            results, failure = call_gemini(
                _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
                max_hypotheses=3, strategies_in_review=_STRATEGIES_IN_REVIEW,
            )
        assert results == []
        assert failure is not None
        assert "none passed validation" in failure

    def test_uses_google_genai_client(self, monkeypatch):
        """Confirm the SDK call goes through google.genai.Client, not GenerativeModel."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _make_gemini_response(
            [_VALID_GEMINI_CANDIDATE]
        )
        fake_genai = _make_fake_genai(mock_client)
        with patch.dict(sys.modules, {"google.genai": fake_genai}):
            call_gemini(
                _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
                max_hypotheses=3, strategies_in_review=_STRATEGIES_IN_REVIEW,
            )
        fake_genai.Client.assert_called_once()
        mock_client.models.generate_content.assert_called_once()


# ---------------------------------------------------------------------------
# call_gemini: failure / graceful degradation paths
# ---------------------------------------------------------------------------

class TestCallGeminiFailures:
    def test_no_api_key_returns_failure_reason(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        results, failure = call_gemini(
            _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
            max_hypotheses=3, strategies_in_review=_STRATEGIES_IN_REVIEW,
        )
        assert results == []
        assert failure is not None
        assert "GEMINI_API_KEY" in failure

    def test_api_exception_returns_failure_reason(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        with _patch_gemini_client(side_effect=RuntimeError("quota exceeded")):
            results, failure = call_gemini(
                _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
                max_hypotheses=3, strategies_in_review=_STRATEGIES_IN_REVIEW,
            )
        assert results == []
        assert failure is not None
        assert "quota exceeded" in failure

    def test_invalid_json_returns_failure_reason(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        bad_resp = MagicMock()
        bad_resp.candidates = []
        bad_resp.text = "Here are some ideas: definitely not JSON { broken }"
        with _patch_gemini_client(bad_resp):
            results, failure = call_gemini(
                _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
                max_hypotheses=3, strategies_in_review=_STRATEGIES_IN_REVIEW,
            )
        assert results == []
        assert failure is not None

    def test_no_json_array_in_response(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        bad_resp = MagicMock()
        bad_resp.candidates = []
        bad_resp.text = "I cannot help with that."
        with _patch_gemini_client(bad_resp):
            results, failure = call_gemini(
                _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
                max_hypotheses=3, strategies_in_review=_STRATEGIES_IN_REVIEW,
            )
        assert results == []
        assert failure is not None
        assert "no JSON array" in failure

    def test_no_fake_fallback_hypotheses_on_error(self, monkeypatch):
        """When Gemini fails, results must be strictly empty — no fabrication."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        results, failure = call_gemini(
            _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
            max_hypotheses=3, strategies_in_review=_STRATEGIES_IN_REVIEW,
        )
        assert results == []


# ---------------------------------------------------------------------------
# assemble_hypothesis
# ---------------------------------------------------------------------------

class TestAssembleHypothesis:
    def test_sets_id_and_created_by(self):
        hyp = assemble_hypothesis(
            _VALID_GEMINI_CANDIDATE, "HYP-20260509-001", _REVIEW_DATE_ISO
        )
        assert hyp["id"] == "HYP-20260509-001"
        assert hyp["created_by"] == "gemini"
        assert hyp["status"] == "proposed"
        assert hyp["scope"] == "futures"

    def test_moves_rationale_to_evidence_notes(self):
        hyp = assemble_hypothesis(
            _VALID_GEMINI_CANDIDATE, "HYP-20260509-001", _REVIEW_DATE_ISO
        )
        assert _VALID_GEMINI_CANDIDATE["rationale"] in hyp["evidence"]["notes"]

    def test_preserves_filter_block_when(self):
        hyp = assemble_hypothesis(
            _VALID_GEMINI_CANDIDATE, "HYP-20260509-001", _REVIEW_DATE_ISO
        )
        assert hyp["filter"]["block_when"] == {"rsi14_lt": 25.0}


# ---------------------------------------------------------------------------
# Mode A (--no-llm) deterministic parse
# ---------------------------------------------------------------------------

class TestModeADeterministicParse:
    def test_parses_hypothesis_from_review_section(self):
        hypotheses = mode_a_parse(
            _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
            seq_start=1, max_hypotheses=5,
        )
        assert len(hypotheses) >= 1
        for hyp in hypotheses:
            assert hyp["scope"] == "futures"
            assert hyp["symbol"] in {"NIFTY", "BANKNIFTY"}

    def test_mode_a_created_by_is_manual(self):
        hypotheses = mode_a_parse(
            _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
            seq_start=1, max_hypotheses=5,
        )
        for hyp in hypotheses:
            assert hyp["created_by"] == "manual"

    def test_max_hypotheses_caps_mode_a(self):
        hypotheses = mode_a_parse(
            _SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT,
            seq_start=1, max_hypotheses=1,
        )
        assert len(hypotheses) <= 1

    def test_mode_a_never_calls_gemini(self, monkeypatch):
        called = []
        monkeypatch.setattr(_mod, "call_gemini", lambda *a, **kw: called.append(True) or ([], None))
        mode_a_parse(_SAMPLE_REVIEW, _REVIEW_DATE_ISO, _DATE_COMPACT, 1, 5)
        assert called == []


# ---------------------------------------------------------------------------
# write_hypothesis_json
# ---------------------------------------------------------------------------

class TestWriteHypothesisJson:
    def test_writes_valid_json(self, tmp_path):
        hyp = assemble_hypothesis(
            _VALID_GEMINI_CANDIDATE, "HYP-20260509-001", _REVIEW_DATE_ISO
        )
        out = tmp_path / "HYP-20260509-001.json"
        write_hypothesis_json(hyp, out)
        loaded = json.loads(out.read_text())
        assert loaded["id"] == "HYP-20260509-001"
        assert loaded["created_by"] == "gemini"

    def test_rejects_oversized_hypothesis(self, tmp_path):
        hyp = assemble_hypothesis(
            _VALID_GEMINI_CANDIDATE, "HYP-20260509-001", _REVIEW_DATE_ISO
        )
        hyp["bloat"] = "x" * (64 * 1024 + 1)
        out = tmp_path / "HYP-20260509-001.json"
        with pytest.raises(ValueError, match="exceeds"):
            write_hypothesis_json(hyp, out)


# ---------------------------------------------------------------------------
# compact_review helper
# ---------------------------------------------------------------------------

class TestCompactReview:
    def test_truncates_at_word_boundary(self):
        long_text = "word " * 1000
        result = compact_review(long_text)
        assert len(result) <= _mod._MAX_REVIEW_CHARS

    def test_short_review_unchanged(self):
        short = "Short review\n"
        assert compact_review(short) == short.rstrip("\n")


# ---------------------------------------------------------------------------
# End-to-end: full main() with mocked Gemini
# ---------------------------------------------------------------------------

class TestMainIntegration:
    """Integration tests that invoke main() with a mocked Gemini client."""

    def _make_review_dir(self, tmp_path):
        review_dir = tmp_path / "wiki" / "daily_reviews"
        review_dir.mkdir(parents=True)
        (review_dir / "2026-05-09.md").write_text(_SAMPLE_REVIEW, encoding="utf-8")
        (tmp_path / "wiki" / "hypotheses").mkdir(parents=True, exist_ok=True)

    def _run_main(self, argv, monkeypatch, tmp_path,
                  mock_client=None, env_key="test-key"):
        monkeypatch.setattr(sys, "argv", argv)
        if env_key:
            monkeypatch.setenv("GEMINI_API_KEY", env_key)
        else:
            monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        self._make_review_dir(tmp_path)

        ctx = (
            patch.dict(sys.modules, {"google.genai": _make_fake_genai(mock_client)})
            if mock_client is not None
            else nullcontext()
        )
        with ctx:
            try:
                _mod.main()
            except SystemExit as e:
                return e.code, tmp_path
        return 0, tmp_path

    def test_gemini_success_writes_json_files(self, monkeypatch, tmp_path):
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _make_gemini_response(
            [_VALID_GEMINI_CANDIDATE]
        )
        code, outdir = self._run_main(
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09"],
            monkeypatch, tmp_path, mock_client=mock_client,
        )
        hyp_files = list((outdir / "wiki" / "hypotheses").glob("HYP-*.json"))
        assert len(hyp_files) == 1
        loaded = json.loads(hyp_files[0].read_text())
        assert loaded["created_by"] == "gemini"
        assert loaded["scope"] == "futures"
        assert code in (0, None)

    def test_gemini_success_writes_no_artifact(self, monkeypatch, tmp_path):
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _make_gemini_response(
            [_VALID_GEMINI_CANDIDATE]
        )
        code, outdir = self._run_main(
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09"],
            monkeypatch, tmp_path, mock_client=mock_client,
        )
        no_props = outdir / "wiki" / "hypotheses" / "no_proposals"
        assert not no_props.exists() or not list(no_props.glob("*.json"))

    def test_gemini_failure_writes_no_hypothesis_files(self, monkeypatch, tmp_path):
        # No API key → Gemini path fails immediately
        code, outdir = self._run_main(
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09"],
            monkeypatch, tmp_path, env_key=None,
        )
        hyp_files = list((outdir / "wiki" / "hypotheses").glob("HYP-*.json"))
        assert hyp_files == []
        assert code == 0

    def test_gemini_failure_writes_no_proposals_artifact(self, monkeypatch, tmp_path):
        # No API key → should write audit artifact
        code, outdir = self._run_main(
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09"],
            monkeypatch, tmp_path, env_key=None,
        )
        artifact = outdir / "wiki" / "hypotheses" / "no_proposals" / "2026-05-09.json"
        assert artifact.exists(), "no-proposals audit artifact must be written on failure"
        data = json.loads(artifact.read_text())
        assert data["status"] == "no_proposals_generated"
        assert data["llm_attempted"] is True
        assert data["hypotheses_written"] == 0
        assert data["scope"] == "futures"

    def test_api_error_writes_no_proposals_artifact(self, monkeypatch, tmp_path):
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("quota exceeded")
        code, outdir = self._run_main(
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09"],
            monkeypatch, tmp_path, mock_client=mock_client,
        )
        hyp_files = list((outdir / "wiki" / "hypotheses").glob("HYP-*.json"))
        assert hyp_files == []
        artifact = outdir / "wiki" / "hypotheses" / "no_proposals" / "2026-05-09.json"
        assert artifact.exists()
        data = json.loads(artifact.read_text())
        assert "quota exceeded" in data["reason"]
        assert code == 0

    def test_invalid_json_response_writes_no_proposals_artifact(self, monkeypatch, tmp_path):
        mock_client = MagicMock()
        bad_resp = MagicMock()
        bad_resp.candidates = []
        bad_resp.text = "I cannot help with that request."
        mock_client.models.generate_content.return_value = bad_resp
        code, outdir = self._run_main(
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09"],
            monkeypatch, tmp_path, mock_client=mock_client,
        )
        hyp_files = list((outdir / "wiki" / "hypotheses").glob("HYP-*.json"))
        assert hyp_files == []
        artifact = outdir / "wiki" / "hypotheses" / "no_proposals" / "2026-05-09.json"
        assert artifact.exists()

    def test_all_candidates_rejected_writes_no_proposals_artifact(self, monkeypatch, tmp_path):
        # All candidates contain pairs content → all rejected → no-proposals artifact
        mock_client = MagicMock()
        pairs_candidate = {
            **_VALID_GEMINI_CANDIDATE,
            "claim": "cointegration spread pairs divergence signal",
        }
        mock_client.models.generate_content.return_value = _make_gemini_response([pairs_candidate])
        code, outdir = self._run_main(
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09"],
            monkeypatch, tmp_path, mock_client=mock_client,
        )
        hyp_files = list((outdir / "wiki" / "hypotheses").glob("HYP-*.json"))
        assert hyp_files == []
        artifact = outdir / "wiki" / "hypotheses" / "no_proposals" / "2026-05-09.json"
        assert artifact.exists()

    def test_review_with_no_strategies_fails_closed(self, monkeypatch, tmp_path):
        # Write a review that mentions no supported strategy names
        review_dir = tmp_path / "wiki" / "daily_reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "2026-05-09.md").write_text(
            "# Futures Daily Review\n\nNo trades today.\n", encoding="utf-8"
        )
        (tmp_path / "wiki" / "hypotheses").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            sys, "argv",
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09"],
        )
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        # Inject a fake google.genai so we can confirm Client was never called
        fake_genai = MagicMock(name="google.genai")
        with patch.dict(sys.modules, {"google.genai": fake_genai}):
            try:
                _mod.main()
            except SystemExit as e:
                code = e.code
            else:
                code = 0

        # Gemini Client must NOT have been instantiated
        fake_genai.Client.assert_not_called()
        # No hypothesis files
        hyp_files = list((tmp_path / "wiki" / "hypotheses").glob("HYP-*.json"))
        assert hyp_files == []
        # Audit artifact must exist
        artifact = tmp_path / "wiki" / "hypotheses" / "no_proposals" / "2026-05-09.json"
        assert artifact.exists()
        assert code == 0

    def test_no_llm_flag_uses_deterministic_parse(self, monkeypatch, tmp_path):
        code, outdir = self._run_main(
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09", "--no-llm"],
            monkeypatch, tmp_path, env_key=None,
        )
        assert code in (0, None)
        for hf in (outdir / "wiki" / "hypotheses").glob("HYP-*.json"):
            loaded = json.loads(hf.read_text())
            assert loaded["created_by"] == "manual"
        # --no-llm must NOT write a no-proposals artifact
        no_props = outdir / "wiki" / "hypotheses" / "no_proposals"
        assert not no_props.exists() or not list(no_props.glob("*.json"))

    def test_max_hypotheses_limits_output(self, monkeypatch, tmp_path):
        two_candidates = [
            _VALID_GEMINI_CANDIDATE,
            {
                **_VALID_GEMINI_CANDIDATE,
                "symbol": "NIFTY",
                "strategy": "VP-07 Wicks Pullback",
                "claim": "Block NIFTY VP-07 SELL when ADX14 < 18",
                "filter": {"block_when": {"adx14_lt": 18.0}},
            },
        ]
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _make_gemini_response(two_candidates)
        code, outdir = self._run_main(
            ["propose_futures_hypotheses", "--wiki-dir", str(tmp_path / "wiki"),
             "--date", "2026-05-09", "--max-hypotheses", "1"],
            monkeypatch, tmp_path, mock_client=mock_client,
        )
        hyp_files = list((outdir / "wiki" / "hypotheses").glob("HYP-*.json"))
        assert len(hyp_files) <= 1


# ---------------------------------------------------------------------------
# Helpers shared by new test classes
# ---------------------------------------------------------------------------

_split_markdown_sections = _mod._split_markdown_sections
_truncate_section_lines = _mod._truncate_section_lines
_MAX_REVIEW_CHARS = _mod._MAX_REVIEW_CHARS


def _make_review(sections: dict, title: str = "# Review") -> str:
    parts = [title, ""]
    for heading, body in sections.items():
        parts.append(f"## {heading}")
        parts.append(body)
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# TestSplitMarkdownSections
# ---------------------------------------------------------------------------

class TestSplitMarkdownSections:
    def test_empty_string(self):
        preamble, sections = _split_markdown_sections("")
        assert preamble == ""
        assert sections == {}

    def test_no_headings(self):
        text = "This is just plain text.\nNo headings here."
        preamble, sections = _split_markdown_sections(text)
        assert preamble == text
        assert sections == {}

    def test_single_section(self):
        text = "# Title\n\n## Summary\nsome text"
        preamble, sections = _split_markdown_sections(text)
        assert preamble == "# Title"
        assert "Summary" in sections
        assert sections["Summary"].startswith("## Summary")

    def test_multiple_sections(self):
        text = (
            "# Daily Review\n\n"
            "## Summary\nThis is the summary.\n\n"
            "## Executed Trades\n| t | s |\n|---|---|\n| 09:00 | NIFTY |"
        )
        preamble, sections = _split_markdown_sections(text)
        assert "Summary" in sections
        assert "Executed Trades" in sections
        assert sections["Summary"].startswith("## Summary")
        assert sections["Executed Trades"].startswith("## Executed Trades")

    def test_section_value_includes_heading(self):
        text = "## Patterns Observed\n- Double reject\n"
        preamble, sections = _split_markdown_sections(text)
        for key, val in sections.items():
            assert val.startswith("## "), f"Section {key!r} value does not start with '## '"

    def test_trailing_whitespace_stripped(self):
        text = "## Log Issues\nsome content\n\n\n   \n"
        preamble, sections = _split_markdown_sections(text)
        assert "Log Issues" in sections
        val = sections["Log Issues"]
        assert val == val.rstrip(), "Section value should have trailing whitespace stripped"


# ---------------------------------------------------------------------------
# TestTruncateSectionLines
# ---------------------------------------------------------------------------

class TestTruncateSectionLines:
    def test_short_fits(self):
        section = "## Summary\nshort content"
        result = _truncate_section_lines(section, budget=500)
        assert result == section
        assert "_Section truncated" not in result

    def test_truncation_adds_marker(self):
        section = "## Summary\n" + ("long line content\n" * 200)
        result = _truncate_section_lines(section, budget=100)
        assert result.endswith("_Section truncated for prompt budget._")

    def test_truncation_within_budget(self):
        section = "## Summary\n" + ("data line\n" * 300)
        budget = 200
        result = _truncate_section_lines(section, budget=budget)
        assert len(result) <= budget

    def test_tiny_budget_returns_empty(self):
        section = "## Summary\nsome content"
        result = _truncate_section_lines(section, budget=5)
        assert result == ""

    def test_cuts_on_complete_lines(self):
        # 10 lines of "xxxxxxxxxx" (10 chars each), total section = 109 chars.
        # marker = "\n_Section truncated for prompt budget._" = 39 chars
        # content_budget = budget - 39.  Each line costs 11 (10 chars + 1 newline).
        # budget=72 → content_budget=33: 3 lines (33) fit, 4 lines (44) do not.
        # 72 < 109 so truncation is triggered.
        line = "x" * 10
        section = "\n".join([line] * 10)
        budget = 72
        result = _truncate_section_lines(section, budget=budget)
        marker = "_Section truncated for prompt budget._"
        assert result.endswith(marker)
        content_part = result[: result.index("\n" + marker)]
        complete_lines = content_part.split("\n")
        assert len(complete_lines) == 3
        for ln in complete_lines:
            assert ln == line, f"Expected complete line {line!r}, got {ln!r}"


# ---------------------------------------------------------------------------
# TestCompactReviewSectionAware
# ---------------------------------------------------------------------------

class TestCompactReviewSectionAware:
    def test_section_priority(self):
        sections = {
            "Log Issues": "x" * 2000,
            "Executed Trades": "| t | s |\n|---|---|\n| 09:00 | NIFTY |",
            "Rejected Signals": "| t | s |\n|---|---|\n| 09:10 | NIFTY |",
            "Patterns Observed": "- 2x reject",
        }
        review = _make_review(sections)
        result = compact_review(review)
        assert "## Executed Trades" in result
        assert "## Rejected Signals" in result

    def test_huge_summary_does_not_starve_trade_evidence(self):
        sections = {
            "Summary": "S" * 4000,
            "Executed Trades": "EXEC_MARK",
            "Rejected Signals": "REJ_MARK",
            "Patterns Observed": "PAT_MARK",
        }
        result = compact_review(_make_review(sections))
        assert "## Summary" in result
        assert "EXEC_MARK" in result
        assert "REJ_MARK" in result
        assert "PAT_MARK" in result
        assert len(result) <= _MAX_REVIEW_CHARS

    def test_huge_executed_table_does_not_starve_rejections_or_patterns(self):
        sections = {
            "Summary": "Short summary.",
            "Executed Trades": "\n".join(
                "| 09:00 | NIFTY | SELL | VP-01 Counter Bull Trap | -100 |"
                for _ in range(500)
            ),
            "Rejected Signals": "REJ_MARK",
            "Patterns Observed": "PAT_MARK",
        }
        result = compact_review(_make_review(sections))
        assert "## Executed Trades" in result
        assert "## Rejected Signals" in result
        assert "REJ_MARK" in result
        assert "## Patterns Observed" in result
        assert "PAT_MARK" in result
        assert len(result) <= _MAX_REVIEW_CHARS

    def test_possible_hypotheses_excluded(self):
        sections = {
            "Summary": "Short summary.",
            "Possible Hypotheses": "- Block VP-01 BUY when RSI14 < 25",
            "Executed Trades": "| t | s |\n|---|---|\n| 09:00 | NIFTY |",
        }
        review = _make_review(sections)
        result = compact_review(review)
        assert "Possible Hypotheses" not in result

    def test_budget_invariant(self):
        filler_400 = "a" * 400
        sections = {
            "Summary": filler_400,
            "Executed Trades": filler_400,
            "Rejected Signals": filler_400,
            "Patterns Observed": filler_400,
            "Indicator Context": filler_400,
            "Log Issues": filler_400,
        }
        review = _make_review(sections)
        result = compact_review(review)
        assert len(result) <= _MAX_REVIEW_CHARS

    def test_missing_sections(self):
        sections = {
            "Summary": "Only summary here.",
            "Executed Trades": "| t | s |\n|---|---|\n| 09:00 | NIFTY |",
        }
        review = _make_review(sections)
        result = compact_review(review)
        assert "## Summary" in result
        assert "## Executed Trades" in result

    def test_fallback_no_sections(self):
        plain_text = "Just some plain text " * 200  # ~4200 chars
        result = compact_review(plain_text)
        assert len(result) <= _MAX_REVIEW_CHARS
        assert result.startswith(plain_text[:50])

    def test_table_no_broken_row(self):
        table_rows = "\n".join(
            "| 09:00 | NIFTY | SELL | VP-01 Counter Bull Trap | ₹500 |"
            for _ in range(30)
        )
        sections = {
            "Executed Trades": table_rows,
        }
        review = _make_review(sections)
        result = compact_review(review)
        _MARKER = "_Section truncated for prompt budget._"
        if "## Executed Trades" in result:
            for line in result.split("\n"):
                if line.startswith("| "):
                    assert line.endswith(" |") or "---" in line, (
                        f"Partial table row found: {line!r}"
                    )

    def test_strategy_extraction_uses_full_review(self):
        # Build prefix that fills > 2500 chars with no strategy names
        summary_filler = "a" * 1400
        log_filler = "b" * 1400
        prefix_sections = {
            "Summary": summary_filler,
            "Log Issues": log_filler,
        }
        prefix = _make_review(prefix_sections)
        # Ensure prefix is longer than _MAX_REVIEW_CHARS
        assert len(prefix) > _MAX_REVIEW_CHARS
        # Add Rejected Signals section AFTER char 2500 in the raw text
        suffix = "\n## Rejected Signals\n| t | s |\n|---|---|\n| 09:10 | NIFTY | VP-07 Wicks Pullback |\n"
        full_review = prefix + suffix
        # Strategy should be found in full text
        assert "VP-07 Wicks Pullback" in extract_strategies_from_review(full_review)
        # But compact_review (which prioritizes Summary before Log Issues before nothing)
        # should NOT include the Rejected Signals content since Log Issues isn't in priority
        # Actually Log Issues IS in priority — so the budget will be spent on Summary + Log Issues
        # The Rejected Signals section comes AFTER Log Issues in priority order so it may be cut.
        # The key assertion: extract_strategies_from_review uses full text, not compact
        result_compact = compact_review(full_review)
        assert len(result_compact) <= _MAX_REVIEW_CHARS


# ---------------------------------------------------------------------------
# TestGeminiPromptContainsPrioritizedSections
# ---------------------------------------------------------------------------

class TestGeminiPromptContainsPrioritizedSections:
    def test_gemini_prompt_uses_compact_review(self, monkeypatch):
        # Build a review where Log Issues is 2000 chars and appears first,
        # but Executed Trades has a clearly identifiable marker string.
        _MARKER_STR = "NIFTY_MARKER_12345"
        sections = {
            "Log Issues": "z" * 2000,
            "Executed Trades": f"| t | s |\n|---|---|\n| 09:00 | {_MARKER_STR} |",
            "Summary": "Short summary.",
        }
        review_text = _make_review(sections)

        # We need a review that has at least one supported strategy for call_gemini
        # to proceed past the strategy check. We add VP-01 Counter Bull Trap to Summary.
        sections_with_strategy = {
            "Log Issues": "z" * 2000,
            "Executed Trades": (
                f"| t | s |\n|---|---|\n| 09:00 | {_MARKER_STR} |"
                "\nVP-01 Counter Bull Trap: 1 signal"
            ),
            "Summary": "Short summary.",
        }
        review_text = _make_review(sections_with_strategy)

        # Capture the prompt passed to generate_content
        captured_prompts = []

        mock_client = MagicMock()

        def _capture_generate(model, contents):
            captured_prompts.append(contents)
            return _make_gemini_response([_VALID_GEMINI_CANDIDATE])

        mock_client.models.generate_content.side_effect = _capture_generate

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        from tools.futures_strategy_engine import SUPPORTED_STRATEGIES
        strategies_in_review = {s for s in SUPPORTED_STRATEGIES if s in review_text}
        assert strategies_in_review, "Test setup error: no strategies found in review"

        with patch.dict(sys.modules, {"google.genai": _make_fake_genai(mock_client)}):
            call_gemini(
                review_text,
                _REVIEW_DATE_ISO,
                _DATE_COMPACT,
                max_hypotheses=3,
                strategies_in_review=strategies_in_review,
            )

        assert captured_prompts, "generate_content was never called"
        prompt = captured_prompts[0]

        # Executed Trades was prioritized — marker must appear in the prompt
        assert _MARKER_STR in prompt, (
            f"Expected {_MARKER_STR!r} in prompt — Executed Trades should be prioritized"
        )
        # Log Issues content (z*2000) should not be at the START of the prompt's review section
        # Find the review section (between --- delimiters in the template)
        review_start = prompt.find("---\n")
        if review_start != -1:
            snippet = prompt[review_start: review_start + 200]
            assert not snippet.startswith("---\nzzz"), (
                "Log Issues content should not appear at the start of the review in the prompt"
            )


# ---------------------------------------------------------------------------
# _extract_loss_tuples
# ---------------------------------------------------------------------------

_LOSS_CLUSTERS_REVIEW = textwrap.dedent("""\
    # Futures Daily Review — 2026-05-20

    ## Summary
    - Date: 2026-05-20
    - Futures trades executed: 5

    ## Loss Clusters

    | Symbol | Strategy | Direction | Count | Total P&L |
    |--------|----------|-----------|-------|-----------|
    | BANKNIFTY | VP-15 Evening Star | SELL | 3 | ₹-12,138.00 |
    | BANKNIFTY | VP-24 Pivot Bounce S2 | BUY | 2 | ₹-9,804.00 |

    ## Rejected Signals

    | Time | Symbol | Strategy | Reason |
    |------|--------|----------|--------|
    | 11:32:41 | BANKNIFTY26MAY26F | VPA No Demand | volume too low |
""")

_REVIEW_WITH_NO_LOSS_CLUSTERS = textwrap.dedent("""\
    # Futures Daily Review — 2026-05-20

    ## Summary
    - No trades today.

    ## Rejected Signals
    | Time | Symbol | Strategy | Reason |
""")


class TestExtractLossTuples:
    def test_parses_loss_clusters_section(self):
        tuples = _extract_loss_tuples(_LOSS_CLUSTERS_REVIEW)
        assert ("BANKNIFTY", "VP-15 Evening Star", "SELL") in tuples
        assert ("BANKNIFTY", "VP-24 Pivot Bounce S2", "BUY") in tuples

    def test_returns_empty_set_when_section_absent(self):
        tuples = _extract_loss_tuples(_REVIEW_WITH_NO_LOSS_CLUSTERS)
        assert tuples == set()

    def test_returns_empty_set_on_empty_string(self):
        assert _extract_loss_tuples("") == set()

    def test_does_not_include_rejected_signals_rows(self):
        tuples = _extract_loss_tuples(_LOSS_CLUSTERS_REVIEW)
        # VPA No Demand is in Rejected Signals not Loss Clusters
        for t in tuples:
            assert t[1] != "VPA No Demand"


# ---------------------------------------------------------------------------
# _validate_gemini_hypothesis — executed loss filter
# ---------------------------------------------------------------------------

class TestValidateGeminiHypothesisLossFilter:
    """Candidates targeting rejected-only strategies must be rejected when losses exist."""

    def _loss_tuples(self):
        return {
            ("BANKNIFTY", "VP-15 Evening Star", "SELL"),
        }

    def test_candidate_rejected_when_strategy_only_in_rejected_signals(self):
        """VPA No Demand only in rejected signals — must be rejected when executed losses exist."""
        from tools.futures_strategy_engine import SUPPORTED_STRATEGIES
        # VPA No Demand must be a supported strategy for this test to be meaningful
        if "VPA No Demand" not in SUPPORTED_STRATEGIES:
            pytest.skip("VPA No Demand not in SUPPORTED_STRATEGIES in this environment")
        bad = {
            **_VALID_GEMINI_CANDIDATE,
            "strategy": "VPA No Demand",
            "direction": "SELL",
            "claim": "Block VPA No Demand SELL when volume too low",
        }
        strategies_with_vpa = _STRATEGIES_IN_REVIEW | {"VPA No Demand"}
        ok, reason = _validate_gemini_hypothesis(
            bad, _REVIEW_DATE_ISO, strategies_with_vpa, self._loss_tuples()
        )
        assert not ok
        assert "rejected signals" in reason.lower() or "executed losses" in reason.lower()

    def test_candidate_accepted_when_strategy_in_executed_losses(self):
        """VP-15 Evening Star is in executed losses — must pass the loss filter."""
        candidate = {
            **_VALID_GEMINI_CANDIDATE,
            "strategy": "VP-15 Evening Star",
            "direction": "SELL",
            "claim": "Block VP-15 Evening Star SELL on BANKNIFTY when RSI14 < 30",
        }
        from tools.futures_strategy_engine import SUPPORTED_STRATEGIES
        if "VP-15 Evening Star" not in SUPPORTED_STRATEGIES:
            pytest.skip("VP-15 Evening Star not in SUPPORTED_STRATEGIES")
        strategies = {"VP-15 Evening Star"}
        ok, reason = _validate_gemini_hypothesis(
            candidate, _REVIEW_DATE_ISO, strategies, self._loss_tuples()
        )
        assert ok, reason

    def test_validation_falls_back_when_loss_tuples_empty(self):
        """When loss_tuples is empty, the loss filter is not applied."""
        ok, reason = _validate_gemini_hypothesis(
            _VALID_GEMINI_CANDIDATE, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW,
            executed_loss_tuples=set()
        )
        assert ok, reason

    def test_validation_falls_back_when_loss_tuples_none(self):
        """When loss_tuples is None, the loss filter is not applied."""
        ok, reason = _validate_gemini_hypothesis(
            _VALID_GEMINI_CANDIDATE, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW,
            executed_loss_tuples=None
        )
        assert ok, reason

    def test_accepts_same_symbol_strategy_different_direction(self):
        """If (symbol, strategy) appears in losses even with different direction, accept."""
        # Add BANKNIFTY VP-01 Counter Bull Trap BUY to loss tuples
        loss_tuples = {("BANKNIFTY", "VP-01 Counter Bull Trap", "BUY")}
        # Candidate proposes VP-01 SELL — (symbol, strategy) matches, direction differs
        ok, reason = _validate_gemini_hypothesis(
            _VALID_GEMINI_CANDIDATE, _REVIEW_DATE_ISO, _STRATEGIES_IN_REVIEW,
            executed_loss_tuples=loss_tuples
        )
        assert ok, reason


# ---------------------------------------------------------------------------
# compact_review — Loss Clusters section in priority
# ---------------------------------------------------------------------------

class TestCompactReviewLossClusters:
    def test_loss_clusters_appears_before_rejected_signals_in_compact_review(self):
        sections = {
            "Summary": "Short summary.",
            "Loss Clusters": "| BANKNIFTY | VP-15 Evening Star | SELL | 3 | ₹-12,138 |",
            "Executed Trades": "| t | s |\n|---|---|\n| 09:00 | BANKNIFTY |",
            "Rejected Signals": "| t | s |\n|---|---|\n| 09:10 | NIFTY |",
        }
        review = _make_review(sections)
        result = compact_review(review)
        loss_pos = result.find("## Loss Clusters")
        rej_pos = result.find("## Rejected Signals")
        assert loss_pos != -1, "Loss Clusters section should appear in compact review"
        assert rej_pos != -1
        assert loss_pos < rej_pos, "Loss Clusters must appear before Rejected Signals"

    def test_loss_clusters_gets_budget_allocation(self):
        # Large sections around it should not starve Loss Clusters completely
        sections = {
            "Summary": "S" * 4000,
            "Loss Clusters": "LOSS_CLUSTER_MARKER",
            "Executed Trades": "E" * 1000,
            "Rejected Signals": "R" * 1000,
        }
        review = _make_review(sections)
        result = compact_review(review)
        assert "LOSS_CLUSTER_MARKER" in result, (
            "Loss Clusters should receive budget allocation and not be starved"
        )
