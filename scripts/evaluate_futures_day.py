#!/usr/bin/env python3
"""
scripts/evaluate_futures_day.py
---------------------------------
Reads runtime artifacts for a given trading day and produces a daily review
markdown in wiki/daily_reviews/YYYY-MM-DD.md.

Source-of-truth priority:
  1. runtime/live_state.json  "spreads_traded" array  (closed option spreads with P&L)
  2. runtime/live_state.json  "trades" array           (legacy futures trades)
  3. journals/YYYYMMDD.md     ENTER_LONG / ENTER_SHORT entries (fallback)

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

FUTURES_SYMBOLS = {"NIFTY", "BANKNIFTY"}
# Match bare logical names and common tsym patterns like NIFTY28APR26F
_FUTURES_TSYM_RE = re.compile(
    r"\b(NIFTY|BANKNIFTY)(\d{2}[A-Z]{3}\d{2}F)?\b", re.IGNORECASE
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


def parse_spreads_from_live_state(live_state_path: Path, review_date: date) -> tuple[list[dict], list[dict]]:
    """
    Parse runtime/live_state.json to extract option-spread activity.

    Returns:
        (closed_spreads, open_spreads)
        where each spread dict has keys: spread_id, symbol, spread_type, direction,
        strategy, opened_at, closed_at, realized_pnl, close_reason, etc.

    Only returns spreads whose opened_at falls on review_date (IST).
    """
    if not live_state_path.exists():
        return [], []
    try:
        data = json.loads(live_state_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  WARNING: Could not parse {live_state_path}: {e}", file=sys.stderr)
        return [], []

    def _parse_ist_date(iso_str: str) -> date | None:
        if not iso_str:
            return None
        try:
            import pytz
            IST = pytz.timezone("Asia/Kolkata")
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            else:
                dt = dt.astimezone(IST)
            return dt.date()
        except Exception:
            return None

    closed = []
    raw_closed = data.get("spreads_traded", [])
    if isinstance(raw_closed, list):
        for s in raw_closed:
            if not isinstance(s, dict):
                continue
            opened_date = _parse_ist_date(s.get("opened_at", ""))
            if opened_date is not None and opened_date != review_date:
                continue
            closed.append(s)

    open_spreads = []
    raw_open = data.get("open_spreads", [])
    if isinstance(raw_open, list):
        for s in raw_open:
            if not isinstance(s, dict):
                continue
            opened_date = _parse_ist_date(s.get("opened_at", ""))
            if opened_date is not None and opened_date != review_date:
                continue
            open_spreads.append(s)

    return closed, open_spreads


def parse_pair_credit_state(pair_state_path: Path, review_date: date) -> tuple[list[dict], list[dict]]:
    """Parse pair-credit replacement-mode state into review spread records."""
    if not pair_state_path.exists():
        return [], []
    try:
        data = json.loads(pair_state_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  WARNING: Could not parse {pair_state_path}: {e}", file=sys.stderr)
        return [], []

    def _parse_ist_date(iso_str: str) -> date | None:
        if not iso_str:
            return None
        try:
            import pytz
            IST = pytz.timezone("Asia/Kolkata")
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            else:
                dt = dt.astimezone(IST)
            return dt.date()
        except Exception:
            return None

    def _normalize(position: dict, is_closed: bool) -> dict:
        record = {
            "spread_id": position.get("position_id", "—"),
            "symbol": position.get("pair", "—"),
            "spread_type": "PAIR_CREDIT",
            "direction": position.get("direction", "—"),
            "strategy": position.get("method") or "pair-credit",
            "opened_at": position.get("opened_at", ""),
            "closed_at": position.get("closed_at", ""),
            "close_reason": position.get("close_reason", "—"),
            "entry_margin": position.get("entry_margin"),
            "entry_net_credit": position.get("entry_net_credit"),
            "prob_profit": position.get("prob_profit"),
            "z_score": position.get("z_score"),
            "half_life": position.get("half_life"),
            "legs": position.get("legs", []),
        }
        if is_closed:
            record["realized_pnl"] = position.get("realized_pnl", 0)
        else:
            record["unrealized_pnl"] = position.get("unrealized_pnl")
        return record

    closed = []
    for position in data.get("closed_positions", []) if isinstance(data.get("closed_positions"), list) else []:
        if not isinstance(position, dict):
            continue
        opened_date = _parse_ist_date(position.get("opened_at", ""))
        if opened_date is not None and opened_date != review_date:
            continue
        closed.append(_normalize(position, is_closed=True))

    open_positions = []
    for position in data.get("open_positions", []) if isinstance(data.get("open_positions"), list) else []:
        if not isinstance(position, dict) or position.get("status") != "OPEN":
            continue
        opened_date = _parse_ist_date(position.get("opened_at", ""))
        if opened_date is not None and opened_date != review_date:
            continue
        open_positions.append(_normalize(position, is_closed=False))

    return closed, open_positions


def parse_live_state(live_state_path: Path, review_date: date) -> list[dict]:
    """
    Parse runtime/live_state.json to extract executed futures trades.

    Returns a list of trade dicts with keys:
      time, symbol, direction, strategy, pnl, quantity, entry_price, exit_price

    Only returns trades whose entry_time falls on review_date (IST).
    Only returns futures trades (NIFTY/BANKNIFTY).
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


