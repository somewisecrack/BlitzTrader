#!/usr/bin/env python3
"""
scripts/diagnose_spread_pnl_day.py

Reads a day's candidate_signals audit files and reconstructs spread P&L events.
Reports: entries, fills, close prices, realized P&L, whether any P&L violated
defined-risk bounds.

Usage:
    python3 scripts/diagnose_spread_pnl_day.py 20260603
    python3 scripts/diagnose_spread_pnl_day.py          # defaults to today
"""
import sys
import json
import os
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent.parent
SIGNALS_DIR = BASE_DIR / "runtime" / "candidate_signals"


def _fmt(v):
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def main(date_str: str | None = None):
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")

    print(f"\n{'='*70}")
    print(f"  Spread P&L Diagnostic — {date_str}")
    print(f"{'='*70}\n")

    # Find matching signal files for the date
    files = sorted(SIGNALS_DIR.glob(f"{date_str}*.json")) if SIGNALS_DIR.exists() else []
    if not files:
        print(f"No candidate_signal files found for {date_str} in {SIGNALS_DIR}")
        return

    all_events: list[dict] = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            if isinstance(data, list):
                all_events.extend(data)
            elif isinstance(data, dict):
                all_events.append(data)
        except Exception as e:
            print(f"  [WARN] Could not read {f.name}: {e}")

    print(f"Loaded {len(all_events)} audit events from {len(files)} file(s)\n")

    # Group by signal_id
    by_signal: dict[str, list] = {}
    for ev in all_events:
        sid = ev.get("signal_id", "UNKNOWN")
        by_signal.setdefault(sid, []).append(ev)

    spreads_opened = 0
    spreads_closed = 0
    total_realized = 0.0
    invalid_pnl_count = 0

    for sid, events in by_signal.items():
        stages = {ev["stage"]: ev for ev in events}

        placed = stages.get("SPREAD_ORDER_PLACED")
        exited = stages.get("SPREAD_EXITED")
        if not placed:
            continue

        spreads_opened += 1
        d = placed.get("details", {})
        spread_id  = d.get("spread_id", sid)
        long_tsym  = d.get("long_tsym",  "?")
        short_tsym = d.get("short_tsym", "?")
        long_fill  = d.get("long_fill",  0)
        short_fill = d.get("short_fill", 0)

        print(f"  [{spread_id}]")
        print(f"    Signal  : {placed.get('signal', {}).get('symbol','?')} "
              f"{placed.get('signal', {}).get('strategy','?')}")
        print(f"    Long    : {long_tsym} @ ₹{_fmt(long_fill)}")
        print(f"    Short   : {short_tsym} @ ₹{_fmt(short_fill)}")

        if exited:
            spreads_closed += 1
            ed = exited.get("details", {})
            realized  = ed.get("realized_pnl")
            max_profit = ed.get("max_profit")
            max_loss   = ed.get("max_loss")
            lc = ed.get("long_close")
            sc = ed.get("short_close")
            reason    = ed.get("reason", "?")

            print(f"    Closed  : reason={reason}")
            if lc is not None:
                print(f"    Long close  : ₹{_fmt(lc)}")
            if sc is not None:
                print(f"    Short close : ₹{_fmt(sc)}")
            if realized is not None:
                print(f"    Realized P&L: ₹{_fmt(realized)}")
                total_realized += float(realized)

            # Check defined-risk violation
            if realized is not None and max_profit is not None and max_loss is not None:
                tolerance = (float(max_profit) + float(max_loss)) * 0.05
                if float(realized) > float(max_profit) + tolerance:
                    invalid_pnl_count += 1
                    print(f"    *** INVALID: realized ₹{_fmt(realized)} > max_profit "
                          f"₹{_fmt(max_profit)} + tolerance ₹{_fmt(tolerance)} ***")
                elif float(realized) < -(float(max_loss) + tolerance):
                    invalid_pnl_count += 1
                    print(f"    *** INVALID: realized ₹{_fmt(realized)} < -max_loss "
                          f"-₹{_fmt(max_loss)} - tolerance ₹{_fmt(tolerance)} ***")
                else:
                    print(f"    [OK] P&L within bounds "
                          f"[-₹{_fmt(max_loss + tolerance)}, ₹{_fmt(max_profit + tolerance)}]")
        else:
            print(f"    Status  : not yet closed (still open?)")

        print()

    print(f"{'─'*70}")
    print(f"  Spreads opened  : {spreads_opened}")
    print(f"  Spreads closed  : {spreads_closed}")
    print(f"  Total realized  : ₹{total_realized:+,.2f}")
    print(f"  Invalid P&L incidents: {invalid_pnl_count}")
    if invalid_pnl_count:
        print(f"\n  *** {invalid_pnl_count} IMPOSSIBLE P&L VIOLATION(S) DETECTED ***")
        print(f"  These would have been blocked by the new validation guards.")
    else:
        print(f"\n  All P&L values within defined-risk bounds.")
    print()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    main(arg)
