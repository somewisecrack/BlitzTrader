"""BlitzTrader calendar/session guards."""
from __future__ import annotations

from datetime import date, datetime, time

import pytz

IST = pytz.timezone("Asia/Kolkata")

def is_pair_credit_opening_scan_time(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    local_now = now.astimezone(IST) if now.tzinfo else IST.localize(now)
    return time(8, 0) <= local_now.time() < time(9, 15)


def is_pair_credit_expiry_close_time(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    local_now = now.astimezone(IST) if now.tzinfo else IST.localize(now)
    return local_now.time() >= time(15, 15)
