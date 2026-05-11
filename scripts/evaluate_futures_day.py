#!/usr/bin/env python3
"""
scripts/evaluate_futures_day.py
---------------------------------
Reads runtime artifacts for a given trading day and produces a daily review
markdown in wiki/daily_reviews/YYYY-MM-DD.md.

Usage:
    python3 scripts/evaluate_futures_day.py --date 2026-05-09
    python3 scripts/evaluate_futures_day.py --date 2026-05-09 --runtime-root /mnt/blitztrader-drive
"""

import argparse
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# ── Constants ──────────────────────────────────────────────────────────────────

FUTURES_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}
# Match bare logical names and common tsym patterns like NIFTY28APR26F
_FUTURES_TSYM_RE = re.compile(
    r"\b(NIFTY|BANKNIFTY|FINNIFTY)(\d{2}[A-Z]{3}\d{2}F)?\b", re.IGNORECASE
)

MAX_OUTPUT_BYTES = 1_000_000  # 1 MB


# ── Helpers ────────────────────────────────────────────────────────────────────


def parse_date_arg(date_str: str) -> date:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        print(f"ERROR: Invalid date format '{date_str}'. Use YYYY-MM-DD.")
        sys.exit(1)


def is_futures_symbol(symbol: str) -> bool:
    """Return True if symbol is or contains a futures-relevant name."""
    if not symbol:
        return False
    sym = symbol.strip().upper()
    # Bare logical
    for fs in FUTURES_SYMBOLS:
        if sym == fs or sym.startswith(fs):
            return True
    return False


def _is_futures_line(line: str) -> bool:
    """Quick check: does this line mention a futures symbol?"""
    return bool(_FUTURES_TSYM_RE.search(line))


# ── Journal parsing ────────────────────────────────────────────────────────────


def parse_journal(journal_path: Path) -> dict:
    """
    Parse futures-related entries from a journal markdown file.

    Expects the real BlitzTrader journal format produced by tools/journal_writer.py:

        ### HH:MM:SS — ACTION
        **Market context:** ...
        **Instrument:** NIFTY28APR26F
        **Strategy applied:** VP-01 ...
        **Reasoning:** ...

    Returns dict with keys: executed, rejected, raw_lines.
    """
    result = {"executed": [], "rejected": [], "raw_lines": []}
    if not journal_path.exists():
        return result

    try:
        text = journal_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: Could not read journal {journal_path}: {e}", file=sys.stderr)
        return result

    # Regex for entry headings: ### HH:MM:SS — ACTION
    HEADING_RE = re.compile(
        r"^###\s+(\d{1,2}:\d{2}:\d{2})\s+[-—]\s+(\w+)", re.IGNORECASE
    )
    # Regex for bold key-value pairs.
    # The real journal format is **Key:** value — colon sits INSIDE the closing **
    # i.e. the literal text is **Key:** followed by a space and the value.
    KV_RE = re.compile(r"^\*\*([^*:]+):\*\*\s*(.+)")

    # Key normalisation map: raw bold label → canonical entry key
    KEY_MAP = {
        "market context": "market_context",
        "instrument": "symbol",
        "strategy applied": "strategy",
        "reasoning": "reason",
    }

    # Actions to completely ignore (exit / eod / admin)
    IGNORE_ACTIONS = {"EXIT", "FORCE_EXIT", "EOD_REPORT", "EOD", "ABORT"}

    entries: list[dict] = []
    current: dict | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        result["raw_lines"].append(line)

        heading_m = HEADING_RE.match(line)
        if heading_m:
            # Save the previous entry before starting a new one
            if current is not None:
                entries.append(current)
            time_val = heading_m.group(1)
            action_val = heading_m.group(2).upper()
            current = {"time": time_val, "action": action_val}
            continue

        if current is None:
            # Lines before the first heading are ignored
            continue

        kv_m = KV_RE.match(line)
        if kv_m:
            raw_key = kv_m.group(1).strip().lower()
            val = kv_m.group(2).strip()
            canonical = KEY_MAP.get(raw_key, raw_key.replace(" ", "_"))
            current[canonical] = val

    # Flush last entry
    if current is not None:
        entries.append(current)

    # Filter to futures-only and categorize by action
    for e in entries:
        action_raw = e.get("action", "")
        sym = e.get("symbol", "")

        if action_raw in IGNORE_ACTIONS:
            continue

        if not is_futures_symbol(sym):
            continue

        if action_raw == "REJECT":
            result["rejected"].append(e)
        elif action_raw in ("ENTER_LONG", "ENTER_SHORT"):
            result["executed"].append(e)
        # Any other unknown actions are silently dropped

    return result


