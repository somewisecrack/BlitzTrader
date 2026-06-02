"""
tools/options_spread_portfolio.py — Open spread tracking, P&L monitoring, and exit management.

Responsibilities:
  - Load open spreads from state_manager on startup.
  - Compute live P&L for each spread from Shoonya quotes.
  - Check exit conditions on every scan tick:
      • Max-loss exit:  current_loss >= SPREAD_MAX_LOSS_EXIT_FRACTION * max_loss
      • Credit TP:      current_profit >= SPREAD_CREDIT_TP_FRACTION * max_profit
      • Debit TP:       current_profit >= SPREAD_DEBIT_TP_FRACTION * max_profit
      • EOD forced close: caller passes force=True at 3:15 PM IST
  - Close a spread: buy-back short leg first (reduce risk), then sell long leg.
    (On close, short leg goes first — the opposite of entry — to reduce net risk
     as fast as possible. Both legs are always closed; never leave single leg open.)
  - Return structured result dicts; never raises.
  - Thread-safe: all state mutations go through state_manager.

Exit order (close spread):
  Step 1: Buy-to-close short leg (LIMIT, best ask; market fallback after timeout)
  Step 2: Sell-to-close long leg (LIMIT, best bid; market fallback after timeout)

If step 1 fails:  log error, do NOT proceed to step 2 (position still intact).
If step 2 fails after step 1:  emergency market order for step 2. Log critical.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime
from typing import Optional

import pytz

from tools.options_spread_execution import OpenSpread

logger = logging.getLogger("BlitzTrader.SpreadPortfolio")

IST = pytz.timezone("Asia/Kolkata")

# Polling interval when waiting for close-leg fill (seconds)
_POLL_INTERVAL = 2
_DEFAULT_FILL_TIMEOUT = 300  # seconds


# ──────────────────────────────────────────────────────────────────────────────
#   LIVE P&L
# ──────────────────────────────────────────────────────────────────────────────

def _get_ltp(token: str, exchange: str, shoonya_client, live_feed) -> Optional[float]:
    """
    Fetch last traded price for an option token.
    Priority: WebSocket LTP → REST quote.
    Returns None if unavailable.
    """
    if live_feed:
        try:
            ltp = live_feed.get_ltp(token)
            if ltp:
                return float(ltp)
        except Exception:
            pass
    if shoonya_client:
        try:
            q = shoonya_client.get_quotes(exchange, token)
            if q:
                for key in ("lp", "ltp"):
                    val = q.get(key)
                    if val:
                        return float(val)
        except Exception:
            pass
    return None


def compute_spread_pnl(
    spread: OpenSpread,
    shoonya_client,
    live_feed,
) -> dict:
    """
    Compute current P&L for an open spread.

    For a BOUGHT option (long leg): current value = current LTP
    For a SOLD option (short leg):  current liability = current LTP

    current_value_long  = long_ltp  * lot_size * lots
    current_liability_short = short_ltp * lot_size * lots

    For DEBIT spreads (net_debit_or_credit >= 0):
      pnl = (long_ltp - short_ltp) * lot_size * lots - net_debit_or_credit * lot_size * lots
          = ((long_ltp - short_ltp) - net_debit_or_credit) * lot_size * lots

    For CREDIT spreads (net_debit_or_credit < 0, i.e. net_credit = -net_debit_or_credit):
      pnl = net_credit * lot_size * lots - (short_ltp - long_ltp) * lot_size * lots
          = (net_credit - (short_ltp - long_ltp)) * lot_size * lots

    Simplified uniformly:
      pnl = (long_ltp - long_fill) * lot_size * lots
          - (short_ltp - short_fill) * lot_size * lots

    Returns dict with keys: long_ltp, short_ltp, unrealized_pnl, data_ok
    """
    lot_size = spread.lot_size
    lots = spread.lots
    qty = lot_size * lots

    long_ltp = _get_ltp(spread.long_token, "NFO", shoonya_client, live_feed)
    short_ltp = _get_ltp(spread.short_token, "NFO", shoonya_client, live_feed)

    if long_ltp is None or short_ltp is None:
        return {
            "long_ltp": long_ltp,
            "short_ltp": short_ltp,
            "unrealized_pnl": None,
            "data_ok": False,
        }

    pnl = (
        (long_ltp - spread.long_fill_price) * qty
        - (short_ltp - spread.short_fill_price) * qty
    )

    return {
        "long_ltp": long_ltp,
        "short_ltp": short_ltp,
        "unrealized_pnl": pnl,
        "data_ok": True,
    }


def should_exit(
    spread: OpenSpread,
    pnl_data: dict,
    max_loss_exit_fraction: float = 0.60,
    credit_tp_fraction: float = 0.60,
    debit_tp_fraction: float = 0.70,
    force: bool = False,
) -> tuple[bool, str]:
    """
    Determine whether an open spread should be exited.

    Returns (True, reason) if exit is warranted, (False, "") otherwise.
    """
    if force:
        return True, "EOD forced close"

    if not pnl_data.get("data_ok"):
        return False, ""  # can't decide without data

    pnl = pnl_data["unrealized_pnl"]
    max_loss = spread.max_loss
    max_profit = spread.max_profit
    is_credit = spread.net_debit_or_credit < 0

    # Max-loss exit (loss side)
    if pnl is not None and max_loss > 0:
        loss = -pnl  # positive = losing money
        if loss >= max_loss_exit_fraction * max_loss:
            return True, (
                f"max-loss exit: loss ₹{loss:.2f} >= "
                f"{max_loss_exit_fraction:.0%} × max_loss ₹{max_loss:.2f}"
            )

    # Take-profit
    if pnl is not None and max_profit > 0:
        tp_fraction = credit_tp_fraction if is_credit else debit_tp_fraction
        if pnl >= tp_fraction * max_profit:
            return True, (
                f"take-profit: profit ₹{pnl:.2f} >= "
                f"{tp_fraction:.0%} × max_profit ₹{max_profit:.2f}"
            )

    return False, ""


# ──────────────────────────────────────────────────────────────────────────────
#   CLOSE ENGINE
# ──────────────────────────────────────────────────────────────────────────────

class SpreadPortfolio:
    """
    Manages all open spreads: P&L monitoring, exit checks, and close execution.

    Usage:
        portfolio = SpreadPortfolio(shoonya_client, state_manager, live_feed)
        # On each scan tick:
        exits = portfolio.check_and_exit_spreads(force_close_all=False)
        # At EOD:
        exits = portfolio.check_and_exit_spreads(force_close_all=True)
    """

    def __init__(
        self,
        shoonya_client,
        state_manager,
        live_feed=None,
        max_loss_exit_fraction: float = 0.60,
        credit_tp_fraction: float = 0.60,
        debit_tp_fraction: float = 0.70,
        fill_timeout_seconds: int = _DEFAULT_FILL_TIMEOUT,
        virtual: bool = True,
    ):
        self._client = shoonya_client
        self._state = state_manager
        self._feed = live_feed
        self._max_loss_frac = max_loss_exit_fraction
        self._credit_tp_frac = credit_tp_fraction
        self._debit_tp_frac = debit_tp_fraction
        self._fill_timeout = fill_timeout_seconds
        self._virtual = virtual

    # ── Portfolio accessors ───────────────────────────────────────────────────

    def get_open_spreads(self) -> list[OpenSpread]:
        """Load current open spreads from state."""
        try:
            state = self._state.get_state()
            raw = state.get("open_spreads", []) or []
            spreads = []
            for d in raw:
                try:
                    spreads.append(_dict_to_open_spread(d))
                except Exception:
                    logger.exception("Failed to deserialize spread: %r", d)
            return spreads
        except Exception:
            logger.exception("get_open_spreads: state read failed")
            return []

    def count_open_spreads(self) -> int:
        return len(self.get_open_spreads())

    # ── Scan tick ─────────────────────────────────────────────────────────────

    def check_and_exit_spreads(self, force_close_all: bool = False) -> list[dict]:
        """
        Check all open spreads and exit those that meet exit criteria.

        Called on every scan tick. At EOD, pass force_close_all=True.
        Returns list of exit result dicts (one per exited spread).
        """
        open_spreads = self.get_open_spreads()
        results = []

        for spread in open_spreads:
            pnl_data = compute_spread_pnl(spread, self._client, self._feed)
            do_exit, reason = should_exit(
                spread, pnl_data,
                max_loss_exit_fraction=self._max_loss_frac,
                credit_tp_fraction=self._credit_tp_frac,
                debit_tp_fraction=self._debit_tp_frac,
                force=force_close_all,
            )
            if do_exit:
                result = self.close_spread(spread, reason=reason, pnl_data=pnl_data)
                results.append(result)

        return results

    # ── Close a spread ────────────────────────────────────────────────────────

    def close_spread(
        self,
        spread: OpenSpread,
        reason: str = "",
        pnl_data: Optional[dict] = None,
    ) -> dict:
        """
        Close both legs of an open spread.

        In virtual mode fills are taken from live quotes (pnl_data) if available,
        otherwise from entry prices.  No broker API calls are made.

        In live mode close order is: buy-to-close short leg FIRST (reduces naked
        risk), then sell-to-close long leg.

        Returns structured result dict.
        """
        qty = spread.lot_size * spread.lots
        logger.info(
            "close_spread[%s]: closing %s %s reason=%r qty=%d mode=%s",
            spread.spread_id, spread.symbol, spread.spread_type, reason, qty,
            "VIRTUAL" if self._virtual else "LIVE",
        )

        if self._virtual:
            return self._close_spread_virtual(spread, reason, pnl_data, qty)
        return self._close_spread_live(spread, reason, pnl_data, qty)

    def _close_spread_virtual(
        self,
        spread: OpenSpread,
        reason: str,
        pnl_data: Optional[dict],
        qty: int,
    ) -> dict:
        """
        Simulate spread close without broker API calls.
        Uses current LTP from pnl_data if available; falls back to entry prices.
        """
        data = pnl_data or {}
        btc_fill = data.get("short_ltp") or spread.short_fill_price
        stc_fill = data.get("long_ltp") or spread.long_fill_price

        realized_pnl = (
            (stc_fill - spread.long_fill_price) * qty
            - (btc_fill - spread.short_fill_price) * qty
        )

        self._remove_spread_from_state(spread.spread_id)
        self._update_daily_pnl(realized_pnl)

        message = (
            f"[VIRTUAL] Spread {spread.spread_id} closed ({reason}): "
            f"{spread.symbol} {spread.spread_type}\n"
            f"  Short closed: {spread.short_tsym} @ ₹{btc_fill:.2f}\n"
            f"  Long  closed: {spread.long_tsym} @ ₹{stc_fill:.2f}\n"
            f"  Realized P&L: ₹{realized_pnl:+.2f}"
        )
        logger.info("close_spread[%s]: %s", spread.spread_id, message)

        return {
            "ok": True,
            "spread_id": spread.spread_id,
            "symbol": spread.symbol,
            "spread_type": spread.spread_type,
            "reason": reason,
            "realized_pnl": realized_pnl,
            "short_close_price": btc_fill,
            "long_close_price": stc_fill,
            "message": message,
        }

    def _close_spread_live(
        self,
        spread: OpenSpread,
        reason: str,
        pnl_data: Optional[dict],
        qty: int,
    ) -> dict:
        """
        Live broker path: buy-to-close short leg first, then sell-to-close long leg.
        """
        # ── Step 1: Buy-to-close short leg ────────────────────────────────
        btc_order_id, err = self._place_close_order(
            tsym=spread.short_tsym,
            action="BUY",   # buy back the short
            qty=qty,
            ref_price=pnl_data.get("short_ltp") if pnl_data else None,
        )
        if btc_order_id is None:
            logger.error(
                "close_spread[%s]: short leg close failed — %s; leaving spread open",
                spread.spread_id, err,
            )
            return {
                "ok": False,
                "spread_id": spread.spread_id,
                "error": f"Short leg close failed: {err}",
                "emergency": False,
            }

        btc_fill, fill_err = self._wait_for_fill(btc_order_id, spread.short_tsym)
        if btc_fill is None:
            self._cancel_order(btc_order_id)
            logger.error(
                "close_spread[%s]: short leg fill timeout — %s; leaving spread open",
                spread.spread_id, fill_err,
            )
            return {
                "ok": False,
                "spread_id": spread.spread_id,
                "error": f"Short leg close fill failed: {fill_err}",
                "emergency": False,
            }

        logger.info(
            "close_spread[%s]: short leg closed %s @ ₹%.2f",
            spread.spread_id, spread.short_tsym, btc_fill,
        )

        # ── Step 2: Sell-to-close long leg ─────────────────────────────────
        stc_order_id, err = self._place_close_order(
            tsym=spread.long_tsym,
            action="SELL",
            qty=qty,
            ref_price=pnl_data.get("long_ltp") if pnl_data else None,
        )
        if stc_order_id is None:
            logger.critical(
                "close_spread[%s]: long leg close FAILED after short already closed — "
                "NAKED LONG EXPOSURE: %s. Attempting market order.",
                spread.spread_id, spread.long_tsym,
            )
            self._emergency_close_long(spread.long_tsym, qty)
            self._remove_spread_from_state(spread.spread_id)
            return {
                "ok": False,
                "spread_id": spread.spread_id,
                "error": f"Long leg close failed (emergency market attempted): {err}",
                "emergency": True,
            }

        stc_fill, fill_err = self._wait_for_fill(stc_order_id, spread.long_tsym)
        if stc_fill is None:
            self._cancel_order(stc_order_id)
            logger.critical(
                "close_spread[%s]: long leg fill timeout after short closed — "
                "emergency market close of %s",
                spread.spread_id, spread.long_tsym,
            )
            self._emergency_close_long(spread.long_tsym, qty)
            self._remove_spread_from_state(spread.spread_id)
            return {
                "ok": False,
                "spread_id": spread.spread_id,
                "error": f"Long leg fill timeout (emergency market attempted): {fill_err}",
                "emergency": True,
            }

        logger.info(
            "close_spread[%s]: long leg closed %s @ ₹%.2f",
            spread.spread_id, spread.long_tsym, stc_fill,
        )

        realized_pnl = (
            (stc_fill - spread.long_fill_price) * qty
            - (btc_fill - spread.short_fill_price) * qty
        )

        self._remove_spread_from_state(spread.spread_id)
        self._update_daily_pnl(realized_pnl)

        message = (
            f"Spread {spread.spread_id} closed ({reason}): "
            f"{spread.symbol} {spread.spread_type}\n"
            f"  Short closed: {spread.short_tsym} @ ₹{btc_fill:.2f}\n"
            f"  Long  closed: {spread.long_tsym} @ ₹{stc_fill:.2f}\n"
            f"  Realized P&L: ₹{realized_pnl:+.2f}"
        )
        logger.info("close_spread[%s]: %s", spread.spread_id, message)

        return {
            "ok": True,
            "spread_id": spread.spread_id,
            "symbol": spread.symbol,
            "spread_type": spread.spread_type,
            "reason": reason,
            "realized_pnl": realized_pnl,
            "short_close_price": btc_fill,
            "long_close_price": stc_fill,
            "message": message,
        }

    # ── Order helpers ─────────────────────────────────────────────────────────

    def _place_close_order(
        self,
        tsym: str,
        action: str,
        qty: int,
        ref_price: Optional[float],
    ) -> tuple[Optional[str], str]:
        """Place a LIMIT close order. Falls back to market if ref_price unavailable."""
        if not self._client:
            return None, "no Shoonya client (paper mode)"
        price_type = "LMT" if ref_price and ref_price > 0 else "MKT"
        price = ref_price if price_type == "LMT" else 0
        # Shoonya API: buy_or_sell uses 'B' / 'S' (not full words)
        shoonya_side = "B" if action == "BUY" else "S"
        try:
            resp = self._client.place_order(
                buy_or_sell=shoonya_side,
                product_type="M",
                exchange="NFO",
                tradingsymbol=tsym,
                quantity=qty,
                discloseqty=0,
                price_type=price_type,
                price=price,
                trigger_price=None,
                retention="DAY",
                remarks=f"BlitzTrader-CLOSE-{tsym}",
            )
            if resp and resp.get("stat") == "Ok":
                return resp.get("norenordno"), ""
            err = (resp or {}).get("emsg", str(resp))
            return None, f"place_order failed: {err}"
        except Exception as e:
            logger.exception("_place_close_order(%s) raised", tsym)
            return None, f"exception: {e}"

    def _wait_for_fill(self, order_id: str, tsym: str) -> tuple[Optional[float], str]:
        if not self._client:
            return None, "no Shoonya client (paper mode)"
        deadline = time.time() + self._fill_timeout
        while time.time() < deadline:
            try:
                orders = self._client.get_order_book()
                for o in (orders or []):
                    if str(o.get("norenordno")) == str(order_id):
                        status = (o.get("status") or "").upper()
                        if status in ("COMPLETE", "FILLED"):
                            try:
                                return float(o.get("avgprc") or o.get("price") or 0), ""
                            except (TypeError, ValueError):
                                return None, "fill price parse error"
                        if status in ("REJECTED", "CANCELLED", "CANCELED"):
                            return None, f"order {status}: {o.get('rejreason', '')}"
            except Exception:
                logger.exception("_wait_for_fill: order book poll failed")
            time.sleep(_POLL_INTERVAL)
        return None, f"fill timeout after {self._fill_timeout}s for {tsym}"

    def _cancel_order(self, order_id: str) -> None:
        if not self._client or not order_id:
            return
        try:
            self._client.cancel_order(order_id)
        except Exception:
            logger.exception("_cancel_order(%s) failed (non-fatal)", order_id)

    def _emergency_close_long(self, tsym: str, qty: int) -> None:
        if not self._client:
            logger.error("No Shoonya client — cannot emergency-close %s", tsym)
            return
        try:
            resp = self._client.place_order(
                buy_or_sell="S",           # Shoonya API: 'S' not 'SELL'
                product_type="M",
                exchange="NFO",
                tradingsymbol=tsym,
                quantity=qty,
                discloseqty=0,
                price_type="MKT",
                price=0,
                trigger_price=None,
                retention="DAY",
                remarks="BlitzTrader-EMERGENCY-CLOSE",
            )
            if resp and resp.get("stat") == "Ok":
                logger.warning("Emergency close placed for %s", tsym)
            else:
                logger.critical("Emergency close FAILED for %s: %s", tsym, resp)
        except Exception:
            logger.exception("_emergency_close_long raised for %s", tsym)

    # ── State mutations ───────────────────────────────────────────────────────

    def _remove_spread_from_state(self, spread_id: str) -> None:
        try:
            state = self._state.get_state()
            open_spreads = [
                s for s in (state.get("open_spreads", []) or [])
                if s.get("spread_id") != spread_id
            ]
            self._state.update_state(open_spreads=open_spreads)
        except Exception:
            logger.exception("_remove_spread_from_state failed for %s (non-fatal)", spread_id)

    def _update_daily_pnl(self, realized_pnl: float) -> None:
        try:
            state = self._state.get_state()
            current = float(state.get("daily_pnl", 0) or 0)
            self._state.update_state(daily_pnl=current + realized_pnl)
        except Exception:
            logger.exception("_update_daily_pnl failed (non-fatal)")

    # ── Status summary ────────────────────────────────────────────────────────

    def build_status_lines(self, shoonya_client=None, live_feed=None) -> list[str]:
        """
        Return human-readable status lines for all open spreads (for Telegram /status).
        """
        client = shoonya_client or self._client
        feed = live_feed or self._feed
        spreads = self.get_open_spreads()
        if not spreads:
            return ["- No open spreads"]

        lines = []
        for i, sp in enumerate(spreads, 1):
            pnl_data = compute_spread_pnl(sp, client, feed)
            pnl_str = (
                f"₹{pnl_data['unrealized_pnl']:+.2f}"
                if pnl_data.get("data_ok") and pnl_data["unrealized_pnl"] is not None
                else "N/A"
            )
            lines.append(
                f"{i}. [{sp.spread_id}] {sp.symbol} {sp.spread_type} "
                f"expiry={sp.expiry} "
                f"long={sp.long_tsym}@{sp.long_fill_price:.2f} "
                f"short={sp.short_tsym}@{sp.short_fill_price:.2f} "
                f"unrealized={pnl_str}"
            )
        return lines


# ──────────────────────────────────────────────────────────────────────────────
#   DESERIALIZATION
# ──────────────────────────────────────────────────────────────────────────────

def _dict_to_open_spread(d: dict) -> OpenSpread:
    """Reconstruct an OpenSpread from a state dict."""
    return OpenSpread(
        spread_id=d["spread_id"],
        symbol=d["symbol"],
        spread_type=d["spread_type"],
        direction=d["direction"],
        expiry=d["expiry"],
        lot_size=int(d["lot_size"]),
        lots=int(d["lots"]),
        long_tsym=d["long_tsym"],
        long_token=d["long_token"],
        long_action=d.get("long_action", "BUY"),
        long_strike=int(d["long_strike"]),
        long_option_type=d["long_option_type"],
        long_fill_price=float(d["long_fill_price"]),
        long_order_id=d["long_order_id"],
        short_tsym=d["short_tsym"],
        short_token=d["short_token"],
        short_action=d.get("short_action", "SELL"),
        short_strike=int(d["short_strike"]),
        short_option_type=d["short_option_type"],
        short_fill_price=float(d["short_fill_price"]),
        short_order_id=d["short_order_id"],
        net_debit_or_credit=float(d["net_debit_or_credit"]),
        max_profit=float(d["max_profit"]),
        max_loss=float(d["max_loss"]),
        breakeven=float(d["breakeven"]),
        opened_at=d["opened_at"],
        signal_id=d.get("signal_id", ""),
        strategy=d.get("strategy", ""),
        underlying_at_entry=float(d.get("underlying_at_entry", 0)),
    )
