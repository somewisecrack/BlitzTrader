#!/usr/bin/env python3
"""Pre-blast signal study for the 2026-06-11 SENSEX expiry-day session.

Merges per-minute OHLCV and depth snapshots for each strike and prints
5-minute aggregates from 12:00 IST so the run-up to each gamma blast can
be compared against the quiet midday baseline.

Signals computed per minute:
  ret        : % change in LTP vs previous minute
  vol_d      : traded volume in that minute (delta of cumulative volume)
  oi_d       : OI change in that minute
  bid/ask    : total qty across best-5 levels
  imb        : (bid_qty - ask_qty) / (bid_qty + ask_qty)
  ord_imb    : same on order counts
  spread     : best ask - best bid
  und        : underlying LTP
  dist       : underlying distance to strike (signed, calls: strike-und)
"""
import json
import sys
from pathlib import Path
from datetime import datetime, time

RAW = Path(__file__).parent / "raw"


def load(name):
    o = {}
    for line in (RAW / f"{name}_ohlcv.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("volume") is None or r.get("ltp") is None:
            continue
        ts = datetime.fromisoformat(r["timestamp_ist"]).replace(tzinfo=None)
        o[ts.replace(second=0, microsecond=0)] = r
    d = {}
    for line in (RAW / f"{name}_depth.jsonl").read_text().splitlines():
        r = json.loads(line)
        if not r.get("best_5_bids") or not r.get("best_5_asks"):
            continue
        ts = datetime.fromisoformat(r["timestamp_ist"]).replace(tzinfo=None)
        d[ts.replace(second=0, microsecond=0)] = r
    rows = []
    prev = None
    for ts in sorted(o):
        r = o[ts]
        dep = d.get(ts)
        row = {
            "ts": ts, "ltp": r["ltp"], "vol": r["volume"], "oi": r["oi"],
            "und": r["underlying_ltp"],
        }
        if dep:
            bq = sum(x["qty"] for x in dep["best_5_bids"])
            aq = sum(x["qty"] for x in dep["best_5_asks"])
            bo = sum(x["orders"] for x in dep["best_5_bids"])
            ao = sum(x["orders"] for x in dep["best_5_asks"])
            row["imb"] = (bq - aq) / (bq + aq) if bq + aq else 0.0
            row["ord_imb"] = (bo - ao) / (bo + ao) if bo + ao else 0.0
            row["spread"] = dep["best_5_asks"][0]["price"] - dep["best_5_bids"][0]["price"]
        if prev:
            row["vol_d"] = max(0, (r["volume"] or 0) - (prev["vol"] or 0))
            row["oi_d"] = (r["oi"] or 0) - (prev["oi"] or 0)
            row["ret"] = (r["ltp"] / prev["ltp"] - 1) * 100 if prev["ltp"] else 0
        rows.append(row)
        prev = row
    return rows


def agg5(rows, start=time(12, 0), end=time(15, 16)):
    """5-minute buckets."""
    buckets = {}
    for r in rows:
        t = r["ts"].time()
        if not (start <= t <= end):
            continue
        key = r["ts"].replace(minute=r["ts"].minute - r["ts"].minute % 5,
                              second=0, microsecond=0)
        buckets.setdefault(key, []).append(r)
    out = []
    for k in sorted(buckets):
        b = buckets[k]
        out.append({
            "t": k.strftime("%H:%M"),
            "ltp_o": b[0]["ltp"], "ltp_c": b[-1]["ltp"],
            "vol": sum(r.get("vol_d", 0) for r in b),
            "oi_d": sum(r.get("oi_d", 0) for r in b),
            "oi": b[-1]["oi"],
            "imb": sum(r.get("imb", 0) for r in b) / max(1, sum(1 for r in b if "imb" in r)),
            "ord": sum(r.get("ord_imb", 0) for r in b) / max(1, sum(1 for r in b if "ord_imb" in r)),
            "spr": sum(r.get("spread", 0) for r in b) / max(1, sum(1 for r in b if "spread" in r)),
            "und": b[-1]["und"],
        })
    return out


def show(name, note=""):
    rows = load(name)
    print(f"\n=== {name} {note} ===")
    print(f"{'time':>5} {'ltp_o':>7} {'ltp_c':>7} {'vol(M)':>7} {'oi_d(k)':>8} "
          f"{'oi(M)':>6} {'imb':>6} {'ord':>6} {'spr':>5} {'und':>9}")
    for a in agg5(rows):
        print(f"{a['t']:>5} {a['ltp_o']:7.2f} {a['ltp_c']:7.2f} "
              f"{a['vol']/1e6:7.2f} {a['oi_d']/1e3:8.0f} {a['oi']/1e6:6.2f} "
              f"{a['imb']:6.2f} {a['ord']:6.2f} {a['spr']:5.2f} {a['und']:9.1f}")


if __name__ == "__main__":
    for n in sys.argv[1:]:
        show(n)
