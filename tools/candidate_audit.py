"""
tools/candidate_audit.py — Durable JSONL audit trail for all candidate signals.

Records every candidate signal at every stage of the pipeline in a per-day JSONL
file. This is a pure append-only, fail-silent audit log — it never affects trading.

File layout:
    CANDIDATE_AUDIT_DIR/YYYYMMDD.jsonl   (one file per trading day)

Each line is a JSON object:
    {
        "ts":              "2026-01-02T09:17:43.123456Z",
        "signal_id":       "BANKNIFTY_VP07_SELL_1735808263",
        "stage":           "RAW_CANDIDATE",
        "symbol":          "BANKNIFTY",
        "strategy":        "VP-07 Wicks Pullback",
        "direction":       "SELL",
        "interval":        "5",
        "entry_reference": 56000.0,
        "stop_loss":       56150.0,
        "target":          55750.0,
        "reason":          "",           // optional: block/reject reason
        "details":         {}            // optional: gatekeeper result, etc.
    }

Stages (in pipeline order):
    RAW_CANDIDATE           — scanner output, before any filter
    HARD_GUARDRAIL_BLOCKED  — rejected by hard guardrails (paused/max-positions/timing/stale)
    HARD_GUARDRAIL_PASSED   — passed hard guardrails, entering Python review
    PYTHON_REVIEW_REJECTED  — rejected by deterministic Python indicator/risk checks
    PYTHON_REVIEW_PASSED    — Python approved; spread builder called
    SPREAD_BUILD_REJECTED   — SpreadBuilder returned None (no valid spread candidate)
    SPREAD_BUILT            — SpreadCandidate built; Gemini gatekeeper called
    GATEKEEPER_REJECTED     — Gemini gatekeeper REJECT or validation/timeout error
    GATEKEEPER_APPROVED     — Gemini gatekeeper APPROVE
    SPREAD_ORDER_PLACED     — both legs filled; OpenSpread recorded
    SPREAD_ORDER_REJECTED   — spread execution layer rejected (leg fill failure etc.)
    SPREAD_EXITED           — spread closed (P&L exit / EOD / manual)
    SPREAD_EXIT_FAILED      — spread close failed (leg fill timeout / error)
    ORDER_PLACED            — legacy: virtual futures order FILLED or PENDING
    ORDER_REJECTED          — legacy: virtual futures order rejected

Invariants:
    - Never raises — all errors are logged and swallowed
    - Thread-safe: uses a per-instance lock on the file handle
    - Never read by trading logic — write-only from the trader's perspective
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("BlitzTrader.CandidateAudit")

_VALID_STAGES = {
    # Original stages (kept for backward compat with historical audit files)
    "RAW_CANDIDATE",
    "HARD_GUARDRAIL_BLOCKED",
    "HARD_GUARDRAIL_PASSED",
    "PYTHON_REVIEW_REJECTED",
    "PYTHON_REVIEW_PASSED",
    "GATEKEEPER_REJECTED",
    "GATEKEEPER_APPROVED",
    "ORDER_PLACED",
    "ORDER_REJECTED",
    # Options spread stages (new live path)
    "SPREAD_BUILD_REJECTED",
    "SPREAD_BUILT",
    "SPREAD_BUILD_SUCCESS",
    "SPREAD_ORDER_PLACED",
    "SPREAD_ORDER_REJECTED",
    "SPREAD_ORDER_FAILED",
    "SPREAD_EXITED",
    "SPREAD_EXIT_FAILED",
}


class CandidateAudit:
    """
    Append-only JSONL audit log for all candidate signals.

    Usage:
        audit = CandidateAudit(Path("/runtime/candidate_signals"))
        audit.record(
            signal_id="BANKNIFTY_VP07_SELL_123",
            stage="RAW_CANDIDATE",
            signal={"symbol": "BANKNIFTY", ...},
        )

    File path resolves to: <audit_dir>/<YYYYMMDD>.jsonl  (UTC date of the write)
    """

    def __init__(self, audit_dir: Path):
        self._dir = Path(audit_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        signal_id: str,
        stage: str,
        signal: dict,
        reason: str = "",
        details: Optional[dict] = None,
    ) -> None:
        """
        Append one stage-transition record to today's JSONL file.
        Never raises — all errors are caught and logged.

        Args:
            signal_id: Unique identifier for this signal (assigned at RAW_CANDIDATE stage).
            stage:     One of _VALID_STAGES. Unknown stages are accepted but logged.
            signal:    The raw signal dict (symbol/strategy/direction/etc. extracted).
            reason:    Human-readable explanation for BLOCKED/REJECTED stages.
            details:   Optional extra payload (e.g. gatekeeper result, decision context).
        """
        try:
            now = datetime.now(timezone.utc)
            entry: dict = {
                "ts": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "signal_id": signal_id,
                "stage": stage,
                "symbol": signal.get("symbol", ""),
                "strategy": signal.get("strategy", ""),
                "direction": signal.get("direction", ""),
                "interval": str(signal.get("interval", "")),
                "entry_reference": signal.get("entry_reference"),
                "stop_loss": signal.get("stop_loss"),
                "target": signal.get("target"),
            }
            if reason:
                entry["reason"] = reason
            if details:
                entry["details"] = details

            if stage not in _VALID_STAGES:
                logger.warning("CandidateAudit: unknown stage %r — recording anyway", stage)

            filename = self._dir / now.strftime("%Y%m%d.jsonl")
            line = json.dumps(entry, default=str)
            with self._lock:
                with open(filename, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            logger.exception("CandidateAudit.record failed (non-fatal, audit skipped)")

    def read_today(self) -> list[dict]:
        """
        Return all records from today's audit file as a list of dicts.
        Returns [] if the file doesn't exist or is unreadable.
        Used by EOD/wiki pipeline — never called from the hot trading path.
        """
        try:
            filename = self._dir / datetime.now(timezone.utc).strftime("%Y%m%d.jsonl")
            if not filename.exists():
                return []
            records = []
            with open(filename, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass  # corrupt line — skip
            return records
        except Exception:
            logger.exception("CandidateAudit.read_today failed")
            return []
