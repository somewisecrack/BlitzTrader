"""
tests/test_candidate_audit.py
------------------------------
Unit tests for CandidateAudit — the durable JSONL candidate signal audit log.

Invariants:
  - Every write is a valid JSON line
  - Never raises (fail-silent)
  - Thread-safe under concurrent writes
  - read_today() returns all records from today's file
  - record() with unknown stage is accepted (with a log warning)
  - "approved" / "decision" are never written by the audit itself
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.candidate_audit import CandidateAudit, _VALID_STAGES


def _signal(**kwargs) -> dict:
    base = {
        "symbol": "BANKNIFTY",
        "strategy": "VP-07 Wicks Pullback",
        "direction": "SELL",
        "interval": "5",
        "entry_reference": 56000.0,
        "stop_loss": 56150.0,
        "target": 55750.0,
    }
    base.update(kwargs)
    return base


class TestCandidateAuditBasic(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.audit = CandidateAudit(Path(self._tmpdir.name))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_record_creates_jsonl_file(self):
        self.audit.record(
            signal_id="test_001",
            stage="RAW_CANDIDATE",
            signal=_signal(),
        )
        files = list(Path(self._tmpdir.name).glob("*.jsonl"))
        self.assertEqual(len(files), 1)

    def test_record_writes_valid_json_line(self):
        self.audit.record(
            signal_id="test_001",
            stage="RAW_CANDIDATE",
            signal=_signal(),
        )
        files = list(Path(self._tmpdir.name).glob("*.jsonl"))
        lines = files[0].read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["signal_id"], "test_001")
        self.assertEqual(data["stage"], "RAW_CANDIDATE")

    def test_record_includes_signal_fields(self):
        self.audit.record(
            signal_id="test_002",
            stage="RAW_CANDIDATE",
            signal=_signal(symbol="NIFTY", direction="BUY"),
        )
        records = self.audit.read_today()
        self.assertEqual(records[0]["symbol"], "NIFTY")
        self.assertEqual(records[0]["direction"], "BUY")
        self.assertAlmostEqual(records[0]["entry_reference"], 56000.0)
        self.assertAlmostEqual(records[0]["stop_loss"], 56150.0)
        self.assertAlmostEqual(records[0]["target"], 55750.0)

    def test_reason_included_when_provided(self):
        self.audit.record(
            signal_id="test_003",
            stage="HARD_GUARDRAIL_BLOCKED",
            signal=_signal(),
            reason="Max positions reached",
        )
        records = self.audit.read_today()
        self.assertIn("reason", records[0])
        self.assertEqual(records[0]["reason"], "Max positions reached")

    def test_reason_omitted_when_empty(self):
        self.audit.record(
            signal_id="test_004",
            stage="RAW_CANDIDATE",
            signal=_signal(),
            reason="",
        )
        records = self.audit.read_today()
        self.assertNotIn("reason", records[0])

    def test_details_included_when_provided(self):
        self.audit.record(
            signal_id="test_005",
            stage="GATEKEEPER_APPROVED",
            signal=_signal(),
            details={"gate_confidence": 0.85, "conditions_checked": ["EMA bull"]},
        )
        records = self.audit.read_today()
        self.assertIn("details", records[0])
        self.assertAlmostEqual(records[0]["details"]["gate_confidence"], 0.85)

    def test_details_omitted_when_none(self):
        self.audit.record(
            signal_id="test_006",
            stage="RAW_CANDIDATE",
            signal=_signal(),
            details=None,
        )
        records = self.audit.read_today()
        self.assertNotIn("details", records[0])

    def test_multiple_records_appended(self):
        for stage in ("RAW_CANDIDATE", "HARD_GUARDRAIL_PASSED", "PYTHON_REVIEW_PASSED"):
            self.audit.record(signal_id="test_007", stage=stage, signal=_signal())
        records = self.audit.read_today()
        self.assertEqual(len(records), 3)
        self.assertEqual([r["stage"] for r in records],
                         ["RAW_CANDIDATE", "HARD_GUARDRAIL_PASSED", "PYTHON_REVIEW_PASSED"])

    def test_record_has_timestamp_field(self):
        self.audit.record(signal_id="test_008", stage="RAW_CANDIDATE", signal=_signal())
        records = self.audit.read_today()
        self.assertIn("ts", records[0])
        self.assertTrue(records[0]["ts"].endswith("Z"))

    def test_unknown_stage_does_not_raise(self):
        """Unknown stages are accepted with a warning — never raise."""
        self.audit.record(
            signal_id="test_009",
            stage="SOME_FUTURE_STAGE",
            signal=_signal(),
        )
        records = self.audit.read_today()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["stage"], "SOME_FUTURE_STAGE")

    def test_no_approved_or_decision_keys_written(self):
        """Audit records must never contain 'approved' or 'decision' keys."""
        self.audit.record(signal_id="test_010", stage="ORDER_PLACED", signal=_signal())
        records = self.audit.read_today()
        self.assertNotIn("approved", records[0])
        self.assertNotIn("decision", records[0])

    def test_read_today_returns_empty_list_if_no_file(self):
        records = self.audit.read_today()
        self.assertEqual(records, [])


class TestCandidateAuditFailSilent(unittest.TestCase):
    """Audit must never raise — failures are swallowed."""

    def test_record_to_nonexistent_parent_creates_dir(self):
        """CandidateAudit creates audit_dir automatically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            deep_path = Path(tmpdir) / "a" / "b" / "c"
            audit = CandidateAudit(deep_path)
            # Should not raise
            audit.record(signal_id="x", stage="RAW_CANDIDATE", signal=_signal())
            self.assertTrue(deep_path.exists())

    def test_read_today_does_not_raise_on_corrupt_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit = CandidateAudit(Path(tmpdir))
            # Write one valid and one corrupt line
            from datetime import datetime, timezone
            fname = Path(tmpdir) / datetime.now(timezone.utc).strftime("%Y%m%d.jsonl")
            fname.write_text('{"valid": true}\nnot json {{\n{"also": "valid"}\n')
            records = audit.read_today()
            # Only the two valid lines come back
            self.assertEqual(len(records), 2)


class TestCandidateAuditThreadSafety(unittest.TestCase):
    """Concurrent writes must not corrupt the JSONL file."""

    def test_concurrent_writes_all_recorded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit = CandidateAudit(Path(tmpdir))
            n = 50
            errors = []

            def write_record(i):
                try:
                    audit.record(
                        signal_id=f"sig_{i:03d}",
                        stage="RAW_CANDIDATE",
                        signal=_signal(symbol=f"SYM{i}"),
                    )
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=write_record, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"Concurrent writes raised: {errors}")
            records = audit.read_today()
            self.assertEqual(len(records), n)
            # Every line must be valid JSON
            files = list(Path(tmpdir).glob("*.jsonl"))
            for line in files[0].read_text().strip().splitlines():
                json.loads(line)  # raises if corrupt


class TestValidStages(unittest.TestCase):
    def test_all_pipeline_stages_defined(self):
        expected = {
            "RAW_CANDIDATE",
            "HARD_GUARDRAIL_BLOCKED",
            "HARD_GUARDRAIL_PASSED",
            "PYTHON_REVIEW_REJECTED",
            "PYTHON_REVIEW_PASSED",
            "GEMMA_OPINION",
            "GATEKEEPER_REJECTED",
            "GATEKEEPER_APPROVED",
            "ORDER_PLACED",
            "ORDER_REJECTED",
        }
        self.assertEqual(_VALID_STAGES, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