# ── Log parsing ────────────────────────────────────────────────────────────────


def parse_log(log_path: Path) -> list:
    """
    Scan log file for ERROR/WARNING lines related to futures.
    Returns list of compact message strings (NOT raw log lines).
    """
    messages = []
    if not log_path.exists():
        return messages

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: Could not read log {log_path}: {e}", file=sys.stderr)
        return messages

    # Only extract error/warning lines mentioning futures symbols
    for line in text.splitlines():
        upper = line.upper()
        if ("ERROR" in upper or "WARNING" in upper) and _is_futures_line(line):
            # Extract a compact summary — not the raw line
            # Strip timestamps and log-level prefixes, keep message part
            # Pattern: "2026-05-09 09:15:23,456 ERROR blitztrader - Some message here"
            msg_m = re.search(r"(ERROR|WARNING)[^\-]*[-:]\s*(.+)", line, re.IGNORECASE)
            if msg_m:
                level = msg_m.group(1).upper()
                msg = msg_m.group(2).strip()[:200]  # cap at 200 chars
                messages.append(f"[{level}] {msg}")
            else:
                # Fallback: strip timestamp prefix, cap length
                compact = re.sub(r"^\S+\s+\S+\s+", "", line).strip()[:200]
                messages.append(compact)

    return messages[:50]  # cap at 50 log messages


# ── Data export parsing ────────────────────────────────────────────────────────


def parse_indicators_md(indicators_path: Path) -> list:
    """Extract futures-relevant indicator lines."""
    lines = []
    if not indicators_path.exists():
        return lines
    try:
        text = indicators_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return lines

    for line in text.splitlines():
        if _is_futures_line(line):
            lines.append(line.strip()[:300])

    return lines[:30]


def parse_signals_md(signals_path: Path) -> dict:
    """
    Parse strategy_signals.md for futures signals.
    Returns dict with keys: executed, rejected.
    """
    result = {"executed": [], "rejected": []}
    if not signals_path.exists():
        return result

    try:
        text = signals_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return result

    for line in text.splitlines():
        if not _is_futures_line(line):
            continue
        upper = line.upper()
        entry = {"raw": line.strip()[:200]}
        # Extract symbol
        sym_m = _FUTURES_TSYM_RE.search(line)
        if sym_m:
            entry["symbol"] = sym_m.group(0).upper()

        if "REJECT" in upper or "SKIP" in upper or "BLOCK" in upper:
            result["rejected"].append(entry)
        elif any(x in upper for x in ("BUY", "SELL", "ENTER_LONG", "ENTER_SHORT", "EXECUTE")):
            result["executed"].append(entry)

    return result


# ── Statistics ────────────────────────────────────────────────────────────────


def compute_trade_stats(executed: list) -> dict:
    """Compute wins, losses, net P&L from executed trade list."""
    wins = 0
    losses = 0
    net_pnl = None
    pnl_values = []

    for t in executed:
        pnl_raw = t.get("p&l", t.get("pnl", t.get("realized_pnl", None)))
        if pnl_raw:
            # Parse ₹1,234.56 or -500.00
            pnl_str = re.sub(r"[₹,\s]", "", str(pnl_raw))
            try:
                pnl_val = float(pnl_str)
                pnl_values.append(pnl_val)
                if pnl_val > 0:
                    wins += 1
                else:
                    losses += 1
            except ValueError:
                pass

    if pnl_values:
        net_pnl = sum(pnl_values)

    return {
        "total": len(executed),
        "wins": wins,
        "losses": losses,
        "net_pnl": net_pnl,
        "pnl_known": len(pnl_values) > 0,
    }


