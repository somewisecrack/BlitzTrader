#!/usr/bin/env python3
"""Cross-day validation of the gamma-blast composite trigger.

Mechanically applies the four-condition trigger from the 2026-06-09 study
to every strike of one or more recorded sessions and scores the outcome,
so the rule set can be compared across indices (NIFTY 50-pt strikes /
NSE volumes vs SENSEX 100-pt strikes / BSE volumes).

Strict trigger, as written in the 2026-06-09 report (--strict; evaluated
per 5-min bucket, armed after 13:30 IST):
  V: bucket volume >= 2x the 12:00-13:25 baseline, 2 consecutive buckets
  P: close premium within 25% of the running post-noon low
  O: OI >= 95% of the running session high
  D: underlying stepping toward the strike — 3 rising bucket-lows for
     calls / 3 falling bucket-highs for puts

Windowed trigger (default) — same four ingredients scored over the last
30 minutes instead of one bucket, which is how they actually co-occur:
  V: >= 2 of the last 6 buckets at >= 1.5x baseline volume
  P: close premium within 25% of the running post-noon low
  O: OI >= 90% of the running session high
  D: underlying closer to the strike than it was 30 min ago

Outcome per strike: premium multiple from first fire to the max LTP within
the next 45 minutes. A strike "blasted" if any forward 15-min multiple
after 13:30 reached >= 3x.

Usage: trigger_scan.py [--strict] RAW_DIR [RAW_DIR ...]
"""
import json
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

ARM = time(13, 30)
BASE_START, BASE_END = time(12, 0), time(13, 25)


def parse_name(stem):
    name = stem.replace("_ohlcv", "")
    m = re.match(r"NIFTY.*?([CP])(\d+)$", name)
    if m:
        return name, m.group(1), int(m.group(2))
    m = re.match(r"SENSEX\d{5}(\d+)(CE|PE)$", name)
    if m:
        return name, m.group(2)[0], int(m.group(1))
    return None


def load_rows(path):
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("volume") is None or r.get("ltp") is None:
            continue
        r["ts"] = datetime.fromisoformat(r["timestamp_ist"]).replace(tzinfo=None)
        rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def buckets(rows):
    out = {}
    prev_vol = None
    for r in rows:
        key = r["ts"].replace(minute=r["ts"].minute - r["ts"].minute % 5,
                              second=0, microsecond=0)
        b = out.setdefault(key, {"vol_d": 0, "ltp_c": None, "ltp_max": 0,
                                 "oi": 0, "und_hi": 0, "und_lo": 1e12,
                                 "und_c": None})
        if prev_vol is not None:
            b["vol_d"] += max(0, (r["volume"] or 0) - prev_vol)
        prev_vol = r["volume"] or prev_vol
        b["ltp_c"] = r["ltp"]
        b["ltp_max"] = max(b["ltp_max"], r["ltp"])
        b["oi"] = r["oi"] or b["oi"]
        if r.get("underlying_ltp"):
            b["und_hi"] = max(b["und_hi"], r["underlying_ltp"])
            b["und_lo"] = min(b["und_lo"], r["underlying_ltp"])
            b["und_c"] = r["underlying_ltp"]
    return sorted(out.items())


def blast_after_arm(rows):
    """Max forward 15-min LTP multiple starting at/after 13:30."""
    best = (0.0, None)
    n = len(rows)
    for i in range(n):
        if rows[i]["ts"].time() < ARM:
            continue
        base = rows[i]["ltp"]
        if not base or base <= 0:
            continue
        peak = base
        for j in range(i + 1, n):
            if (rows[j]["ts"] - rows[i]["ts"]).total_seconds() > 900:
                break
            if rows[j]["ltp"] and rows[j]["ltp"] > peak:
                peak = rows[j]["ltp"]
        if peak / base > best[0]:
            best = (peak / base, rows[i]["ts"])
    return best


