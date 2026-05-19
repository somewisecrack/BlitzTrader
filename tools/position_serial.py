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
- Pairs exit is VIRTUAL ONLY — no Shoonya broker orders placed
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


def _match_pair_identity(index_entry: dict, live_pos) -> tuple[bool, str]:
    """
    Compare a stored index entry against a live PairPosition dataclass instance.

    Returns (True, "") on match, (False, reason) on mismatch.
    """
    ie = index_entry

    # pair_name
    if ie.get("pair_name", "") != live_pos.pair_name:
        return False, (
            f"pair_name mismatch: index={ie.get('pair_name')} "
            f"live={live_pos.pair_name}"
        )

    # opened_at — only when both sides non-empty
    ie_oa = str(ie.get("opened_at") or "").strip()
    lp_oa = str(live_pos.opened_at or "").strip()
    if ie_oa and lp_oa and ie_oa != lp_oa:
        return False, f"opened_at mismatch: index={ie_oa} live={lp_oa}"

    ll = live_pos.long_leg
    sl = live_pos.short_leg

    # long leg symbol / tradingsymbol / token
    if ie.get("long_symbol", "") != ll.symbol:
        return False, f"long_symbol mismatch: index={ie.get('long_symbol')} live={ll.symbol}"
    if ie.get("long_tradingsymbol", "") != ll.tradingsymbol:
        return False, (
            f"long_tradingsymbol mismatch: index={ie.get('long_tradingsymbol')} "
            f"live={ll.tradingsymbol}"
        )
    ie_lt = str(ie.get("long_token") or "").strip()
    lp_lt = str(ll.token or "").strip()
    if ie_lt and lp_lt and ie_lt != lp_lt:
        return False, f"long_token mismatch: index={ie_lt} live={lp_lt}"

    # short leg symbol / tradingsymbol / token
    if ie.get("short_symbol", "") != sl.symbol:
        return False, f"short_symbol mismatch: index={ie.get('short_symbol')} live={sl.symbol}"
    if ie.get("short_tradingsymbol", "") != sl.tradingsymbol:
        return False, (
            f"short_tradingsymbol mismatch: index={ie.get('short_tradingsymbol')} "
            f"live={sl.tradingsymbol}"
        )
    ie_st = str(ie.get("short_token") or "").strip()
    lp_st = str(sl.token or "").strip()
    if ie_st and lp_st and ie_st != lp_st:
        return False, f"short_token mismatch: index={ie_st} live={lp_st}"

    # long qty
    try:
        if int(ie.get("long_qty", 0)) != int(ll.qty):
            return False, f"long_qty mismatch: index={ie.get('long_qty')} live={ll.qty}"
    except (TypeError, ValueError):
        return False, "long_qty is non-numeric"

    # short qty
    try:
        if int(ie.get("short_qty", 0)) != int(sl.qty):
            return False, f"short_qty mismatch: index={ie.get('short_qty')} live={sl.qty}"
    except (TypeError, ValueError):
        return False, "short_qty is non-numeric"

    # long entry_price
    try:
        ie_lep = float(ie.get("long_entry_price", 0))
        lp_lep = float(ll.entry_price)
    except (TypeError, ValueError):
        return False, "long_entry_price is non-numeric"
    if abs(ie_lep - lp_lep) >= 1e-4:
        return False, f"long_entry_price mismatch: index={ie_lep} live={lp_lep}"

    # short entry_price
    try:
        ie_sep = float(ie.get("short_entry_price", 0))
        lp_sep = float(sl.entry_price)
    except (TypeError, ValueError):
        return False, "short_entry_price is non-numeric"
    if abs(ie_sep - lp_sep) >= 1e-4:
        return False, f"short_entry_price mismatch: index={ie_sep} live={lp_sep}"

    return True, ""


# ──────────────────────────────────────────────────────────────
#   P&L SUMMARY HELPERS
# ──────────────────────────────────────────────────────────────

