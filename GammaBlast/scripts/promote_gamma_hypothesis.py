#!/usr/bin/env python3
"""
scripts/promote_gamma_hypothesis.py — Promote a backtested hypothesis to a runtime rule.

Reads a hypothesis JSON (must have status=BACKTESTED and verdict=IMPROVE).
Writes a deterministic rule JSON to wiki/promoted_rules/<rule_id>.json.

Runtime (config.py) loads wiki/promoted_rules/*.json at startup to override
default thresholds with promoted values.

Promotion criteria:
  - status == "BACKTESTED"
  - backtest_result.verdict == "IMPROVE"
  - delta_precision >= 0 (no precision regression)
  - delta_recall >= 0   (no recall regression)

Usage:
    python3 scripts/promote_gamma_hypothesis.py wiki/hypotheses/20260610_volburst.json
    python3 scripts/promote_gamma_hypothesis.py wiki/hypotheses/20260610_volburst.json --force
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_IST = timezone(timedelta(hours=5, minutes=30))

_ALLOWED_PARAMETERS = {
    "VOLUME_BURST_RATIO",
    "VOLUME_BURST_MIN_IN_WINDOW",
    "PREMIUM_MAX_RATIO",
    "OI_HIGH_RATIO",
    "BID_IMBALANCE_MIN",
    "TRAIL_ACTIVATION_MULT",
    "TRAIL_INITIAL_FRACTION",
    "TRAIL_TIGHT_MULT",
    "TRAIL_TIGHT_FRACTION",
    "HARD_STOP_FRACTION",
    "STALE_DATA_SECONDS",
}


def _safe_to_promote(hyp: dict) -> tuple[bool, str]:
    status = hyp.get("status")
    if status != "BACKTESTED":
        return False, f"status is {status!r}, must be BACKTESTED"
    br = hyp.get("backtest_result") or {}
    verdict = br.get("verdict")
    if verdict != "IMPROVE":
        return False, f"verdict is {verdict!r}, must be IMPROVE"
    dp = br.get("delta_precision", -1)
    dr = br.get("delta_recall", -1)
    if dp < 0:
        return False, f"delta_precision={dp:.3f} < 0 (precision regression)"
    if dr < 0:
        return False, f"delta_recall={dr:.3f} < 0 (recall regression)"
    change = hyp.get("proposed_change") or {}
    param = change.get("parameter")
    if param not in _ALLOWED_PARAMETERS:
        return False, f"parameter {param!r} not in allowed set"
    return True, ""


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("hypothesis_file")
    parser.add_argument("--force", action="store_true", help="Promote even if verdict != IMPROVE")
    args = parser.parse_args()

    hyp_path = Path(args.hypothesis_file)
    if not hyp_path.exists():
        print(f"File not found: {hyp_path}", file=sys.stderr)
        return 1

    hyp = json.loads(hyp_path.read_text())

    ok, reason = _safe_to_promote(hyp)
    if not ok and not args.force:
        print(f"Cannot promote: {reason}")
        print("Use --force to override promotion gate (not recommended).")
        return 1
    if not ok:
        print(f"WARNING: promoting despite: {reason}")

    change = hyp.get("proposed_change", {})
    rule = {
        "rule_id": hyp.get("hypothesis_id", "unknown"),
        "parameter": change.get("parameter"),
        "value": change.get("proposed_value"),
        "scope": change.get("scope", "BOTH"),
        "description": hyp.get("description", ""),
        "promoted_at": datetime.now(_IST).isoformat(timespec="seconds"),
        "backtest_summary": hyp.get("backtest_result"),
    }

    rules_dir = _ROOT / "wiki" / "promoted_rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    out = rules_dir / f"{rule['rule_id']}.json"
    out.write_text(json.dumps(rule, indent=2), encoding="utf-8")

    hyp["status"] = "PROMOTED"
    hyp_path.write_text(json.dumps(hyp, indent=2), encoding="utf-8")

    print(f"Rule promoted: {out}")
    print(f"  {rule['parameter']} = {rule['value']} (scope={rule['scope']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
