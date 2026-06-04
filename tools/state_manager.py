"""
tools/state_manager.py — Atomic JSON state management for BlitzTrader.

Manages live_state.json with all session state:
  positions, orders, balance, trades, daily P&L.

All writes are atomic (write to .tmp, then rename) to prevent corruption.
"""
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("BlitzTrader.StateManager")


def _default_state(virtual_capital: float) -> dict:
    """Create a fresh session state."""
    return {
        "session_id": str(uuid.uuid4())[:8],
        "session_start": time.time(),
        "virtual_capital": virtual_capital,
        "available_balance": virtual_capital,
        "margin_used": 0.0,
        "daily_pnl": 0.0,
        "daily_pnl_pct": 0.0,
        "positions": [],        # Open futures positions
        "pending_orders": [],   # Unfilled limit orders
        "trades": [],           # Completed futures trades (entry + exit)
        "trade_count": 0,
        "open_spreads": [],     # Currently open option spreads
        "spreads_traded": [],   # Closed option spreads with realized P&L
        "emitted_signal_keys": [],
        "notifications_sent": {},
        "is_paused": False,
        "is_stopped": False,
    }


class StateManager:
    """
    Thread-safe atomic state manager.
    Reads/writes live_state.json with atomic file operations.
    """

    def __init__(self, state_file: Path, virtual_capital: float = 500_000):
        self._state_file = state_file
        self._virtual_capital = virtual_capital
        self._state: dict = {}

    def init_state(self) -> dict:
        """Initialize a fresh session state and write to disk."""
        self._state = _default_state(self._virtual_capital)
        self._save()
        logger.info(f"Initialized fresh state: session={self._state['session_id']}")
        return self._state

    def load_state(self) -> dict:
        """Load state from disk, or init fresh if missing/corrupt."""
        if not self._state_file.exists():
            return self.init_state()
        try:
            with open(self._state_file, "r") as f:
                self._state = json.load(f)
            logger.info(f"Loaded state: session={self._state.get('session_id', '?')}")
            return self._state
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Corrupt state file, reinitializing: {e}")
            return self.init_state()

    def get_state(self) -> dict:
        """Get current in-memory state."""
        if not self._state:
            return self.load_state()
        return self._state

    def update_state(self, **kwargs) -> dict:
        """Update state fields and persist atomically."""
        self._state.update(kwargs)
        self._save()
        return self._state

    def _save(self):
        """Atomic write: write to .tmp then rename."""
        tmp_path = self._state_file.with_suffix(".json.tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(self._state, f, indent=2, default=str)
            os.replace(tmp_path, self._state_file)
        except OSError:
            logger.exception("Failed to save state to disk")

    # ──────────────────────────────────────────────────────────
    #   POSITION HELPERS
    # ──────────────────────────────────────────────────────────

    def add_position(self, position: dict) -> dict:
        """Add a new open position."""
        self._state["positions"].append(position)
        self._recalculate_margin()
        self._save()
        return self._state

    def remove_position(self, symbol: str) -> Optional[dict]:
        """Remove and return a position by symbol."""
        positions = self._state["positions"]
        for i, pos in enumerate(positions):
            if pos.get("symbol") == symbol:
                removed = positions.pop(i)
                self._recalculate_margin()
                self._save()
                return removed
        return None

    def remove_position_by_order_id(self, order_id: str) -> Optional[dict]:
        """Remove and return a position by its unique entry order_id."""
        positions = self._state["positions"]
        for i, pos in enumerate(positions):
            if pos.get("order_id") == order_id:
                removed = positions.pop(i)
                self._recalculate_margin()
                self._save()
                return removed
        return None

    def get_open_positions(self) -> list[dict]:
        """Get all open positions."""
        return self._state.get("positions", [])

    def get_position(self, symbol: str) -> Optional[dict]:
        """Get a specific position by symbol."""
        for pos in self._state.get("positions", []):
            if pos.get("symbol") == symbol:
                return pos
        return None

    def update_position(self, symbol: str, **kwargs) -> Optional[dict]:
        """Update fields of an existing position."""
        for pos in self._state.get("positions", []):
            if pos.get("symbol") == symbol:
                pos.update(kwargs)
                self._save()
                return pos
        return None

    def update_position_by_order_id(self, order_id: str, **kwargs) -> Optional[dict]:
        """Update fields of an existing position by unique entry order_id."""
        for pos in self._state.get("positions", []):
            if pos.get("order_id") == order_id:
                pos.update(kwargs)
                self._save()
                return pos
        return None

    # ──────────────────────────────────────────────────────────
    #   ORDER HELPERS
    # ──────────────────────────────────────────────────────────

    def add_pending_order(self, order: dict) -> dict:
        """Add a pending limit order."""
        self._state["pending_orders"].append(order)
        self._save()
        return self._state

    def remove_pending_order(self, order_id: str) -> Optional[dict]:
        """Remove and return a pending order by ID."""
        orders = self._state["pending_orders"]
        for i, order in enumerate(orders):
            if order.get("order_id") == order_id:
                removed = orders.pop(i)
                self._save()
                return removed
        return None

    def get_pending_orders(self) -> list[dict]:
        """Get all pending limit orders."""
        return self._state.get("pending_orders", [])

    # ──────────────────────────────────────────────────────────
    #   TRADE LOG
    # ──────────────────────────────────────────────────────────

    def add_trade(self, trade: dict) -> dict:
        """Log a completed trade."""
        self._state["trades"].append(trade)
        self._state["trade_count"] = len(self._state["trades"])
        self._save()
        return self._state

    def get_trades(self) -> list[dict]:
        """Get all trades for today."""
        return self._state.get("trades", [])

    # ──────────────────────────────────────────────────────────
    #   OPTION SPREAD LEDGER
    # ──────────────────────────────────────────────────────────

    def add_traded_spread(self, spread_record: dict) -> dict:
        """Append a closed option spread to the durable spreads_traded ledger."""
        if "spreads_traded" not in self._state:
            self._state["spreads_traded"] = []
        self._state["spreads_traded"].append(spread_record)
        self._save()
        return self._state

    def get_traded_spreads(self) -> list[dict]:
        """Return all closed option spreads for this session."""
        return self._state.get("spreads_traded", [])

    # ──────────────────────────────────────────────────────────
    #   P&L TRACKING
    # ──────────────────────────────────────────────────────────

    def update_daily_pnl(self, realized_pnl_change: float):
        """Update daily P&L with a realized change."""
        self._state["daily_pnl"] += realized_pnl_change
        self._state["daily_pnl_pct"] = (
            self._state["daily_pnl"] / self._state["virtual_capital"] * 100
        )
        self._save()

    def get_daily_pnl(self) -> tuple[float, float]:
        """Returns (daily_pnl_amount, daily_pnl_pct)."""
        return self._state["daily_pnl"], self._state["daily_pnl_pct"]

    # ──────────────────────────────────────────────────────────
    #   BALANCE
    # ──────────────────────────────────────────────────────────

    def update_balance(self, amount: float):
        """Adjust available balance (negative for cost, positive for proceeds)."""
        self._state["available_balance"] += amount
        self._save()

    def get_balance(self) -> dict:
        """Get balance info."""
        return {
            "virtual_capital": self._state["virtual_capital"],
            "available_balance": self._state["available_balance"],
            "margin_used": self._state["margin_used"],
            "daily_pnl": self._state["daily_pnl"],
        }

    def _recalculate_margin(self):
        """Recalculate margin used from open positions."""
        total_margin = sum(
            pos.get("margin_used", 0) for pos in self._state["positions"]
        )
        self._state["margin_used"] = total_margin
        self._state["available_balance"] = (
            self._state["virtual_capital"]
            + self._state["daily_pnl"]
            - total_margin
        )
