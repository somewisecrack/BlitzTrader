#!/usr/bin/env python3
"""
scripts/propose_futures_hypotheses.py
----------------------------------------
Reads the latest (or a specified) compact daily review produced by
evaluate_futures_day.py and generates structured hypothesis files in
wiki/hypotheses/.

Default (production) behavior:
    Attempt Gemini once with a short bounded prompt.  Gemini proposes
    candidate futures filters as strict JSON.  Python validates schema,
    rejects anything involving pairs/equity/LLM-gating, and writes only
    the hypotheses that pass.

    If Gemini is unavailable (no API key, package missing, quota hit,
    invalid output, etc.) the script logs the reason, writes NO fake
    hypotheses, and exits cleanly with code 0.

--no-llm mode (dry / manual operation):
    Deterministic parse of the "## Possible Hypotheses" section written
    by evaluate_futures_day.py.  Never calls any external API.

Usage:
    python3 scripts/propose_futures_hypotheses.py
    python3 scripts/propose_futures_hypotheses.py --date 2026-05-09
    python3 scripts/propose_futures_hypotheses.py --review wiki/daily_reviews/2026-05-09.md
    python3 scripts/propose_futures_hypotheses.py --no-llm
    python3 scripts/propose_futures_hypotheses.py --max-hypotheses 2
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

try:
    from tools.futures_hypothesis import validate_hypothesis
    from tools.futures_strategy_engine import SUPPORTED_STRATEGIES
except ImportError as e:
    print(
        f"ERROR: Could not import tools: {e}\n"
        "Run this script from the repo root or ensure PYTHONPATH includes the repo root.",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────────

FUTURES_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}
IST = timezone(timedelta(hours=5, minutes=30))
MAX_OUTPUT_BYTES = 64_000   # 64 KB ceiling per hypothesis file

# Low-cost model used when config doesn't specify one
_FALLBACK_MODEL = "gemini-2.5-flash-lite"

# Max chars of review text sent to Gemini — keeps prompt short and cost low
_MAX_REVIEW_CHARS = 2_500

# Supported filter field names (mirrors tools/futures_filter_loader.py)
_SUPPORTED_FILTER_FIELDS = {
    "rsi14_lt", "rsi14_gt",
    "adx14_lt", "adx14_gt",
    "ema_stacked_bull", "ema_stacked_bear",
    "price_below_vwap", "price_above_vwap",
}

# Pairs-related keywords that must never appear in Gemini proposals
_PAIRS_KEYWORDS = {
    "cointegration", "z_score", "spread", "hedge_ratio", "coint_pvalue",
    "pair", "pairs", "johansen", "cadf", "pair_capital",
}

# ── IST helper ─────────────────────────────────────────────────────────────────


def _now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


# ── Review helpers ─────────────────────────────────────────────────────────────


def find_latest_review(wiki_dir: Path) -> Path | None:
    reviews_dir = wiki_dir / "daily_reviews"
    if not reviews_dir.exists():
        return None
    candidates = sorted(reviews_dir.glob("????-??-??.md"), reverse=True)
    return candidates[0] if candidates else None


def load_review(review_path: Path) -> str:
    if not review_path.exists():
        print(f"ERROR: Review file not found: {review_path}", file=sys.stderr)
        sys.exit(1)
    return review_path.read_text(encoding="utf-8", errors="replace")


def extract_review_date(review_path: Path) -> str:
    """Return YYYYMMDD from a filename like 2026-05-09.md."""
    return review_path.stem.replace("-", "")


def compact_review(review_text: str) -> str:
    """Return the first _MAX_REVIEW_CHARS characters of the review (UTF-8 safe)."""
    text = review_text[:_MAX_REVIEW_CHARS]
    # Don't cut mid-word
    last_nl = text.rfind("\n")
    return text[:last_nl] if last_nl > 0 else text


def extract_strategies_from_review(review_text: str) -> set[str]:
    """Return SUPPORTED_STRATEGIES names that appear verbatim in the review text.

    Only strategies present in the review AND in SUPPORTED_STRATEGIES are
    returned.  This set gates which strategies Gemini is permitted to propose.
    """
    return {s for s in SUPPORTED_STRATEGIES if s in review_text}


# ── Gemini LLM mode ───────────────────────────────────────────────────────────


_GEMINI_PROMPT_TEMPLATE = """\
You are a post-market quantitative research assistant for futures trading.

