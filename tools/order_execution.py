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
        max_positions: int = 2,
        max_risk_amount: float = 15000,  # 5% of 3L
        max_daily_loss: float = 15000,  # 5% of 3L
        no_entry_after: str = "15:05",
    ):
        self._state = state_manager
        self._ledger = virtual_ledger
        self._feed = live_feed
        self._client = shoonya_client
        self._max_positions = max_positions
        self._max_risk_amount = max_risk_amount
        self._max_daily_loss = max_daily_loss
        self._no_entry_after = no_entry_after

        self._token_cache = {}

        # Limit order monitoring
        self._limit_order_callbacks = {}

    # ──────────────────────────────────────────────────────────
    #   GUARDRAILS (enforced before every order)
    # ──────────────────────────────────────────────────────────

    def _check_guardrails(self, is_new_entry: bool = True) -> Optional[str]:
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

        # Check max positions
        open_positions = state.get("positions", [])
        if len(open_positions) >= self._max_positions:
            return (
                f"BLOCKED: Already at maximum {self._max_positions} open positions. "
                f"Close an existing position before opening a new one. "
                f"Current positions: {[p['symbol'] for p in open_positions]}"
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

        :param symbol:     Trading symbol (e.g., 'NIFTY27MAR24500CE')
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

        # Enforce Options-Only Guardrail (No Futures)
        sym_up = symbol.upper().strip()
        if not (sym_up.startswith("NIFTY") or sym_up.startswith("BANKNIFTY")):
            return {"error": "BLOCKED: Only NIFTY and BANKNIFTY instruments are allowed.", "status": "REJECTED"}
        if "FUT" in sym_up:
            return {"error": "BLOCKED: Only Options are allowed. Futures (FUT) are disabled by user.", "status": "REJECTED"}

        # Run state guardrails
        guardrail_error = self._check_guardrails(is_new_entry=True)
        if guardrail_error:
            logger.warning(f"Guardrail blocked order: {guardrail_error}")
            return {"error": guardrail_error, "status": "REJECTED"}

        # Get live bid/ask
        bid_ask = self._get_bid_ask(symbol)
        if not bid_ask:
            return {
                "error": f"Cannot get bid/ask for {symbol}. Market data unavailable.",
                "status": "REJECTED",
            }
        best_bid, best_ask = bid_ask

        # Check risk
        entry_price = (best_bid + best_ask) / 2 if order_type == "MARKET" else price
        is_valid, reason = self._ledger.validate_position_size(
            entry_price=entry_price,
            quantity=quantity,
            max_risk_amount=self._max_risk_amount,
        )
        if not is_valid:
            return {"error": f"BLOCKED: {reason}", "status": "REJECTED"}

        if order_type == "MARKET":
            # Immediate fill
            fill = self._ledger.execute_market_fill(
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                best_bid=best_bid,
                best_ask=best_ask,
            )
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

        # Get current price for exit
        bid_ask = self._get_bid_ask(symbol)
        if not bid_ask:
            return {"error": f"Cannot get price to close {symbol}. Try again."}

        best_bid, best_ask = bid_ask

        # Exit direction is opposite of entry
        exit_direction = "SELL" if position["direction"] == "BUY" else "BUY"

        fill = self._ledger.execute_market_fill(
            symbol=symbol,
            direction=exit_direction,
            quantity=position["quantity"],
            best_bid=best_bid,
            best_ask=best_ask,
        )

        # Calculate realized P&L
        pnl = self._ledger.calculate_realized_pnl(
            entry_price=position["entry_price"],
            exit_price=fill["fill_price"],
            quantity=position["quantity"],
            direction=position["direction"],
        )

        # Update state
        self._state.remove_position(symbol)
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
            result = self.close_position(pos["symbol"])
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
                fill = self._ledger.execute_limit_fill(
                    symbol=order["symbol"],
                    direction=order["direction"],
                    quantity=order["quantity"],
                    limit_price=order["limit_price"],
                )
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

        :returns: List of auto-closed position dicts (symbol, reason, pnl)
        """
        positions = self._state.get_open_positions()
        auto_closed = []

        for pos in list(positions):
            sl = pos.get("stop_loss")
            tgt = pos.get("target")

            if sl is None and tgt is None:
                continue

            current_price = self._get_current_price(pos["symbol"])
            if current_price is None:
                continue

            direction = pos["direction"]
            reason = None

            if direction == "BUY":
                if sl is not None and current_price <= sl:
                    reason = f"Stop-loss hit: price {current_price:.2f} <= SL {sl:.2f}"
                elif tgt is not None and current_price >= tgt:
                    reason = f"Target hit: price {current_price:.2f} >= target {tgt:.2f}"
            elif direction == "SELL":
                if sl is not None and current_price >= sl:
                    reason = f"Stop-loss hit: price {current_price:.2f} >= SL {sl:.2f}"
                elif tgt is not None and current_price <= tgt:
                    reason = f"Target hit: price {current_price:.2f} <= target {tgt:.2f}"

            if reason:
                logger.info(f"AUTO-CLOSE {pos['symbol']}: {reason}")
                result = self.close_position(pos["symbol"])
                result["auto_close_reason"] = reason
                auto_closed.append(result)

        return auto_closed

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
        """Resolve symbol to Shoonya token via search (cached)."""
        if symbol in self._token_cache:
            return self._token_cache[symbol]

        results = self._client.search_scrip("NFO", symbol)
        if not results:
            results = self._client.search_scrip("NSE", symbol)
        if results:
            token = results[0].get("token", "")
            self._token_cache[symbol] = token
            return token
        return None

    def _resolve_exchange(self, symbol: str) -> str:
        """Determine exchange for a symbol."""
        sym = symbol.upper()
        if any(x in sym for x in ["CE", "PE", "FUT"]):
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

        # Deduct cost from balance (for option buys = premium)
        if fill["direction"] == "BUY":
            self._state.update_balance(-fill["cost"])
        else:
            self._state.update_balance(fill["cost"])

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
