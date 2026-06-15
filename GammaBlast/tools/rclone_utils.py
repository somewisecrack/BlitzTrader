"""Shared rclone execution with bounded retry for transient Drive quotas."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

logger = logging.getLogger("GammaBlast.Rclone")

_TRANSIENT_DRIVE_ERRORS = (
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quota exceeded",
    "too many requests",
    "resource_exhausted",
)


def is_transient_drive_error(result: subprocess.CompletedProcess) -> bool:
    """
    Return True if the rclone result indicates a transient Drive quota or
    rate-limit failure that is worth retrying.

    Checks both stdout and stderr (case-insensitive) for known transient
    error markers.
    """
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return any(marker in output for marker in _TRANSIENT_DRIVE_ERRORS)


def run_rclone_with_backoff(
    args: Sequence[str],
    *,
    config_path: Path | None = None,
    attempts: int = 4,
    base_delay_seconds: int = 15,
    dry_run: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, int, str], None] | None = None,
) -> subprocess.CompletedProcess:
    """
    Run rclone, retrying only on temporary Drive quota / rate-limit failures.

    Args:
        args:                Rclone sub-command and positional arguments,
                             e.g. ["copy", "/local/path", "gdrive:backup/"].
        config_path:         Optional path to a non-default rclone config file.
        attempts:            Maximum number of total attempts (default 4).
        base_delay_seconds:  Initial retry delay in seconds; doubles each attempt
                             (exponential back-off, default 15 s → 30 s → 60 s).
        dry_run:             If True, appends --dry-run to the rclone command.
        sleeper:             Callable used for sleeping between retries; injectable
                             for unit tests (default time.sleep).
        on_retry:            Optional callback invoked before each retry sleep with
                             (attempt_number, delay_seconds, error_snippet).

    Returns:
        The subprocess.CompletedProcess from the final attempt, whether it
        succeeded or not.  Callers should inspect .returncode.
    """
    cmd: list[str] = ["rclone"]
    if config_path is not None:
        cmd.extend(["--config", str(config_path)])
    cmd.extend(
        [
            "--tpslimit", "4",
            "--tpslimit-burst", "4",
            "--retries", "3",
            "--low-level-retries", "5",
            "--retries-sleep", "10s",
        ]
    )
    cmd.extend(args)
    if dry_run:
        cmd.append("--dry-run")

    result: subprocess.CompletedProcess | None = None
    for attempt in range(1, max(1, attempts) + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result
        if attempt >= attempts or not is_transient_drive_error(result):
            return result

        delay = base_delay_seconds * (2 ** (attempt - 1))
        if on_retry:
            on_retry(attempt, delay, (result.stderr or result.stdout or "").strip())
        logger.warning(
            "GammaBlast.Rclone: transient error on attempt %d/%d — retrying in %ds",
            attempt,
            attempts,
            delay,
        )
        sleeper(delay)

    assert result is not None
    return result