# ── Pattern detection ─────────────────────────────────────────────────────────


def detect_patterns(executed: list, rejected: list) -> list:
    """Detect repeated failure modes or patterns from rejected/executed trades."""
    patterns = []

    # Count rejected by symbol + reason
    reject_counts: dict = {}
    for r in rejected:
        sym = r.get("symbol", "UNKNOWN")
        reason = r.get("reason", r.get("reason:", "unknown reason"))
        key = f"{sym}: {reason}"
        reject_counts[key] = reject_counts.get(key, 0) + 1

    for key, count in reject_counts.items():
        if count >= 2:
            patterns.append(f"{count}x {key}")

    # Check for repeated losses on same symbol
    loss_by_sym: dict = {}
    for t in executed:
        sym = t.get("symbol", "UNKNOWN")
        pnl_raw = t.get("p&l", t.get("pnl", ""))
        pnl_str = re.sub(r"[₹,\s]", "", str(pnl_raw))
        try:
            pnl_val = float(pnl_str)
            if pnl_val < 0:
                loss_by_sym[sym] = loss_by_sym.get(sym, 0) + 1
        except ValueError:
            pass

    for sym, count in loss_by_sym.items():
        if count >= 2:
            patterns.append(f"{count} consecutive losses on {sym}")

    return patterns


def generate_hypotheses(executed: list, rejected: list, patterns: list) -> list:
    """Generate hypothesis suggestions from patterns."""
    hyps = []

    for p in patterns:
        # Rejection-based hypotheses
        if "NIFTY SELL" in p or "BANKNIFTY SELL" in p or "FINNIFTY SELL" in p:
            sym_m = re.match(r"\d+x\s+(\w+):", p)
            sym = sym_m.group(1) if sym_m else "NIFTY"
            hyps.append(
                f"Block {sym} SELL when RSI14 < 20 (oversold extension — prevents shorting into exhaustion)"
            )
        elif "NIFTY BUY" in p or "BANKNIFTY BUY" in p or "FINNIFTY BUY" in p:
            sym_m = re.match(r"\d+x\s+(\w+):", p)
            sym = sym_m.group(1) if sym_m else "NIFTY"
            hyps.append(
                f"Block {sym} BUY when RSI14 > 80 (overbought extension — prevents buying into exhaustion)"
            )

    # Rejected signals → block filter hypotheses
    reject_reasons: dict = {}
    for r in rejected:
        sym = r.get("symbol", "")
        direction = r.get("direction", r.get("action", ""))
        reason = r.get("reason", r.get("reason:", ""))
        if sym and reason:
            key = f"{sym}|{direction}|{reason}"
            reject_reasons[key] = reject_reasons.get(key, 0) + 1

    for key, count in reject_reasons.items():
        parts = key.split("|")
        if len(parts) == 3:
            sym, direction, reason = parts
            reason_lower = reason.lower()
            if "ema" in reason_lower and count >= 1:
                hyps.append(
                    f"Block {sym} {direction} when EMA stack is bearish (signals rejected: {count}x for EMA filter)"
                )
            elif "adx" in reason_lower and count >= 1:
                hyps.append(
                    f"Block {sym} {direction} when ADX14 < 25 (low trend strength, rejected {count}x)"
                )

    # Deduplicate
    seen = set()
    unique_hyps = []
    for h in hyps:
        if h not in seen:
            seen.add(h)
            unique_hyps.append(h)

    return unique_hyps


# ── Output formatting ─────────────────────────────────────────────────────────


