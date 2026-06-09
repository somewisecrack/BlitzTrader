import subprocess

from tools.rclone_utils import is_transient_drive_error, run_rclone_with_backoff


def _result(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["rclone"], returncode=returncode, stdout="", stderr=stderr
    )


def test_transient_drive_quota_errors_are_detected():
    assert is_transient_drive_error(
        _result(1, "googleapi: Error 403: Quota exceeded, rateLimitExceeded")
    )
    assert is_transient_drive_error(_result(1, "429 Too Many Requests"))
    assert not is_transient_drive_error(_result(1, "directory not found"))


def test_rclone_retries_transient_quota_error(monkeypatch):
    results = iter(
        [
            _result(1, "403 rateLimitExceeded"),
            _result(0),
        ]
    )
    calls = []
    sleeps = []
    monkeypatch.setattr(
        "tools.rclone_utils.subprocess.run",
        lambda cmd, **kwargs: calls.append(cmd) or next(results),
    )

    result = run_rclone_with_backoff(
        ["copy", "/tmp/source", "gdrive:dest"],
        sleeper=sleeps.append,
    )

    assert result.returncode == 0
    assert len(calls) == 2
    assert sleeps == [15]
    assert "--tpslimit" in calls[0]


def test_rclone_does_not_retry_permanent_error(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tools.rclone_utils.subprocess.run",
        lambda cmd, **kwargs: calls.append(cmd) or _result(1, "invalid token"),
    )

    result = run_rclone_with_backoff(
        ["lsf", "gdrive:"],
        sleeper=lambda _delay: None,
    )

    assert result.returncode == 1
    assert len(calls) == 1
