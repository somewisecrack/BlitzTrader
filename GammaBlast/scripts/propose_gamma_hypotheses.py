#!/usr/bin/env python3
"""
scripts/propose_gamma_hypotheses.py — GammaBlast post-market hypothesis generator.

Reads the daily review and candidate audit and uses Gemini to propose
specific, testable improvements to the candidate engine rules.

Gemini is ONLY called post-market (after 15:30 IST). It is NEVER called
from the live scanning path. All promoted rules must be deterministic Python.

Output: wiki/hypotheses/<YYYYMMDD>_<slug>.json
Schema:
{
  "hypothesis_id": "20260610_volburst_threshold",
  "created_at": "...",
  "rule_version": "v1",
  "description": "Reduce volume burst ratio from 1.5 to 1.3 for SENSEX",
  "proposed_change": {
    "parameter": "VOLUME_BURST_RATIO",
    "current_value": 1.5,
    "proposed_value": 1.3,
    "scope": "SENSEX"
  },
  "rationale": "...",
  "status": "PROPOSED",
  "backtest_result": null
}
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import config  # noqa: E402

_IST = timezone(timedelta(hours=5, minutes=30))
_SCHEMA = {
    "type": "object",
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["hypothesis_id", "description", "proposed_change", "rationale"],
                "properties": {
                    "hypothesis_id": {"type": "string"},
                    "description": {"type": "string"},
                    "proposed_change": {"type": "object"},
                    "rationale": {"type": "string"},
                },
            },
        }
    },
}


def _today() -> date:
    return datetime.now(_IST).date()


def _load_review(target_date: date) -> str:
    ds = target_date.strftime("%Y%m%d")
    p = _ROOT / "wiki" / "daily_reviews" / f"{ds}.md"
    return p.read_text(encoding="utf-8") if p.exists() else "(no review)"


def _load_audit_summary(target_date: date) -> str:
    ds = target_date.strftime("%Y%m%d")
    p = config.RUNTIME_STORAGE_DIR / "candidate_signals" / f"{ds}.jsonl"
    if not p.exists():
        return "(no audit)"
    lines = p.read_text(errors="replace").splitlines()
    return f"{len(lines)} audit records"


def _call_gemini(prompt: str) -> str:
    try:
        import google.generativeai as genai
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(config.GEMINI_MODEL)
        resp = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                max_output_tokens=2048,
            ),
            request_options={"timeout": config.GEMINI_API_TIMEOUT_SECONDS},
        )
        return resp.text or ""
    except Exception as e:
        print(f"Gemini call failed: {e}", file=sys.stderr)
        return ""


def propose(target_date: date) -> list[dict]:
    if not config.GEMINI_API_KEY:
        print("No GEMINI_API_KEY — skipping hypothesis generation")
        return []

    review = _load_review(target_date)
    audit_summary = _load_audit_summary(target_date)
    ds = target_date.strftime("%Y%m%d")

    prompt = f"""You are GammaBlast, a virtual options-scanner analyst.
Today is {target_date}. You are running post-market (no live data access).

Daily review:
{review[:3000]}

Audit summary: {audit_summary}

Current rule parameters (v1 deterministic rules):
- VOLUME_BURST_RATIO: {config.VOLUME_BURST_RATIO}
- VOLUME_BURST_MIN_IN_WINDOW: {config.VOLUME_BURST_MIN_IN_WINDOW}
- PREMIUM_MAX_RATIO: {config.PREMIUM_MAX_RATIO}
- OI_HIGH_RATIO: {config.OI_HIGH_RATIO}
- BID_IMBALANCE_MIN: {config.BID_IMBALANCE_MIN}
- TRAIL_ACTIVATION_MULT: {config.TRAIL_ACTIVATION_MULT}
- TRAIL_INITIAL_FRACTION: {config.TRAIL_INITIAL_FRACTION}
- TRAIL_TIGHT_MULT: {config.TRAIL_TIGHT_MULT}
- TRAIL_TIGHT_FRACTION: {config.TRAIL_TIGHT_FRACTION}

Propose 1-3 specific, testable rule changes that could improve:
1. Candidate precision (fewer false positives that don't blast)
2. Candidate recall (not missing blasts)
3. Exit timing (capturing more of the blast multiple)

Each hypothesis must be a concrete parameter change or new deterministic condition.
No LLM calls in the runtime path. Output deterministic Python only.

Respond ONLY with valid JSON matching this schema:
{{"hypotheses": [{{"hypothesis_id": "{ds}_<slug>", "description": "...", "proposed_change": {{"parameter": "...", "current_value": ..., "proposed_value": ..., "scope": "BOTH|NIFTY|SENSEX"}}, "rationale": "..."}}]}}"""

    raw = _call_gemini(prompt)
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
        return parsed.get("hypotheses", [])
    except json.JSONDecodeError:
        # try to extract JSON block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())["hypotheses"]
            except Exception:
                pass
        print(f"Could not parse Gemini response: {raw[:200]}", file=sys.stderr)
        return []


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    args = parser.parse_args()
    target = (datetime.strptime(args.date, "%Y-%m-%d").date()
              if args.date else _today())

    hypotheses = propose(target)
    if not hypotheses:
        print("No hypotheses generated.")
        return

    hyp_dir = _ROOT / "wiki" / "hypotheses"
    hyp_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(_IST).isoformat(timespec="seconds")
    saved = []
    for h in hypotheses:
        h.setdefault("created_at", now)
        h.setdefault("rule_version", "v1")
        h.setdefault("status", "PROPOSED")
        h.setdefault("backtest_result", None)
        slug = h.get("hypothesis_id", f"{target.strftime('%Y%m%d')}_hyp")
        out = hyp_dir / f"{slug}.json"
        out.write_text(json.dumps(h, indent=2), encoding="utf-8")
        saved.append(str(out))
        print(f"Saved: {out}")
        print(f"  {h.get('description', '')}")

    print(f"\n{len(saved)} hypothesis/hypotheses saved.")


if __name__ == "__main__":
    main()