def build_review_markdown(
    review_date: date,
    executed: list,
    rejected: list,
    stats: dict,
    patterns: list,
    hypotheses: list,
    log_messages: list,
    indicator_notes: list,
) -> str:
    date_str = review_date.isoformat()
    lines = []

    lines.append(f"# Futures Daily Review — {date_str}")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append(f"- Date: {date_str}")
    lines.append(f"- Futures trades executed: {stats['total']}")
    if stats["total"] > 0:
        win_rate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
        lines.append(f"- Wins: {stats['wins']} | Losses: {stats['losses']} | Win rate: {win_rate:.0f}%")
        if stats["pnl_known"] and stats["net_pnl"] is not None:
            lines.append(f"- Net P&L: ₹{stats['net_pnl']:+,.2f}")
        else:
            lines.append("- Net P&L: N/A (P&L data not available in journal)")
    else:
        lines.append("- Wins: 0 | Losses: 0 | Win rate: N/A")
        lines.append("- Net P&L: N/A")
    lines.append(f"- Rejected signals: {len(rejected)}")
    lines.append("")

    # Log issues (compact, no raw log content)
    if log_messages:
        lines.append("## Log Issues")
        for msg in log_messages[:20]:
            lines.append(f"- {msg}")
        lines.append("")

    # Indicator context
    if indicator_notes:
        lines.append("## Indicator Context")
        for note in indicator_notes[:15]:
            lines.append(f"- {note}")
        lines.append("")

    # Executed trades table
    lines.append("## Executed Trades")
    if executed:
        lines.append("| Time | Symbol | Direction | Strategy | P&L |")
        lines.append("|------|--------|-----------|----------|-----|")
        for t in executed:
            time_val = t.get("time", t.get("timestamp", "—"))
            sym = t.get("symbol", "—")
            direction = t.get("direction", t.get("action", "—"))
            strategy = t.get("strategy", "—")
            pnl = t.get("p&l", t.get("pnl", t.get("realized_pnl", "—")))
            lines.append(f"| {time_val} | {sym} | {direction} | {strategy} | {pnl} |")
    else:
        lines.append("_No futures trades executed on this date._")
    lines.append("")

    # Rejected signals table
    lines.append("## Rejected Signals")
    if rejected:
        lines.append("| Time | Symbol | Strategy | Reason |")
        lines.append("|------|--------|----------|--------|")
        for r in rejected:
            time_val = r.get("time", r.get("timestamp", "—"))
            sym = r.get("symbol", "—")
            strategy = r.get("strategy", "—")
            reason = r.get("reason", "—")
            lines.append(f"| {time_val} | {sym} | {strategy} | {reason} |")
    else:
        lines.append("_No rejected futures signals on this date._")
    lines.append("")

    # Patterns observed
    lines.append("## Patterns Observed")
    if patterns:
        for p in patterns:
            lines.append(f"- {p}")
    else:
        lines.append("- No repeated failure modes detected.")
    lines.append("")

    # Possible hypotheses
    lines.append("## Possible Hypotheses")
    if hypotheses:
        for h in hypotheses:
            lines.append(f"- {h}")
    else:
        lines.append("- Insufficient data to propose hypotheses for this date.")
        if stats["total"] == 0:
            lines.append("  - Consider reviewing dates with more trading activity.")
    lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="BlitzTrader futures daily review generator"
    )
    parser.add_argument("--date", required=True, help="Date to review (YYYY-MM-DD)")
    parser.add_argument(
        "--runtime-root",
        default=None,
        help="Runtime storage root (default: $RUNTIME_STORAGE_DIR or repo root)",
    )
    parser.add_argument(
        "--wiki-dir",
        default=None,
        help="Wiki directory (default: {repo_root}/wiki)",
    )
    args = parser.parse_args()

    review_date = parse_date_arg(args.date)
    date_compact = review_date.strftime("%Y%m%d")

    # Resolve paths
    repo_root = _REPO_ROOT
    if args.runtime_root:
        runtime_root = Path(args.runtime_root).expanduser().resolve()
    else:
        env_runtime = os.environ.get("RUNTIME_STORAGE_DIR")
        runtime_root = Path(env_runtime) if env_runtime else repo_root

    wiki_dir = Path(args.wiki_dir).expanduser().resolve() if args.wiki_dir else repo_root / "wiki"

    print(f"[evaluate_futures_day] Date: {review_date.isoformat()}")
    print(f"[evaluate_futures_day] Runtime root: {runtime_root}")
    print(f"[evaluate_futures_day] Wiki dir: {wiki_dir}")

    # Define input paths
    journal_path = runtime_root / "journals" / f"{date_compact}.md"
    log_path = runtime_root / "logs" / f"blitztrader_{date_compact}.log"
    indicators_path = runtime_root / "data_exports" / date_compact / "indicators.md"
    signals_path = runtime_root / "data_exports" / date_compact / "strategy_signals.md"

    # --- Guard: skip pairs files (by path name check) ---
    def is_pairs_file(p: Path) -> bool:
        name = p.name.lower()
        return any(x in name for x in ("_pairs.md", "_pairs.json", "pairs_state.json"))

    # Read sources
    all_executed = []
    all_rejected = []
    log_messages = []
    indicator_notes = []

    # 1. Journal
    if not is_pairs_file(journal_path):
        print(f"[evaluate_futures_day] Reading journal: {journal_path}")
        journal_data = parse_journal(journal_path)
        all_executed.extend(journal_data["executed"])
        all_rejected.extend(journal_data["rejected"])
        if not journal_path.exists():
            print("  (journal not found, skipping)")

    # 2. Log file
    if log_path.exists() and not is_pairs_file(log_path):
        print(f"[evaluate_futures_day] Reading log: {log_path}")
        log_messages = parse_log(log_path)
        print(f"  Found {len(log_messages)} futures-related error/warning messages")
    else:
        print(f"[evaluate_futures_day] Log not found (skipping): {log_path}")

    # 3. Indicators
    if indicators_path.exists() and not is_pairs_file(indicators_path):
        print(f"[evaluate_futures_day] Reading indicators: {indicators_path}")
        indicator_notes = parse_indicators_md(indicators_path)
    else:
        print(f"[evaluate_futures_day] Indicators not found (skipping): {indicators_path}")

    # 4. Strategy signals
    if signals_path.exists() and not is_pairs_file(signals_path):
        print(f"[evaluate_futures_day] Reading signals: {signals_path}")
        signals_data = parse_signals_md(signals_path)
        # Merge signals — deduplicate by raw content
        existing_syms = {(t.get("symbol", ""), t.get("time", "")) for t in all_executed}
        for s in signals_data["executed"]:
            if (s.get("symbol", ""), s.get("time", "")) not in existing_syms:
                all_executed.append(s)
        for s in signals_data["rejected"]:
            all_rejected.append(s)
    else:
        print(f"[evaluate_futures_day] Signals not found (skipping): {signals_path}")

    # Filter: only futures symbols
    all_executed = [t for t in all_executed if is_futures_symbol(t.get("symbol", ""))]
    all_rejected = [r for r in all_rejected if is_futures_symbol(r.get("symbol", ""))]

    # Compute stats
    stats = compute_trade_stats(all_executed)
    patterns = detect_patterns(all_executed, all_rejected)
    hypotheses = generate_hypotheses(all_executed, all_rejected, patterns)

    # Build output
    review_md = build_review_markdown(
        review_date=review_date,
        executed=all_executed,
        rejected=all_rejected,
        stats=stats,
        patterns=patterns,
        hypotheses=hypotheses,
        log_messages=log_messages,
        indicator_notes=indicator_notes,
    )

    # Write output
    output_dir = wiki_dir / "daily_reviews"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{review_date.isoformat()}.md"

    encoded = review_md.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        print(
            f"WARNING: Output exceeds 1 MB ({len(encoded)} bytes). Truncating.",
            file=sys.stderr,
        )
        review_md = review_md.encode("utf-8")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        review_md += "\n\n_[Output truncated: exceeded 1 MB limit]_\n"

    output_path.write_text(review_md, encoding="utf-8")
    print(f"[evaluate_futures_day] Review written: {output_path}")
    print(f"  Trades executed: {stats['total']}, Rejected: {len(all_rejected)}")


if __name__ == "__main__":
    main()
