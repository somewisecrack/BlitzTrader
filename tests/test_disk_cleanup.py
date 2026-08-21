from __future__ import annotations

import os
import time
from pathlib import Path

from scripts import disk_cleanup


def test_remove_stale_tmp_entries_removes_only_old_top_level_paths(tmp_path: Path):
    old_file = tmp_path / "old.txt"
    old_file.write_text("old")
    old_dir = tmp_path / "old-dir"
    old_dir.mkdir()
    (old_dir / "payload.txt").write_text("old")
    fresh_file = tmp_path / "fresh.txt"
    fresh_file.write_text("fresh")
    old_timestamp = time.time() - 8 * 24 * 60 * 60
    os.utime(old_file, (old_timestamp, old_timestamp))
    os.utime(old_dir, (old_timestamp, old_timestamp))

    removed = disk_cleanup.remove_stale_tmp_entries(tmp_path, 7)

    assert set(removed) == {old_file, old_dir}
    assert not old_file.exists()
    assert not old_dir.exists()
    assert fresh_file.exists()


def test_cleanup_skips_when_free_space_is_above_trigger(monkeypatch):
    monkeypatch.setattr(disk_cleanup, "free_mb", lambda: 4096.0)

    cleaned, free = disk_cleanup.cleanup_if_needed()

    assert cleaned is False
    assert free == 4096.0
