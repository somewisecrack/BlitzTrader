#!/usr/bin/env python3
"""
scripts/evaluate_gamma_day.py — GammaBlast post-market daily review.

Reads:
  - data_exports/YYYYMMDD/gamma_ladder/**/*.jsonl  (raw ladder data)
  - candidate_signals/YYYYMMDD.jsonl               (candidate audit)
  - journals/YYYYMMDD.md                           (journal)
  - live_state.json                                (position book)

Produces:
  - wiki/daily_reviews/YYYYMMDD.md  (compact structured review)

Usage:
    python3 scripts/evaluate_gamma_day.py
    python3 scripts/evaluate_gamma_day.py --date 2026-06-10
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import config  # noqa: E402

_IST = timezone(timedelta(hours=5, minutes=30))


def _today() -> date:
    return datetime.now(_IST).date()


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _load_ladder(base: Path, ds: str) -> list[dict]:
    ladder_dir = base / "data_exports" / ds / "gamma_ladder"
    rows = []
    if not ladder_dir.exists():
        return rows
    for f in ladder_dir.rglob("*.jsonl"):
        rows.extend(_load_jsonl(f))
    return rows


def _max_mult(rows: list[dict], minutes: int = 15) -> tuple[float, str]:
    """Max rolling N-minute LTP multiple across all rows of one strike."""
    best = (0.0, "")
    n = len(rows)
    for i in range(n):
        base = rows[i].get("ltp")
        if not base or base <= 0:
            continue
        peak = base
        pt = rows[i].get("timestamp_ist", "")
        for j in range(i + 1, n):
            try:
                delta = (datetime.fromisoformat(rows[j]["timestamp_ist"])
                         - datetime.fromisoformat(rows[i]["timestamp_ist"]))
                if delta.total_seconds() > minutes * 60:
                    break
            except (KeyError, ValueError):
                break
            v = rows[j].get("ltp") or 0
            if v > peak:
                peak = v
                pt = rows[j].get("timestamp_ist", "")
        mult = peak / base
        if mult > best[0]:
            best = (mult, pt)
    return best


def _strike_key(row: dict) -> str:
    return f"{row.get('symbol', '')}{row.get('option_type', '')}{row.get('strike', '')}"


def evaluate(target_date: date) -> str:
    ds = target_date.strftime("%Y%m%d")
    base = config.RUNTIME_STORAGE_DIR

    ladder_rows = _load_ladder(base, ds)
    audit_rows = _load_jsonl(base / "candidate_signals" / f"{ds}.jsonl")

    # group ladder rows by strike key
    by_strike: dict[str, list] = defaultdict(list)
    for r in ladder_rows:
        by_strike[_strike_key(r)].append(r)

    # sort each strike's rows by time
    for k in by_strike:
        by_strike[k].sort(key=lambda r: r.get("timestamp_ist", ""))

    # compute blast multiples
    blast_results = []
    for key, rows in by_strike.items():
        m15, at = _max_mult(rows, 15)
        if m15 >= 2.0 or (rows and rows[0].get("strike")):
            sample = rows[0]
            blast_results.append({
                "key": key,
                "symbol": sample.get("symbol"),
                "strike": sample.get("strike"),
                "option_type": sample.get("option_type"),
                "rows": len(rows),
                "m15": m15,
                "at": at,
            })

    blast_results.sort(key=lambda r: -r["m15"])

    # candidate summary
    coiled = [r for r in audit_rows if r.get("stage") == "COILED_DETECTED"]
    entries = [r for r in audit_rows if r.get("stage") == "VIRTUAL_ENTRY"]
    exits = [r for r in audit_rows if r.get("stage") == "VIRTUAL_EXIT"]
    eod_closes = [r for r in audit_rows if r.get("stage") == "EOD_FORCE_CLOSE"]

    total_pnl = 0.0
    for e in exits + eod_closes:
        d = e.get("details") or {}
        total_pnl += float(d.get("pnl", 0) or 0)

    # build report
    lines = [
        f"# GammaBlast Daily Review — {target_date.strftime('%d %b %Y')}",
        "",
        f"**Ladder strikes tracked:** {len(by_strike)}",
        f"**Total OHLCV snapshots:** {len(ladder_rows)}",
        f"**Candidates COILED:** {len(coiled)}",
        f"**Virtual entries:** {len(entries)}",
        f"**Exits (intraday):** {len(exits)}  **EOD closes:** {len(eod_closes)}",
        f"**Total virtual P&L:** ₹{total_pnl:,.2f}",
        "",
        "## Blast table (max 15-min multiple per strike)",
        "",
        "| Strike | Type | Samples | Max 15m | At |",
        "|--------|------|---------|---------|-----|",
    ]
    for r in blast_results[:20]:
        flag = " **BLAST**" if r["m15"] >= 3 else (" partial" if r["m15"] >= 2 else "")
        at_str = r["at"][11:16] if r["at"] else "?"
        lines.append(
            f"| {r['strike']} | {r['option_type']} | {r['rows']} "
            f"| ×{r['m15']:.2f}{flag} | {at_str} |"
        )

    if entries:
        lines += ["", "## Virtual positions entered", ""]
        for e in entries:
            d = e.get("details") or {}
            lines.append(
                f"- {e.get('symbol')} {e.get('strike')} {e.get('option_type')} "
                f"@ ₹{d.get('entry_price', '?')} — {e.get('ts', '')[:16]}"
            )

    lines += [
        "",
        "## Improvement opportunities",
        "",
        "_(fill in via propose_gamma_hypotheses.py)_",
        "",
    ]
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    args = parser.parse_args()
    target = (datetime.strptime(args.date, "%Y-%m-%d").date()
              if args.date else _today())

    report = evaluate(target)
    ds = target.strftime("%Y%m%d")
    out = _ROOT / "wiki" / "daily_reviews" / f"{ds}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"Review written: {out}")
    print(report)


if __name__ == "__main__":
    main()
