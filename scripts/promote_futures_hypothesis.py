#!/usr/bin/env python3
"""
scripts/promote_futures_hypothesis.py
---------------------------------------
Reads a hypothesis + its backtest result; if promotion_decision.promote
is True, writes a promoted filter to wiki/promoted_filters/ and updates
the hypothesis status to "promoted".

Usage:
    python3 scripts/promote_futures_hypothesis.py --hypothesis wiki/hypotheses/HYP-20260509-001.yaml
    python3 scripts/promote_futures_hypothesis.py \
        --hypothesis wiki/hypotheses/HYP-20260509-001.yaml \
        --result wiki/backtest_results/HYP-20260509-001.json
"""

import json
import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Import tools/futures_hypothesis.py
try:
    from tools.futures_hypothesis import (
        load_hypothesis,
        validate_hypothesis,
        write_hypothesis,
        load_backtest_result,
        validate_backtest_result,
        write_backtest_result,
        promote_if_passed,
    )
except ImportError as e:
    print(
        f"ERROR: Could not import tools.futures_hypothesis: {e}\n"
        "Make sure tools/futures_hypothesis.py exists and is importable.",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))
MAX_OUTPUT_BYTES = 1_000_000


# ── Helpers ────────────────────────────────────────────────────────────────────


def ist_now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def load_yaml_or_json(path: Path) -> dict:
    """Load a YAML or JSON file, preferring the API functions."""
    # Try JSON first
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    # Try hypothesis loader
    try:
        return load_hypothesis(path)
    except Exception:
        pass
    # Try result loader
    try:
        return load_backtest_result(path)
    except Exception:
        pass
    raise ValueError(f"Cannot parse file as JSON or YAML: {path}")