def _build_updated_pnl_summary(
    state_manager,
    pairs_portfolio,
    live_feed=None,
    shoonya_client=None,
    active_tokens=None,
) -> str:
    """Build the Overall/Futures/Pairs P&L sections only (no Open Positions list).

    Delegates to build_status_message() so unrealized P&L uses live_feed/shoonya_client,
    and pairs P&L comes from the real pairs_portfolio (not a null stub).
    """
    full, _ = build_status_message(
        state_manager=state_manager,
        pairs_portfolio=pairs_portfolio,
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

        # FIX 1: store full identity fields for futures
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

        # FIX 2: store full identity fields for pairs
        index_entries.append({
            "serial": serial,
            "type": "pairs",
            "pair_name": pair_name,
            "opened_at": str(pos.opened_at or ""),
            "timeframe": getattr(pos, "timeframe", ""),
            "long_symbol": ll.symbol,
            "long_tradingsymbol": ll.tradingsymbol,
            "long_token": str(ll.token or ""),
            "long_qty": int(ll.qty),
            "long_entry_price": float(ll.entry_price),
            "short_symbol": sl_leg.symbol,
            "short_tradingsymbol": sl_leg.tradingsymbol,
            "short_token": str(sl_leg.token or ""),
            "short_qty": int(sl_leg.qty),
            "short_entry_price": float(sl_leg.entry_price),
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
    - Pairs exit is VIRTUAL ONLY — no Shoonya broker orders
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
            pairs_portfolio=pairs_portfolio,
            shoonya_client=shoonya_client,
        )
    elif pos_type == "pairs":
        return _exit_pairs_by_serial(
            serial, entry, pairs_portfolio, shoonya_client,
            state_manager, telegram_handler, live_feed, active_tokens,
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
    pairs_portfolio=None,
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

    # FIX 1: full identity revalidation
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

    # FIX 4: build P&L summary using real pairs_portfolio and live prices
    pnl_summary = _build_updated_pnl_summary(
        state_manager, pairs_portfolio,
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


def _exit_pairs_by_serial(
    serial: int,
    entry: dict,
    pairs_portfolio,
    shoonya_client,
    state_manager,
    telegram_handler,
    live_feed,
    active_tokens: dict,
) -> dict:
    """
    Close BOTH legs of a pairs position identified by serial.

    VIRTUAL ONLY — no Shoonya broker orders are placed.
    Uses _close_leg() directly, exactly like EOD pairs close behavior.
    """
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

    # FIX 2: full identity revalidation
    matched, mismatch_reason = _match_pair_identity(entry, live_pos)
    if not matched:
        msg = (
            f"Serial #{serial} no longer matches the current open pair position. "
            f"Please request fresh status. Reason: {mismatch_reason}"
        )
        logger.warning(
            "exit_by_serial(%d): identity mismatch for pair %s — %s",
            serial, pair_name, mismatch_reason,
        )
        invalidate_position_index()
        return {"success": False, "error": msg}

    ll = live_pos.long_leg
    sl_leg = live_pos.short_leg

    # FIX 3: handle partially-closed pair before touching state
    if bool(ll.closed_at) != bool(sl_leg.closed_at):
        msg = (
            f"Pair serial #{serial} ({pair_name}) has one leg already closed — "
            "abnormal state. Manual review required."
        )
        logger.error(
            "exit_by_serial(%d): pair %s is in partial-close state "
            "(long_closed=%s short_closed=%s)",
            serial, pair_name, bool(ll.closed_at), bool(sl_leg.closed_at),
        )
        return {"success": False, "error": msg}

    # FIX 3: resolve both exit prices BEFORE touching any state
    from broker.shoonya_client import ResolvedScrip

    long_scrip = ResolvedScrip(ll.symbol, ll.tradingsymbol, ll.token)
    short_scrip = ResolvedScrip(sl_leg.symbol, sl_leg.tradingsymbol, sl_leg.token)

    long_exit_price = pairs_portfolio._entry_price(shoonya_client, long_scrip, "SELL")
    short_exit_price = pairs_portfolio._entry_price(shoonya_client, short_scrip, "BUY")

    if long_exit_price is None and short_exit_price is None:
        msg = (
            f"Could not safely close pair serial #{serial} because price was "
            f"unavailable for both {ll.symbol} and {sl_leg.symbol}. No state changed."
        )
        logger.warning("exit_by_serial(%d): both exit prices unavailable for pair %s", serial, pair_name)
        return {"success": False, "error": msg}

    if long_exit_price is None:
        msg = (
            f"Could not safely close pair serial #{serial} because price was "
            f"unavailable for {ll.symbol} (LONG leg). No state changed."
        )
        logger.warning("exit_by_serial(%d): long exit price unavailable for %s", serial, ll.symbol)
        return {"success": False, "error": msg}

    if short_exit_price is None:
        msg = (
            f"Could not safely close pair serial #{serial} because price was "
            f"unavailable for {sl_leg.symbol} (SHORT leg). No state changed."
        )
        logger.warning("exit_by_serial(%d): short exit price unavailable for %s", serial, sl_leg.symbol)
        return {"success": False, "error": msg}

    # Both prices confirmed — now close virtually (NO broker orders)
    logger.info(
        "exit_by_serial(%d): virtually closing pair %s "
        "(long=%s @ %.2f, short=%s @ %.2f)",
        serial, pair_name,
        ll.symbol, long_exit_price,
        sl_leg.symbol, short_exit_price,
    )

    long_pnl = pairs_portfolio._close_leg(ll, long_exit_price)
    short_pnl = pairs_portfolio._close_leg(sl_leg, short_exit_price)

    live_pos.closed_at = _now_ist().isoformat()
    live_pos.pnl = round(long_pnl + short_pnl, 2)
    pairs_portfolio._persist()

    total_pnl = live_pos.pnl

    # FIX 4: P&L summary using real pairs_portfolio and live prices; invalidate AFTER state is settled
    pnl_summary = _build_updated_pnl_summary(
        state_manager, pairs_portfolio,
        live_feed=live_feed, shoonya_client=shoonya_client, active_tokens=active_tokens,
    )
    invalidate_position_index()

    tg_msg = (
        f"Exited serial #{serial}: pair {pair_name}\n"
        f"  LONG {long_sym} exit: ₹{long_exit_price:.2f} | SHORT {short_sym} exit: ₹{short_exit_price:.2f}\n"
        f"Realized P&L: ₹{total_pnl:+,.2f}\n"
        f"\n{pnl_summary}"
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

