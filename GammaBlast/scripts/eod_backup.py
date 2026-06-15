#!/usr/bin/env python3
"""
scripts/eod_backup.py — GammaBlast end-of-day backup to Google Drive via rclone.

Backs up today's:
  - data_exports/YYYYMMDD/       (gamma ladder JSONL)
  - journals/YYYYMMDD.md
  - logs/gammablast_YYYYMMDD.log
  - live_state.json
  - candidate_signals/YYYYMMDD.jsonl
  - wiki/daily_reviews/YYYYMMDD.md (if exists)

After successful backup, prunes local files older than LOCAL_KEEP_DAYS.
Does NOT write to BlitzTrader's Drive folder.

Usage:
    python3 scripts/eod_backup.py
    python3 scripts/eod_backup.py --date 2026-06-10
    python3 scripts/eod_backup.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
from tools.rclone_utils import run_rclone_with_backoff  # noqa: E402
from tools.expiry_calendar import is_gammablast_day  # noqa: E402

_IST = timezone(timedelta(hours=5, minutes=30))
_LOCAL_KEEP_DAYS = 2
_RCLONE_CONF = Path("/home/gammablast/.config/rclone/rclone.conf")


def _today() -> date:
    return datetime.now(_IST).date()


def _now_str() -> str:
    return datetime.now(_IST).isoformat(timespec="seconds")


def _run_rclone(args: list[str], dry_run: bool = False):
    def _retry_cb(attempt, delay, error):
        tail = error.splitlines()[-1] if error else "Drive quota"
        print(f"  Retry {attempt}/4 in {delay}s: {tail}", file=sys.stderr)

    result = run_rclone_with_backoff(
        args,
        config_path=_RCLONE_CONF if _RCLONE_CONF.exists() else None,
        dry_run=dry_run,
        on_retry=_retry_cb,
    )
    for line in (result.stdout or "").strip().splitlines():
        print(f"    {line}")
    for line in (result.stderr or "").strip().splitlines():
        print(f"    ERR: {line}", file=sys.stderr)
    return result


def backup_item(src: Path, remote: str, folder: str, rel: str, dry_run: bool) -> bool:
    if not src.exists():
        print(f"  SKIP (missing): {src}")
        return True
    dest = f"{remote}:{folder}/{rel}"
    print(f"  {src.name} → {dest}")
    verb = "copyto" if src.is_file() else "copy"
    r = _run_rclone([verb, str(src), dest], dry_run)
    if r.returncode != 0:
        print(f"  ERROR rc={r.returncode}", file=sys.stderr)
        return False
    return True


def prune_old(base: Path, pattern: str, keep_days: int, dry_run: bool):
    cutoff = _today() - timedelta(days=keep_days)
    for p in sorted(base.glob(pattern)):
        try:
            stem = p.stem if p.is_file() else p.name
            d = datetime.strptime(stem[:8], "%Y%m%d").date()
            if d < cutoff:
                if dry_run:
                    print(f"  [dry-run] would prune {p}")
                else:
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                    print(f"  pruned: {p}")
        except (ValueError, OSError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Override date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Run even on non-GammaBlast days")
    args = parser.parse_args()

    target_date = (datetime.strptime(args.date, "%Y-%m-%d").date()
                   if args.date else _today())

    if not args.force and not is_gammablast_day(target_date):
        print(f"Not a GammaBlast day ({target_date}). Use --force to override.")
        return 0

    ds = target_date.strftime("%Y%m%d")
    remote = config.RCLONE_REMOTE
    folder = config.RCLONE_FOLDER  # "GammaBlast" — NOT BlitzTrader
    base = config.RUNTIME_STORAGE_DIR

    print(f"GammaBlast EOD backup — {_now_str()}")
    print(f"  Date: {target_date}  remote: {remote}:{folder}  dry_run={args.dry_run}")

    ok = True
    ok &= backup_item(base / "data_exports" / ds, remote, folder, f"data_exports/{ds}", args.dry_run)
    ok &= backup_item(base / "journals" / f"{ds}.md", remote, folder, f"journals/{ds}.md", args.dry_run)
    ok &= backup_item(base / "logs" / f"gammablast_{ds}.log", remote, folder, f"logs/gammablast_{ds}.log", args.dry_run)
    ok &= backup_item(base / "live_state.json", remote, folder, f"state/{ds}_live_state.json", args.dry_run)
    ok &= backup_item(base / "candidate_signals" / f"{ds}.jsonl", remote, folder, f"candidate_signals/{ds}.jsonl", args.dry_run)

    wiki_review = _ROOT / "wiki" / "daily_reviews" / f"{ds}.md"
    if wiki_review.exists():
        ok &= backup_item(wiki_review, remote, folder, f"wiki/daily_reviews/{ds}.md", args.dry_run)

    if ok and not args.dry_run:
        print("Pruning old local files...")
        prune_old(base / "data_exports", "*", _LOCAL_KEEP_DAYS, args.dry_run)
        prune_old(base / "journals", "*.md", _LOCAL_KEEP_DAYS, args.dry_run)
        prune_old(base / "logs", "*.log", _LOCAL_KEEP_DAYS, args.dry_run)
        prune_old(base / "candidate_signals", "*.jsonl", _LOCAL_KEEP_DAYS, args.dry_run)

    status = "SUCCESS" if ok else "PARTIAL/FAILED"
    print(f"Backup {status} — {_now_str()}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
