#!/usr/bin/env python3
"""
scripts/run_futures_wiki_loop.py
---------------------------------
Post-market wiki automation: evaluate → propose → backtest → promote.

Runs the full wiki self-improvement pipeline for a single trading day.
Designed to be invoked by blitztrader-wiki-loop.timer at 15:30 IST Mon-Fri.
On non-trading days exits 0 silently (systemd won't flag it as failed).

Usage:
    python3 scripts/run_futures_wiki_loop.py
    python3 scripts/run_futures_wiki_loop.py --date 2026-05-13
    python3 scripts/run_futures_wiki_loop.py --date 2026-05-13 --runtime-root /opt/blitztrader/runtime
    python3 scripts/run_futures_wiki_loop.py --force   # run even on non-trading days
"""

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tools.market_calendar import is_nse_trading_day, get_market_holiday_name  # noqa: E402

_IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    return datetime.now(_IST).date()


def _run(cmd: list[str], label: str) -> subprocess.CompletedProcess:
    """Run a subprocess, stream nothing — capture stdout+stderr."""
    print(f"[wiki-loop] {label}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            print(f"  {line}")
    if result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            print(f"  STDERR: {line}", file=sys.stderr)
    return result


def _find_today_hypotheses(wiki_dir: Path, run_date: date) -> list[Path]:
    """Return all HYP-YYYYMMDD-*.json files written today in wiki/hypotheses/."""
    hyp_dir = wiki_dir / "hypotheses"
    if not hyp_dir.exists():
        return []
    date_str = run_date.strftime("%Y%m%d")
    return sorted(hyp_dir.glob(f"HYP-{date_str}-*.json"))


def _write_summary(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BlitzTrader post-market wiki loop runner"
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date YYYY-MM-DD to run (default: today IST)",
    )
    parser.add_argument(
        "--runtime-root",
        default=None,
        help="Runtime storage directory (default: RUNTIME_STORAGE_DIR env or repo root)",
    )
    parser.add_argument(
        "--wiki-dir",
        default=None,
        help="Wiki directory (default: {repo_root}/wiki)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Run even on non-trading days (for testing/backfill)",
    )
    args = parser.parse_args()

    run_date = date.fromisoformat(args.date) if args.date else _today_ist()
    wiki_dir = Path(args.wiki_dir).expanduser().resolve() if args.wiki_dir else _REPO_ROOT / "wiki"

    # --- Trading day guard ---
    if not args.force:
        if not is_nse_trading_day(run_date):
            holiday = get_market_holiday_name(run_date)
            reason = f"holiday: {holiday}" if holiday else "weekend"
            print(f"[wiki-loop] {run_date} is not a trading day ({reason}). Skipping.")
            sys.exit(0)

    date_str = run_date.isoformat()
    python = sys.executable

    summary: dict = {
        "date": date_str,
        "started_at": datetime.now(_IST).isoformat(),
        "steps": {},
    }

    # ── Step 1: evaluate_futures_day ─────────────────────────────────────────
    eval_cmd = [python, str(_REPO_ROOT / "scripts" / "evaluate_futures_day.py"), "--date", date_str]
    if args.runtime_root:
        eval_cmd += ["--runtime-root", args.runtime_root]
    if args.wiki_dir:
        eval_cmd += ["--wiki-dir", str(wiki_dir)]

    eval_result = _run(eval_cmd, f"Step 1: evaluate_futures_day --date {date_str}")
    summary["steps"]["evaluate"] = {
        "returncode": eval_result.returncode,
        "ok": eval_result.returncode == 0,
    }
    if eval_result.returncode != 0:
        print(f"[wiki-loop] evaluate_futures_day failed (rc={eval_result.returncode}). Aborting.", file=sys.stderr)
        summary["aborted"] = True
        summary["abort_reason"] = "evaluate_futures_day failed"
        _write_summary(wiki_dir / "metrics" / f"{date_str}-wiki-run.json", summary)
        sys.exit(1)

    # ── Step 2: propose_futures_hypotheses ───────────────────────────────────
    propose_cmd = [
        python,
        str(_REPO_ROOT / "scripts" / "propose_futures_hypotheses.py"),
        "--date", date_str,
    ]
    if args.wiki_dir:
        propose_cmd += ["--wiki-dir", str(wiki_dir)]

    propose_result = _run(propose_cmd, f"Step 2: propose_futures_hypotheses --date {date_str}")
    summary["steps"]["propose"] = {
        "returncode": propose_result.returncode,
        "ok": propose_result.returncode == 0,
    }
    if propose_result.returncode != 0:
        print(f"[wiki-loop] propose_futures_hypotheses failed (rc={propose_result.returncode}). Continuing to existing hypotheses.", file=sys.stderr)

    # ── Steps 3+4: backtest then promote each hypothesis ─────────────────────
    hyp_files = _find_today_hypotheses(wiki_dir, run_date)
    summary["steps"]["hypotheses"] = {"found": len(hyp_files), "results": []}

    if not hyp_files:
        print(f"[wiki-loop] No hypotheses found for {date_str}.")
    else:
        print(f"[wiki-loop] Found {len(hyp_files)} hypothesis file(s).")

    for hyp_path in hyp_files:
        hyp_name = hyp_path.stem
        hyp_entry: dict = {"id": hyp_name}

        # Step 3: backtest
        backtest_cmd = [
            python,
            str(_REPO_ROOT / "scripts" / "backtest_futures_hypothesis.py"),
            "--hypothesis", str(hyp_path),
        ]
        if args.wiki_dir:
            backtest_cmd += ["--wiki-dir", str(wiki_dir)]

        bt_result = _run(backtest_cmd, f"  Step 3: backtest {hyp_name}")
        hyp_entry["backtest_returncode"] = bt_result.returncode
        hyp_entry["backtest_ok"] = bt_result.returncode == 0

        if bt_result.returncode != 0:
            print(f"[wiki-loop] backtest failed for {hyp_name} (rc={bt_result.returncode}). Skipping promote.", file=sys.stderr)
            hyp_entry["promote_skipped"] = True
            summary["steps"]["hypotheses"]["results"].append(hyp_entry)
            continue

        # Step 4: promote
        promote_cmd = [
            python,
            str(_REPO_ROOT / "scripts" / "promote_futures_hypothesis.py"),
            "--hypothesis", str(hyp_path),
        ]
        if args.wiki_dir:
            promote_cmd += ["--wiki-dir", str(wiki_dir)]

        promo_result = _run(promote_cmd, f"  Step 4: promote {hyp_name}")
        hyp_entry["promote_returncode"] = promo_result.returncode
        hyp_entry["promote_ok"] = promo_result.returncode == 0

        summary["steps"]["hypotheses"]["results"].append(hyp_entry)

    # ── Write summary ─────────────────────────────────────────────────────────
    summary["finished_at"] = datetime.now(_IST).isoformat()
    summary["aborted"] = False
    metrics_path = wiki_dir / "metrics" / f"{date_str}-wiki-run.json"
    _write_summary(metrics_path, summary)
    print(f"[wiki-loop] Summary written: {metrics_path}")
    print(f"[wiki-loop] Done for {date_str}.")


if __name__ == "__main__":
    main()
