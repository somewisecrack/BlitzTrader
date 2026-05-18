#!/usr/bin/env python3
"""
scripts/evaluate_futures_day.py
---------------------------------
Reads runtime artifacts for a given trading day and produces a daily review
markdown in wiki/daily_reviews/YYYY-MM-DD.md.

Source-of-truth priority:
  1. runtime/live_state.json  "trades" array  (actual executed trades with P&L)
  2. journals/YYYYMMDD.md     ENTER_LONG / ENTER_SHORT entries (fallback)

NEVER counts emitted_signal_keys, placeholder rows, or pairs trades.

Usage:
    python3 scripts/evaluate_futures_day.py --date 2026-05-09
    python3 scripts/evaluate_futures_day.py --date 2026-05-09 --runtime-root /mnt/blitztrader-drive
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, date, timezone
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


def _epoch_to_date(epoch: float) -> date:
    """Convert a POSIX epoch float to a date in IST (UTC+5:30)."""
    try:
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        return datetime.fromtimestamp(epoch, tz=IST).date()
    except ImportError:
        # Fallback: UTC+5:30 offset
        IST_OFFSET = 5.5 * 3600
        return datetime.utcfromtimestamp(epoch + IST_OFFSET).date()


def _epoch_to_time_str(epoch: float) -> str:
    """Convert a POSIX epoch float to HH:MM:SS in IST."""
    try:
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        return datetime.fromtimestamp(epoch, tz=IST).strftime("%H:%M:%S")
    except ImportError:
        IST_OFFSET = 5.5 * 3600
        return datetime.utcfromtimestamp(epoch + IST_OFFSET).strftime("%H:%M:%S")


# ── live_state.json parsing (PRIMARY source) ───────────────────────────────────


def parse_live_state(live_state_path: Path, review_date: date) -> list[dict]:
    """
    Parse runtime/live_state.json to extract executed futures trades.

    Returns a list of trade dicts with keys:
      time, symbol, direction, strategy, pnl, quantity, entry_price, exit_price

    Only returns trades whose entry_time falls on review_date (IST).
    Only returns futures trades (NIFTY/BANKNIFTY/FINNIFTY).
    Never returns placeholder rows.
    """
    if not live_state_path.exists():
        return []
    try:
        data = json.loads(live_state_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  WARNING: Could not parse {live_state_path}: {e}", file=sys.stderr)
        return []

    raw_trades = data.get("trades", [])
    if not isinstance(raw_trades, list):
        return []

    result = []
    for t in raw_trades:
        if not isinstance(t, dict):
            continue
        symbol = str(t.get("symbol", "")).strip()
        if not is_futures_symbol(symbol):
            continue

        # Filter to the review date using entry_time epoch
        entry_epoch = t.get("entry_time")
        if entry_epoch is not None:
            try:
                trade_date = _epoch_to_date(float(entry_epoch))
                if trade_date != review_date:
                    continue
            except (TypeError, ValueError):
                pass  # no entry_time → include anyway (edge case)

        direction = str(t.get("direction", "")).upper()
        pnl_raw = t.get("pnl")
        try:
            pnl_val = float(pnl_raw) if pnl_raw is not None else None
        except (TypeError, ValueError):
            pnl_val = None

        time_str = "—"
        if entry_epoch is not None:
            try:
                time_str = _epoch_to_time_str(float(entry_epoch))
            except (TypeError, ValueError):
                pass

        result.append({
            "time": time_str,
            "symbol": symbol,
            "direction": direction,
            "strategy": t.get("strategy", "—"),
            "pnl": pnl_val,
            "quantity": t.get("quantity"),
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price"),
        })

    return result


# ── Journal parsing (FALLBACK source for executed trades + rejected signals) ───


def parse_journal(journal_path: Path) -> dict:
    """
    Parse futures-related entries from a journal markdown file.

    ONLY counts:
      - ENTER_LONG / ENTER_SHORT actions as executed trades
      - REJECT actions as rejected signals

    NEVER counts emitted_signal_keys or non-trade log lines.
    Rows are only included if they have a valid futures symbol.

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
    IGNORE_ACTIONS = {"EXIT", "FORCE_EXIT", "EOD_REPORT", "EOD", "ABORT", "START"}

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

        # Only include entries with a valid, non-placeholder symbol
        if not sym or "—" in sym or not is_futures_symbol(sym):
            continue

        if action_raw == "REJECT":
            # Only include if strategy and reason are present (not placeholder)
            strategy = e.get("strategy", "")
            reason = e.get("reason", e.get("reason:", ""))
            if not strategy or "—" in strategy:
                continue
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


