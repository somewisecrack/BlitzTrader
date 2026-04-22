#!/usr/bin/env python3
"""
scripts/postmortem.py
----------------------
Post-mortem analysis for a given trading date.

Reads:
  - live_state.json         — current / last session state
  - journals/<YYYYMMDD>.md  — daily journal file for the date
  - data_exports/           — CSV feed/indicator files

For each trade found, prints:
  - entry time, direction, symbol, quantity, entry price, stop_loss, target
  - exit time, exit price, realized P&L
  - symbol_type: "futures_tsym" | "bare_logical" | "option" | "unknown"

Prints a summary: total trades, total P&L, symbol type breakdown.

Usage:
    python3 scripts/postmortem.py --date 2026-04-10
    python3 scripts/postmortem.py --date 2026-04-10 --project-root /path/to/vm-export
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

BARE_LOGICAL = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}

# Regex for futures tsym: starts with NIFTY or BANKNIFTY, has digits + month, ends with F
_FUTURES_RE = re.compile(r"^(NIFTY|BANKNIFTY)\d{2}[A-Z]{3}\d{2}F$", re.IGNORECASE)
_OPTION_RE  = re.compile(r"(CE|PE)$", re.IGNORECASE)


def classify_symbol(symbol: str) -> str:
    """Classify a symbol as futures_tsym, bare_logical, option, or unknown."""
    if not symbol:
        return "unknown"
    sym = symbol.strip().upper()
    if sym in BARE_LOGICAL:
        return "bare_logical"
    if _FUTURES_RE.match(sym):
        return "futures_tsym"
    if _OPTION_RE.search(sym):
        return "option"
    return "unknown"


def fmt_ts(ts) -> str:
    """Format a Unix timestamp or ISO string to a readable datetime string."""
    if ts is None:
        return "N/A"
    try:
        f = float(ts)
        return datetime.fromtimestamp(f).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return str(ts)


def parse_date_arg(date_str: str) -> date:
    """Parse --date argument (YYYY-MM-DD)."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        print(f"ERROR: Invalid date format '{date_str}'. Use YYYY-MM-DD.")
        sys.exit(1)


# ── Data source readers ────────────────────────────────────────────────────────

def read_live_state(project_root: Path) -> dict:
    """Read live_state.json. Returns dict or empty dict."""
    p = project_root / "live_state.json"
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception as e:
        print(f"  WARNING: Could not read {p}: {e}")
        return {}


def read_journal_file(project_root: Path, target_date: date) -> str:
    """Read the journal file for target_date. Returns file text or empty string."""
    journal_dir = project_root / "journals"
    fname = target_date.strftime("%Y%m%d") + ".md"
    p = journal_dir / fname
    if not p.exists():
        return ""
    try:
        with open(p) as f:
            return f.read()
    except Exception as e:
        print(f"  WARNING: Could not read {p}: {e}")
        return ""


def find_csv_files(data_root: Path) -> list:
    """Find all CSV files under a data root."""
    data_dir = data_root / "data_exports"
    if not data_dir.exists():
        return []
    return sorted(data_dir.rglob("*.csv"))


def find_data_export_dir(data_root: Path, target_date: date) -> Path:
    """Return the per-day export dir for the target date if present."""
    return data_root / "data_exports" / target_date.strftime("%Y%m%d")


def extract_trades_from_state(state: dict, target_date: date) -> list:
    """
    Extract trades from live_state.json that match target_date.
    Returns list of trade dicts.
    """
    trades = state.get("trades", [])
    result = []
    for t in trades:
        # Try to match by entry_time or exit_time
        entry_ts = t.get("entry_time")
        try:
            entry_dt = datetime.fromtimestamp(float(entry_ts)).date() if entry_ts else None
        except (ValueError, TypeError, OSError):
            entry_dt = None

        if entry_dt == target_date or not entry_dt:
            result.append(t)

    return result


def extract_trades_from_journal(journal_text: str) -> list:
    """
    Parse trade information from the journal markdown text.
    Looks for common patterns like entry/exit blocks.
    Returns a list of partial trade dicts (best-effort parsing).
    """
    trades = []
    if not journal_text:
        return trades

    # Look for JSON-like trade blocks embedded in the journal
    json_blocks = re.findall(r"\{[^{}]*\"symbol\"[^{}]*\}", journal_text, re.DOTALL)
    for block in json_blocks:
        try:
            t = json.loads(block)
            if "symbol" in t:
                trades.append(t)
        except json.JSONDecodeError:
            pass

    # Also look for table-style rows: | NIFTY28APR26F | BUY | 25 | ...
    table_re = re.compile(
        r"\|\s*([A-Z0-9]+)\s*\|\s*(BUY|SELL)\s*\|\s*(\d+)\s*\|"
        r"\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|",
        re.IGNORECASE,
    )
    for m in table_re.finditer(journal_text):
        trades.append({
            "symbol": m.group(1),
            "direction": m.group(2).upper(),
            "quantity": int(m.group(3)),
            "entry_price": float(m.group(4)),
            "exit_price": float(m.group(5)),
            "_source": "journal_table",
        })

    return trades


def print_trade(t: dict, idx: int):
    """Print a formatted trade record."""
    sym = t.get("symbol", "N/A")
    sym_type = classify_symbol(sym)
    pnl = t.get("pnl", t.get("realized_pnl", None))
    pnl_str = f"₹{pnl:+,.2f}" if pnl is not None else "N/A"

    print(f"\n  Trade #{idx}")
    print(f"    symbol       : {sym}")
    print(f"    symbol_type  : {sym_type}")
    print(f"    direction    : {t.get('direction', 'N/A')}")
    print(f"    quantity     : {t.get('quantity', 'N/A')}")
    print(f"    entry_price  : {t.get('entry_price', 'N/A')}")
    print(f"    stop_loss    : {t.get('stop_loss', 'N/A')}")
    print(f"    target       : {t.get('target', 'N/A')}")
    print(f"    entry_time   : {fmt_ts(t.get('entry_time'))}")
    print(f"    exit_price   : {t.get('exit_price', 'N/A')}")
    print(f"    exit_time    : {fmt_ts(t.get('exit_time'))}")
    print(f"    realized_pnl : {pnl_str}")


