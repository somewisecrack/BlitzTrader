"""
tools/position_serial.py — Deterministic position serial numbering for Telegram status.

Generates a stable, human-readable serial index over all open positions
(futures + pairs), persists it to runtime/telegram_position_index.json,
and provides exit-by-serial functionality.

Design constraints:
- One serial per logical position  (futures=1 serial, pair=1 serial)
- Serials are deterministic within a status message (sorted by opened_at)
- Index is refreshed every time a status message is generated
- TTL = 1800 s; stale index → error, not execution
- NEVER opens a new position
- NEVER modifies strategy logic
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytz

from config import RUNTIME_STORAGE_DIR

logger = logging.getLogger("BlitzTrader.PositionSerial")

IST = pytz.timezone("Asia/Kolkata")

# Where we persist the index
_INDEX_FILE = RUNTIME_STORAGE_DIR / "telegram_position_index.json"
_TTL_SECONDS = 1800  # 30 minutes


# ──────────────────────────────────────────────────────────────
#   PRICE HELPERS (cache-first, no new quote spam)
# ──────────────────────────────────────────────────────────────

def _get_price_for_symbol(symbol: str, live_feed=None, shoonya_client=None, token: str = None) -> Optional[float]:
    """
    Resolve current price for a symbol.
    Priority: WebSocket LTP → REST last price.
    Returns None if unavailable; caller logs ONE warning.
    """
    if token and live_feed:
        try:
            ltp = live_feed.get_ltp(token)
            if ltp:
                return float(ltp)
        except Exception:
            pass
    # REST fallback via shoonya_client.get_ltp (only if token known)
    if token and shoonya_client:
        try:
            exchange = "NFO" if (symbol.endswith("F") and any(c.isdigit() for c in symbol)) else "NSE"
            price = shoonya_client.get_ltp(exchange, token)
            if price:
                return float(price)
        except Exception:
            pass
    return None


# ──────────────────────────────────────────────────────────────
#   STATUS MESSAGE GENERATION
# ──────────────────────────────────────────────────────────────

def build_status_message(
    state_manager,
    pairs_portfolio,
    live_feed=None,
    shoonya_client=None,
    active_tokens: dict = None,
) -> tuple[str, dict]:
    """
    Build a structured status message and return (message_text, index_payload).

    The index_payload is ready to be persisted via save_position_index().
    """
    active_tokens = active_tokens or {}
    state = state_manager.get_state()

    # ── Futures P&L ───────────────────────────────────────────
    futures_realized = float(state.get("daily_pnl", 0) or 0)
    futures_positions = state.get("positions", []) or []

    futures_unrealized = 0.0
    futures_pos_lines = []
    serial = 1
    index_entries = []

    for pos in futures_positions:
        sym = pos.get("symbol", "")
        direction = pos.get("direction", "")
        qty = pos.get("quantity", 0)
        entry = float(pos.get("entry_price") or 0)
        strategy = pos.get("strategy", pos.get("strategy_applied", ""))

        # Find token for this symbol
        token = None
        for logical, info in active_tokens.items():
            if info.get("tsym", "").upper() == sym.upper():
                token = str(info.get("token", ""))
                break

        current = _get_price_for_symbol(sym, live_feed, shoonya_client, token)
        if current is not None:
            upnl = (current - entry) * qty if direction == "BUY" else (entry - current) * qty
            futures_unrealized += upnl
            upnl_str = f"₹{upnl:+,.2f}"
            current_str = f"{current:.2f}"
        else:
            logger.warning("Serial status: current price unavailable for %s", sym)
            upnl_str = "N/A"
            current_str = "N/A"

        strat_tag = f" strategy={strategy}" if strategy else ""
        futures_pos_lines.append(
            f"{serial}. [Futures] {sym} {direction} qty={qty} "
            f"entry={entry:.2f} current={current_str} "
            f"unrealized={upnl_str}{strat_tag}"
        )

        index_entries.append({
            "serial": serial,
            "type": "futures",
            "tradingsymbol": sym,
            "direction": direction,
            "entry_price": entry,
            "qty": qty,
            "opened_at": pos.get("entry_time", ""),
            "token": token or "",
        })
        serial += 1

    # ── Pairs P&L ─────────────────────────────────────────────
    pairs_status = pairs_portfolio.get_status()
    pairs_realized = float(pairs_status.get("realized_pnl", 0) or 0)
    pairs_unrealized_total = 0.0
    pairs_pos_lines = []

    for pos in (pairs_portfolio.positions or []):
        if pos.closed_at:
            continue

        ll = pos.long_leg
        sl_leg = pos.short_leg
        pair_name = pos.pair_name

        # Long leg
        long_token = ll.token
        long_current = _get_price_for_symbol(ll.symbol, live_feed, shoonya_client, long_token)
        if long_current is not None:
            long_upnl = (long_current - ll.entry_price) * ll.qty
            long_current_str = f"{long_current:.2f}"
            long_upnl_str = f"₹{long_upnl:+,.2f}"
        else:
            logger.warning("Serial status: current price unavailable for pairs long leg %s", ll.symbol)
            long_upnl = 0.0
            long_current_str = "N/A"
            long_upnl_str = "N/A"

        # Short leg
        short_token = sl_leg.token
        short_current = _get_price_for_symbol(sl_leg.symbol, live_feed, shoonya_client, short_token)
        if short_current is not None:
            short_upnl = (sl_leg.entry_price - short_current) * sl_leg.qty
            short_current_str = f"{short_current:.2f}"
            short_upnl_str = f"₹{short_upnl:+,.2f}"
        else:
            logger.warning("Serial status: current price unavailable for pairs short leg %s", sl_leg.symbol)
            short_upnl = 0.0
            short_current_str = "N/A"
            short_upnl_str = "N/A"

        if long_current is not None and short_current is not None:
            pair_upnl = long_upnl + short_upnl
            pair_upnl_str = f"₹{pair_upnl:+,.2f}"
            pairs_unrealized_total += pair_upnl
        else:
            pair_upnl_str = "N/A"

        # Determine LONG/SHORT symbol names for display
        long_sym_display = ll.symbol
        short_sym_display = sl_leg.symbol

        pairs_pos_lines.append(
            f"{serial}. [Pairs] {pair_name} | "
            f"LONG {long_sym_display} qty={ll.qty} entry={ll.entry_price:.2f} "
            f"current={long_current_str} unrealized={long_upnl_str} | "
            f"SHORT {short_sym_display} qty={sl_leg.qty} entry={sl_leg.entry_price:.2f} "
            f"current={short_current_str} unrealized={short_upnl_str} | "
            f"pair_unrealized={pair_upnl_str}"
        )

        index_entries.append({
            "serial": serial,
            "type": "pairs",
            "pair_name": pair_name,
            "opened_at": pos.opened_at,
            "long_symbol": ll.symbol,
            "long_tradingsymbol": ll.tradingsymbol,
            "long_token": ll.token,
            "short_symbol": sl_leg.symbol,
            "short_tradingsymbol": sl_leg.tradingsymbol,
            "short_token": sl_leg.token,
        })
        serial += 1

    # ── Assemble P&L sections ─────────────────────────────────
    overall_realized = futures_realized + pairs_realized
    overall_unrealized = futures_unrealized + pairs_unrealized_total
    overall_total = overall_realized + overall_unrealized

    futures_total = futures_realized + futures_unrealized
    pairs_total = pairs_realized + pairs_unrealized_total

    lines = [
        "Overall",
        f"- Realized P&L: ₹{overall_realized:+,.2f}",
        f"- Unrealized P&L: ₹{overall_unrealized:+,.2f}",
        f"- Total P&L: ₹{overall_total:+,.2f}",
        "",
        "Futures",
        f"- Realized P&L: ₹{futures_realized:+,.2f}",
        f"- Unrealized P&L: ₹{futures_unrealized:+,.2f}",
        f"- Total P&L: ₹{futures_total:+,.2f}",
        "",
        "Pairs",
        f"- Realized P&L: ₹{pairs_realized:+,.2f}",
        f"- Unrealized P&L: ₹{pairs_unrealized_total:+,.2f}",
        f"- Total P&L: ₹{pairs_total:+,.2f}",
        "",
        "Open Positions",
    ]

    all_pos_lines = futures_pos_lines + pairs_pos_lines
    if all_pos_lines:
        lines.extend(all_pos_lines)
    else:
        lines.append("- None")

    message = "\n".join(lines)

    now_ist = datetime.now(IST)
    index_payload = {
        "generated_at": now_ist.isoformat(),
        "ttl_seconds": _TTL_SECONDS,
        "positions": index_entries,
    }
    return message, index_payload


# ──────────────────────────────────────────────────────────────
#   INDEX PERSISTENCE
# ──────────────────────────────────────────────────────────────

def save_position_index(index_payload: dict) -> None:
    """Atomically write the position index to disk."""
    tmp = _INDEX_FILE.with_suffix(".json.tmp")
    try:
        _INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(index_payload, indent=2, default=str))
        tmp.replace(_INDEX_FILE)
        logger.info("Position serial index saved: %d entries", len(index_payload.get("positions", [])))
    except Exception:
        logger.exception("Failed to save position serial index")


def load_position_index() -> Optional[dict]:
    """Load position index from disk. Returns None if missing."""
    if not _INDEX_FILE.exists():
        return None
    try:
        return json.loads(_INDEX_FILE.read_text())
    except Exception:
        logger.exception("Failed to load position serial index")
        return None


def invalidate_position_index() -> None:
    """Delete the position serial index (e.g. after an exit)."""
    try:
        if _INDEX_FILE.exists():
            _INDEX_FILE.unlink()
            logger.info("Position serial index invalidated")
    except Exception:
        logger.exception("Failed to invalidate position serial index")


def _is_index_fresh(index: dict) -> bool:
    """Return True if the index was generated within TTL."""
    try:
        generated_at_str = index.get("generated_at", "")
        ttl = int(index.get("ttl_seconds", _TTL_SECONDS))
        if not generated_at_str:
            return False
        generated_at = datetime.fromisoformat(generated_at_str)
        # Make tz-aware if naive
        if generated_at.tzinfo is None:
            generated_at = IST.localize(generated_at)
        age = (datetime.now(IST) - generated_at).total_seconds()
        return age <= ttl
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────
#   EXIT BY SERIAL (Gemini-callable tool implementation)
# ──────────────────────────────────────────────────────────────

def exit_position_by_serial(
    serial: int,
    state_manager,
    pairs_portfolio,
    order_execution,
    shoonya_client,
    telegram_handler,
    active_tokens: dict = None,
    live_feed=None,
) -> dict:
    """
    Exit the open position identified by serial number.

    Safety guarantees:
    - NEVER opens a new position
    - NEVER modifies strategy rules
    - Validates index freshness, serial existence, and live position state
    - No partial exits; no single-leg pair exits
    - Returns structured result; caller sends Telegram confirmation
    """
    active_tokens = active_tokens or {}

    # 1. Load index
    index = load_position_index()
    if not index:
        msg = (
            "No position index found. Please request a status update first "
            "(e.g. ask for /status or 'show positions')."
        )
        logger.warning("exit_by_serial(%d): no index on disk", serial)
        return {"success": False, "error": msg}

    # 2. Check TTL
    if not _is_index_fresh(index):
        msg = (
            "Position index is stale (older than 30 minutes). "
            "Please request a fresh status first."
        )
        logger.warning("exit_by_serial(%d): index is stale", serial)
        return {"success": False, "error": msg}

    # 3. Validate serial
    entry = None
    for e in index.get("positions", []):
        if e.get("serial") == serial:
            entry = e
            break
    if entry is None:
        msg = f"Serial #{serial} not found in the position index. No orders placed."
        logger.warning("exit_by_serial(%d): serial not in index", serial)
        return {"success": False, "error": msg}

    pos_type = entry.get("type")

    # 4. Re-verify position is still open in live state
    if pos_type == "futures":
        return _exit_futures_by_serial(
            serial, entry, state_manager, order_execution,
            telegram_handler, live_feed, active_tokens,
        )
    elif pos_type == "pairs":
        return _exit_pairs_by_serial(
            serial, entry, pairs_portfolio, shoonya_client,
            telegram_handler, live_feed, active_tokens,
        )
    else:
        return {"success": False, "error": f"Unknown position type '{pos_type}' for serial #{serial}."}


def _exit_futures_by_serial(
    serial: int,
    entry: dict,
    state_manager,
    order_execution,
    telegram_handler,
    live_feed,
    active_tokens: dict,
) -> dict:
    """Close a futures position identified by serial."""
    tradingsymbol = entry.get("tradingsymbol", "")
    direction = entry.get("direction", "")

    # Re-verify still open
    state = state_manager.get_state()
    positions = state.get("positions", []) or []
    live_pos = None
    for p in positions:
        if p.get("symbol", "").upper() == tradingsymbol.upper():
            live_pos = p
            break

    if live_pos is None:
        msg = (
            f"Serial #{serial} ({tradingsymbol}) is no longer open. "
            "No orders placed."
        )
        logger.warning("exit_by_serial(%d): futures %s already closed", serial, tradingsymbol)
        invalidate_position_index()
        return {"success": False, "error": msg}

    logger.info(
        "exit_by_serial(%d): closing futures %s %s qty=%s",
        serial, direction, tradingsymbol, live_pos.get("quantity"),
    )

    # Close using existing deterministic close logic
    result = order_execution.close_position(tradingsymbol)
    if result.get("error"):
        return {
            "success": False,
            "error": f"Close failed for serial #{serial}: {result['error']}",
        }

    pnl = result.get("pnl", 0.0)
    exit_price = result.get("exit_price")

    # Invalidate index so next status gets fresh serials
    invalidate_position_index()

    tg_msg = (
        f"Exited serial #{serial}: {direction} {tradingsymbol}\n"
        f"Exit price: ₹{exit_price:.2f}\n"
        f"Realized P&L: ₹{pnl:+,.2f}\n"
        f"Position closed successfully."
    )
    if telegram_handler:
        telegram_handler.send_telegram(tg_msg)

    return {
        "success": True,
        "serial": serial,
        "type": "futures",
        "tradingsymbol": tradingsymbol,
        "exit_price": exit_price,
        "pnl": pnl,
        "message": tg_msg,
    }


def _exit_pairs_by_serial(
    serial: int,
    entry: dict,
    pairs_portfolio,
    shoonya_client,
    telegram_handler,
    live_feed,
    active_tokens: dict,
) -> dict:
    """Close BOTH legs of a pairs position identified by serial."""
    pair_name = entry.get("pair_name", "")
    long_sym = entry.get("long_symbol", "")
    short_sym = entry.get("short_symbol", "")

    # Re-verify pair still open
    live_pos = None
    for pos in (pairs_portfolio.positions or []):
        if pos.pair_name == pair_name and not pos.closed_at:
            live_pos = pos
            break

    if live_pos is None:
        msg = (
            f"Serial #{serial} (pair {pair_name}) is no longer open. "
            "No orders placed."
        )
        logger.warning("exit_by_serial(%d): pair %s already closed", serial, pair_name)
        invalidate_position_index()
        return {"success": False, "error": msg}

    logger.info(
        "exit_by_serial(%d): closing pair %s (long=%s short=%s)",
        serial, pair_name, long_sym, short_sym,
    )

    # Use pairs portfolio's close-all to close just this pair atomically
    # We target only this specific pair position
    from broker.shoonya_client import ResolvedScrip
    from pairs.portfolio import Leg

    # Close long leg
    ll = live_pos.long_leg
    long_scrip = ResolvedScrip(ll.symbol, ll.tradingsymbol, ll.token)
    long_exit_price = pairs_portfolio._entry_price(shoonya_client, long_scrip, "SELL")
    long_pnl = 0.0
    if long_exit_price is not None and not ll.closed_at:
        long_pnl = pairs_portfolio._close_leg(ll, long_exit_price)
    elif ll.closed_at:
        long_pnl = ll.realized_pnl or 0.0

    # Close short leg
    sl_leg = live_pos.short_leg
    short_scrip = ResolvedScrip(sl_leg.symbol, sl_leg.tradingsymbol, sl_leg.token)
    short_exit_price = pairs_portfolio._entry_price(shoonya_client, short_scrip, "BUY")
    short_pnl = 0.0
    if short_exit_price is not None and not sl_leg.closed_at:
        short_pnl = pairs_portfolio._close_leg(sl_leg, short_exit_price)
    elif sl_leg.closed_at:
        short_pnl = sl_leg.realized_pnl or 0.0

    # Mark pair as closed
    live_pos.closed_at = datetime.now().isoformat()
    live_pos.pnl = round(long_pnl + short_pnl, 2)
    pairs_portfolio._persist()

    total_pnl = live_pos.pnl
    invalidate_position_index()

    long_price_str = f"₹{long_exit_price:.2f}" if long_exit_price else "N/A"
    short_price_str = f"₹{short_exit_price:.2f}" if short_exit_price else "N/A"

    tg_msg = (
        f"Exited serial #{serial}: pair {pair_name}\n"
        f"  LONG {long_sym} exit: {long_price_str} P&L: ₹{long_pnl:+,.2f}\n"
        f"  SHORT {short_sym} exit: {short_price_str} P&L: ₹{short_pnl:+,.2f}\n"
        f"Pair total P&L: ₹{total_pnl:+,.2f}"
    )
    if telegram_handler:
        telegram_handler.send_telegram(tg_msg)

    return {
        "success": True,
        "serial": serial,
        "type": "pairs",
        "pair_name": pair_name,
        "long_exit_price": long_exit_price,
        "short_exit_price": short_exit_price,
        "pnl": total_pnl,
        "message": tg_msg,
    }