def scan_strike(path, strict):
    parsed = parse_name(path.stem)
    if not parsed:
        return None
    name, cp, strike = parsed
    rows = load_rows(path)
    if not rows:
        return None
    bks = buckets(rows)
    base_vols = [b["vol_d"] for k, b in bks
                 if BASE_START <= k.time() <= BASE_END and b["vol_d"] > 0]
    if not base_vols:
        return None
    baseline = sum(base_vols) / len(base_vols)

    run_low, run_oi_hi = None, 0
    fire = None
    for idx, (k, b) in enumerate(bks):
        run_oi_hi = max(run_oi_hi, b["oi"])
        if k.time() >= time(12, 0) and b["ltp_c"]:
            run_low = b["ltp_c"] if run_low is None else min(run_low, b["ltp_c"])
        if fire or k.time() < ARM or run_low is None:
            continue
        p = b["ltp_c"] is not None and b["ltp_c"] <= 1.25 * run_low
        if strict:
            v = (b["vol_d"] >= 2 * baseline
                 and idx > 0 and bks[idx - 1][1]["vol_d"] >= 2 * baseline)
            o = run_oi_hi > 0 and b["oi"] >= 0.95 * run_oi_hi
            if idx >= 2:
                w = [bks[idx - 2][1], bks[idx - 1][1], b]
                d = (all(x["und_lo"] < 1e12 for x in w)
                     and ((cp == "C" and w[0]["und_lo"] < w[1]["und_lo"] < w[2]["und_lo"])
                          or (cp == "P" and w[0]["und_hi"] > w[1]["und_hi"] > w[2]["und_hi"])))
            else:
                d = False
        else:
            win = [x for _, x in bks[max(0, idx - 5):idx + 1]]
            v = sum(1 for x in win if x["vol_d"] >= 1.5 * baseline) >= 2
            o = run_oi_hi > 0 and b["oi"] >= 0.90 * run_oi_hi
            then = bks[idx - 6][1]["und_c"] if idx >= 6 else None
            d = (then is not None and b["und_c"] is not None
                 and abs(b["und_c"] - strike) < abs(then - strike))
        if v and p and o and d:
            # outcome: max LTP in the 45 min after the fire bucket closes
            t0 = k + timedelta(minutes=5)
            entry = b["ltp_c"]
            peak = entry
            for k2, b2 in bks:
                if t0 <= k2 <= t0 + timedelta(minutes=45):
                    peak = max(peak, b2["ltp_max"])
            fire = (k, entry, peak / entry if entry else 0)

    mult, t_blast = blast_after_arm(rows)
    return name, cp, strike, fire, mult, t_blast


def main():
    args = sys.argv[1:]
    strict = "--strict" in args
    for raw in [a for a in args if a != "--strict"]:
        print(f"\n### {raw}  ({'strict' if strict else 'windowed'})")
        print(f"{'strike':22s} {'fired':>6s} {'entry':>8s} {'x45m':>6s} "
              f"{'blast15(>=3x)':>14s}  verdict")
        for f in sorted(Path(raw).glob("*_ohlcv.jsonl")):
            res = scan_strike(f, strict)
            if not res:
                continue
            name, cp, strike, fire, mult, t_blast = res
            blasted = mult >= 3
            if fire:
                verdict = "HIT" if fire[2] >= 2 else ("FP(exit)" if not blasted else "early")
            else:
                verdict = "MISS" if blasted else "-"
            ftime = fire[0].strftime("%H:%M") if fire else "-"
            fent = f"{fire[1]:.2f}" if fire else "-"
            fx = f"{fire[2]:.2f}" if fire else "-"
            bl = (f"x{mult:.1f}@{t_blast.strftime('%H:%M')}" if blasted else
                  f"x{mult:.1f}")
            print(f"{name:22s} {ftime:>6s} {fent:>8s} {fx:>6s} {bl:>14s}  {verdict}")


if __name__ == "__main__":
    main()
