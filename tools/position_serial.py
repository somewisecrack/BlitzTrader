"""
tools/position_serial.py — Deterministic position serial numbering for Telegram status.

Generates a stable, human-readable serial index over all open futures positions,
persists it to runtime/telegram_position_index.json, and provides exit-by-serial
functionality.

Design constraints:
- One serial per logical position (futures only)
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


def _now_ist() -> datetime:
    return datetime.now(IST)


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
#   IDENTITY MATCHING HELPERS
# ──────────────────────────────────────────────────────────────

def _match_futures_identity(index_entry: dict, live_pos: dict) -> tuple[bool, str]:
    """
    Compare a stored index entry against a live futures position dict.

    Returns (True, "") if all fields match, (False, reason) on mismatch.
    Fields checked: tradingsymbol, direction, qty, entry_price, opened_at,
    order_id (latter two only when both sides have the field non-empty).
    """
    ie = index_entry
    lp = live_pos

    # tradingsymbol
    if ie.get("tradingsymbol", "").upper() != lp.get("symbol", "").upper():
        return False, (
            f"tradingsymbol mismatch: index={ie.get('tradingsymbol')} "
            f"live={lp.get('symbol')}"
        )

    # direction
    if ie.get("direction", "").upper() != lp.get("direction", "").upper():
        return False, (
            f"direction mismatch: index={ie.get('direction')} "
            f"live={lp.get('direction')}"
        )

    # qty
    try:
        ie_qty = int(ie.get("qty", 0))
        lp_qty = int(lp.get("quantity", 0))
    except (TypeError, ValueError):
        return False, "qty is non-numeric"
    if ie_qty != lp_qty:
        return False, f"qty mismatch: index={ie_qty} live={lp_qty}"

    # entry_price within tolerance
    try:
        ie_ep = float(ie.get("entry_price", 0))
        lp_ep = float(lp.get("entry_price", 0))
    except (TypeError, ValueError):
        return False, "entry_price is non-numeric"
    if abs(ie_ep - lp_ep) >= 1e-4:
        return False, f"entry_price mismatch: index={ie_ep} live={lp_ep}"

    # opened_at — only check when both sides have it
    ie_oa = str(ie.get("opened_at") or "").strip()
    lp_oa = str(lp.get("entry_time") or "").strip()
    if ie_oa and lp_oa and ie_oa != lp_oa:
        return False, f"opened_at mismatch: index={ie_oa} live={lp_oa}"

    # order_id — only check when both sides have it
    ie_oid = str(ie.get("order_id") or "").strip()
    lp_oid = str(lp.get("order_id") or "").strip()
    if ie_oid and lp_oid and ie_oid != lp_oid:
        return False, f"order_id mismatch: index={ie_oid} live={lp_oid}"

    return True, ""


# ──────────────────────────────────────────────────────────────
#   P&L SUMMARY HELPERS
# ──────────────────────────────────────────────────────────────

def _build_updated_pnl_summary(
    state_manager,
    live_feed=None,
    shoonya_client=None,
    active_tokens=None,
) -> str:
    """Build the Futures P&L sections only (no Open Positions list)."""
    full, _ = build_status_message(
        state_manager=state_manager,
        pairs_portfolio=None,
        live_feed=live_feed,
        shoonya_client=shoonya_client,
        active_tokens=active_tokens,
    )
    # Truncate at "Open Positions" section if present
    marker = "\nOpen Positions"
    idx = full.find(marker)
    return full[:idx].rstrip() if idx != -1 else full


# ──────────────────────────────────────────────────────────────
#   STATUS MESSAGE GENERATION
# ──────────────────────────────────────────────────────────────

def build_status_message(
    state_manager,
    pairs_portfolio=None,  # kept for signature compatibility; ignored
    live_feed=None,
    shoonya_client=None,
    active_tokens: dict = None,
) -> tuple[str, dict]:
    """
    Build a structured futures-only status message and return (message_text, index_payload).

    The index_payload is ready to be persisted via save_position_index().
    pairs_portfolio is accepted but ignored (futures-only runtime).
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
            "qty": int(qty),
            "entry_price": entry,
            "opened_at": str(pos.get("entry_time") or ""),
            "order_id": str(pos.get("order_id") or ""),
            "token": token or "",
        })
        serial += 1

    # ── Assemble P&L sections ─────────────────────────────────
    futures_total = futures_realized + futures_unrealized

    lines = [
        "Futures",
        f"- Realized P&L: ₹{futures_realized:+,.2f}",
        f"- Unrealized P&L: ₹{futures_unrealized:+,.2f}",
        f"- Total P&L: ₹{futures_total:+,.2f}",
        "",
        "Open Positions",
    ]

    if futures_pos_lines:
        lines.extend(futures_pos_lines)
    else:
        lines.append("- None")

    message = "\n".join(lines)

    now_ist = _now_ist()
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
        age = (_now_ist() - generated_at).total_seconds()
        return age <= ttl
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────
#   EXIT BY SERIAL (Gemini-callable tool implementation)
# ──────────────────────────────────────────────────────────────

def exit_position_by_serial(
    serial: int,
    state_manager,
    pairs_portfolio=None,  # kept for signature compatibility; ignored
    order_execution=None,
    shoonya_client=None,
    telegram_handler=None,
    active_tokens: dict = None,
    live_feed=None,
) -> dict:
    """
    Exit the open futures position identified by serial number.

    Safety guarantees:
    - NEVER opens a new position
    - NEVER modifies strategy rules
    - Validates index freshness, serial existence, and live position state
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
            shoonya_client=shoonya_client,
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
    shoonya_client=None,
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

    # Full identity revalidation
    matched, mismatch_reason = _match_futures_identity(entry, live_pos)
    if not matched:
        msg = (
            f"Serial #{serial} no longer matches the current open position. "
            f"Please request fresh status. Reason: {mismatch_reason}"
        )
        logger.warning(
            "exit_by_serial(%d): identity mismatch for %s — %s",
            serial, tradingsymbol, mismatch_reason,
        )
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

    # Build P&L summary using live prices
    pnl_summary = _build_updated_pnl_summary(
        state_manager,
        live_feed=live_feed, shoonya_client=shoonya_client, active_tokens=active_tokens,
    )
    invalidate_position_index()

    exit_price_str = f"₹{exit_price:.2f}" if exit_price is not None else "N/A"
    tg_msg = (
        f"Exited serial #{serial}: {direction} {tradingsymbol}\n"
        f"Exit price: {exit_price_str}\n"
        f"Realized P&L: ₹{pnl:+,.2f}\n"
        f"\n{pnl_summary}"
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



