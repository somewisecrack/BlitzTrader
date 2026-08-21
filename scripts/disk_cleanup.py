"""Safely reclaim VM disk space before a BlitzTrader session starts.

This script deliberately leaves all BlitzTrader positions, ledgers, runtime
state, journals, and repository files alone. It only trims system journals,
APT caches/indexes, and stale top-level /tmp artifacts when free space falls
below the configured trigger.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import time
from pathlib import Path

from config import (
    DISK_CLEANUP_JOURNAL_MAX_MB,
    DISK_CLEANUP_TMP_MAX_AGE_DAYS,
    DISK_CLEANUP_TRIGGER_MB,
    RUNTIME_STORAGE_DIR,
)

logger = logging.getLogger("BlitzTrader.DiskCleanup")


def free_mb(path: Path = RUNTIME_STORAGE_DIR) -> float:
    return shutil.disk_usage(path).free / (1024 * 1024)


def remove_stale_tmp_entries(
    tmp_dir: Path,
    max_age_days: int,
    *,
    dry_run: bool = False,
) -> list[Path]:
    """Remove only stale top-level /tmp entries, never their newer siblings."""
    cutoff = time.time() - max_age_days * 24 * 60 * 60
    removed: list[Path] = []
    if not tmp_dir.exists():
        return removed

    for entry in tmp_dir.iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            removed.append(entry)
            if dry_run:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Could not remove stale temporary entry: %s", entry, exc_info=True)
    return removed


def _run(command: list[str], *, dry_run: bool) -> None:
    logger.info("Disk cleanup command: %s", " ".join(command))
    if not dry_run:
        subprocess.run(command, check=False)


def cleanup_if_needed(*, dry_run: bool = False) -> tuple[bool, float]:
    before = free_mb()
    if before >= DISK_CLEANUP_TRIGGER_MB:
        logger.info(
            "Disk cleanup not needed: %.0f MB free (trigger: %d MB)",
            before,
            DISK_CLEANUP_TRIGGER_MB,
        )
        return False, before

    logger.warning(
        "Disk cleanup triggered: %.0f MB free (trigger: %d MB)",
        before,
        DISK_CLEANUP_TRIGGER_MB,
    )
    _run(["journalctl", f"--vacuum-size={DISK_CLEANUP_JOURNAL_MAX_MB}M"], dry_run=dry_run)
    _run(["apt-get", "clean"], dry_run=dry_run)

    apt_lists = Path("/var/lib/apt/lists")
    if apt_lists.exists():
        for entry in apt_lists.iterdir():
            if dry_run:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)

    removed = remove_stale_tmp_entries(
        Path("/tmp"), DISK_CLEANUP_TMP_MAX_AGE_DAYS, dry_run=dry_run
    )
    after = free_mb()
    logger.info(
        "Disk cleanup complete: %.0f MB -> %.0f MB free; removed %d stale /tmp entries",
        before,
        after,
        len(removed),
    )
    return True, after


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cleanup_if_needed(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
