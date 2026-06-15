"""
GammaBlast/tools/candidate_audit.py — Append-only JSONL audit trail for all GammaBlast candidate signals.

Records every candidate signal at every stage of the expiry-day pipeline in a
per-day JSONL file. This is a pure append-only, fail-silent audit log — it never
affects scanning or virtual trading logic.

File layout:
    <audit_dir>/YYYYMMDD.jsonl   (one file per trading day, UTC date)

Each line is a JSON object:
    {
        "ts":               "2026-06-10T09:17:43.123Z",
        "candidate_id":     "NIFTY_24500_CE_1718000000",
        "stage":            "COILED_DETECTED",
        "symbol":           "NIFTY",
        "expiry":           "10-JUN-2026",
        "strike":           24500,
        "option_type":      "CE",
        "confidence_score": 0.75,
        "reason":           "",
        "details":          {}
    }

Stages (in pipeline order):
    COILED_DETECTED     — Stage 1 ARM conditions met; candidate enters watchlist
    COILED_EXPIRED      — Candidate no longer meets ARM conditions; removed
    RELEASE_TRIGGERED   — Stage 2 breakout/release entry conditions met
    VIRTUAL_ENTRY       — Virtual position opened at market
    TRAILING_UPDATE     — Trailing stop level updated
    VIRTUAL_EXIT        — Virtual position closed (target/stop/trail hit)
    EOD_FORCE_CLOSE     — Forced close at 15:15 IST, end-of-day sweep
    INVALIDATED         — Candidate rejected (data error, liquidity filter, etc.)

Invariants:
    - Never raises — all errors are logged and swallowed
    - Thread-safe: uses a per-instance lock around file writes
    - Never read by scanning or virtual-trading logic — write-only hot path
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("GammaBlast.CandidateAudit")

_VALID_STAGES = {
    "COILED_DETECTED",
    "COILED_EXPIRED",
    "RELEASE_TRIGGERED",
    "VIRTUAL_ENTRY",
    "TRAILING_UPDATE",
    "VIRTUAL_EXIT",
    "EOD_FORCE_CLOSE",
    "INVALIDATED",
}


class CandidateAudit:
    """
    Append-only JSONL audit log for all GammaBlast candidate signals.

    Usage:
        audit = CandidateAudit(Path("/runtime/gammablast/candidate_signals"))
        audit.record(
            candidate_id="NIFTY_24500_CE_1718000000",
            stage="COILED_DETECTED",
            symbol="NIFTY",
            expiry="10-JUN-2026",
            strike=24500,
            option_type="CE",
            confidence_score=0.75,
        )

    File path resolves to: <audit_dir>/<YYYYMMDD>.jsonl  (UTC date of the write)
    """

    def __init__(self, audit_dir: Path) -> None:
        self._dir = Path(audit_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        candidate_id: str,
        stage: str,
        symbol: str,
        expiry: str,
        strike: int,
        option_type: str,
        confidence_score: float = 0.0,
        reason: str = "",
        details: Optional[dict] = None,
    ) -> None:
        """
        Append one stage-transition record to today's JSONL file.
        Never raises — all errors are caught and logged.

        Args:
            candidate_id:     Unique identifier for this candidate
                              (e.g. "NIFTY_24500_CE_1718000000").
            stage:            One of _VALID_STAGES. Unknown stages are accepted
                              but trigger a warning log.
            symbol:           Underlying symbol, e.g. "NIFTY" or "BANKNIFTY".
            expiry:           Expiry date string, e.g. "10-JUN-2026".
            strike:           Strike price as an integer, e.g. 24500.
            option_type:      "CE" or "PE".
            confidence_score: Float in [0.0, 1.0] representing scanner confidence.
            reason:           Human-readable explanation (especially for
                              COILED_EXPIRED / INVALIDATED stages).
            details:          Optional extra payload (scanner metrics, prices, etc.).
        """
        try:
            now = datetime.now(timezone.utc)
            entry: dict = {
                "ts": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "candidate_id": candidate_id,
                "stage": stage,
                "symbol": symbol,
                "expiry": expiry,
                "strike": strike,
                "option_type": option_type,
                "confidence_score": confidence_score,
                "reason": reason,
                "details": details if details is not None else {},
            }

            if stage not in _VALID_STAGES:
                logger.warning(
                    "CandidateAudit: unknown stage %r — recording anyway", stage
                )

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
        Returns [] if the file does not exist or cannot be read.
        Intended for EOD reporting — never called from the hot scanning path.
        """
        try:
            filename = self._dir / datetime.now(timezone.utc).strftime("%Y%m%d.jsonl")
            if not filename.exists():
                return []
            records: list[dict] = []
            with open(filename, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass  # corrupt line — skip silently
            return records
        except Exception:
            logger.exception("CandidateAudit.read_today failed")
            return []