You have been given a compact daily review of NIFTY/BANKNIFTY/FINNIFTY futures signals.
Your task is to propose a small number of filter hypotheses that could block low-quality signals.

RULES:
- You are performing post-market research only. You must NEVER make live trade decisions.
- Do NOT invent prices, candle data, or strategies not mentioned in the review.
- Do NOT suggest pairs trading, cointegration, spreads, or equity strategies.
- Prefer 1-2 high-confidence hypotheses over many weak guesses.
- Each hypothesis must be directly backtestable by a Python script using yfinance OHLCV data.
- Do NOT depend on subjective chart reading; use only objective numeric conditions.

OUTPUT FORMAT:
Respond with a valid JSON array ONLY. No markdown, no prose, no code fences.
Each element must be a JSON object with EXACTLY these keys:
  "scope"              : must be the string "futures"
  "symbol"             : one of "NIFTY", "BANKNIFTY", "FINNIFTY"
  "strategy"           : exact strategy name as it appears in the review (e.g. "VP-01 Counter Bull Trap")
  "claim"              : one sentence describing what signal condition to block
  "direction"          : "BUY" or "SELL"
  "filter"             : object with key "block_when": object of indicator conditions
  "rationale"          : one sentence citing evidence from the review
  "source_review_date" : the date of this review (YYYY-MM-DD)
  "created_by"         : must be the string "gemini"
  "status"             : must be the string "proposed"

ALLOWED filter.block_when keys (numeric threshold unless noted):
  rsi14_lt, rsi14_gt, adx14_lt, adx14_gt,
  ema_stacked_bull (boolean), ema_stacked_bear (boolean),
  price_below_vwap (boolean), price_above_vwap (boolean)

COMPACT DAILY REVIEW:
---
{review}
---

