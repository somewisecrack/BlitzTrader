"""
tests/test_futures_wiki_loop.py
Tests for scripts/run_futures_wiki_loop.py
"""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_SCRIPT = str(_REPO_ROOT / "scripts" / "run_futures_wiki_loop.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    r = subprocess.CompletedProcess(args=[], returncode=returncode)
    r.stdout = stdout
    r.stderr = stderr
    return r


def _invoke(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the wiki loop script as a subprocess."""
    import os
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, _SCRIPT] + args,
        capture_output=True,
        text=True,
        env=run_env,
    )


# ---------------------------------------------------------------------------
# Non-trading day guard
# ---------------------------------------------------------------------------

class TestNonTradingDayGuard:
    def test_weekend_exits_zero(self, tmp_path):
        """Saturday should exit 0 without running any pipeline step."""
        result = _invoke(["--date", "2026-05-09", "--wiki-dir", str(tmp_path)])
        assert result.returncode == 0
        assert "not a trading day" in result.stdout.lower()
        # No metrics file written for skipped day
        assert not (tmp_path / "metrics" / "2026-05-09-wiki-run.json").exists()

    def test_nse_holiday_exits_zero(self, tmp_path):
        """Diwali holiday should exit 0."""
        result = _invoke(["--date", "2026-11-10", "--wiki-dir", str(tmp_path)])
        assert result.returncode == 0
        assert "not a trading day" in result.stdout.lower()

    def test_force_runs_on_weekend(self, tmp_path):
        """--force bypasses the trading day check (subprocess will fail on missing evaluate, that's fine)."""
        result = _invoke(["--date", "2026-05-09", "--wiki-dir", str(tmp_path), "--force"])
        # It will fail because there's no review file, but it should NOT exit 0 with "not a trading day"
        assert "not a trading day" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# Normal orchestration (mocked subprocesses)
# ---------------------------------------------------------------------------

class TestNormalRun:
    def _patch_run(self, side_effects):
        return patch("subprocess.run", side_effect=side_effects)

    def test_evaluate_failure_aborts(self, tmp_path):
        """If evaluate_futures_day fails, pipeline aborts and summary records it."""
        side_effects = [
            _make_completed(1, stderr="evaluate error"),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            from scripts.run_futures_wiki_loop import main
            with pytest.raises(SystemExit) as exc_info:
                with patch("sys.argv", ["run_futures_wiki_loop.py",
                                        "--date", "2026-05-13",
                                        "--wiki-dir", str(tmp_path),
                                        "--force"]):
                    main()
        assert exc_info.value.code == 1
        summary_path = tmp_path / "metrics" / "2026-05-13-wiki-run.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert summary["aborted"] is True
        assert summary["steps"]["evaluate"]["ok"] is False

    def test_no_hypotheses_exits_cleanly(self, tmp_path):
        """If propose produces no hypotheses, loop exits cleanly with summary."""
        side_effects = [
            _make_completed(0, stdout="evaluate ok"),  # evaluate
            _make_completed(0, stdout="propose ok"),   # propose
        ]
        with patch("subprocess.run", side_effect=side_effects):
            with patch("sys.argv", ["run_futures_wiki_loop.py",
                                    "--date", "2026-05-13",
                                    "--wiki-dir", str(tmp_path),
                                    "--force"]):
                from scripts.run_futures_wiki_loop import main
                main()  # should not raise

        summary_path = tmp_path / "metrics" / "2026-05-13-wiki-run.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert summary["aborted"] is False
        assert summary["steps"]["hypotheses"]["found"] == 0

    def test_single_hypothesis_backtest_and_promote(self, tmp_path):
        """One hypothesis: evaluate → propose → backtest → promote all succeed."""
        hyp_dir = tmp_path / "hypotheses"
        hyp_dir.mkdir(parents=True)
        hyp_path = hyp_dir / "HYP-20260513-001.json"
        hyp_path.write_text("{}", encoding="utf-8")

        side_effects = [
            _make_completed(0, stdout="evaluate ok"),
            _make_completed(0, stdout="propose ok"),
            _make_completed(0, stdout="backtest ok"),
            _make_completed(0, stdout="promote ok"),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            with patch("sys.argv", ["run_futures_wiki_loop.py",
                                    "--date", "2026-05-13",
                                    "--wiki-dir", str(tmp_path),
                                    "--force"]):
                from scripts.run_futures_wiki_loop import main
                main()

        summary = json.loads((tmp_path / "metrics" / "2026-05-13-wiki-run.json").read_text())
        assert summary["steps"]["hypotheses"]["found"] == 1
        result = summary["steps"]["hypotheses"]["results"][0]
        assert result["backtest_ok"] is True
        assert result["promote_ok"] is True

    def test_multiple_hypotheses_all_processed(self, tmp_path):
        """Three hypotheses: each gets backtest + promote calls."""
        hyp_dir = tmp_path / "hypotheses"
        hyp_dir.mkdir(parents=True)
        for i in range(1, 4):
            (hyp_dir / f"HYP-20260513-00{i}.json").write_text("{}", encoding="utf-8")

        # evaluate, propose, then 3×(backtest + promote)
        side_effects = (
            [_make_completed(0)] * 2 +
            [_make_completed(0), _make_completed(0)] * 3
        )
        with patch("subprocess.run", side_effect=side_effects):
            with patch("sys.argv", ["run_futures_wiki_loop.py",
                                    "--date", "2026-05-13",
                                    "--wiki-dir", str(tmp_path),
                                    "--force"]):
                from scripts.run_futures_wiki_loop import main
                main()

        summary = json.loads((tmp_path / "metrics" / "2026-05-13-wiki-run.json").read_text())
        assert summary["steps"]["hypotheses"]["found"] == 3
        assert len(summary["steps"]["hypotheses"]["results"]) == 3

    def test_backtest_failure_skips_promote_continues(self, tmp_path):
        """If backtest fails for one hypothesis, promote is skipped but loop continues."""
        hyp_dir = tmp_path / "hypotheses"
        hyp_dir.mkdir(parents=True)
        for i in range(1, 3):
            (hyp_dir / f"HYP-20260513-00{i}.json").write_text("{}", encoding="utf-8")

        side_effects = [
            _make_completed(0),   # evaluate
            _make_completed(0),   # propose
            _make_completed(1),   # backtest HYP-001 FAILS
            # no promote for HYP-001
            _make_completed(0),   # backtest HYP-002 ok
            _make_completed(0),   # promote HYP-002 ok
        ]
        with patch("subprocess.run", side_effect=side_effects):
            with patch("sys.argv", ["run_futures_wiki_loop.py",
                                    "--date", "2026-05-13",
                                    "--wiki-dir", str(tmp_path),
                                    "--force"]):
                from scripts.run_futures_wiki_loop import main
                main()  # should not raise

        summary = json.loads((tmp_path / "metrics" / "2026-05-13-wiki-run.json").read_text())
        results = summary["steps"]["hypotheses"]["results"]
        assert results[0]["backtest_ok"] is False
        assert results[0].get("promote_skipped") is True
        assert results[1]["backtest_ok"] is True
        assert results[1]["promote_ok"] is True

    def test_propose_failure_continues_to_existing_hypotheses(self, tmp_path):
        """If propose fails, loop still runs backtest+promote on existing hypothesis files."""
        hyp_dir = tmp_path / "hypotheses"
        hyp_dir.mkdir(parents=True)
        (hyp_dir / "HYP-20260513-001.json").write_text("{}", encoding="utf-8")

        side_effects = [
            _make_completed(0),   # evaluate
            _make_completed(1),   # propose FAILS — should warn but continue
            _make_completed(0),   # backtest
            _make_completed(0),   # promote
        ]
        with patch("subprocess.run", side_effect=side_effects):
            with patch("sys.argv", ["run_futures_wiki_loop.py",
                                    "--date", "2026-05-13",
                                    "--wiki-dir", str(tmp_path),
                                    "--force"]):
                from scripts.run_futures_wiki_loop import main
                main()

        summary = json.loads((tmp_path / "metrics" / "2026-05-13-wiki-run.json").read_text())
        assert summary["steps"]["propose"]["ok"] is False
        assert summary["steps"]["hypotheses"]["found"] == 1
        assert summary["steps"]["hypotheses"]["results"][0]["backtest_ok"] is True

    def test_summary_file_written_on_success(self, tmp_path):
        """Summary JSON is written with correct structure and date."""
        side_effects = [
            _make_completed(0),
            _make_completed(0),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            with patch("sys.argv", ["run_futures_wiki_loop.py",
                                    "--date", "2026-05-13",
                                    "--wiki-dir", str(tmp_path),
                                    "--force"]):
                from scripts.run_futures_wiki_loop import main
                main()

        path = tmp_path / "metrics" / "2026-05-13-wiki-run.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["date"] == "2026-05-13"
        assert "started_at" in data
        assert "finished_at" in data
        assert data["aborted"] is False


# ---------------------------------------------------------------------------
# Scope / no-pairs guard
# ---------------------------------------------------------------------------

class TestScopeGuard:
    def test_script_contains_no_pairs_paths(self):
        """run_futures_wiki_loop.py must not reference pairs scripts."""
        text = (_REPO_ROOT / "scripts" / "run_futures_wiki_loop.py").read_text()
        assert "pairs" not in text.lower()
        assert "scanner" not in text.lower()

    def test_service_file_contains_no_pairs_paths(self):
        text = (_REPO_ROOT / "blitztrader-wiki-loop.service").read_text()
        assert "pairs" not in text.lower()

    def test_timer_file_contains_no_pairs_paths(self):
        text = (_REPO_ROOT / "blitztrader-wiki-loop.timer").read_text()
        assert "pairs" not in text.lower()

    def test_service_does_not_require_blitztrader_service(self):
        """Wiki loop must not couple to blitztrader.service — different lifecycles."""
        text = (_REPO_ROOT / "blitztrader-wiki-loop.service").read_text()
        assert "blitztrader.service" not in text

    def test_timer_fires_at_1530_ist(self):
        text = (_REPO_ROOT / "blitztrader-wiki-loop.timer").read_text()
        assert "15:30:00" in text
        assert "Asia/Kolkata" in text

    def test_service_uses_is_trading_day_exec_condition(self):
        text = (_REPO_ROOT / "blitztrader-wiki-loop.service").read_text()
        assert "ExecCondition" in text
        assert "is_trading_day.py" in text
