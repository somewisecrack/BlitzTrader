#!/usr/bin/env python3
"""Per-day blast detector across the recorded ATM ladder (any index).

Globs *_ohlcv.jsonl under a day directory (recurses into per-index
subdirs), parses the option symbol for index / expiry / strike, and
reports the max rolling 5/15/30-min LTP multiple per strike plus the best
15-min window. Flags a strike as BLAST if its 15-min multiple >= 3, PARTIAL
if >= 2. Prints days-to-expiry so off-expiry behaviour is visible.

Usage: scan_day.py DAY_DIR [DAY_DIR ...]
"""
import json
import re
import sys
from datetime import datetime, date
from pathlib import Path

MONTHS = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6, JUL=7, AUG=8,
              SEP=9, OCT=10, NOV=11, DEC=12)


def parse_symbol(stem):
    s = stem.replace("_ohlcv", "")
    m = re.match(r"(NIFTY|BANKNIFTY)(\d{2})([A-Z]{3})(\d{2})([CP])(\d+)$", s)
    if m:
        idx, dd, mon, yy, cp, strike = m.groups()
        exp = date(2000 + int(yy), MONTHS[mon], int(dd))
        return idx, exp, cp, int(strike)
    m = re.match(r"SENSEX(\d{2})(\d)(\d{2})(\d+)(CE|PE)$", s)
    if m:
        yy, mm, dd, strike, cp = m.groups()
        exp = date(2000 + int(yy), int(mm), int(dd))
        return "SENSEX", exp, cp[0], int(strike)
    return None


def load(path):
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("ltp") is None:
            continue
        # Drop settlement/underlying-leak prints: a near-ATM index-option
        # premium never approaches the index level. Cap absolutely and
        # relative to the underlying.
        if r["ltp"] > 3000:
            continue
        if r.get("underlying_ltp") and r["ltp"] >= 0.4 * r["underlying_ltp"]:
            continue
        r["ts"] = datetime.fromisoformat(r["timestamp_ist"]).replace(tzinfo=None)
        rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def roll_max_mult(rows, minutes):
    best = (0.0, None, None, None)
    n = len(rows)
    j = 0
    for i in range(n):
        base = rows[i]["ltp"]
        if not base or base <= 0:
            continue
        if j < i:
            j = i
        peak, pt = base, rows[i]["ts"]
        k = i
        while k < n and (rows[k]["ts"] - rows[i]["ts"]).total_seconds() <= minutes * 60:
            if rows[k]["ltp"] and rows[k]["ltp"] > peak:
                peak, pt = rows[k]["ltp"], rows[k]["ts"]
            k += 1
        if peak / base > best[0]:
            best = (peak / base, rows[i]["ts"], base, peak)
    return best


def scan_dir(d):
    files = sorted(Path(d).rglob("*_ohlcv.jsonl"))
    out = []
    daydir = Path(d).name
    for f in files:
        ps = parse_symbol(f.stem)
        if not ps:
            continue
        idx, exp, cp, strike = ps
        rows = load(f)
        if len(rows) < 5:
            continue
        m5 = roll_max_mult(rows, 5)[0]
        m15 = roll_max_mult(rows, 15)
        m30 = roll_max_mult(rows, 30)[0]
        ltps = [r["ltp"] for r in rows if r["ltp"]]
        unds = [r["underlying_ltp"] for r in rows if r.get("underlying_ltp")]
        try:
            dday = datetime.strptime(daydir, "%Y%m%d").date()
            dte = (exp - dday).days
        except ValueError:
            dte = None
        out.append(dict(idx=idx, cp=cp, strike=strike, exp=exp, dte=dte,
                        rows=len(rows), lo=min(ltps), hi=max(ltps),
                        m5=m5, m15=m15[0], m15w=m15[1], m15base=m15[2],
                        m15peak=m15[3], m30=m30,
                        und_lo=min(unds) if unds else None,
                        und_hi=max(unds) if unds else None))
    return daydir, out


def main():
    for d in sys.argv[1:]:
        daydir, rows = scan_dir(d)
        if not rows:
            print(f"\n### {daydir}: no ohlcv found")
            continue
        any_r = rows[0]
        idxs = sorted({r["idx"] for r in rows})
        und = next((f"{r['und_lo']:.0f}-{r['und_hi']:.0f}" for r in rows if r["und_lo"]), "?")
        print(f"\n### {daydir}  indices={','.join(idxs)}  "
              f"exp={','.join(str(r) for r in sorted({x['exp'] for x in rows}))}  "
              f"dte={sorted({x['dte'] for x in rows if x['dte'] is not None})}  und={und}")
        print(f"{'strike':16s} {'dte':>3s} {'rows':>4s} {'rng':>16s} "
              f"{'x5':>5s} {'x15':>5s} {'x30':>5s}  {'15m window':>22s}  flag")
        for r in sorted(rows, key=lambda r: -r["m15"]):
            flag = "BLAST" if r["m15"] >= 3 else ("partial" if r["m15"] >= 2 else "")
            wt = r["m15w"].strftime("%H:%M") if r["m15w"] else "?"
            name = f"{r['idx'][:6]}{r['cp']}{r['strike']}"
            print(f"{name:16s} {str(r['dte']):>3s} {r['rows']:>4d} "
                  f"{r['lo']:.1f}-{r['hi']:.1f}".ljust(33)
                  + f"{r['m5']:>5.2f} {r['m15']:>5.2f} {r['m30']:>5.2f}  "
                  f"{wt} {r['m15base']:.1f}->{r['m15peak']:.1f}".rjust(22) + f"  {flag}")


if __name__ == "__main__":
    main()