Propose at most {max_hypotheses} hypotheses. Output the JSON array now.
"""


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences so json.loads can parse the result."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.IGNORECASE)
    return text.strip()


def _extract_response_text(response) -> str:
    """Extract text content from a google-genai SDK response object.

    Mirrors the pattern used in agent_loop.py: iterate candidates[0].content.parts.
    Falls back to response.text for simpler response objects.
    """
    try:
        candidates = response.candidates
        if candidates:
            parts = candidates[0].content.parts
            if parts:
                return "".join(p.text for p in parts if getattr(p, "text", None))
    except Exception:
        pass
    return getattr(response, "text", "") or ""


def write_no_proposals_artifact(
    wiki_dir: Path,
    date_iso: str,
    reason: str,
    llm_attempted: bool,
) -> None:
    """Write a compact audit record when no hypotheses were generated.

    Written to wiki/hypotheses/no_proposals/YYYY-MM-DD.json.
    Never contains raw logs, reviews, candle data, or Gemini response text.
    """
    no_props_dir = wiki_dir / "hypotheses" / "no_proposals"
    no_props_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "date": date_iso,
        "created_at": _now_ist(),
        "scope": "futures",
        "status": "no_proposals_generated",
        "reason": reason,
        "created_by": "python",
        "llm_attempted": llm_attempted,
        "hypotheses_written": 0,
    }
    out = no_props_dir / f"{date_iso}.json"
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[propose_futures_hypotheses] No-proposals audit written: {out}")


def _reject_pairs_content(hyp: dict) -> str | None:
    """Return a rejection reason if pairs-related content is found, else None."""
    def _scan(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in _PAIRS_KEYWORDS:
                    return f"pairs field in key: {k!r}"
                if isinstance(v, str):
                    for kw in _PAIRS_KEYWORDS:
                        if kw in v.lower():
                            return f"pairs keyword {kw!r} in value"
                hit = _scan(v)
                if hit:
                    return hit
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                hit = _scan(item)
                if hit:
                    return hit
        return None

    return _scan(hyp)


def _validate_gemini_hypothesis(
    raw: dict,
    review_date_iso: str,
    strategies_in_review: set[str],
) -> tuple[bool, str]:
    """Validate a single raw hypothesis dict from Gemini output.

    strategies_in_review must contain only SUPPORTED_STRATEGIES names that
    were actually observed in that day's compact review.  Proposals for any
    other strategy are rejected — we do not trust Gemini's strategy naming.
    """
    if not isinstance(raw, dict):
        return False, "not a JSON object"

    # Reject oversized individual items
    if len(json.dumps(raw)) > MAX_OUTPUT_BYTES:
        return False, "hypothesis JSON exceeds 64 KB"

    # created_by must be gemini
    if raw.get("created_by", "").lower() != "gemini":
        return False, f"created_by must be 'gemini', got {raw.get('created_by')!r}"

    # Pairs content check (fast, before expensive validate_hypothesis)
    pairs_hit = _reject_pairs_content(raw)
    if pairs_hit:
        return False, f"pairs content rejected: {pairs_hit}"

    # Strategy must be both supported AND seen in today's review
    strategy = raw.get("strategy", "")
    if strategy not in SUPPORTED_STRATEGIES:
        return False, f"strategy not in SUPPORTED_STRATEGIES: {strategy!r}"
    if strategy not in strategies_in_review:
        return False, f"strategy not present in today's review: {strategy!r}"

    # Strip unknown keys to a safe subset before calling validate_hypothesis
    safe = {
        "scope":     raw.get("scope"),
        "symbol":    raw.get("symbol", ""),
        "strategy":  strategy,
        "direction": raw.get("direction"),
        "claim":     raw.get("claim", ""),
        "filter":    raw.get("filter"),
        "status":    raw.get("status", "proposed"),
    }

    # Validate filter.block_when fields
    flt = safe.get("filter") or {}
    block_when = flt.get("block_when") if isinstance(flt, dict) else {}
    if isinstance(block_when, dict):
        unsupported = set(block_when.keys()) - _SUPPORTED_FILTER_FIELDS
        if unsupported:
            return False, f"unsupported filter fields: {sorted(unsupported)}"

    ok, reason = validate_hypothesis(safe)
    if not ok:
        return False, reason

    return True, ""


def call_gemini(
    review_text: str,
    review_date_iso: str,
    date_compact: str,
    max_hypotheses: int,
    strategies_in_review: set[str],
) -> tuple[list[dict], str | None]:
    """Call Gemini once with a bounded prompt using the google-genai SDK.

    Uses GEMINI_SCHEDULED_MODEL from config (low-cost), falls back to
    _FALLBACK_MODEL.  Exactly one attempt — no retry loop.

    Returns (validated_hypotheses, failure_reason).
    On success failure_reason is None.  On any error the list is empty
    and failure_reason is a short human-readable string.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return [], "GEMINI_API_KEY is not set"

    try:
        from google import genai  # google-genai>=1.0.0 (same SDK as agent_loop.py)
    except ImportError:
        return [], "google-genai package is not installed"

    # Prefer low-cost scheduled model specified in config
    try:
        from config import GEMINI_SCHEDULED_MODEL, GEMINI_MODEL
        model_name = GEMINI_SCHEDULED_MODEL or GEMINI_MODEL or _FALLBACK_MODEL
    except ImportError:
        model_name = _FALLBACK_MODEL

    prompt = _GEMINI_PROMPT_TEMPLATE.format(
        review=compact_review(review_text),
        max_hypotheses=max_hypotheses,
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model_name, contents=prompt)
        raw_text = _extract_response_text(response)
    except Exception as exc:
        return [], f"Gemini API error: {exc}"

    # One JSON-cleanup pass: strip markdown fences, find outermost array
    raw_text = _strip_json_fences(raw_text)

    # Find the outermost JSON array in the response
    array_m = re.search(r"\[.*\]", raw_text, re.DOTALL)
    if not array_m:
        return [], f"Gemini response contained no JSON array (got {len(raw_text)} chars)"

    try:
        candidates = json.loads(array_m.group(0))
    except json.JSONDecodeError as exc:
        return [], f"Gemini JSON parse failed: {exc}"

    if not isinstance(candidates, list):
        return [], "Gemini output was not a JSON array"

    # Reject oversized arrays
    if len(json.dumps(candidates)) > 10 * MAX_OUTPUT_BYTES:
        return [], "Gemini output exceeded size limit (640 KB)"

    # Cap to max_hypotheses before validation
    candidates = candidates[:max_hypotheses]

    validated = []
    for i, raw in enumerate(candidates):
        ok, reason = _validate_gemini_hypothesis(raw, review_date_iso, strategies_in_review)
        if not ok:
            print(
                f"  [Gemini] Rejected candidate {i + 1}: {reason}",
                file=sys.stderr,
            )
            continue
        validated.append(raw)

    if not validated:
        return [], "Gemini returned candidates but none passed validation"

    return validated, None


