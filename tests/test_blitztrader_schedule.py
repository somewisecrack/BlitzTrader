from pathlib import Path

from scripts.is_trading_day import main


ROOT = Path(__file__).resolve().parents[1]


def test_monday_is_allowed():
    assert main(["is_trading_day.py", "--date", "2026-06-22"]) == 0


def test_tuesday_is_reserved_for_gammablast():
    assert main(["is_trading_day.py", "--date", "2026-06-23"]) == 1


def test_wednesday_is_allowed():
    assert main(["is_trading_day.py", "--date", "2026-06-24"]) == 0


def test_thursday_is_allowed():
    assert main(["is_trading_day.py", "--date", "2026-06-25"]) == 0


def test_friday_is_allowed():
    assert main(["is_trading_day.py", "--date", "2026-06-19"]) == 0


def test_all_blitztrader_timers_run_every_weekday():
    timer_names = (
        "blitztrader.timer",
        "blitztrader-eod-backup.timer",
    )
    for timer_name in timer_names:
        text = (ROOT / timer_name).read_text()
        assert "OnCalendar=Mon,Tue,Wed,Thu,Fri " in text


def test_wiki_loop_timer_is_deactivated_by_default():
    text = (ROOT / "blitztrader-wiki-loop.timer").read_text()
    assert "ConditionPathExists=/opt/blitztrader/ENABLE_WIKI_LOOP" in text
    assert "OnCalendar=Mon,Tue,Wed,Thu,Fri " in text
