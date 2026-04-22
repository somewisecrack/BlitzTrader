"""
Systemd ExecCondition helper for BlitzTrader.

Exit 0 on NSE trading days. Exit 1 on weekends/known NSE holidays so systemd
skips ExecStart without marking the service as failed.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.market_calendar import get_market_holiday_name, is_nse_trading_day  # noqa: E402


def _parse_date_arg(argv: list[str]) -> date:
    if len(argv) >= 3 and argv[1] == "--date":
        return datetime.strptime(argv[2], "%Y-%m-%d").date()
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv
    day = _parse_date_arg(args)
    if is_nse_trading_day(day):
        print(f"NSE trading day: {day.isoformat()}")
        return 0

    reason = get_market_holiday_name(day) or "weekend"
    print(f"NSE market closed: {day.isoformat()} ({reason})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