# ── Statistics ────────────────────────────────────────────────────────────────


def compute_trade_stats(executed: list) -> dict:
    """Compute wins, losses, net P&L from executed trade list."""
    wins = 0
    losses = 0
    net_pnl = None
    pnl_values = []

    for t in executed:
        pnl_raw = t.get("pnl")
        if pnl_raw is not None:
            if isinstance(pnl_raw, (int, float)):
                pnl_val = float(pnl_raw)
            else:
                # Parse ₹1,234.56 or -500.00
                pnl_str = re.sub(r"[₹,\s]", "", str(pnl_raw))
                try:
                    pnl_val = float(pnl_str)
                except ValueError:
                    continue
            pnl_values.append(pnl_val)
            if pnl_val > 0:
                wins += 1
            else:
                losses += 1

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
    """Detect repeated failure modes or patterns from rejected/executed trades.

    Only derives patterns from actual executed trades and actual rejected signals.
    Never generates placeholder patterns or uses "unknown reason".
    """
    patterns = []

    # Count rejected by symbol + reason (only concrete reasons)
    reject_counts: dict = {}
    for r in rejected:
        sym = r.get("symbol", "")
        reason = r.get("reason", r.get("reason:", ""))
        # Skip entries with placeholder/empty reasons
        if not sym or not reason or "—" in reason or "unknown" in reason.lower():
            continue
        key = f"{sym}: {reason}"
        reject_counts[key] = reject_counts.get(key, 0) + 1

    for key, count in reject_counts.items():
        if count >= 2:
            patterns.append(f"{count}x {key}")

    # Check for repeated losses on same symbol
    loss_by_sym: dict = {}
    for t in executed:
        sym = t.get("symbol", "")
        if not sym:
            continue
        pnl_raw = t.get("pnl")
        if pnl_raw is None:
            continue
        if isinstance(pnl_raw, (int, float)):
            pnl_val = float(pnl_raw)
        else:
            pnl_str = re.sub(r"[₹,\s]", "", str(pnl_raw))
            try:
                pnl_val = float(pnl_str)
            except ValueError:
                continue
        if pnl_val < 0:
            loss_by_sym[sym] = loss_by_sym.get(sym, 0) + 1

    for sym, count in loss_by_sym.items():
        if count >= 2:
            patterns.append(f"{count} repeated losses on {sym}")

    return patterns


# ── Output formatting ─────────────────────────────────────────────────────────


