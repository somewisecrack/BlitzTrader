"""
scripts/is_gammablast_day.py — systemd ExecCondition helper for GammaBlast.

Exits 0 on NIFTY-expiry Tuesdays and SENSEX-expiry Thursdays (exchange open).
Exits 1 on all other days — systemd skips ExecStart without marking failure.

Usage (ExecCondition in gammablast.service):
    ExecCondition=/opt/gammablast/venv/bin/python scripts/is_gammablast_day.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.expiry_calendar import active_symbol_for_day, holiday_name  # noqa: E402


def _parse_date_arg(argv: list[str]) -> date:
    if len(argv) >= 3 and argv[1] == "--date":
        return datetime.strptime(argv[2], "%Y-%m-%d").date()
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv
    day = _parse_date_arg(args)
    symbol = active_symbol_for_day(day)
    if symbol:
        print(f"GammaBlast day: {day.isoformat()} ({symbol} expiry)")
        return 0
    hol = holiday_name(day)
    reason = hol if hol else f"weekday={day.strftime('%A')}, not NIFTY-Tuesday or SENSEX-Thursday"
    print(f"GammaBlast skip: {day.isoformat()} ({reason})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
