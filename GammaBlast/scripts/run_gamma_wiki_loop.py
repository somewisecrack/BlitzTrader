#!/usr/bin/env python3
"""
scripts/run_gamma_wiki_loop.py — GammaBlast post-market wiki improvement loop.

Orchestrates:
  1. evaluate_gamma_day      — build daily review from ladder + audit data
  2. propose_gamma_hypotheses — generate rule change proposals via Gemini
  3. backtest_gamma_hypothesis — replay proposed changes against today's data
  4. promote_gamma_hypothesis  — promote improvements that pass the gate

Runs after market close (timer fires at 15:30 IST). Gemini calls happen
only in this post-market script, never in the live scanning path.

Usage:
    python3 scripts/run_gamma_wiki_loop.py
    python3 scripts/run_gamma_wiki_loop.py --date 2026-06-10 --skip-gemini
"""
from __future__ import annotations

import sys
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from tools.expiry_calendar import is_gammablast_day  # noqa: E402

_IST = timezone(timedelta(hours=5, minutes=30))
_PYTHON = str(_ROOT / "venv" / "bin" / "python") if (_ROOT / "venv").exists() else sys.executable


def _run(script: str, extra_args: list[str] = ()) -> int:
    cmd = [_PYTHON, str(_ROOT / "scripts" / script)] + list(extra_args)
    print(f"\n>>> {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=_ROOT)
    return result.returncode


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--skip-gemini", action="store_true", help="Skip hypothesis generation")
    parser.add_argument("--force", action="store_true", help="Run even on non-GammaBlast days")
    args = parser.parse_args()

    target: date
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = datetime.now(_IST).date()

    if not args.force and not is_gammablast_day(target):
        print(f"Not a GammaBlast day ({target}). Use --force to override.")
        return 0

    date_arg = ["--date", target.strftime("%Y-%m-%d")]
    print(f"=== GammaBlast wiki loop — {target} ===")

    # Step 1: daily review
    rc = _run("evaluate_gamma_day.py", date_arg)
    if rc != 0:
        print(f"evaluate_gamma_day failed (rc={rc})")

    # Step 2: hypothesis generation (may call Gemini)
    if not args.skip_gemini:
        rc = _run("propose_gamma_hypotheses.py", date_arg)
        if rc != 0:
            print(f"propose_gamma_hypotheses failed (rc={rc}) — continuing")
    else:
        print("Skipping Gemini hypothesis generation (--skip-gemini)")

    # Step 3: backtest all PROPOSED hypotheses for today
    hyp_dir = _ROOT / "wiki" / "hypotheses"
    ds = target.strftime("%Y%m%d")
    backtest_failures = 0
    for hyp_file in sorted(hyp_dir.glob(f"{ds}_*.json")):
        import json
        hyp = json.loads(hyp_file.read_text())
        if hyp.get("status") == "PROPOSED":
            rc = _run("backtest_gamma_hypothesis.py", [str(hyp_file), "--date", str(target)])
            if rc != 0:
                print(f"backtest failed for {hyp_file.name}")
                backtest_failures += 1

    # Step 4: promote hypotheses that pass the gate
    promotions = 0
    for hyp_file in sorted(hyp_dir.glob(f"{ds}_*.json")):
        import json
        hyp = json.loads(hyp_file.read_text())
        if hyp.get("status") == "BACKTESTED":
            br = hyp.get("backtest_result") or {}
            if br.get("verdict") == "IMPROVE":
                rc = _run("promote_gamma_hypothesis.py", [str(hyp_file)])
                if rc == 0:
                    promotions += 1

    print(f"\n=== Wiki loop complete — {target} ===")
    print(f"  Backtest failures: {backtest_failures}")
    print(f"  Rules promoted:    {promotions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