# ── Journal-based strategy enrichment ─────────────────────────────────────────


def _enrich_trades_from_journal(trades: list[dict], journal_text: str) -> list[dict]:
    """Enrich executed trades that have missing/empty strategy with data from journal.

    For each trade with strategy == '—' or '':
      - Find ENTER_LONG / ENTER_SHORT journal entries for the same symbol+direction
        within 120 seconds of the trade's entry_time.
      - If exactly one match: fill strategy from journal.
      - If multiple matches: pick nearest timestamp.
      - If no match: leave strategy as '—'.

    P&L always comes from live_state (never overwritten by journal).

    Args:
        trades: list of trade dicts from parse_live_state (may be mutated in-place).
        journal_text: raw text of the day's journal file.

    Returns:
        The same list with strategy fields enriched where possible.
    """
    if not trades or not journal_text:
        return trades

    # Parse ENTER_LONG / ENTER_SHORT entries from the journal text
    HEADING_RE = re.compile(
        r"^###\s+(\d{1,2}:\d{2}:\d{2})\s+[-—]\s+(ENTER_LONG|ENTER_SHORT)\s*$",
        re.IGNORECASE,
    )
    KV_RE = re.compile(r"^\*\*([^*:]+):\*\*\s*(.+)")
    KEY_MAP = {
        "instrument": "symbol",
        "strategy applied": "strategy",
    }

    journal_entries: list[dict] = []
    current: dict | None = None

    for raw_line in journal_text.splitlines():
        line = raw_line.strip()
        heading_m = HEADING_RE.match(line)
        if heading_m:
            if current is not None:
                journal_entries.append(current)
            time_str = heading_m.group(1)
            action = heading_m.group(2).upper()
            direction = "BUY" if action == "ENTER_LONG" else "SELL"
            current = {"time_str": time_str, "direction": direction}
            continue
        if current is None:
            continue
        kv_m = KV_RE.match(line)
        if kv_m:
            raw_key = kv_m.group(1).strip().lower()
            val = kv_m.group(2).strip()
            canonical = KEY_MAP.get(raw_key, raw_key.replace(" ", "_"))
            current[canonical] = val

    if current is not None:
        journal_entries.append(current)

    # Convert journal HH:MM:SS strings to seconds-since-midnight for matching
    def _hms_to_seconds(hms: str) -> int | None:
        parts = hms.split(":")
        if len(parts) != 3:
            return None
        try:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return h * 3600 + m * 60 + s
        except ValueError:
            return None

    # Build a lookup of journal entries keyed by (symbol_logical, direction)
    # symbol_logical: strip the expiry suffix, keep NIFTY / BANKNIFTY
    def _logical(sym: str) -> str:
        sym = sym.strip().upper()
        if sym.startswith("BANKNIFTY"):
            return "BANKNIFTY"
        if sym.startswith("NIFTY"):
            return "NIFTY"
        return sym

    journal_by_sym_dir: dict[tuple, list[dict]] = {}
    for e in journal_entries:
        sym_j = e.get("symbol", "")
        if not sym_j or not is_futures_symbol(sym_j):
            continue
        strategy_j = e.get("strategy", "")
        if not strategy_j or "—" in strategy_j:
            continue
        secs = _hms_to_seconds(e.get("time_str", ""))
        if secs is None:
            continue
        key = (_logical(sym_j), e.get("direction", ""))
        journal_by_sym_dir.setdefault(key, []).append({
            "secs": secs,
            "strategy": strategy_j,
        })

    # Enrich each trade that lacks a strategy
    TOLERANCE_SECS = 120

    for trade in trades:
        strat = trade.get("strategy", "")
        if strat and strat != "—":
            continue  # already has strategy

        # Derive trade entry time in seconds-since-midnight (IST)
        entry_epoch = None
        time_str = trade.get("time", "")
        if time_str and time_str != "—":
            entry_epoch_secs = _hms_to_seconds(time_str)
        else:
            entry_epoch_secs = None

        sym_t = _logical(trade.get("symbol", ""))
        dir_t = trade.get("direction", "").upper()
        key = (sym_t, dir_t)
        candidates = journal_by_sym_dir.get(key, [])

        if not candidates or entry_epoch_secs is None:
            continue

        # Filter within tolerance
        within = [c for c in candidates if abs(c["secs"] - entry_epoch_secs) <= TOLERANCE_SECS]
        if not within:
            continue

        # Pick nearest
        best = min(within, key=lambda c: abs(c["secs"] - entry_epoch_secs))
        trade["strategy"] = best["strategy"]

    return trades


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


