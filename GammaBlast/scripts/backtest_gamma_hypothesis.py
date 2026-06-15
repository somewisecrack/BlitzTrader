#!/usr/bin/env python3
"""
scripts/backtest_gamma_hypothesis.py — Replay recorded ladder JSONL to test a hypothesis.

Loads a hypothesis JSON from wiki/hypotheses/, replays the matching day's
ladder data with the proposed parameter override, and compares candidate
signals vs baseline.

Usage:
    python3 scripts/backtest_gamma_hypothesis.py wiki/hypotheses/20260610_volburst.json
    python3 scripts/backtest_gamma_hypothesis.py wiki/hypotheses/20260610_volburst.json --date 2026-06-10
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


def _load_ladder_for_date(target_date: date) -> dict[str, list[dict]]:
    """Load all ladder rows grouped by (strike, option_type) string key."""
    ds = target_date.strftime("%Y%m%d")
    ladder_dir = config.RUNTIME_STORAGE_DIR / "data_exports" / ds / "gamma_ladder"
    by_key: dict[str, list] = defaultdict(list)
    if not ladder_dir.exists():
        return by_key
    for f in ladder_dir.rglob("*.jsonl"):
        for row in _load_jsonl(f):
            k = f"{row.get('symbol', '')}{row.get('option_type', '')}{row.get('strike', '')}"
            by_key[k].append(row)
    for k in by_key:
        by_key[k].sort(key=lambda r: r.get("timestamp_ist", ""))
    return by_key


def _run_coiled_scan(
    by_key: dict,
    volume_burst_ratio: float,
    volume_burst_min: int,
    premium_max_ratio: float,
    oi_high_ratio: float,
) -> dict[str, list[str]]:
    """
    Simple replay: for each strike, find timestamps where COILED conditions fire.
    Returns {key: [timestamps_of_first_fire]}.
    """
    results: dict[str, list[str]] = {}
    BASELINE_START = "12:00"
    BASELINE_END   = "13:25"
    ARM_START      = "13:00"

    for key, rows in by_key.items():
        baseline_vols = []
        session_low: float | None = None
        session_high_oi: int = 0
        window: list[dict] = []
        fires: list[str] = []

        prev_vol = None
        bucketed: list[dict] = []
        for r in rows:
            ts = r.get("timestamp_ist", "")
            vol = r.get("volume")
            oi = r.get("oi") or 0
            ltp = r.get("ltp")
            und = r.get("underlying_ltp")

            vol_d = 0
            if vol is not None and prev_vol is not None:
                vol_d = max(0, int(vol) - int(prev_vol))
            prev_vol = vol

            if ltp and ts[11:16] >= "12:00":
                session_low = min(session_low or ltp, ltp)
            session_high_oi = max(session_high_oi, oi or 0)

            b = {"ts": ts, "vol_d": vol_d, "ltp": ltp, "oi": oi, "und": und}
            if BASELINE_START <= ts[11:16] <= BASELINE_END and vol_d > 0:
                baseline_vols.append(vol_d)
            bucketed.append(b)

        if not baseline_vols:
            continue
        baseline = sum(baseline_vols) / len(baseline_vols)

        for i, b in enumerate(bucketed):
            if b["ts"][11:16] < ARM_START:
                continue
            if session_low is None or session_high_oi == 0:
                continue
            win = bucketed[max(0, i - 5):i + 1]
            v = sum(1 for w in win if w["vol_d"] >= volume_burst_ratio * baseline) >= volume_burst_min
            p = b["ltp"] is not None and b["ltp"] <= premium_max_ratio * session_low
            o = b["oi"] >= oi_high_ratio * session_high_oi
            if v and p and o and not fires:
                fires.append(b["ts"])
        if fires:
            results[key] = fires

    return results


def _blast_keys(by_key: dict) -> set[str]:
    """Keys where max 15-min LTP multiple >= 3."""
    blasts = set()
    for key, rows in by_key.items():
        for i, r in enumerate(rows):
            base = r.get("ltp") or 0
            if base <= 0:
                continue
            peak = base
            for j in range(i + 1, len(rows)):
                try:
                    delta_s = (datetime.fromisoformat(rows[j]["timestamp_ist"])
                               - datetime.fromisoformat(rows[i]["timestamp_ist"])).total_seconds()
                except (KeyError, ValueError):
                    break
                if delta_s > 900:
                    break
                v = rows[j].get("ltp") or 0
                peak = max(peak, v)
            if peak / base >= 3.0:
                blasts.add(key)
                break
    return blasts


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("hypothesis_file", help="Path to hypothesis JSON")
    parser.add_argument("--date", help="Date to replay YYYY-MM-DD")
    args = parser.parse_args()

    hyp_path = Path(args.hypothesis_file)
    if not hyp_path.exists():
        print(f"File not found: {hyp_path}", file=sys.stderr)
        return 1

    hyp = json.loads(hyp_path.read_text())
    change = hyp.get("proposed_change", {})
    param = change.get("parameter", "")
    proposed_value = change.get("proposed_value")

    # Determine date from hypothesis_id if not given
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        hid = hyp.get("hypothesis_id", "")
        ds = hid[:8]
        try:
            target = datetime.strptime(ds, "%Y%m%d").date()
        except ValueError:
            target = datetime.now(_IST).date()

    print(f"Backtest: {hyp.get('hypothesis_id')} on {target}")
    print(f"  Change: {param} → {proposed_value}")

    by_key = _load_ladder_for_date(target)
    if not by_key:
        print(f"No ladder data for {target}", file=sys.stderr)
        return 1

    blast_keys = _blast_keys(by_key)
    print(f"  Ground truth blasts: {len(blast_keys)} — {sorted(blast_keys)}")

    base_params = {
        "volume_burst_ratio": config.VOLUME_BURST_RATIO,
        "volume_burst_min": config.VOLUME_BURST_MIN_IN_WINDOW,
        "premium_max_ratio": config.PREMIUM_MAX_RATIO,
        "oi_high_ratio": config.OI_HIGH_RATIO,
    }
    prop_params = dict(base_params)

    _param_map = {
        "VOLUME_BURST_RATIO": "volume_burst_ratio",
        "VOLUME_BURST_MIN_IN_WINDOW": "volume_burst_min",
        "PREMIUM_MAX_RATIO": "premium_max_ratio",
        "OI_HIGH_RATIO": "oi_high_ratio",
    }
    if param in _param_map and proposed_value is not None:
        prop_params[_param_map[param]] = proposed_value

    baseline_fires = _run_coiled_scan(by_key, **base_params)
    proposed_fires = _run_coiled_scan(by_key, **prop_params)

    def _score(fires: dict) -> dict:
        flagged = set(fires.keys())
        tp = flagged & blast_keys
        fp = flagged - blast_keys
        fn = blast_keys - flagged
        prec = len(tp) / len(flagged) if flagged else 0.0
        rec = len(tp) / len(blast_keys) if blast_keys else 0.0
        return {"flagged": len(flagged), "tp": len(tp), "fp": len(fp), "fn": len(fn),
                "precision": prec, "recall": rec}

    bs = _score(baseline_fires)
    ps = _score(proposed_fires)

    print("\n  Baseline  vs  Proposed")
    print(f"  Flagged:   {bs['flagged']:3d}  →  {ps['flagged']:3d}")
    print(f"  TP:        {bs['tp']:3d}  →  {ps['tp']:3d}")
    print(f"  FP:        {bs['fp']:3d}  →  {ps['fp']:3d}")
    print(f"  FN:        {bs['fn']:3d}  →  {ps['fn']:3d}")
    print(f"  Precision: {bs['precision']:.2f}  →  {ps['precision']:.2f}")
    print(f"  Recall:    {bs['recall']:.2f}  →  {ps['recall']:.2f}")

    result = {
        "baseline": bs,
        "proposed": ps,
        "delta_precision": ps["precision"] - bs["precision"],
        "delta_recall": ps["recall"] - bs["recall"],
        "verdict": "IMPROVE" if (ps["precision"] >= bs["precision"] and ps["recall"] >= bs["recall"])
                   else "MIXED" if (ps["tp"] > bs["tp"]) else "REGRESS",
    }
    hyp["backtest_result"] = result
    hyp["status"] = "BACKTESTED"
    hyp_path.write_text(json.dumps(hyp, indent=2), encoding="utf-8")
    print(f"\nVerdict: {result['verdict']} — updated {hyp_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