def print_summary(trades: list):
    """Print summary statistics."""
    total = len(trades)
    total_pnl = sum(
        t.get("pnl", t.get("realized_pnl", 0)) or 0 for t in trades
    )

    type_counts = {}
    for t in trades:
        sym_type = classify_symbol(t.get("symbol", ""))
        type_counts[sym_type] = type_counts.get(sym_type, 0) + 1

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Total trades    : {total}")
    if total > 0:
        print(f"  Total P&L       : ₹{total_pnl:+,.2f}")
        print(f"  Symbol type breakdown:")
        for stype, count in sorted(type_counts.items()):
            correct = "OK" if stype == "futures_tsym" else "PROBLEM"
            print(f"    {stype:<18}: {count}  [{correct}]")

        wins = sum(1 for t in trades if (t.get("pnl") or t.get("realized_pnl") or 0) > 0)
        print(f"  Wins / Losses   : {wins} / {total - wins}")

    futures_count = type_counts.get("futures_tsym", 0)
    wrong_count = total - futures_count
    if total > 0:
        if wrong_count == 0:
            print("\n  STATUS: All trades used correct futures tsym. OK.")
        else:
            print(f"\n  STATUS: {wrong_count} trade(s) used wrong symbol type (bare_logical/option/unknown).")
            print("          Review symbol_type above for details.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="BlitzTrader post-mortem analysis")
    parser.add_argument("--date", required=True, help="Date to analyse (YYYY-MM-DD)")
    parser.add_argument(
        "--project-root",
        default=None,
        help="Optional alternate BlitzTrader project root or exported session bundle root",
    )
    args = parser.parse_args()

    target_date = parse_date_arg(args.date)
    project_root = Path(args.project_root).expanduser().resolve() if args.project_root else Path(__file__).parent.parent

    print("=" * 60)
    print(f"BlitzTrader Post-Mortem: {target_date.isoformat()}")
    print("=" * 60)

    # ── Identify all data files ──────────────────────────────────────────────
    inspected = []

    state_path  = project_root / "live_state.json"
    journal_dir = project_root / "journals"
    journal_path = journal_dir / (target_date.strftime("%Y%m%d") + ".md")
    data_exports = project_root / "data_exports"

    inspected.append(str(state_path))
    inspected.append(str(journal_path))

    csv_files = find_csv_files(project_root)
    for f in csv_files:
        inspected.append(str(f))

    print("\nData files inspected:")
    for p in inspected:
        exists = "EXISTS" if Path(p).exists() else "NOT FOUND"
        print(f"  [{exists}] {p}")

    # ── Read sources ─────────────────────────────────────────────────────────
    state       = read_live_state(project_root)
    journal_txt = read_journal_file(project_root, target_date)

    daily_export_dir = find_data_export_dir(project_root, target_date)

    # ── Extract trades ───────────────────────────────────────────────────────
    trades_from_state   = extract_trades_from_state(state, target_date)
    trades_from_journal = extract_trades_from_journal(journal_txt)

    # Deduplicate: prefer state trades; append journal trades only if no overlap
    state_symbols = {(t.get("symbol"), t.get("entry_price")) for t in trades_from_state}
    unique_journal = [
        t for t in trades_from_journal
        if (t.get("symbol"), t.get("entry_price")) not in state_symbols
    ]
    all_trades = trades_from_state + unique_journal

    # Filter by date for state trades (already done); journal trades are assumed
    # to be from the journal file of the target date, so include them all.

    if not all_trades:
        print(f"\nNo trades found for {target_date.isoformat()}.")

        # Show session state context
        if state:
            session_start_ts = state.get("session_start")
            session_date = (
                datetime.fromtimestamp(float(session_start_ts)).date()
                if session_start_ts else None
            )
            print(f"\nlive_state.json session_start date: {session_date or 'N/A'}")
            print(f"Trade count in state             : {state.get('trade_count', 0)}")
            print(f"Session P&L                      : ₹{state.get('daily_pnl', 0):+,.2f}")
        else:
            print("\nlive_state.json not found or empty.")

        if not journal_txt:
            print(f"Journal file not found: {journal_path}")
        else:
            print(f"Journal file found ({len(journal_txt)} chars) but no trade records parsed.")

        if daily_export_dir.exists():
            csv_names = sorted(p.name for p in daily_export_dir.glob("*.csv"))
            print(f"Daily export folder present     : {daily_export_dir}")
            print(f"CSV files available             : {', '.join(csv_names) if csv_names else 'None'}")
            signal_csv = daily_export_dir / "strategy_signals.csv"
            if signal_csv.exists():
                try:
                    with open(signal_csv, newline="") as f:
                        signal_count = max(sum(1 for _ in csv.DictReader(f)), 0)
                    print(f"Scanner signal rows             : {signal_count}")
                except Exception as e:
                    print(f"Could not read strategy_signals.csv: {e}")
        else:
            print(f"Daily export folder not found   : {daily_export_dir}")

        return

    print(f"\nFound {len(all_trades)} trade(s) for {target_date.isoformat()}:\n")
    for i, t in enumerate(all_trades, 1):
        print_trade(t, i)

    print_summary(all_trades)


if __name__ == "__main__":
    main()