def build_loss_clusters_section(executed: list) -> str:
    """Build the ## Loss Clusters section from executed trades.

    Groups losing trades (pnl < 0) by (symbol, strategy, direction), sums P&L,
    counts trades.  Omits rows where strategy is '—' (unknown).
    Sorts by total P&L ascending (worst first).

    Returns the full section text (including heading).
    """
    # Gather losing trades with known strategy
    cluster_map: dict[tuple, dict] = {}
    all_losses_have_unknown_strategy = True
    has_any_loss = False

    for t in executed:
        pnl_raw = t.get("pnl")
        if pnl_raw is None:
            continue
        try:
            pnl_val = float(pnl_raw)
        except (TypeError, ValueError):
            continue
        if pnl_val >= 0:
            continue

        has_any_loss = True
        sym = t.get("symbol", "")
        strategy = (t.get("strategy") or "").strip()
        if not strategy or strategy == "—":
            continue

        all_losses_have_unknown_strategy = False
        direction = t.get("direction", "").upper()
        key = (sym, strategy, direction)
        if key not in cluster_map:
            cluster_map[key] = {"count": 0, "total_pnl": 0.0}
        cluster_map[key]["count"] += 1
        cluster_map[key]["total_pnl"] += pnl_val

    lines = ["## Loss Clusters"]

    if not has_any_loss:
        lines.append("No losing executed trades today.")
        return "\n".join(lines)

    if all_losses_have_unknown_strategy:
        lines.append("Strategy names unavailable for executed losses — see Executed Trades.")
        return "\n".join(lines)

    if not cluster_map:
        lines.append("No losing executed trades with known strategy today.")
        return "\n".join(lines)

    # Sort by total P&L ascending (worst first)
    rows = sorted(cluster_map.items(), key=lambda kv: kv[1]["total_pnl"])

    lines.append("| Symbol | Strategy | Direction | Count | Total P&L |")
    lines.append("|--------|----------|-----------|-------|-----------|")
    for (sym, strategy, direction), agg in rows:
        lines.append(
            f"| {sym} | {strategy} | {direction} | {agg['count']} | ₹{agg['total_pnl']:+,.2f} |"
        )
    return "\n".join(lines)