# ── Hypothesis assembly ────────────────────────────────────────────────────────


def _next_seq(hypotheses_dir: Path, date_compact: str) -> int:
    existing = (
        list(hypotheses_dir.glob(f"HYP-{date_compact}-*.yaml"))
        + list(hypotheses_dir.glob(f"HYP-{date_compact}-*.json"))
    )
    return len(existing) + 1


def assemble_hypothesis(
    raw: dict,
    hyp_id: str,
    review_date_iso: str,
) -> dict:
    """Build the canonical hypothesis dict from a validated Gemini raw dict."""
    now = _now_ist()
    return {
        "id":         hyp_id,
        "created_at": now,
        "scope":      raw["scope"],
        "strategy":   raw["strategy"],
        "symbol":     raw["symbol"],
        "direction":  raw.get("direction"),
        "claim":      raw.get("claim", ""),
        "filter":     raw.get("filter") or {"block_when": {}},
        "evidence": {
            "dates": [review_date_iso],
            "notes": [raw.get("rationale", "")],
        },
        "status":              "proposed",
        "created_by":          "gemini",
        "source_review_date":  raw.get("source_review_date", review_date_iso),
    }


# ── Mode A: deterministic parse ───────────────────────────────────────────────

_INDICATOR_PATTERNS = [
    ("rsi14",     re.compile(r"RSI[-_]?14", re.IGNORECASE)),
    ("adx14",     re.compile(r"ADX[-_]?14", re.IGNORECASE)),
    ("ema9",      re.compile(r"EMA[-_]?9\b", re.IGNORECASE)),
    ("ema21",     re.compile(r"EMA[-_]?21\b", re.IGNORECASE)),
    ("ema50",     re.compile(r"EMA[-_]?50\b", re.IGNORECASE)),
    ("ema_stack", re.compile(r"EMA\s+stack", re.IGNORECASE)),
    ("macd",      re.compile(r"\bMACD\b", re.IGNORECASE)),
    ("atr14",     re.compile(r"ATR[-_]?14", re.IGNORECASE)),
]

_COMPARISON_PATTERNS = [
    ("<",  re.compile(r"\bbelow\b|\bless\s+than\b|\blt\b|<(?!=)", re.IGNORECASE)),
    (">",  re.compile(r"\babove\b|\bgreater\s+than\b|\bgt\b|>(?!=)", re.IGNORECASE)),
    ("<=", re.compile(r"<=|at\s+most", re.IGNORECASE)),
    (">=", re.compile(r">=|at\s+least", re.IGNORECASE)),
]


def _parse_symbol(text: str) -> str | None:
    for sym in FUTURES_SYMBOLS:
        if re.search(rf"\b{sym}\b", text, re.IGNORECASE):
            return sym.upper()
    return None


def _parse_direction(text: str) -> str:
    upper = text.upper()
    if "SELL" in upper or "SHORT" in upper:
        return "SELL"
    return "BUY"


def _parse_indicator(text: str) -> tuple[str | None, float | None, str | None]:
    indicator_name = None
    for name, pat in _INDICATOR_PATTERNS:
        if pat.search(text):
            indicator_name = name
            break

    threshold = None
    if indicator_name:
        clean = re.sub(
            r"RSI[-_]?14|ADX[-_]?14|EMA[-_]?\d+|ATR[-_]?\d+", "", text, flags=re.IGNORECASE
        )
        m = re.search(r"(\d+(?:\.\d+)?)", clean)
        if m:
            threshold = float(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)", text)
        if m:
            threshold = float(m.group(1))

    comparison = None
    for op, pat in _COMPARISON_PATTERNS:
        if pat.search(text):
            comparison = op
            break

    return indicator_name, threshold, comparison


