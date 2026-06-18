#!/usr/bin/env python3
"""Scan per-strike OHLCV jsonl files for gamma-blast moves.

A row is one ~1-minute snapshot. Filters out corrupt rows where the
underlying quote was merged in (volume is null). For each strike,
computes rolling LTP gains over 5/15/30-minute windows and prints the
strongest expansion episodes.
"""
import json
import sys
from pathlib import Path
from datetime import datetime

RAW = Path(__file__).parent / "raw"


def load(path: Path):
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("volume") is None or r.get("ltp") is None:
            continue  # corrupt/underlying-merged row
        r["ts"] = datetime.fromisoformat(r["timestamp_ist"])
        rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def best_window(rows, minutes):
    """Max LTP multiple low->high within forward window of `minutes`."""
    best = None
    n = len(rows)
    for i in range(n):
        t0 = rows[i]["ts"]
        base = rows[i]["ltp"]
        if base is None or base <= 0:
            continue
        peak = base
        peak_j = i
        for j in range(i + 1, n):
            if (rows[j]["ts"] - t0).total_seconds() > minutes * 60:
                break
            if rows[j]["ltp"] and rows[j]["ltp"] > peak:
                peak = rows[j]["ltp"]
                peak_j = j
        mult = peak / base
        if best is None or mult > best[0]:
            best = (mult, rows[i], rows[peak_j])
    return best


def main():
    files = sorted(RAW.glob("SENSEX2*_ohlcv.jsonl"))
    print(f"{'strike':22s} {'rows':>5s} {'day_rng(ltp)':>16s} "
          f"{'x5m':>6s} {'x15m':>7s} {'x30m':>7s}  blast15 window")
    for f in files:
        rows = load(f)
        if not rows:
            print(f"{f.stem:22s} EMPTY")
            continue
        name = f.stem.replace("_ohlcv", "")
        ltps = [r["ltp"] for r in rows if r["ltp"]]
        b5 = best_window(rows, 5)
        b15 = best_window(rows, 15)
        b30 = best_window(rows, 30)
        w = ""
        if b15:
            w = (f"{b15[1]['ts'].strftime('%H:%M')}→{b15[2]['ts'].strftime('%H:%M')} "
                 f"{b15[1]['ltp']:.2f}→{b15[2]['ltp']:.2f}")
        print(f"{name:22s} {len(rows):5d} {min(ltps):7.2f}-{max(ltps):<8.2f} "
              f"{b5[0] if b5 else 0:6.2f} {b15[0] if b15 else 0:7.2f} "
              f"{b30[0] if b30 else 0:7.2f}  {w}")


if __name__ == "__main__":
    main()