def build_review_markdown(
    review_date: date,
    executed: list,
    rejected: list,
    stats: dict,
    patterns: list,
    log_messages: list,
    indicator_notes: list,
    source_note: str = "",
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
            lines.append("- Net P&L: N/A (P&L data not available)")
    else:
        lines.append("- Wins: 0 | Losses: 0 | Win rate: N/A")
        lines.append("- Net P&L: N/A")
    lines.append(f"- Rejected signals: {len(rejected)}")
    if source_note:
        lines.append(f"- _Source: {source_note}_")
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
            time_val = t.get("time", "—")
            # Skip rows where required fields are placeholders
            sym = t.get("symbol", "")
            direction = t.get("direction", "")
            if not sym or not direction or "—" in sym:
                continue
            strategy = t.get("strategy") or "—"
            pnl_raw = t.get("pnl")
            if pnl_raw is None:
                pnl_str = "—"
            elif isinstance(pnl_raw, (int, float)):
                pnl_str = f"₹{pnl_raw:+,.2f}"
            else:
                pnl_str = str(pnl_raw)
            lines.append(f"| {time_val} | {sym} | {direction} | {strategy} | {pnl_str} |")
    else:
        lines.append("_No futures trades executed on this date._")
    lines.append("")

    # Rejected signals table
    lines.append("## Rejected Signals")
    if rejected:
        lines.append("| Time | Symbol | Strategy | Reason |")
        lines.append("|------|--------|----------|--------|")
        for r in rejected:
            time_val = r.get("time", "—")
            sym = r.get("symbol", "")
            strategy = r.get("strategy", "")
            reason = r.get("reason", r.get("reason:", ""))
            # Skip placeholder rows
            if not sym or not strategy or not reason:
                continue
            if "—" in sym or "—" in strategy:
                continue
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
    live_state_path = runtime_root / "live_state.json"
    journal_path = runtime_root / "journals" / f"{date_compact}.md"
    log_path = runtime_root / "logs" / f"blitztrader_{date_compact}.log"
    indicators_path = runtime_root / "data_exports" / date_compact / "indicators.md"

    # ── SOURCE 1: live_state.json (primary — actual executed trades with P&L) ──
    all_executed: list[dict] = []
    all_rejected: list[dict] = []
    log_messages: list[str] = []
    indicator_notes: list[str] = []
    source_note = ""

    if live_state_path.exists():
        print(f"[evaluate_futures_day] Reading live_state.json: {live_state_path}")
        live_trades = parse_live_state(live_state_path, review_date)
        if live_trades:
            all_executed = live_trades
            source_note = "live_state.json (executed trades with realized P&L)"
            print(f"  Found {len(all_executed)} futures trades in live_state.json")
        else:
            print("  No futures trades found in live_state.json for this date")
    else:
        print(f"[evaluate_futures_day] live_state.json not found: {live_state_path}")

    # ── SOURCE 2: Journal (fallback for executed trades if live_state.json empty) ──
    # Always parse journal for REJECTED signals (not recorded in live_state.json)
    print(f"[evaluate_futures_day] Reading journal: {journal_path}")
    journal_data = parse_journal(journal_path)

    if not all_executed and journal_data["executed"]:
        # Fallback: use journal ENTER_LONG/ENTER_SHORT entries
        all_executed = journal_data["executed"]
        source_note = "journal (ENTER_LONG/ENTER_SHORT entries — P&L may be unavailable)"
        print(f"  Using journal fallback: {len(all_executed)} executed trade(s)")
    elif not journal_path.exists():
        print("  (journal not found, skipping)")

    # Rejected signals always come from journal
    all_rejected = journal_data["rejected"]
    print(f"  Found {len(all_rejected)} rejected signal(s) in journal")

    # ── SOURCE 3: Log file (error/warning messages only) ──
    if log_path.exists():
        print(f"[evaluate_futures_day] Reading log: {log_path}")
        log_messages = parse_log(log_path)
        print(f"  Found {len(log_messages)} futures-related error/warning messages")
    else:
        print(f"[evaluate_futures_day] Log not found (skipping): {log_path}")

    # ── SOURCE 4: Indicators ──
    if indicators_path.exists():
        print(f"[evaluate_futures_day] Reading indicators: {indicators_path}")
        indicator_notes = parse_indicators_md(indicators_path)
    else:
        print(f"[evaluate_futures_day] Indicators not found (skipping): {indicators_path}")

    # Final filter: only futures symbols (should already be filtered but be safe)
    all_executed = [t for t in all_executed if is_futures_symbol(t.get("symbol", ""))]
    all_rejected = [r for r in all_rejected if is_futures_symbol(r.get("symbol", ""))]

    # Compute stats
    stats = compute_trade_stats(all_executed)
    patterns = detect_patterns(all_executed, all_rejected)

    # Build output
    review_md = build_review_markdown(
        review_date=review_date,
        executed=all_executed,
        rejected=all_rejected,
        stats=stats,
        patterns=patterns,
        log_messages=log_messages,
        indicator_notes=indicator_notes,
        source_note=source_note,
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