def _build_block_when(indicator, threshold, comparison, direction) -> dict:
    block_when: dict = {}
    if indicator and threshold is not None:
        if comparison in ("<", "<="):
            block_when[f"{indicator}_lt"] = threshold
        elif comparison in (">", ">="):
            block_when[f"{indicator}_gt"] = threshold
        else:
            block_when[f"{indicator}_lt" if direction == "SELL" else f"{indicator}_gt"] = threshold
    if "ema_stack" in (indicator or ""):
        block_when["ema_stacked_bear" if direction == "SELL" else "ema_stacked_bull"] = False
    return block_when


def _line_to_hypothesis(
    line: str, date_compact: str, seq: int, review_date_iso: str
) -> dict | None:
    has_block = "block" in line.lower()
    has_when = "when" in line.lower()
    has_indicator = any(pat.search(line) for _, pat in _INDICATOR_PATTERNS)
    if not (has_block or (has_when and has_indicator)):
        return None

    symbol = _parse_symbol(line)
    if not symbol:
        return None

    direction = _parse_direction(line)
    indicator, threshold, comparison = _parse_indicator(line)
    block_when = _build_block_when(indicator, threshold, comparison, direction)

    strategy_m = re.search(r"VP-\d+[^\(,\.]+", line, re.IGNORECASE)
    strategy = (
        strategy_m.group(0).strip()
        if strategy_m
        else ("VP-01 Counter Bull Trap" if direction == "SELL" else "VP-02 Counter Bear Trap")
    )

    return {
        "id":         f"HYP-{date_compact}-{seq:03d}",
        "created_at": _now_ist(),
        "scope":      "futures",
        "strategy":   strategy,
        "symbol":     symbol,
        "direction":  direction,
        "claim":      line.strip(),
        "filter":     {"block_when": block_when},
        "evidence":   {"dates": [review_date_iso], "notes": [line.strip()]},
        "status":     "proposed",
        "created_by": "manual",
        "source_review_date": review_date_iso,
    }


