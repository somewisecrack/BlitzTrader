#!/usr/bin/env python3
"""
scripts/eod_backup.py
----------------------
End-of-day backup: copy today's runtime data to Google Drive via rclone,
then prune old local files to keep VM disk lean.

What is backed up to Drive each day:
  - data_exports/YYYYMMDD/  (feed ticks, indicators, strategy signals)
  - journals/YYYYMMDD.md    (trading decisions, EOD P&L)
  - logs/blitztrader_YYYYMMDD.log
  - live_state.json

What is pruned from VM after successful backup:
  - data_exports/ dirs older than LOCAL_KEEP_DAYS (default 2)
  - journal .md files older than LOCAL_KEEP_DAYS
  - log files older than LOCAL_KEEP_DAYS

Timer: blitztrader-eod-backup.timer fires at 16:00 IST Mon-Fri.
       (After market close and after wiki loop at 15:30.)

Usage:
    python3 scripts/eod_backup.py
    python3 scripts/eod_backup.py --date 2026-05-13
    python3 scripts/eod_backup.py --date 2026-05-13 --force
    python3 scripts/eod_backup.py --dry-run
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tools.market_calendar import is_nse_trading_day, get_market_holiday_name  # noqa: E402
from tools.rclone_utils import run_rclone_with_backoff  # noqa: E402

_IST = timezone(timedelta(hours=5, minutes=30))

# Local runtime dirs to keep after backup (older gets pruned)
_LOCAL_KEEP_DAYS = 2

# rclone config path for blitztrader user
_RCLONE_CONF = Path("/home/blitztrader/.config/rclone/rclone.conf")


def _today_ist() -> date:
    return datetime.now(_IST).date()


def _now_ist() -> str:
    return datetime.now(_IST).isoformat(timespec="seconds")


def _run_rclone(args: list[str], dry_run: bool = False) -> subprocess.CompletedProcess:
    """Run rclone with throttling and bounded quota-aware retry."""
    if dry_run:
        print(f"  [dry-run] rclone {' '.join(args)}")

    def _report_retry(attempt: int, delay: int, error: str) -> None:
        tail = error.splitlines()[-1] if error else "Drive quota error"
        print(
            f"    Temporary Drive quota limit: {tail}. "
            f"Retrying in {delay}s (attempt {attempt + 1}/4).",
            file=sys.stderr,
        )

    result = run_rclone_with_backoff(
        args,
        config_path=_RCLONE_CONF,
        dry_run=dry_run,
        on_retry=_report_retry,
    )
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            print(f"    {line}")
    if result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            print(f"    {line}", file=sys.stderr)
    return result


def backup_dir(src: Path, remote: str, folder: str, rel_dest: str,
               dry_run: bool) -> bool:
    """Copy a local directory to remote:folder/rel_dest. Returns True on success."""
    if not src.exists() or not any(src.iterdir()):
        print(f"  SKIP (empty or missing): {src}")
        return True  # nothing to back up is not a failure
    dest = f"{remote}:{folder}/{rel_dest}"
    print(f"  {src} → {dest}")
    result = _run_rclone(["copy", str(src), dest], dry_run)
    if result.returncode != 0:
        print(f"  ERROR: rclone copy failed (rc={result.returncode})", file=sys.stderr)
        return False
    return True


def backup_file(src: Path, remote: str, folder: str, rel_dest: str,
                dry_run: bool) -> bool:
    """Copy a single local file to remote:folder/rel_dest. Returns True on success."""
    if not src.exists():
        print(f"  SKIP (missing): {src}")
        return True
    dest = f"{remote}:{folder}/{rel_dest}"
    print(f"  {src} → {dest}")
    result = _run_rclone(["copyto", str(src), dest], dry_run)
    if result.returncode != 0:
        print(f"  ERROR: rclone copyto failed (rc={result.returncode})", file=sys.stderr)
        return False
    return True


def prune_old_local(runtime_dir: Path, keep_days: int, run_date: date,
                    dry_run: bool) -> None:
    """Remove local runtime files older than keep_days."""
    cutoff = run_date - timedelta(days=keep_days)

    # data_exports/YYYYMMDD dirs
    exports_dir = runtime_dir / "data_exports"
    if exports_dir.exists():
        for day_dir in sorted(exports_dir.iterdir()):
            if not day_dir.is_dir():
                continue
            try:
                dir_date = date(int(day_dir.name[:4]), int(day_dir.name[4:6]), int(day_dir.name[6:8]))
            except (ValueError, IndexError):
                continue
            if dir_date < cutoff:
                print(f"  Pruning: {day_dir}")
                if not dry_run:
                    shutil.rmtree(day_dir)

    # journals/YYYYMMDD*.md files
    journals_dir = runtime_dir / "journals"
    if journals_dir.exists():
        for f in sorted(journals_dir.glob("*.md")):
            try:
                file_date = date(int(f.stem[:4]), int(f.stem[4:6]), int(f.stem[6:8]))
            except (ValueError, IndexError):
                continue
            if file_date < cutoff:
                print(f"  Pruning: {f}")
                if not dry_run:
                    f.unlink()

    # logs/blitztrader_YYYYMMDD*.log and .log.gz
    logs_dir = runtime_dir / "logs"
    if logs_dir.exists():
        for f in sorted(logs_dir.glob("blitztrader_*.log*")):
            stem = f.name.replace(".log.gz", "").replace(".log", "")
            date_part = stem.replace("blitztrader_", "")
            try:
                file_date = date(int(date_part[:4]), int(date_part[4:6]), int(date_part[6:8]))
            except (ValueError, IndexError):
                continue
            if file_date < cutoff:
                print(f"  Pruning: {f}")
                if not dry_run:
                    f.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BlitzTrader EOD backup to Google Drive"
    )
    parser.add_argument("--date", default=None,
                        help="Date YYYY-MM-DD (default: today IST)")
    parser.add_argument("--runtime-root", default=None,
                        help="Runtime storage dir (default: RUNTIME_STORAGE_DIR env or /opt/blitztrader)")
    parser.add_argument("--remote", default=None,
                        help="rclone remote name (default: RCLONE_REMOTE env or 'gdrive')")
    parser.add_argument("--folder", default=None,
                        help="Drive folder name (default: RCLONE_FOLDER env or 'BlitzTrader')")
    parser.add_argument("--keep-days", type=int, default=_LOCAL_KEEP_DAYS,
                        help=f"Days of local data to keep after backup (default: {_LOCAL_KEEP_DAYS})")
    parser.add_argument("--force", action="store_true",
                        help="Run even on non-trading days")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be done without copying or deleting")
    args = parser.parse_args()

    run_date = date.fromisoformat(args.date) if args.date else _today_ist()
    date_str = run_date.strftime("%Y%m%d")

    runtime_dir = Path(
        args.runtime_root or os.environ.get("RUNTIME_STORAGE_DIR", "/opt/blitztrader")
    ).expanduser().resolve()

    remote = args.remote or os.environ.get("RCLONE_REMOTE", "gdrive")
    folder = args.folder or os.environ.get("RCLONE_FOLDER", "BlitzTrader")

    dry_tag = " [DRY RUN]" if args.dry_run else ""
    print(f"[eod-backup]{dry_tag} Date: {run_date.isoformat()}")
    print(f"[eod-backup] Runtime: {runtime_dir}")
    print(f"[eod-backup] Destination: {remote}:{folder}")

    # ── Trading day guard ─────────────────────────────────────────────────────
    if not args.force:
        if not is_nse_trading_day(run_date):
            holiday = get_market_holiday_name(run_date)
            reason = f"holiday: {holiday}" if holiday else "weekend"
            print(f"[eod-backup] {run_date} is not a trading day ({reason}). Skipping.")
            sys.exit(0)

    summary: dict = {
        "date": run_date.isoformat(),
        "started_at": _now_ist(),
        "remote": f"{remote}:{folder}",
        "backed_up": [],
        "skipped": [],
        "errors": [],
    }

    # ── Backup ────────────────────────────────────────────────────────────────
    print(f"\n[eod-backup] Backing up to Drive...")

    tasks = [
        # (description, fn, src, rel_dest)
        ("data_exports",      "dir",  runtime_dir / "data_exports" / date_str,          f"data_exports/{date_str}"),
        ("journal",           "file", runtime_dir / "journals" / f"{date_str}.md",       f"journals/{date_str}.md"),
        ("log",               "file", runtime_dir / "logs" / f"blitztrader_{date_str}.log", f"logs/blitztrader_{date_str}.log"),
        ("live_state",        "file", runtime_dir / "live_state.json",                   "live_state.json"),
    ]

    all_ok = True
    for desc, kind, src, rel_dest in tasks:
        print(f"  [{desc}]")
        if kind == "dir":
            ok = backup_dir(src, remote, folder, rel_dest, args.dry_run)
        else:
            ok = backup_file(src, remote, folder, rel_dest, args.dry_run)

        if ok:
            if src.exists():
                summary["backed_up"].append(rel_dest)
            else:
                summary["skipped"].append(rel_dest)
        else:
            summary["errors"].append(rel_dest)
            all_ok = False

    # ── Prune old local data ──────────────────────────────────────────────────
    if all_ok:
        print(f"\n[eod-backup] Pruning local data older than {args.keep_days} day(s)...")
        prune_old_local(runtime_dir, args.keep_days, run_date, args.dry_run)
    else:
        print(
            "\n[eod-backup] WARNING: some backups failed — skipping local prune to avoid data loss.",
            file=sys.stderr,
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    summary["finished_at"] = _now_ist()
    summary["ok"] = all_ok

    metrics_dir = _REPO_ROOT / "wiki" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    summary_path = metrics_dir / f"{run_date.isoformat()}-backup.json"
    if not args.dry_run:
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\n[eod-backup] {'OK' if all_ok else 'FAILED'}")
    print(f"  Backed up : {summary['backed_up']}")
    if summary["skipped"]:
        print(f"  Skipped   : {summary['skipped']}")
    if summary["errors"]:
        print(f"  Errors    : {summary['errors']}", file=sys.stderr)
    if not args.dry_run:
        print(f"  Summary   : {summary_path}")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
