#!/usr/bin/env python3
"""
Diagnose one day's option-spread P&L events from BlitzTrader audit/log files.

Usage:
    python3 scripts/diagnose_spread_pnl_day.py 20260603
    python3 scripts/diagnose_spread_pnl_day.py

The script is read-only. It exists to catch impossible defined-risk spread P&L,
such as an option leg being priced with a NIFTY/BANKNIFTY underlying value.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
PNL_ROUNDING_TOLERANCE_RS = 25.0


@dataclass
class SpreadDiag:
    spread_id: str
    symbol: str = ""
    strategy: str = ""
    direction: str = ""
    long_tsym: str = ""
    short_tsym: str = ""
    long_fill: float | None = None
    short_fill: float | None = None
    long_close: float | None = None
    short_close: float | None = None
    realized_pnl: float | None = None
    max_profit: float | None = None
    max_loss: float | None = None
    exit_reason: str = ""
    opened_from: set[str] = field(default_factory=set)
    exited_from: set[str] = field(default_factory=set)

    def invalid_reason(self) -> str:
        if self.realized_pnl is None:
            return ""
        upper = (
            self.max_profit + PNL_ROUNDING_TOLERANCE_RS
            if self.max_profit is not None
            else None
        )
        lower = (
            -(self.max_loss + PNL_ROUNDING_TOLERANCE_RS)
            if self.max_loss is not None
            else None
        )
        if upper is not None and self.realized_pnl > upper:
            return (
                f"realized {money(self.realized_pnl)} > max_profit+tolerance "
                f"{money(upper)}"
            )
        if lower is not None and self.realized_pnl < lower:
            return (
                f"realized {money(self.realized_pnl)} < -max_loss-tolerance "
                f"{money(lower)}"
            )
        return ""


def money(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return f"Rs {float(value):+,.2f}"
    except Exception:
        return str(value)


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def iter_audit_events(date_str: str) -> list[dict]:
    dirs = [
        BASE_DIR / "candidate_signals",
        BASE_DIR / "runtime" / "candidate_signals",
    ]
    files: list[Path] = []
    for directory in dirs:
        if directory.exists():
            files.extend(sorted(directory.glob(f"{date_str}*.jsonl")))
            files.extend(sorted(directory.glob(f"{date_str}*.json")))

    events: list[dict] = []
    for path in files:
        text = path.read_text(errors="replace")
        if path.suffix == ".jsonl":
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    event["_source_file"] = str(path)
                    events.append(event)
                except json.JSONDecodeError as exc:
                    print(f"WARN: could not parse JSONL line in {path}: {exc}")
        else:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                print(f"WARN: could not parse JSON file {path}: {exc}")
                continue
            rows = payload if isinstance(payload, list) else [payload]
            for event in rows:
                if isinstance(event, dict):
                    event["_source_file"] = str(path)
                    events.append(event)
    return events


def iter_log_lines(date_str: str) -> list[str]:
    paths = [
        BASE_DIR / "logs" / f"blitztrader_{date_str}.log",
        BASE_DIR / "runtime" / "logs" / f"blitztrader_{date_str}.log",
        Path("/tmp") / f"blitz_{date_str}.log",
    ]
    lines: list[str] = []
    for path in paths:
        if path.exists():
            lines.extend(path.read_text(errors="replace").splitlines())
    return lines


def get_spread(spreads: dict[str, SpreadDiag], spread_id: str) -> SpreadDiag:
    if spread_id not in spreads:
        spreads[spread_id] = SpreadDiag(spread_id=spread_id)
    return spreads[spread_id]


def merge_audit(spreads: dict[str, SpreadDiag], events: list[dict]) -> None:
    for event in events:
        stage = event.get("stage")
        details = event.get("details") or {}
        if stage == "SPREAD_ORDER_PLACED":
            spread_id = details.get("spread_id")
            if not spread_id:
                continue
            diag = get_spread(spreads, spread_id)
            diag.symbol = event.get("symbol") or diag.symbol
            diag.strategy = event.get("strategy") or diag.strategy
            diag.direction = event.get("direction") or diag.direction
            diag.long_tsym = details.get("long_tsym") or diag.long_tsym
            diag.short_tsym = details.get("short_tsym") or diag.short_tsym
            diag.long_fill = as_float(details.get("long_fill")) or diag.long_fill
            diag.short_fill = as_float(details.get("short_fill")) or diag.short_fill
            diag.opened_from.add("audit")
        elif stage in {"SPREAD_EXITED", "SPREAD_EXIT_FAILED"}:
            spread_id = event.get("signal_id")
            if not spread_id:
                continue
            diag = get_spread(spreads, spread_id)
            diag.exit_reason = event.get("reason") or diag.exit_reason
            diag.realized_pnl = (
                as_float(details.get("realized_pnl"))
                if details.get("realized_pnl") is not None
                else diag.realized_pnl
            )
            diag.exited_from.add("audit")


OPEN_RE = re.compile(
    r"SpreadExecution\[(SPR-[^\]]+)\]: VIRTUAL\s+(\w+)\s+(\w+)\s+(\w+)"
    r".*long=([^@\s]+)@([0-9.,]+)\s+short=([^@\s]+)@([0-9.,]+)"
)
CLOSE_START_RE = re.compile(
    r"close_spread\[(SPR-[^\]]+)\].*closed \((.*?)\):|"
    r"close_spread\[(SPR-[^\]]+)\]: closing .* reason='(.*?)'"
)
MAX_PROFIT_RE = re.compile(r"max_profit .*?([0-9][0-9.,]*)")
MAX_LOSS_RE = re.compile(r"max_loss .*?([0-9][0-9.,]*)")
SHORT_CLOSE_RE = re.compile(r"Short closed:\s+([^@\s]+)\s+@\s+Rs?\s*([+-]?[0-9.,]+)|Short closed:\s+([^@\s]+)\s+@\s+₹([+-]?[0-9.,]+)")
LONG_CLOSE_RE = re.compile(r"Long\s+closed:\s+([^@\s]+)\s+@\s+Rs?\s*([+-]?[0-9.,]+)|Long\s+closed:\s+([^@\s]+)\s+@\s+₹([+-]?[0-9.,]+)")
REALIZED_RE = re.compile(r"Realized P&L:\s+Rs?\s*([+-]?[0-9.,]+)|Realized P&L:\s+₹([+-]?[0-9.,]+)")


def first_match_float(match: re.Match[str]) -> float | None:
    for group in match.groups():
        value = as_float(group)
        if value is not None:
            return value
    return None


def merge_logs(spreads: dict[str, SpreadDiag], lines: list[str]) -> None:
    current_close_id: str | None = None
    for line in lines:
        open_match = OPEN_RE.search(line)
        if open_match:
            spread_id, symbol, spread_type, direction, long_tsym, long_fill, short_tsym, short_fill = open_match.groups()
            diag = get_spread(spreads, spread_id)
            diag.symbol = diag.symbol or symbol
            diag.direction = diag.direction or direction
            diag.long_tsym = diag.long_tsym or long_tsym
            diag.short_tsym = diag.short_tsym or short_tsym
            diag.long_fill = as_float(long_fill) if diag.long_fill is None else diag.long_fill
            diag.short_fill = as_float(short_fill) if diag.short_fill is None else diag.short_fill
            diag.opened_from.add("log")
            continue

        close_match = CLOSE_START_RE.search(line)
        if close_match:
            spread_id = close_match.group(1) or close_match.group(3)
            reason = close_match.group(2) or close_match.group(4) or ""
            current_close_id = spread_id
            diag = get_spread(spreads, spread_id)
            diag.exit_reason = reason or diag.exit_reason
            diag.exited_from.add("log")
            mp = MAX_PROFIT_RE.search(reason)
            ml = MAX_LOSS_RE.search(reason)
            if mp:
                diag.max_profit = first_match_float(mp) or diag.max_profit
            if ml:
                diag.max_loss = first_match_float(ml) or diag.max_loss
            continue

        if current_close_id:
            diag = get_spread(spreads, current_close_id)
            short_match = SHORT_CLOSE_RE.search(line)
            if short_match:
                diag.short_close = first_match_float(short_match) or diag.short_close
                continue
            long_match = LONG_CLOSE_RE.search(line)
            if long_match:
                diag.long_close = first_match_float(long_match) or diag.long_close
                continue
            realized_match = REALIZED_RE.search(line)
            if realized_match:
                diag.realized_pnl = first_match_float(realized_match) or diag.realized_pnl
                current_close_id = None


def main(date_str: str | None = None) -> int:
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")

    spreads: dict[str, SpreadDiag] = {}
    audit_events = iter_audit_events(date_str)
    log_lines = iter_log_lines(date_str)
    merge_audit(spreads, audit_events)
    merge_logs(spreads, log_lines)

    print(f"Spread P&L Diagnostic - {date_str}")
    print("=" * 72)
    print(f"Audit events: {len(audit_events)}")
    print(f"Log lines:    {len(log_lines)}")
    print(f"Spreads:      {len(spreads)}")
    print()

    invalid_count = 0
    total_realized = 0.0
    for spread_id in sorted(spreads):
        diag = spreads[spread_id]
        invalid = diag.invalid_reason()
        if invalid:
            invalid_count += 1
        if diag.realized_pnl is not None:
            total_realized += diag.realized_pnl

        print(f"[{spread_id}] {diag.symbol} {diag.strategy} {diag.direction}")
        print(f"  long:  {diag.long_tsym or 'N/A'} entry={money(diag.long_fill)} close={money(diag.long_close)}")
        print(f"  short: {diag.short_tsym or 'N/A'} entry={money(diag.short_fill)} close={money(diag.short_close)}")
        print(f"  pnl:   {money(diag.realized_pnl)} max_profit={money(diag.max_profit)} max_loss={money(diag.max_loss)}")
        if diag.exit_reason:
            print(f"  exit:  {diag.exit_reason}")
        print(f"  src:   opened={','.join(sorted(diag.opened_from)) or 'none'} exited={','.join(sorted(diag.exited_from)) or 'none'}")
        if invalid:
            print(f"  INVALID: {invalid}")
        print()

    print("-" * 72)
    print(f"Total realized from parsed spread exits: {money(total_realized)}")
    print(f"Invalid defined-risk P&L incidents:     {invalid_count}")
    return 1 if invalid_count else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
