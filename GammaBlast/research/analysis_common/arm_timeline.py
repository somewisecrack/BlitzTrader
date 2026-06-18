#!/usr/bin/env python3
"""Arming timeline: at each 5-min bucket, which strikes meet the ARM
conditions, and which of those eventually blasted (>=3x forward 15m).

Answers "how early was the candidate set clear, and how clean was it" by
printing, per bucket from 13:00, the armed set with blasters marked '*'.

Usage: arm_timeline.py RAW_DIR [RAW_DIR ...]
"""
import sys
from datetime import time
from pathlib import Path

# reuse the parsing/bucketing from trigger_scan in the same dir
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trigger_scan import parse_name, load_rows, buckets, blast_after_arm, ARM


def strike_series(path):
    parsed = parse_name(path.stem)
    if not parsed:
        return None
    name, cp, strike = parsed
    rows = load_rows(path)
    if not rows:
        return None
    bks = buckets(rows)
    base = [b["vol_d"] for k, b in bks
            if time(12, 0) <= k.time() <= time(13, 25) and b["vol_d"] > 0]
    if not base:
        return None
    baseline = sum(base) / len(base)
    mult, _ = blast_after_arm(rows)

    armed_at = {}  # bucket time -> True if armed conditions hold
    run_low, run_oi_hi = None, 0
    for idx, (k, b) in enumerate(bks):
        run_oi_hi = max(run_oi_hi, b["oi"])
        if k.time() >= time(12, 0) and b["ltp_c"]:
            run_low = b["ltp_c"] if run_low is None else min(run_low, b["ltp_c"])
        if k.time() < time(13, 0) or run_low is None:
            continue
        win = [x for _, x in bks[max(0, idx - 5):idx + 1]]
        v = sum(1 for x in win if x["vol_d"] >= 1.5 * baseline) >= 2
        p = b["ltp_c"] is not None and b["ltp_c"] <= 1.25 * run_low
        o = run_oi_hi > 0 and b["oi"] >= 0.90 * run_oi_hi
        then = bks[idx - 6][1]["und_c"] if idx >= 6 else None
        d = (then is not None and b["und_c"] is not None
             and abs(b["und_c"] - strike) < abs(then - strike))
        armed_at[k.time()] = (v and p and o)  # fuel only (no direction)
        armed_at[(k.time(), "d")] = (v and p and o and d)  # + direction
    short = name.replace("NIFTY09JUN26", "").replace("SENSEX266117", "")
    return short, mult >= 3, armed_at


def main():
    times = [time(h, m) for h in (13, 14) for m in range(0, 60, 5)] + [time(15, 0), time(15, 5)]
    for raw in sys.argv[1:]:
        series = [s for s in (strike_series(f) for f in sorted(Path(raw).glob("*_ohlcv.jsonl"))) if s]
        blasters = {n for n, bl, _ in series if bl}
        print(f"\n### {raw}   (blasters: {' '.join(sorted(blasters))})")
        print(f"{'time':>5s}  {'#fuel':>5s} {'#fuel+dir':>9s}   armed-fuel set (*=eventual blaster)")
        for t in times:
            fuel = [(n, bl) for n, bl, a in series if a.get(t)]
            fdir = [n for n, bl, a in series if a.get((t, "d"))]
            if not fuel:
                continue
            tag = " ".join(f"{n}*" if bl else n for n, bl in sorted(fuel))
            hit = sum(1 for _, bl in fuel if bl)
            print(f"{t.strftime('%H:%M'):>5s}  {len(fuel):>2d}({hit})  {len(fdir):>5d}      {tag}")


if __name__ == "__main__":
    main()