def update_hypothesis_status(hyp: dict, hyp_path: Path, new_status: str):
    """Update hypothesis status field and rewrite the file."""
    hyp["status"] = new_status
    hyp["promoted_at"] = ist_now()
    try:
        write_hypothesis(hyp, hyp_path)
    except Exception:
        # Fallback: write as JSON
        hyp_path.write_text(
            json.dumps(hyp, indent=2, ensure_ascii=False), encoding="utf-8"
        )


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="BlitzTrader futures hypothesis promoter"
    )
    parser.add_argument("--hypothesis", required=True, help="Path to hypothesis YAML/JSON")
    parser.add_argument(
        "--result",
        default=None,
        help="Path to backtest result JSON (default: wiki/backtest_results/{hypothesis_id}.json)",
    )
    parser.add_argument(
        "--wiki-dir",
        default=None,
        help="Wiki directory (default: {repo_root}/wiki)",
    )
    args = parser.parse_args()

    wiki_dir = Path(args.wiki_dir).expanduser().resolve() if args.wiki_dir else _REPO_ROOT / "wiki"
    hyp_path = Path(args.hypothesis).expanduser().resolve()

    if not hyp_path.exists():
        print(f"ERROR: Hypothesis file not found: {hyp_path}", file=sys.stderr)
        sys.exit(1)

    # Step 1: Load hypothesis
    print(f"[promote_futures_hypothesis] Loading hypothesis: {hyp_path}")
    try:
        hypothesis = load_hypothesis(hyp_path)
    except Exception as e:
        print(f"ERROR: Could not load hypothesis: {e}", file=sys.stderr)
        sys.exit(1)

    ok, reason = validate_hypothesis(hypothesis)
    if not ok:
        print(f"ERROR: Hypothesis validation failed: {reason}", file=sys.stderr)
        sys.exit(1)

    hyp_id = hypothesis.get("id", hyp_path.stem)
    print(f"  Hypothesis ID: {hyp_id}")

    # Step 2: Resolve result path
    if args.result:
        result_path = Path(args.result).expanduser().resolve()
    else:
        result_path = wiki_dir / "backtest_results" / f"{hyp_id}.json"

    if not result_path.exists():
        print(
            f"ERROR: Backtest result not found: {result_path}\n"
            f"Run scripts/backtest_futures_hypothesis.py --hypothesis {hyp_path} first.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[promote_futures_hypothesis] Loading backtest result: {result_path}")
    try:
        result = load_backtest_result(result_path)
    except Exception as e:
        print(f"ERROR: Could not load backtest result: {e}", file=sys.stderr)
        sys.exit(1)

    ok, reason = validate_backtest_result(result)
    if not ok:
        print(f"ERROR: Backtest result validation failed: {reason}", file=sys.stderr)
        sys.exit(1)

    # Step 3: Call promote_if_passed
    print("[promote_futures_hypothesis] Evaluating promotion...")
    promoted_filter = promote_if_passed(hypothesis, result)

    if promoted_filter is None:
        # Promotion denied
        decision = result.get("promotion_decision", {})
        deny_reason = decision.get("reason", "Promotion thresholds not met.")
        print(f"[promote_futures_hypothesis] NOT promoted: {deny_reason}")
        print("  No files written. Hypothesis status unchanged.")
        sys.exit(0)

    # Step 4: Write promoted filter
    # promoted_filter already contains "id" from promote_if_passed — do NOT add filter_id.

    if "promoted_at" not in promoted_filter:
        promoted_filter["promoted_at"] = ist_now()

    if "status" not in promoted_filter:
        promoted_filter["status"] = "active"

    # Enrich with backtest summary (compact — no raw OHLCV)
    if "backtest_summary" not in promoted_filter:
        promoted_filter["backtest_summary"] = {
            "period": result.get("period"),
            "interval": result.get("interval"),
            "ticker": result.get("ticker"),
            "baseline_trades": result.get("baseline", {}).get("trades"),
            "filtered_trades": result.get("filtered", {}).get("trades"),
            "filtered_win_rate": result.get("filtered", {}).get("win_rate"),
            "filtered_net_pnl_points": result.get("filtered", {}).get("net_pnl_points"),
            "filtered_profit_factor": result.get("filtered", {}).get("profit_factor"),
            "promotion_reason": result.get("promotion_decision", {}).get("reason"),
        }

    filter_id = promoted_filter["id"]
    promoted_dir = wiki_dir / "promoted_filters"
    promoted_dir.mkdir(parents=True, exist_ok=True)
    output_path = promoted_dir / f"{filter_id}.json"

    json_content = json.dumps(promoted_filter, indent=2, ensure_ascii=False)
    encoded = json_content.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        print(
            f"WARNING: Promoted filter exceeds 1 MB ({len(encoded)} bytes).",
            file=sys.stderr,
        )
    output_path.write_text(json_content, encoding="utf-8")
    print(f"[promote_futures_hypothesis] Promoted filter written: {output_path}")

    # Size check
    size = output_path.stat().st_size
    if size > MAX_OUTPUT_BYTES:
        print(
            f"WARNING: Promoted filter file is {size} bytes (> 1 MB).",
            file=sys.stderr,
        )

    # Step 5: Update hypothesis status to "promoted"
    print(f"[promote_futures_hypothesis] Updating hypothesis status to 'promoted': {hyp_path}")
    update_hypothesis_status(hypothesis, hyp_path, "promoted")
    print(f"  Hypothesis file updated: {hyp_path}")

    # Step 6: Confirmation
    print()
    print("=" * 60)
    print(f"PROMOTED: {hyp_id}")
    print(f"  Filter ID    : {filter_id}")
    print(f"  Symbol       : {hypothesis.get('symbol')}")
    print(f"  Direction    : {hypothesis.get('direction')}")
    print(f"  Strategy     : {hypothesis.get('strategy')}")
    print(f"  Claim        : {hypothesis.get('claim')}")
    print(f"  Filter file  : {output_path}")
    print(f"  Hypothesis   : {hyp_path} [status=promoted]")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  The BlitzTrader live session loads active promoted filters automatically from")
    print("  wiki/promoted_filters/ at startup. Deploy this repository update to the VM")
    print("  and restart the session — the next live session will apply this filter.")
    print()
    print("  LLM/Gemini is NOT involved in deployment. All filter loading is Python-driven.")


if __name__ == "__main__":
    main()
