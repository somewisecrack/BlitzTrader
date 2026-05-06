"""
tools/order_execution.py — Virtual order execution with guardrails.

Guardrails are enforced HERE in Python — Claude cannot reason around them.
Fill prices come from live WebSocket data for realism.
"""
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

import pytz

logger = logging.getLogger("BlitzTrader.OrderExecution")

IST = pytz.timezone("Asia/Kolkata")

# Known bare logical names that must not be used directly for execution.
# Callers must use the resolved futures tsym (e.g. NIFTY28APR26F).
_BARE_LOGICAL_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}


class OrderExecutionTools:
    """
    Order execution tool implementations with guardrail enforcement.
    """

    def __init__(
        self,
        state_manager,
        virtual_ledger,
        live_feed,
        shoonya_client,
        max_positions: int = 3,
        max_daily_trades: int = 10,
        max_risk_amount: float = 25000,  # 5% of 5L
        max_daily_loss: float = 25000,  # 5% of 5L
        no_entry_after: str = "15:05",
        active_tokens: dict = None,
    ):
        self._state = state_manager
        self._ledger = virtual_ledger
        self._feed = live_feed
        self._client = shoonya_client
        self._max_positions = max_positions
        self._max_daily_trades = max_daily_trades
        self._max_risk_amount = max_risk_amount
        self._max_daily_loss = max_daily_loss
        self._no_entry_after = no_entry_after

        # active_tokens: {symbol: {exchange, token, tsym, ...}}
        # Pre-populated with resolved futures tokens from main.py
        self._active_tokens = active_tokens or {}
        self._token_cache = {}

        # Limit order monitoring
        self._limit_order_callbacks = {}

    # ──────────────────────────────────────────────────────────
    #   GUARDRAILS (enforced before every order)
    # ──────────────────────────────────────────────────────────

    def _check_guardrails(self, is_new_entry: bool = True, symbol: str = "") -> Optional[str]:
        """
        Run all guardrail checks. Returns error message if blocked, None if OK.
        """
        state = self._state.get_state()

        # Check if trading is stopped (daily loss limit hit)
        if state.get("is_stopped"):
            return (
                "BLOCKED: Trading is stopped for today. "
                "Daily loss limit was hit. No more orders allowed."
            )

        # Check if paused (user sent /pause)
        if is_new_entry and state.get("is_paused"):
            return (
                "BLOCKED: Trading is paused by user command. "
                "Wait for /resume before opening new positions."
            )

        if not is_new_entry:
            return None  # Exits are always allowed

        open_positions = state.get("positions", [])
        pending_orders = state.get("pending_orders", [])
        completed_trades = state.get("trades", [])

        # Check daily trade cap. Count completed trades plus open/pending entries
        # so intraday restarts and still-open positions cannot bypass the limit.
        daily_entries = len(completed_trades) + len(open_positions) + len(pending_orders)
        if daily_entries >= self._max_daily_trades:
            return (
                f"BLOCKED: Daily trade cap reached ({daily_entries}/"
                f"{self._max_daily_trades}). No more new entries allowed today. "
                "Exits remain allowed."
            )

        # Check max positions
        if len(open_positions) >= self._max_positions:
            return (
                f"BLOCKED: Already at maximum {self._max_positions} open positions. "
                f"Close an existing position before opening a new one. "
                f"Current positions: {[p['symbol'] for p in open_positions]}"
            )

        instrument = self._logical_instrument(symbol)
        if instrument:
            for pos in open_positions:
                if self._logical_instrument(pos.get("symbol", "")) == instrument:
                    return (
                        f"BLOCKED: No pyramiding. Already have an open {instrument} "
                        f"position ({pos.get('direction')} {pos.get('quantity')}x "
                        f"{pos.get('symbol')}). Close it before opening another "
                        f"{instrument} position."
                    )
            for order in pending_orders:
                if self._logical_instrument(order.get("symbol", "")) == instrument:
                    return (
                        f"BLOCKED: No pyramiding. Pending {instrument} order "
                        f"{order.get('order_id')} already exists. Cancel or let it "
                        f"expire before placing another {instrument} entry."
                    )

        # Check time cutoff
        now_ist = datetime.now(IST)
        cutoff_h, cutoff_m = map(int, self._no_entry_after.split(":"))
        cutoff_time = now_ist.replace(hour=cutoff_h, minute=cutoff_m, second=0)
        if now_ist >= cutoff_time:
            return (
                f"BLOCKED: No new entries after {self._no_entry_after} IST. "
                f"Current time: {now_ist.strftime('%H:%M:%S')} IST. "
                "You may only close existing positions."
            )

        # Check daily loss limit
        daily_pnl = state.get("daily_pnl", 0)
        if daily_pnl <= -self._max_daily_loss:
            self._state.update_state(is_stopped=True)
            return (
                f"BLOCKED: Daily loss limit hit. P&L: ₹{daily_pnl:,.0f} "
                f"(limit: -₹{self._max_daily_loss:,.0f}). "
                "Close all positions immediately and stop trading."
            )

        return None

    # ──────────────────────────────────────────────────────────
    #   TOOLS (callable by Claude)
    # ──────────────────────────────────────────────────────────

    def place_virtual_order(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
    ) -> dict:
        """
        Place a virtual order (MARKET or LIMIT).

        :param symbol:     Futures trading symbol (e.g., 'NIFTY28APR26F').
                           Options (CE/PE) are BLOCKED — use futures tsym only.
        :param direction:  'BUY' or 'SELL'
        :param quantity:    Number of units
        :param order_type: 'MARKET' or 'LIMIT'
        :param price:      Required for LIMIT orders, ignored for MARKET
        :param stop_loss:  Optional stop-loss price for auto-close
        :param target:     Optional target price for auto-close
        :returns: Order confirmation or error
        """
        direction = direction.upper()
        order_type = order_type.upper()

        if direction not in ("BUY", "SELL"):
            return {"error": f"Invalid direction: {direction}. Use BUY or SELL."}
        if order_type not in ("MARKET", "LIMIT"):
            return {"error": f"Invalid order_type: {order_type}. Use MARKET or LIMIT."}
        if order_type == "LIMIT" and price is None:
            return {"error": "LIMIT orders require a price."}
        if quantity <= 0:
            return {"error": f"Invalid quantity: {quantity}. Must be positive."}

        # Enforce Futures-Only Guardrail: only approved index futures are allowed.
        # Options (CE/PE) are NOT permitted for live execution.
        sym_up = symbol.upper().strip()

        # Reject bare logical names — caller must use the resolved futures tsym.
        if sym_up in _BARE_LOGICAL_NAMES:
            return {
                "error": (
                    f"BLOCKED: bare logical name '{symbol}' not accepted. "
                    f"Use the resolved futures tsym "
                    f"(e.g. NIFTY28APR26F, BANKNIFTY28APR26F, FINNIFTY28APR26F). "
                    f"Check the ACTIVE FUTURES INSTRUMENTS section in context for the correct tsym."
                ),
                "status": "REJECTED",
            }

        if not (sym_up.startswith("NIFTY") or sym_up.startswith("BANKNIFTY") or sym_up.startswith("FINNIFTY")):
            return {
                "error": "BLOCKED: Only NIFTY, BANKNIFTY, and FINNIFTY futures are allowed.",
                "status": "REJECTED",
            }
        if sym_up.endswith("CE") or sym_up.endswith("PE"):
            return {
                "error": (
                    "BLOCKED: Options (CE/PE) are disabled for live execution. "
                    "Use the resolved NIFTY/BANKNIFTY/FINNIFTY futures tsym "
                    "(e.g. NIFTY28APR26F) instead."
                ),
                "status": "REJECTED",
            }

        lot_size = self._ledger.get_lot_size(symbol)
        if not lot_size:
            return {
                "error": f"BLOCKED: Cannot determine futures lot size for {symbol}.",
                "status": "REJECTED",
            }
        if quantity != lot_size:
            return {
                "error": (
                    f"BLOCKED: Only 1 lot allowed per trade for {symbol}. "
                    f"Required quantity is exactly {lot_size}; requested {quantity}."
                ),
                "status": "REJECTED",
            }

        # Run state guardrails
        guardrail_error = self._check_guardrails(is_new_entry=True, symbol=symbol)
        if guardrail_error:
            logger.warning(f"Guardrail blocked order: {guardrail_error}")
            return {"error": guardrail_error, "status": "REJECTED"}

        # Get order book snapshot for realistic market-order simulation.
        book = self._get_order_book(symbol)
        if not book:
            return {
                "error": f"Cannot get order book for {symbol}. Market data unavailable.",
                "status": "REJECTED",
            }
        best_bid = book["best_bid"]
        best_ask = book["best_ask"]

        # Check risk
        if order_type == "MARKET":
            preview = self._ledger.preview_market_fill(
                direction=direction,
                quantity=quantity,
                best_bid=best_bid,
                best_ask=best_ask,
                bids=book.get("bids"),
                asks=book.get("asks"),
            )
            entry_price = preview["fill_price"]
        else:
            entry_price = price
        direction_error = self._validate_exit_levels(
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target=target,
        )
        if direction_error:
            return {"error": f"BLOCKED: {direction_error}", "status": "REJECTED"}

        is_valid, reason = self._ledger.validate_position_size(
            entry_price=entry_price,
            quantity=quantity,
            max_risk_amount=self._max_risk_amount,
            stop_loss=stop_loss,   # risk = |entry - SL| * qty when SL is provided
        )
        if not is_valid:
            return {"error": f"BLOCKED: {reason}", "status": "REJECTED"}

        margin_required, margin_error = self._get_shoonya_order_margin(
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            entry_price=entry_price,
        )
        if margin_error:
            logger.warning(f"Guardrail blocked order: {margin_error}")
            return {"error": margin_error, "status": "REJECTED"}

        if order_type == "MARKET":
            # Immediate fill
            fill = self._ledger.execute_market_fill(
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                best_bid=best_bid,
                best_ask=best_ask,
                bids=book.get("bids"),
                asks=book.get("asks"),
            )
            fill["margin_used"] = margin_required
            fill["stop_loss"] = stop_loss
            fill["target"] = target
            return self._process_fill(fill)

        else:
            # LIMIT order — add to pending, monitor in background
            order_id = str(uuid.uuid4())[:8]
            pending = {
                "order_id": order_id,
                "symbol": symbol,
                "direction": direction,
                "quantity": quantity,
                "order_type": "LIMIT",
                "limit_price": price,
                "placed_at": time.time(),
                "expires_at": time.time() + 300,  # 5 minute timeout
                "status": "PENDING",
                "best_bid_at_placement": best_bid,
                "best_ask_at_placement": best_ask,
                "stop_loss": stop_loss,
                "target": target,
            }
            self._state.add_pending_order(pending)
            logger.info(
                f"LIMIT ORDER placed: {direction} {quantity}x {symbol} "
                f"@ ₹{price:.2f} (expires in 5min)"
            )

            return {
                "order_id": order_id,
                "symbol": symbol,
                "direction": direction,
                "quantity": quantity,
                "order_type": "LIMIT",
                "limit_price": price,
                "stop_loss": stop_loss,
                "target": target,
                "status": "PENDING",
                "message": (
                    f"Limit order placed. Will fill if LTP reaches ₹{price:.2f} "
                    f"within 5 minutes, otherwise auto-cancels. "
                    f"Current bid/ask: ₹{best_bid:.2f}/₹{best_ask:.2f}"
                ),
            }

    def cancel_order(self, order_id: str) -> dict:
        """
        Cancel a pending limit order.

        :param order_id: The order_id returned by place_virtual_order
        :returns: Cancellation confirmation or error
        """
        removed = self._state.remove_pending_order(order_id)
        if removed:
            logger.info(f"Order cancelled: {order_id} ({removed['symbol']})")
            return {
                "order_id": order_id,
                "symbol": removed["symbol"],
                "status": "CANCELLED",
                "message": f"Limit order for {removed['symbol']} cancelled.",
            }
        return {"error": f"No pending order found with id: {order_id}"}

    def close_position(self, symbol: str) -> dict:
        """
        Close an open position at market price.

        :param symbol: Symbol of the position to close
        :returns: Close confirmation with P&L
        """
        position = self._state.get_position(symbol)
        if not position:
            return {"error": f"No open position for {symbol}"}

        return self._close_position_record(position)

    def _close_position_record(self, position: dict) -> dict:
        """Close a specific position record, preserving same-symbol positions."""
        symbol = position["symbol"]

        # Get current price for exit
        book = self._get_order_book(symbol)
        if not book:
            return {"error": f"Cannot get order book to close {symbol}. Try again."}

        best_bid = book["best_bid"]
        best_ask = book["best_ask"]

        # Exit direction is opposite of entry
        exit_direction = "SELL" if position["direction"] == "BUY" else "BUY"

        fill = self._ledger.execute_market_fill(
            symbol=symbol,
            direction=exit_direction,
            quantity=position["quantity"],
            best_bid=best_bid,
            best_ask=best_ask,
            bids=book.get("bids"),
            asks=book.get("asks"),
        )

        # Calculate realized P&L
        pnl = self._ledger.calculate_realized_pnl(
            entry_price=position["entry_price"],
            exit_price=fill["fill_price"],
            quantity=position["quantity"],
            direction=position["direction"],
        )

        # Update state
        order_id = position.get("order_id")
        if order_id:
            removed = self._state.remove_position_by_order_id(order_id)
        else:
            removed = self._state.remove_position(symbol)
        if not removed:
            return {
                "error": (
                    f"Position {symbol} could not be removed from state "
                    f"(order_id={order_id or 'N/A'})."
                ),
                "status": "ERROR",
            }
        self._state.update_daily_pnl(pnl)
        self._state.add_trade({
            "symbol": symbol,
            "direction": position["direction"],
            "quantity": position["quantity"],
            "entry_price": position["entry_price"],
            "exit_price": fill["fill_price"],
            "pnl": pnl,
            "pnl_pct": round(pnl / (position["entry_price"] * position["quantity"]) * 100, 2),
            "entry_time": position.get("entry_time"),
            "exit_time": time.time(),
            "entry_order_id": order_id,
        })

        logger.info(f"CLOSED: {symbol} @ ₹{fill['fill_price']:.2f}, P&L: ₹{pnl:,.2f}")

        return {
            "symbol": symbol,
            "exit_price": fill["fill_price"],
            "pnl": pnl,
            "pnl_pct": round(pnl / (position["entry_price"] * position["quantity"]) * 100, 2),
            "status": "CLOSED",
            "message": (
                f"Closed {position['direction']} {symbol} @ ₹{fill['fill_price']:.2f}. "
                f"P&L: ₹{pnl:+,.2f}"
            ),
        }

    def close_all_positions(self) -> dict:
        """
        Close all open positions at market. Used for EOD or /abort.

        :returns: Summary of all closures
        """
        positions = self._state.get_open_positions()
        if not positions:
            return {"message": "No open positions to close.", "closed": []}

        results = []
        for pos in list(positions):  # Copy list since we modify during iteration
            result = self._close_position_record(pos)
            results.append(result)

        # Cancel all pending orders too
        for order in list(self._state.get_pending_orders()):
            self.cancel_order(order["order_id"])

        total_pnl = sum(r.get("pnl", 0) for r in results if "pnl" in r)

        return {
            "closed": results,
            "total_pnl": total_pnl,
            "remaining_positions": len(self._state.get_open_positions()),
            "message": (
                f"Closed {len(results)} position(s). Total P&L: ₹{total_pnl:+,.2f}"
            ),
        }

    # ──────────────────────────────────────────────────────────
    #   ACCOUNT TOOLS (callable by Claude)
    # ──────────────────────────────────────────────────────────

    def get_open_positions(self) -> dict:
        """
        Get all currently open virtual positions with unrealized P&L.

        :returns: {positions: [{symbol, direction, qty, entry_price, current_price, unrealized_pnl}]}
        """
        positions = self._state.get_open_positions()
        enriched = []

        for pos in positions:
            # Get current price
            current_price = self._get_current_price(pos["symbol"])

            unrealized = self._ledger.calculate_unrealized_pnl(
                entry_price=pos["entry_price"],
                current_price=current_price or pos["entry_price"],
                quantity=pos["quantity"],
                direction=pos["direction"],
            )

            enriched.append({
                "symbol": pos["symbol"],
                "direction": pos["direction"],
                "quantity": pos["quantity"],
                "entry_price": pos["entry_price"],
                "current_price": current_price or pos["entry_price"],
                "unrealized_pnl": unrealized,
                "entry_time": pos.get("entry_time"),
                "stop_loss": pos.get("stop_loss"),
                "target": pos.get("target"),
            })

        return {"positions": enriched, "count": len(enriched)}

    def get_virtual_balance(self) -> dict:
        """Get current virtual cash, margin used, available margin."""
        return self._state.get_balance()

    def get_todays_trades(self) -> dict:
        """Get all trades executed today."""
        trades = self._state.get_trades()
        return {"trades": trades, "count": len(trades)}

    def get_daily_pnl(self) -> dict:
        """Get current net P&L for the session."""
        pnl, pnl_pct = self._state.get_daily_pnl()
        return {
            "daily_pnl": pnl,
            "daily_pnl_pct": round(pnl_pct, 2),
            "formatted": f"₹{pnl:+,.2f} ({pnl_pct:+.2f}%)",
        }

    # ──────────────────────────────────────────────────────────
    #   LIMIT ORDER MONITORING (called on each WebSocket tick)
    # ──────────────────────────────────────────────────────────

    def check_pending_limit_orders(self, token: str = None):
        """
        Check all pending limit orders against current prices.
        Called on each tick or periodically.
        Auto-cancels expired orders, fills triggered ones.
        """
        now = time.time()
        pending = self._state.get_pending_orders()

        for order in list(pending):
            # Check expiry
            if now >= order.get("expires_at", 0):
                self._state.remove_pending_order(order["order_id"])
                logger.info(
                    f"LIMIT ORDER EXPIRED: {order['order_id']} "
                    f"({order['symbol']} @ ₹{order['limit_price']:.2f})"
                )
                continue

            # Check if we should try to fill
            current_price = self._get_current_price(order["symbol"])
            if current_price is None:
                continue

            should_fill = self._ledger.check_limit_fill(
                direction=order["direction"],
                limit_price=order["limit_price"],
                current_ltp=current_price,
            )

            if should_fill:
                guardrail_error = self._check_guardrails(is_new_entry=True, symbol=order["symbol"])
                if guardrail_error:
                    self._state.remove_pending_order(order["order_id"])
                    logger.warning(
                        f"LIMIT ORDER CANCELLED BY GUARDRAIL: {order['order_id']} "
                        f"({order['symbol']}): {guardrail_error}"
                    )
                    continue

                margin_required, margin_error = self._get_shoonya_order_margin(
                    symbol=order["symbol"],
                    direction=order["direction"],
                    quantity=order["quantity"],
                    entry_price=order.get("limit_price"),
                )
                if margin_error:
                    self._state.remove_pending_order(order["order_id"])
                    logger.warning(
                        f"LIMIT ORDER CANCELLED BY MARGIN GUARDRAIL: {order['order_id']} "
                        f"({order['symbol']}): {margin_error}"
                    )
                    continue

                fill = self._ledger.execute_limit_fill(
                    symbol=order["symbol"],
                    direction=order["direction"],
                    quantity=order["quantity"],
                    limit_price=order["limit_price"],
                )
                fill["margin_used"] = margin_required
                fill["stop_loss"] = order.get("stop_loss")
                fill["target"] = order.get("target")
                self._state.remove_pending_order(order["order_id"])
                self._process_fill(fill)
                logger.info(
                    f"LIMIT ORDER FILLED: {order['order_id']} "
                    f"{order['direction']} {order['symbol']} @ ₹{order['limit_price']:.2f}"
                )

    def check_sl_target(self) -> list:
        """
        Check all open positions for stop-loss or target breach.
        Closes positions deterministically when thresholds are hit.

        Uses EXECUTABLE sides for trigger checks:
        - BUY/LONG closing: checks best_bid (where you actually sell)
        - SELL/SHORT closing: checks best_ask (where you actually buy)

        :returns: List of auto-closed position dicts (symbol, reason, pnl)
        """
        positions = self._state.get_open_positions()
        auto_closed = []

        for pos in list(positions):
            sl = pos.get("stop_loss")
            tgt = pos.get("target")

            if sl is None and tgt is None:
                continue

            # Get executable sides (not just LTP)
            book = self._get_order_book(pos["symbol"])
            if not book:
                continue

            best_bid = book.get("best_bid")
            best_ask = book.get("best_ask")
            if best_bid is None or best_ask is None:
                continue

            self._apply_trailing_stop(pos, best_bid if pos["direction"] == "BUY" else best_ask)
            sl = pos.get("stop_loss")

            direction = pos["direction"]
            reason = None

            if direction == "BUY":
                # Closing LONG = sell at best_bid
                if sl is not None and best_bid <= sl:
                    reason = f"Stop-loss hit: bid {best_bid:.2f} <= SL {sl:.2f}"
                elif tgt is not None and best_bid >= tgt:
                    reason = f"Target hit: bid {best_bid:.2f} >= target {tgt:.2f}"
            elif direction == "SELL":
                # Closing SHORT = buy at best_ask
                if sl is not None and best_ask >= sl:
                    reason = f"Stop-loss hit: ask {best_ask:.2f} >= SL {sl:.2f}"
                elif tgt is not None and best_ask <= tgt:
                    reason = f"Target hit: ask {best_ask:.2f} <= target {tgt:.2f}"

            if reason:
                logger.info(f"AUTO-CLOSE {pos['symbol']}: {reason}")
                result = self._close_position_record(pos)
                result["auto_close_reason"] = reason
                auto_closed.append(result)

        return auto_closed

    def _apply_trailing_stop(self, pos: dict, current_price: float) -> bool:
        """
        Deterministically ratchet stop-loss as profit expands.

        Rule:
        - At +0.5% favorable move, move SL to entry/breakeven.
        - At +1.0% favorable move, lock +0.5%.
        - At +2.0% favorable move, lock +1.0%.
        - For every additional +1% favorable move, lock another +1%.
        BUY entry 100: price 100.5 => SL 100, 101 => SL 100.5,
        102 => SL 101, 103 => SL 102. SELL mirrors this.
        The stop never moves backwards.
        """
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            return False

        direction = pos.get("direction")
        if direction == "BUY":
            favorable_pct = (current_price - entry) / entry * 100
        elif direction == "SELL":
            favorable_pct = (entry - current_price) / entry * 100
        else:
            return False

        if favorable_pct < 0.5:
            return False

        if favorable_pct < 1:
            locked_pct = 0.0
        elif favorable_pct < 2:
            locked_pct = 0.5
        else:
            locked_pct = float(int(favorable_pct) - 1)

        old_sl = pos.get("stop_loss")
        if direction == "BUY":
            new_sl = round(entry * (1 + locked_pct / 100), 2)
            if old_sl is not None and new_sl <= float(old_sl):
                return False
        else:
            new_sl = round(entry * (1 - locked_pct / 100), 2)
            if old_sl is not None and new_sl >= float(old_sl):
                return False

        order_id = pos.get("order_id")
        updates = {
            "stop_loss": new_sl,
            "trailing_stop_active": True,
            "trailing_profit_pct": round(favorable_pct, 2),
            "trailing_locked_pct": locked_pct,
            "trailing_updated_at": time.time(),
        }
        updated = (
            self._state.update_position_by_order_id(order_id, **updates)
            if order_id else self._state.update_position(pos["symbol"], **updates)
        )
        if updated:
            pos.update(updates)
            locked_label = (
                str(int(locked_pct)) if float(locked_pct).is_integer()
                else str(locked_pct)
            )
            logger.info(
                f"TRAILING STOP UPDATED {pos['symbol']} order_id={order_id or 'N/A'}: "
                f"profit={favorable_pct:.2f}% locked={locked_label}% "
                f"SL {old_sl if old_sl is not None else 'None'} -> {new_sl:.2f}"
            )
            return True
        return False

    # ──────────────────────────────────────────────────────────
    #   INTERNAL HELPERS
    # ──────────────────────────────────────────────────────────

    def _get_bid_ask(self, symbol: str) -> Optional[tuple[float, float]]:
        """Get best bid/ask from WebSocket feed or REST fallback."""
        # Try resolving token from search
        token = self._resolve_token(symbol)
        exchange = self._resolve_exchange(symbol)

        if token and self._feed:
            result = self._feed.get_best_bid_ask(token)
            if result:
                return result

        # REST fallback
        if token and exchange:
            return self._client.get_best_bid_ask_rest(exchange, token)

        return None

    def _get_order_book(self, symbol: str) -> Optional[dict]:
        """
        Get a market-depth snapshot for realistic market-order simulation.

        Prefers REST get_quotes() because Shoonya exposes up to 5 levels there.
        Falls back to top-of-book from the live feed when only bp1/sp1 is available.
        """
        token = self._resolve_token(symbol)
        exchange = self._resolve_exchange(symbol)
        live_quote = self._feed.get_live_quote(token) if token and self._feed else None

        bids: list[dict] = []
        asks: list[dict] = []
        best_bid = best_ask = None

        if token and exchange and self._client and hasattr(self._client, "get_quotes"):
            resp = self._client.get_quotes(exchange, token)
            if resp:
                for i in range(1, 6):
                    bp = resp.get(f"bp{i}")
                    bq = resp.get(f"bq{i}")
                    sp = resp.get(f"sp{i}")
                    sq = resp.get(f"sq{i}")
                    if bp is not None and bq is not None:
                        try:
                            bid_price = float(bp)
                            bid_qty = int(bq)
                        except (TypeError, ValueError):
                            bid_price = bid_qty = 0
                        if bid_price > 0 and bid_qty > 0:
                            bids.append({"price": bid_price, "qty": bid_qty})
                    if sp is not None and sq is not None:
                        try:
                            ask_price = float(sp)
                            ask_qty = int(sq)
                        except (TypeError, ValueError):
                            ask_price = ask_qty = 0
                        if ask_price > 0 and ask_qty > 0:
                            asks.append({"price": ask_price, "qty": ask_qty})
                if bids and asks:
                    best_bid = bids[0]["price"]
                    best_ask = asks[0]["price"]

        if (best_bid is None or best_ask is None) and live_quote:
            live_bid = live_quote.get("best_bid")
            live_ask = live_quote.get("best_ask")
            live_bq = live_quote.get("bid_qty") or 0
            live_aq = live_quote.get("ask_qty") or 0
            if live_bid and live_ask:
                best_bid = float(live_bid)
                best_ask = float(live_ask)
                if not bids and live_bq:
                    bids = [{"price": best_bid, "qty": int(live_bq)}]
                if not asks and live_aq:
                    asks = [{"price": best_ask, "qty": int(live_aq)}]

        if best_bid is None or best_ask is None:
            bid_ask = self._get_bid_ask(symbol)
            if not bid_ask:
                return None
            best_bid, best_ask = bid_ask
            if not bids:
                bids = [{"price": best_bid, "qty": 1}]
            if not asks:
                asks = [{"price": best_ask, "qty": 1}]

        return {
            "symbol": symbol,
            "best_bid": float(best_bid),
            "best_ask": float(best_ask),
            "bids": bids,
            "asks": asks,
            "source": "rest_depth" if len(bids) > 1 or len(asks) > 1 else "top_of_book",
        }

    def _logical_instrument(self, symbol: str) -> Optional[str]:
        """Map a futures tsym to its logical instrument for no-pyramiding checks."""
        sym = (symbol or "").upper()
        if "BANKNIFTY" in sym:
            return "BANKNIFTY"
        if "FINNIFTY" in sym:
            return "FINNIFTY"
        if "NIFTY" in sym:
            return "NIFTY"
        return None

    def _check_margin_capacity(self, margin_required: float) -> Optional[str]:
        """Reject entries that would exceed virtual capital buying power."""
        state = self._state.get_state()
        capital = float(state.get("virtual_capital", 0) or 0)
        daily_pnl = float(state.get("daily_pnl", 0) or 0)
        margin_used = float(state.get("margin_used", 0) or 0)
        available_margin = capital + daily_pnl - margin_used
        if margin_required > available_margin:
            return (
                f"BLOCKED: Margin required ₹{margin_required:,.0f} exceeds "
                f"available virtual margin ₹{available_margin:,.0f}. "
                f"Capital ₹{capital:,.0f}, current margin used ₹{margin_used:,.0f}, "
                f"daily P&L ₹{daily_pnl:,.0f}."
            )
        return None

    def _get_shoonya_order_margin(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        entry_price: float,
    ) -> tuple[Optional[float], Optional[str]]:
        """
        Ask Shoonya RMS for actual order margin before accepting a virtual fill.

        Fail closed: if Shoonya cannot return margin, the simulator rejects the
        trade rather than using an invented approximation.
        """
        if not self._client or not hasattr(self._client, "get_order_margin"):
            return None, "BLOCKED: Shoonya margin API unavailable."

        exchange = self._resolve_exchange(symbol)
        trantype = "B" if direction == "BUY" else "S"
        resp = self._client.get_order_margin(
            exchange=exchange,
            tradingsymbol=symbol,
            quantity=quantity,
            price=entry_price,
            transaction_type=trantype,
            product="M",
            price_type="LMT",
        )
        if not resp:
            return None, "BLOCKED: Shoonya margin API returned no response."

        stat = resp.get("stat")
        remarks = str(resp.get("remarks", ""))
        emsg = str(resp.get("emsg", ""))
        if stat != "Ok":
            reason = emsg or remarks or "unknown Shoonya margin rejection"
            return None, f"BLOCKED: Shoonya margin check failed: {reason}"

        raw_margin = resp.get("ordermargin")
        if raw_margin in (None, ""):
            raw_margin = resp.get("marginused")
        try:
            margin_required = float(raw_margin)
        except (TypeError, ValueError):
            return None, (
                "BLOCKED: Shoonya margin response did not contain numeric "
                f"ordermargin/marginused: {resp}"
            )

        capacity_error = self._check_margin_capacity(margin_required)
        if capacity_error:
            return None, capacity_error

        logger.info(
            "Shoonya margin OK: %s %s %sx %s @ %.2f margin=₹%,.2f remarks=%s",
            trantype,
            exchange,
            quantity,
            symbol,
            entry_price,
            margin_required,
            remarks or "n/a",
        )
        if remarks and "insufficient" in remarks.lower():
            logger.warning(
                "Shoonya margin response included remarks=%r despite stat=Ok. "
                "Using numeric ordermargin=%s for virtual-capacity checks.",
                remarks,
                f"₹{margin_required:,.2f}",
            )
        return margin_required, None

    @staticmethod
    def _validate_exit_levels(
        direction: str,
        entry_price: float,
        stop_loss: Optional[float],
        target: Optional[float],
    ) -> Optional[str]:
        """
        Validate that SL/target are on the correct side of entry.

        This prevents pathological fills like a SELL with target above entry,
        which can be auto-closed as a "target hit" while still losing money.
        """
        if entry_price is None:
            return "Cannot validate stop/target without an entry price."

        if direction == "BUY":
            if stop_loss is not None and stop_loss >= entry_price:
                return (
                    f"BUY stop_loss must be below entry. "
                    f"entry={entry_price:.2f}, stop_loss={stop_loss:.2f}"
                )
            if target is not None and target <= entry_price:
                return (
                    f"BUY target must be above entry. "
                    f"entry={entry_price:.2f}, target={target:.2f}"
                )
        elif direction == "SELL":
            if stop_loss is not None and stop_loss <= entry_price:
                return (
                    f"SELL stop_loss must be above entry. "
                    f"entry={entry_price:.2f}, stop_loss={stop_loss:.2f}"
                )
            if target is not None and target >= entry_price:
                return (
                    f"SELL target must be below entry. "
                    f"entry={entry_price:.2f}, target={target:.2f}"
                )

        return None

    def _get_current_price(self, symbol: str) -> Optional[float]:
        """Get current LTP from WebSocket or REST."""
        token = self._resolve_token(symbol)

        if token and self._feed:
            ltp = self._feed.get_ltp(token)
            if ltp:
                return ltp

        exchange = self._resolve_exchange(symbol)
        if token and exchange:
            return self._client.get_ltp(exchange, token)

        return None

    def _resolve_token(self, symbol: str) -> Optional[str]:
        """Resolve symbol to Shoonya token.

        Priority:
        1. active_tokens map (pre-resolved futures tokens from main.py)
        2. In-memory token cache (from previous searches)
        3. search_scrip API (NFO first, then NSE)
        """
        sym_up = symbol.upper().strip()
        if sym_up in _BARE_LOGICAL_NAMES:
            return None

        if symbol in self._token_cache:
            return self._token_cache[symbol]

        # Check active_tokens map first (handles futures tsym lookups like NIFTY28APR26F)
        for logical, info in self._active_tokens.items():
            tsym = info.get("tsym", "")
            if tsym and tsym.upper() == sym_up:
                token = str(info.get("token", ""))
                if token:
                    self._token_cache[symbol] = token
                    return token
        results = self._client.search_scrip("NFO", symbol)
        if not results:
            results = self._client.search_scrip("NSE", symbol)
        if results:
            token = results[0].get("token", "")
            self._token_cache[symbol] = token
            return token
        return None

    def _resolve_exchange(self, symbol: str) -> str:
        """Determine exchange for a symbol.

        Futures tsyms (e.g. NIFTY28APR26F, BANKNIFTY28APR26F) end with 'F'
        and contain month-year digits; they trade on NFO.
        Bare index names (NIFTY, BANKNIFTY, FINNIFTY) are on NSE when present.
        """
        sym = symbol.upper()
        if any(x in sym for x in ["CE", "PE"]):
            return "NFO"
        # Futures tsym: ends with 'F' and has digits before it (e.g. NIFTY28APR26F)
        if sym.endswith("F") and any(c.isdigit() for c in sym):
            return "NFO"
        # Explicit FUT keyword
        if "FUT" in sym:
            return "NFO"
        return "NSE"

    def _process_fill(self, fill: dict) -> dict:
        """Process a fill: add position to state, return confirmation."""
        position = {
            "symbol": fill["symbol"],
            "direction": fill["direction"],
            "quantity": fill["quantity"],
            "entry_price": fill["fill_price"],
            "margin_used": fill["margin_used"],
            "entry_time": fill["fill_time"],
            "order_id": fill["order_id"],
            "stop_loss": fill.get("stop_loss"),
            "target": fill.get("target"),
        }
        self._state.add_position(position)

        return {
            "order_id": fill["order_id"],
            "symbol": fill["symbol"],
            "direction": fill["direction"],
            "quantity": fill["quantity"],
            "fill_price": fill["fill_price"],
            "cost": fill["cost"],
            "status": "FILLED",
            "message": (
                f"Filled: {fill['direction']} {fill['quantity']}x {fill['symbol']} "
                f"@ ₹{fill['fill_price']:.2f} (cost: ₹{fill['cost']:,.2f})"
            ),
        }