def parse_candidate_audit(audit_path: Path) -> dict:
    """
    Parse the candidate audit JSONL file for a trading day.

    Returns a summary dict covering both the futures pipeline and the
    option-spread pipeline stages.
    """
    result = {
        "raw_total": 0,
        "guardrail_blocked": 0,
        "python_rejected": 0,
        "gatekeeper_rejected": 0,
        "gatekeeper_approved": 0,
        "orders_placed": 0,
        "spread_build_rejected": 0,
        "spread_build_success": 0,
        "spread_order_placed": 0,
        "spread_order_failed": 0,
        "by_signal_id": {},
    }
    if not audit_path.exists():
        return result

    try:
        with open(audit_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stage = rec.get("stage", "")
                sid = rec.get("signal_id", "")
                if sid not in result["by_signal_id"]:
                    result["by_signal_id"][sid] = []
                result["by_signal_id"][sid].append(rec)

                if stage == "RAW_CANDIDATE":
                    result["raw_total"] += 1
                elif stage == "HARD_GUARDRAIL_BLOCKED":
                    result["guardrail_blocked"] += 1
                elif stage == "PYTHON_REVIEW_REJECTED":
                    result["python_rejected"] += 1
                elif stage == "GATEKEEPER_REJECTED":
                    result["gatekeeper_rejected"] += 1
                elif stage == "GATEKEEPER_APPROVED":
                    result["gatekeeper_approved"] += 1
                elif stage == "ORDER_PLACED":
                    result["orders_placed"] += 1
                elif stage == "SPREAD_BUILD_REJECTED":
                    result["spread_build_rejected"] += 1
                elif stage == "SPREAD_BUILD_SUCCESS":
                    result["spread_build_success"] += 1
                elif stage == "SPREAD_ORDER_PLACED":
                    result["spread_order_placed"] += 1
                elif stage == "SPREAD_ORDER_FAILED":
                    result["spread_order_failed"] += 1
                # Historical GEMMA_OPINION stages are silently skipped.
    except Exception:
        pass

    return result


def build_spread_stats(closed_spreads: list) -> dict:
    """Compute summary stats from closed option spreads."""
    wins = sum(1 for s in closed_spreads if float(s.get("realized_pnl", 0)) > 0)
    losses = sum(1 for s in closed_spreads if float(s.get("realized_pnl", 0)) <= 0)
    net_pnl = sum(float(s.get("realized_pnl", 0)) for s in closed_spreads)
    return {
        "total": len(closed_spreads),
        "wins": wins,
        "losses": losses,
        "net_pnl": net_pnl,
    }


def build_review_markdown(
    review_date: date,
    executed: list,
    rejected: list,
    stats: dict,
    patterns: list,
    log_messages: list,
    indicator_notes: list,
    source_note: str = "",
    candidate_audit: dict | None = None,
    closed_spreads: list | None = None,
    open_spreads: list | None = None,
) -> str:
    date_str = review_date.isoformat()
    closed_spreads = closed_spreads or []
    open_spreads = open_spreads or []
    spread_stats = build_spread_stats(closed_spreads)
    lines = []

    lines.append(f"# Daily Review — {date_str}")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append(f"- Date: {date_str}")

    # Option spread summary (primary trading vehicle)
    if closed_spreads or open_spreads:
        open_unrealized_values = []
        for spread in open_spreads:
            try:
                value = spread.get("unrealized_pnl")
                if value is not None:
                    open_unrealized_values.append(float(value))
            except (TypeError, ValueError):
                pass
        lines.append(f"- Option spreads opened: {spread_stats['total'] + len(open_spreads)}")
        lines.append(f"- Option spreads exited: {spread_stats['total']}")
        lines.append(f"- Open spreads remaining: {len(open_spreads)}")
        if spread_stats["total"] > 0:
            win_rate = spread_stats["wins"] / spread_stats["total"] * 100
            lines.append(
                f"- Spread wins: {spread_stats['wins']} | "
                f"Losses: {spread_stats['losses']} | Win rate: {win_rate:.0f}%"
            )
        lines.append(f"- Net realized option-spread P&L: ₹{spread_stats['net_pnl']:+,.2f}")
        if open_unrealized_values:
            lines.append(f"- Open unrealized option-spread P&L: ₹{sum(open_unrealized_values):+,.2f}")
    else:
        lines.append("- Option spreads opened: 0")
        lines.append("- Option spreads exited: 0")

    # Legacy futures summary (only shown when futures trades actually exist)
    if stats["total"] > 0:
        win_rate = stats["wins"] / stats["total"] * 100
        lines.append(f"- Legacy futures trades: {stats['total']}")
        lines.append(f"- Futures wins: {stats['wins']} | Losses: {stats['losses']} | Win rate: {win_rate:.0f}%")
        if stats["pnl_known"] and stats["net_pnl"] is not None:
            lines.append(f"- Legacy futures P&L: ₹{stats['net_pnl']:+,.2f}")

    lines.append(f"- Rejected signals: {len(rejected)}")
    if source_note:
        lines.append(f"- _Source: {source_note}_")
    lines.append("")

    # Log issues
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

    # Option spreads table
    lines.append("## Option Spreads")
    if closed_spreads:
        lines.append("| Opened | Spread ID | Symbol | Type | Strategy | Realized P&L | Reason |")
        lines.append("|--------|-----------|--------|------|----------|-------------|--------|")
        for s in closed_spreads:
            opened = str(s.get("opened_at", "—"))[:16]
            sid = s.get("spread_id", "—")
            sym = s.get("symbol", "—")
            stype = s.get("spread_type", "—")
            strategy = s.get("strategy") or "—"
            pnl = float(s.get("realized_pnl", 0))
            reason = s.get("close_reason", "—")
            lines.append(f"| {opened} | {sid} | {sym} | {stype} | {strategy} | ₹{pnl:+,.2f} | {reason} |")
    if open_spreads:
        lines.append("")
        lines.append("**Open spreads (not yet exited):**")
        for s in open_spreads:
            opened = str(s.get("opened_at", "—"))[:16]
            sid = s.get("spread_id", "—")
            sym = s.get("symbol", "—")
            stype = s.get("spread_type", "—")
            unrealized = s.get("unrealized_pnl")
            try:
                pnl_text = f" | unrealized ₹{float(unrealized):+,.2f}" if unrealized is not None else " | unrealized unavailable"
            except (TypeError, ValueError):
                pnl_text = " | unrealized unavailable"
            lines.append(f"- {sid}: {sym} {stype} opened {opened}{pnl_text}")
    if not closed_spreads and not open_spreads:
        lines.append("_No option spreads opened on this date._")
    lines.append("")

    # Legacy futures trades table (only shown when they exist)
    if executed:
        lines.append("## Legacy Futures Trades")
        lines.append("| Time | Symbol | Direction | Strategy | P&L |")
        lines.append("|------|--------|-----------|----------|-----|")
        for t in executed:
            time_val = t.get("time", "—")
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
        lines.append("")

        loss_clusters_section = build_loss_clusters_section(executed)
        lines.append(loss_clusters_section)
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
            if not sym or not strategy or not reason:
                continue
            if "—" in sym or "—" in strategy:
                continue
            lines.append(f"| {time_val} | {sym} | {strategy} | {reason} |")
    else:
        lines.append("_No rejected signals on this date._")
    lines.append("")

    # Signal Gate Audit
    if candidate_audit:
        lines.append("## Signal Gate Audit")
        raw = candidate_audit.get("raw_total", 0)
        blocked = candidate_audit.get("guardrail_blocked", 0)
        py_rej = candidate_audit.get("python_rejected", 0)
        gk_rej = candidate_audit.get("gatekeeper_rejected", 0)
        gk_app = candidate_audit.get("gatekeeper_approved", 0)
        spread_built = candidate_audit.get("spread_build_success", 0)
        spread_rej = candidate_audit.get("spread_build_rejected", 0)
        spread_placed = candidate_audit.get("spread_order_placed", 0)
        spread_failed = candidate_audit.get("spread_order_failed", 0)
        placed = candidate_audit.get("orders_placed", 0)
        lines.append("| Stage | Count |")
        lines.append("|-------|-------|")
        lines.append(f"| Raw scanner candidates | {raw} |")
        lines.append(f"| Hard guardrail blocked | {blocked} |")
        lines.append(f"| Python review rejected | {py_rej} |")
        lines.append(f"| Gemini gatekeeper rejected | {gk_rej} |")
        lines.append(f"| Gemini gatekeeper approved | {gk_app} |")
        if spread_built or spread_rej:
            lines.append(f"| Spread build success | {spread_built} |")
            lines.append(f"| Spread build rejected | {spread_rej} |")
        if spread_placed or spread_failed:
            lines.append(f"| Spread orders placed | {spread_placed} |")
            lines.append(f"| Spread orders failed | {spread_failed} |")
        if placed:
            lines.append(f"| Futures orders placed | {placed} |")
        lines.append("")
    else:
        lines.append("## Signal Gate Audit")
        lines.append("_No candidate audit file found for this date._")
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
    pair_credit_state_path = runtime_root / "pair_credit_positions.json"
    journal_path = runtime_root / "journals" / f"{date_compact}.md"
    log_path = runtime_root / "logs" / f"blitztrader_{date_compact}.log"
    indicators_path = runtime_root / "data_exports" / date_compact / "indicators.md"

    # ── SOURCE 1: live_state.json (primary — actual executed trades with P&L) ──
    all_executed: list[dict] = []
    all_rejected: list[dict] = []
    log_messages: list[str] = []
    indicator_notes: list[str] = []
    source_note = ""

    closed_spreads: list[dict] = []
    open_spreads_list: list[dict] = []

    if live_state_path.exists():
        print(f"[evaluate_futures_day] Reading live_state.json: {live_state_path}")
        closed_spreads, open_spreads_list = parse_spreads_from_live_state(live_state_path, review_date)
        if closed_spreads or open_spreads_list:
            print(
                f"  Found {len(closed_spreads)} closed spread(s) and "
                f"{len(open_spreads_list)} open spread(s) in live_state.json"
            )
        live_trades = parse_live_state(live_state_path, review_date)
        if live_trades:
            all_executed = live_trades
            source_note = "live_state.json (executed trades with realized P&L)"
            print(f"  Found {len(all_executed)} legacy futures trades in live_state.json")
        else:
            print("  No legacy futures trades found in live_state.json for this date")
    else:
        print(f"[evaluate_futures_day] live_state.json not found: {live_state_path}")

    if pair_credit_state_path.exists():
        print(f"[evaluate_futures_day] Reading pair_credit_positions.json: {pair_credit_state_path}")
        pc_closed, pc_open = parse_pair_credit_state(pair_credit_state_path, review_date)
        if pc_closed or pc_open:
            closed_spreads.extend(pc_closed)
            open_spreads_list.extend(pc_open)
            print(f"  Found {len(pc_closed)} closed pair-credit spread(s) and {len(pc_open)} open pair-credit spread(s)")
    else:
        print(f"[evaluate_futures_day] pair_credit_positions.json not found: {pair_credit_state_path}")

    # ── SOURCE 2: Journal (fallback for executed trades if live_state.json empty) ──
    # Always parse journal for REJECTED signals (not recorded in live_state.json)
    # Also used to enrich strategy names on executed trades from live_state.json.
    print(f"[evaluate_futures_day] Reading journal: {journal_path}")
    journal_data = parse_journal(journal_path)
    journal_text_raw = ""
    if journal_path.exists():
        try:
            journal_text_raw = journal_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if (
        not all_executed
        and not closed_spreads
        and not open_spreads_list
        and journal_data["executed"]
    ):
        # Fallback: use journal ENTER_LONG/ENTER_SHORT entries
        all_executed = journal_data["executed"]
        source_note = "journal (ENTER_LONG/ENTER_SHORT entries — P&L may be unavailable)"
        print(f"  Using journal fallback: {len(all_executed)} executed trade(s)")
    elif journal_data["executed"] and (closed_spreads or open_spreads_list):
        print(
            "  Skipping journal trade fallback because option spreads are "
            "already available in live_state.json"
        )
    elif not journal_path.exists():
        print("  (journal not found, skipping)")

    # Enrich strategy names on executed trades from journal (Fix B)
    if all_executed and journal_text_raw:
        before_enrich = sum(1 for t in all_executed if (t.get("strategy") or "—") == "—")
        all_executed = _enrich_trades_from_journal(all_executed, journal_text_raw)
        after_enrich = sum(1 for t in all_executed if (t.get("strategy") or "—") == "—")
        enriched_count = before_enrich - after_enrich
        if enriched_count > 0:
            print(f"  Journal enrichment: filled strategy for {enriched_count} trade(s)")

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

    # ── SOURCE 5: Candidate audit JSONL ──
    audit_path = runtime_root / "candidate_signals" / f"{date_compact}.jsonl"
    candidate_audit_data = None
    if audit_path.exists():
        print(f"[evaluate_futures_day] Reading candidate audit: {audit_path}")
        candidate_audit_data = parse_candidate_audit(audit_path)
        print(
            f"  Audit: {candidate_audit_data['raw_total']} raw, "
            f"{candidate_audit_data['guardrail_blocked']} guardrail-blocked, "
            f"{candidate_audit_data['python_rejected']} Python-rejected, "
            f"{candidate_audit_data['gatekeeper_rejected']} gate-rejected, "
            f"{candidate_audit_data['spread_order_placed']} spread-placed, "
            f"{candidate_audit_data['orders_placed']} futures-placed"
        )
    else:
        print(f"[evaluate_futures_day] Candidate audit not found (skipping): {audit_path}")

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
        candidate_audit=candidate_audit_data,
        closed_spreads=closed_spreads,
        open_spreads=open_spreads_list,
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
    spread_stat = build_spread_stats(closed_spreads)
    print(f"[evaluate_futures_day] Review written: {output_path}")
    print(
        f"  Spreads: {spread_stat['total']} closed, {len(open_spreads_list)} open | "
        f"Futures trades: {stats['total']} | Rejected signals: {len(all_rejected)}"
    )


if __name__ == "__main__":
    main()