def _extract_hypotheses_section(review_text: str) -> list[str]:
    m = re.search(
        r"## Possible Hypotheses\s*\n(.*?)(?=\n## |\Z)",
        review_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    lines = []
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith(("-", "*")):
            content = stripped.lstrip("-*").strip()
            if content:
                lines.append(content)
    return lines


def mode_a_parse(
    review_text: str,
    review_date_iso: str,
    date_compact: str,
    seq_start: int,
    max_hypotheses: int,
) -> list[dict]:
    lines = _extract_hypotheses_section(review_text)
    print(f"  Found {len(lines)} hypothesis line(s) in review.")
    hypotheses = []
    seq = seq_start
    for line in lines:
        if len(hypotheses) >= max_hypotheses:
            break
        hyp = _line_to_hypothesis(line, date_compact, seq, review_date_iso)
        if hyp is None:
            print(f"  Skipping non-parseable line: {line[:80]}")
            continue
        ok, reason = validate_hypothesis(hyp)
        if not ok:
            print(f"  Validation failed: {reason}", file=sys.stderr)
            continue
        hypotheses.append(hyp)
        seq += 1
    return hypotheses


# ── Write helpers ─────────────────────────────────────────────────────────────


def write_hypothesis_json(hyp: dict, path: Path) -> None:
    content = json.dumps(hyp, indent=2, ensure_ascii=False)
    if len(content.encode()) > MAX_OUTPUT_BYTES:
        raise ValueError(f"Hypothesis JSON exceeds {MAX_OUTPUT_BYTES} bytes")
    path.write_text(content, encoding="utf-8")


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="BlitzTrader futures hypothesis proposer (Gemini post-market only)"
    )
    parser.add_argument(
        "--review",
        default=None,
        help="Path to a specific compact daily review markdown file",
    )
    parser.add_argument(
        "--wiki-dir",
        default=None,
        help="Wiki directory (default: {repo_root}/wiki)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date YYYY-MM-DD to select specific review",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help=(
            "Skip Gemini and use deterministic hypothesis parsing only. "
            "Intended for dry runs or manual operation."
        ),
    )
    parser.add_argument(
        "--max-hypotheses",
        type=int,
        default=3,
        metavar="N",
        help="Maximum number of hypotheses to propose per run (default: 3)",
    )
    args = parser.parse_args()

    if args.max_hypotheses < 1:
        print("ERROR: --max-hypotheses must be at least 1", file=sys.stderr)
        sys.exit(1)

    wiki_dir = (
        Path(args.wiki_dir).expanduser().resolve()
        if args.wiki_dir
        else _REPO_ROOT / "wiki"
    )

    # Resolve review path
    if args.review:
        review_path = Path(args.review).expanduser().resolve()
    elif args.date:
        review_path = wiki_dir / "daily_reviews" / f"{args.date}.md"
    else:
        review_path = find_latest_review(wiki_dir)
        if review_path is None:
            print(
                "ERROR: No daily reviews found in wiki/daily_reviews/. "
                "Run evaluate_futures_day.py first.",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"[propose_futures_hypotheses] Review: {review_path}")

    review_text = load_review(review_path)
    date_compact = extract_review_date(review_path)
    try:
        review_date_iso = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    except Exception:
        review_date_iso = date_compact

    hypotheses_dir = wiki_dir / "hypotheses"
    hypotheses_dir.mkdir(parents=True, exist_ok=True)
    seq_start = _next_seq(hypotheses_dir, date_compact)

    hypotheses: list[dict] = []

    if args.no_llm:
        # ── Mode A: deterministic parse (no Gemini, no artifact) ─────────────
        print("[propose_futures_hypotheses] --no-llm: deterministic parse mode")
        hypotheses = mode_a_parse(
            review_text, review_date_iso, date_compact,
            seq_start, args.max_hypotheses,
        )

    else:
        # ── Default: Gemini (post-market only, one attempt) ───────────────────

        # Step 1: extract strategy names seen in the review.  Fail closed if
        # none found — we must not let Gemini invent strategy names.
        strategies_in_review = extract_strategies_from_review(review_text)
        if not strategies_in_review:
            reason = "no supported strategy names found in review text"
            print(
                f"[propose_futures_hypotheses] {reason}",
                file=sys.stderr,
            )
            write_no_proposals_artifact(wiki_dir, review_date_iso, reason, llm_attempted=True)
            sys.exit(0)

        print(
            "[propose_futures_hypotheses] Attempting Gemini hypothesis proposal "
            f"(max {args.max_hypotheses}, "
            f"strategies seen: {sorted(strategies_in_review)})..."
        )

        # Step 2: one Gemini attempt
        validated_raws, failure_reason = call_gemini(
            review_text, review_date_iso, date_compact,
            args.max_hypotheses, strategies_in_review,
        )

        if failure_reason:
            # Clean exit — no fake hypotheses, no fallback, write audit artifact
            print(
                f"[propose_futures_hypotheses] No proposals generated: {failure_reason}",
                file=sys.stderr,
            )
            write_no_proposals_artifact(
                wiki_dir, review_date_iso, failure_reason, llm_attempted=True
            )
            print("[propose_futures_hypotheses] Workflow continues unaffected.")
            sys.exit(0)

        # Assign canonical IDs and assemble full hypothesis dicts
        seq = seq_start
        for raw in validated_raws:
            hyp_id = f"HYP-{date_compact}-{seq:03d}"
            hyp = assemble_hypothesis(raw, hyp_id, review_date_iso)
            hypotheses.append(hyp)
            seq += 1

        print(f"  Gemini produced {len(hypotheses)} valid hypothesis/hypotheses.")

    # ── Write files ───────────────────────────────────────────────────────────
    if not hypotheses:
        print("[propose_futures_hypotheses] No hypotheses to write.")
        sys.exit(0)

    written = []
    for hyp in hypotheses:
        hyp_id = hyp["id"]
        output_path = hypotheses_dir / f"{hyp_id}.json"
        try:
            write_hypothesis_json(hyp, output_path)
        except Exception as exc:
            print(f"  ERROR writing {hyp_id}: {exc}", file=sys.stderr)
            continue
        written.append(output_path)
        print(f"  Written: {output_path}")

    print(
        f"[propose_futures_hypotheses] Done. "
        f"{len(written)} hypothesis file(s) created."
    )


if __name__ == "__main__":
    main()
