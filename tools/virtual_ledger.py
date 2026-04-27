"""
tools/virtual_ledger.py — Virtual fill simulation and P&L calculation.

ALL financial math happens here — deterministic, no LLM involvement.
Claude never does arithmetic on fills or balances.
"""
import logging
import math
import time
import uuid
from typing import Optional

logger = logging.getLogger("BlitzTrader.VirtualLedger")


class VirtualLedger:
    """
    Handles fill simulation, P&L calculation, and margin estimation.
    All calculations are deterministic — no hallucination risk.
    """

    # Fallback lot sizes. Live sessions override these from Shoonya's contract
    # metadata when available because NSE lot sizes can change.
    DEFAULT_LOT_SIZES = {
        "BANKNIFTY": 15,
        "FINNIFTY": 60,
        "NIFTY": 25,
    }

    # Fallback margin per lot. Live order-entry margin is fetched from Shoonya
    # RMS by OrderExecutionTools; this is retained for tests/offline fallback.
    DEFAULT_MARGIN_PER_LOT = {
        "BANKNIFTY": 100_000,
        "FINNIFTY": 100_000,
        "NIFTY": 100_000,
    }

    def __init__(self, lot_sizes: Optional[dict] = None, margin_per_lot: Optional[dict] = None):
        self.LOT_SIZES = dict(self.DEFAULT_LOT_SIZES)
        if lot_sizes:
            for symbol, lot_size in lot_sizes.items():
                try:
                    parsed = int(lot_size)
                except (TypeError, ValueError):
                    logger.warning("Ignoring invalid lot size for %s: %r", symbol, lot_size)
                    continue
                if parsed > 0:
                    self.LOT_SIZES[symbol.upper()] = parsed

        self.MARGIN_PER_LOT = dict(self.DEFAULT_MARGIN_PER_LOT)
        if margin_per_lot:
            for symbol, margin in margin_per_lot.items():
                try:
                    parsed_margin = float(margin)
                except (TypeError, ValueError):
                    logger.warning("Ignoring invalid margin for %s: %r", symbol, margin)
                    continue
                if parsed_margin > 0:
                    self.MARGIN_PER_LOT[symbol.upper()] = parsed_margin

    def _logical_instrument(self, symbol: str) -> Optional[str]:
        """Classify symbols without mistaking FINNIFTY/BANKNIFTY for NIFTY."""
        symbol_upper = symbol.upper()
        for index in ("BANKNIFTY", "FINNIFTY", "NIFTY"):
            if index in symbol_upper:
                return index
        return None

    def execute_market_fill(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        best_bid: float,
        best_ask: float,
        bids: Optional[list[dict]] = None,
        asks: Optional[list[dict]] = None,
    ) -> dict:
        """
        Simulate a MARKET order fill by sweeping the visible order book.

        :param symbol:    Trading symbol (e.g., 'NIFTY27MAR24500CE')
        :param direction: 'BUY' or 'SELL'
        :param quantity:  Number of units
        :param best_bid:  Current best bid price
        :param best_ask:  Current best ask price
        :param bids:      Visible bid levels, highest first
        :param asks:      Visible ask levels, lowest first
        :returns: Fill confirmation dict
        """
        preview = self.preview_market_fill(
            direction=direction,
            quantity=quantity,
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bids,
            asks=asks,
        )
        fill_price = preview["fill_price"]

        cost = fill_price * quantity
        margin_required = self.estimate_margin(symbol, quantity)

        fill = {
            "order_id": str(uuid.uuid4())[:8],
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": "MARKET",
            "fill_price": fill_price,
            "cost": cost,
            "margin_used": margin_required,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "levels_consumed": preview["levels_consumed"],
            "unfilled_qty": preview["unfilled_qty"],
            "book_side": preview["book_side"],
            "fill_time": time.time(),
            "status": "FILLED",
        }

        logger.info(
            f"MARKET FILL: {direction} {quantity}x {symbol} @ ₹{fill_price:.2f} "
            f"(bid={best_bid:.2f}, ask={best_ask:.2f}, "
            f"levels={preview['levels_consumed']}, unfilled={preview['unfilled_qty']})"
        )
        return fill

    def preview_market_fill(
        self,
        direction: str,
        quantity: int,
        best_bid: float,
        best_ask: float,
        bids: Optional[list[dict]] = None,
        asks: Optional[list[dict]] = None,
    ) -> dict:
        """
        Estimate a MARKET order fill by consuming the visible book.

        BUY orders sweep asks from lowest to highest.
        SELL orders sweep bids from highest to lowest.
        If visible depth is insufficient, the remainder is filled beyond the
        visible book one inferred tick at a time to stay conservative.
        """
        direction = direction.upper()
        if direction not in ("BUY", "SELL"):
            raise ValueError(f"Invalid direction for market fill preview: {direction}")
        if quantity <= 0:
            raise ValueError(f"Invalid market fill quantity: {quantity}")

        raw_side = asks if direction == "BUY" else bids
        side = self._normalize_book_side(raw_side, direction=direction)
        fallback_price = best_ask if direction == "BUY" else best_bid
        tick_size = self._infer_tick_size(side, best_bid, best_ask)

        remaining = int(quantity)
        total_cost = 0.0
        levels_consumed = []

        for level in side:
            if remaining <= 0:
                break
            level_qty = int(level["qty"])
            if level_qty <= 0:
                continue
            take_qty = min(remaining, level_qty)
            level_price = float(level["price"])
            total_cost += level_price * take_qty
            levels_consumed.append({
                "price": round(level_price, 2),
                "qty": take_qty,
            })
            remaining -= take_qty

        if remaining > 0:
            last_visible_price = side[-1]["price"] if side else fallback_price
            overflow_price = self._price_beyond_book(
                last_visible_price=last_visible_price,
                tick_size=tick_size,
                steps=1,
                direction=direction,
            )
            total_cost += overflow_price * remaining
            levels_consumed.append({
                "price": round(overflow_price, 2),
                "qty": remaining,
                "synthetic": True,
            })
            remaining = 0

        avg_fill = round(total_cost / quantity, 2)
        return {
            "fill_price": avg_fill,
            "levels_consumed": levels_consumed,
            "unfilled_qty": remaining,
            "book_side": "asks" if direction == "BUY" else "bids",
            "tick_size": tick_size,
        }

    def _normalize_book_side(self, levels: Optional[list[dict]], direction: str) -> list[dict]:
        """
        Clean and sort book levels for deterministic execution.
        """
        cleaned = []
        for level in levels or []:
            try:
                price = float(level.get("price", 0))
                qty = int(level.get("qty", 0))
            except (TypeError, ValueError):
                continue
            if price > 0 and qty > 0:
                cleaned.append({"price": price, "qty": qty})

        reverse = direction.upper() == "SELL"
        cleaned.sort(key=lambda item: item["price"], reverse=reverse)
        return cleaned

    def _infer_tick_size(
        self,
        side: list[dict],
        best_bid: float,
        best_ask: float,
    ) -> float:
        """
        Infer a conservative tick size from visible depth or spread.
        """
        positive_diffs = []
        for idx in range(1, len(side)):
            diff = abs(float(side[idx]["price"]) - float(side[idx - 1]["price"]))
            if diff > 0:
                positive_diffs.append(diff)

        spread = abs(float(best_ask) - float(best_bid))
        if positive_diffs:
            return round(min(positive_diffs), 2)
        if spread > 0:
            return round(spread, 2)
        return 0.05

    def _price_beyond_book(
        self,
        last_visible_price: float,
        tick_size: float,
        steps: int,
        direction: str,
    ) -> float:
        """
        Move beyond the visible book in the unfavorable direction.
        """
        move = tick_size * max(1, int(steps))
        if direction.upper() == "BUY":
            return round(last_visible_price + move, 2)
        return round(max(0.01, last_visible_price - move), 2)

    def check_limit_fill(
        self,
        direction: str,
        limit_price: float,
        current_ltp: float,
    ) -> bool:
        """
        Check if a limit order should fill based on current LTP.

        BUY LIMIT: fills when LTP <= limit_price
        SELL LIMIT: fills when LTP >= limit_price
        """
        if direction == "BUY":
            return current_ltp <= limit_price
        else:  # SELL
            return current_ltp >= limit_price

    def execute_limit_fill(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        limit_price: float,
    ) -> dict:
        """
        Simulate a LIMIT order fill at the limit price.
        Called when check_limit_fill() returns True.
        """
        cost = limit_price * quantity
        margin_required = self.estimate_margin(symbol, quantity)

        fill = {
            "order_id": str(uuid.uuid4())[:8],
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": "LIMIT",
            "fill_price": limit_price,
            "cost": cost,
            "margin_used": margin_required,
            "limit_price": limit_price,
            "fill_time": time.time(),
            "status": "FILLED",
        }

        logger.info(
            f"LIMIT FILL: {direction} {quantity}x {symbol} @ ₹{limit_price:.2f}"
        )
        return fill

    def calculate_unrealized_pnl(
        self,
        entry_price: float,
        current_price: float,
        quantity: int,
        direction: str,
    ) -> float:
        """
        Calculate unrealized P&L for an open position.

        LONG: (current - entry) * quantity
        SHORT: (entry - current) * quantity
        """
        if direction == "BUY":
            return round((current_price - entry_price) * quantity, 2)
        else:
            return round((entry_price - current_price) * quantity, 2)

    def calculate_realized_pnl(
        self,
        entry_price: float,
        exit_price: float,
        quantity: int,
        direction: str,
    ) -> float:
        """
        Calculate realized P&L when closing a position.
        """
        if direction == "BUY":
            return round((exit_price - entry_price) * quantity, 2)
        else:
            return round((entry_price - exit_price) * quantity, 2)

    def calculate_risk_amount(
        self,
        entry_price: float,
        stop_loss: float,
        quantity: int,
    ) -> float:
        """Calculate the risk amount for position sizing validation."""
        return abs(entry_price - stop_loss) * quantity

    def validate_position_size(
        self,
        entry_price: float,
        quantity: int,
        max_risk_amount: float,
        stop_loss: Optional[float] = None,
    ) -> tuple[bool, str]:
        """
        Validate that position size is within risk limits.
        If no stop_loss provided, uses total position cost.

        :returns: (is_valid, reason)
        """
        if stop_loss:
            risk = self.calculate_risk_amount(entry_price, stop_loss, quantity)
            if risk > max_risk_amount:
                return False, (
                    f"Risk ₹{risk:.0f} exceeds max ₹{max_risk_amount:.0f}. "
                    f"Reduce quantity or tighten stop loss."
                )
        else:
            # Without stop loss, use full position value as risk proxy
            position_value = entry_price * quantity
            if position_value > max_risk_amount * 10:  # 10x risk amount = rough margin check
                return False, (
                    f"Position value ₹{position_value:.0f} too large. "
                    f"Max risk per trade: ₹{max_risk_amount:.0f}."
                )
        return True, "Position size OK"

    def get_lot_size(self, symbol: str) -> Optional[int]:
        """Return the futures lot size for a supported symbol, if known."""
        index = self._logical_instrument(symbol)
        return self.LOT_SIZES.get(index) if index else None

    def estimate_margin(self, symbol: str, quantity: int) -> float:
        """
        Estimate margin required for a position.
        Live order-entry margin is fetched from Shoonya RMS before fills.
        This method is a fallback used by the virtual ledger when no broker
        margin has been injected by the caller.
        """
        # Determine if it's options (CE/PE) or futures (FUT)
        symbol_upper = symbol.upper()

        if "CE" in symbol_upper or "PE" in symbol_upper:
            # Options: buyer pays full premium, no margin
            # Margin is just the cost (handled by the fill)
            return 0.0  # Premium is the cost, not margin
        else:
            index = self._logical_instrument(symbol)
            if index:
                lot_size = self.LOT_SIZES[index]
                lots = quantity / lot_size
                return lots * self.MARGIN_PER_LOT.get(index, 100_000)
            return quantity * 100  # Fallback
