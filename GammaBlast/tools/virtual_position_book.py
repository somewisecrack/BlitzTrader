"""
GammaBlast — Virtual Position Book
Manages virtual option positions with state persistence to live_state.json.
No broker mutations: place_order / cancel_order are never called.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("GammaBlast.VirtualPositionBook")


class VirtualPositionBook:
    """
    Manages virtual long-option positions for GammaBlast's expiry-day scanner.

    State is persisted atomically to ``state_file`` (live_state.json) after
    every mutation so that the book survives process restarts.

    Hard constraint: one position per unique (symbol, expiry, strike, option_type).
    All positions are BUY-side; PnL = (exit_price - entry_price) * lots * lot_size.
    """

    def __init__(self, state_file: Path, lot_sizes: dict) -> None:
        """
        Parameters
        ----------
        state_file:
            Path to live_state.json.  If the file exists its contents are
            loaded on startup; otherwise an empty book is created.
        lot_sizes:
            Mapping of symbol -> lot_size used as a fallback reference.
            The lot_size stored on each position is the authoritative value.
        """
        self.state_file = state_file
        self.lot_sizes = lot_sizes
        # positions dict: position_id -> position dict
        self.positions: dict[str, dict] = {}
        self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load positions from state_file if it exists."""
        if self.state_file.exists():
            try:
                with self.state_file.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.positions = data.get("positions", {})
                logger.info(
                    "Loaded %d position(s) from %s",
                    len(self.positions),
                    self.state_file,
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.error(
                    "Failed to load state from %s: %s — starting with empty book",
                    self.state_file,
                    exc,
                )
                self.positions = {}
        else:
            logger.info(
                "No existing state file at %s — starting with empty book",
                self.state_file,
            )
            self.positions = {}

    def save_state(self) -> None:
        """
        Write live_state.json atomically: write to a sibling .tmp file then
        rename so readers never see a partially written file.
        """
        tmp_path = self.state_file.with_suffix(".json.tmp")
        payload = {"positions": self.positions}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with tmp_path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
            tmp_path.replace(self.state_file)
            logger.debug("State saved to %s", self.state_file)
        except OSError as exc:
            logger.error("Failed to save state to %s: %s", self.state_file, exc)
            raise

    # ------------------------------------------------------------------
    # Duplicate guard
    # ------------------------------------------------------------------

    def can_open(
        self,
        symbol: str,
        expiry: str,
        strike: int,
        option_type: str,
    ) -> bool:
        """
        Return False if a position already exists for the same
        (symbol, expiry, strike, option_type) combination regardless of its
        status.  Prevents accidental double-entries.
        """
        key = (symbol, expiry, strike, option_type)
        for pos in self.positions.values():
            if (
                pos["symbol"] == symbol
                and pos["expiry"] == expiry
                and pos["strike"] == strike
                and pos["option_type"] == option_type
            ):
                logger.debug(
                    "Duplicate guard: position already exists for %s", key
                )
                return False
        return True

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    def open_position(
        self,
        symbol: str,
        expiry: str,
        strike: int,
        option_type: str,
        tsym: str,
        token: str,
        exchange: str,
        lots: int,
        lot_size: int,
        entry_price: float,
        entry_time: str,
    ) -> str:
        """
        Create a new virtual long position and persist state.

        Hard constraint: exactly one lot per unique (symbol, expiry, strike,
        option_type).  Raises ``ValueError`` if ``can_open()`` returns False.

        Parameters
        ----------
        symbol:       Underlying symbol, e.g. "NIFTY"
        expiry:       Expiry string, e.g. "10-JUN-2026"
        strike:       Strike price as integer, e.g. 24500
        option_type:  "CE" or "PE"
        tsym:         Trading symbol, e.g. "NIFTY10JUN26C24500"
        token:        Instrument token string
        exchange:     Exchange segment, e.g. "NFO"
        lots:         Number of lots (hard-capped at 1 by contract)
        lot_size:     Lot size for the instrument
        entry_price:  Fill price (LTP at time of virtual entry)
        entry_time:   ISO-8601 string, e.g. "2026-06-10T14:25:00"

        Returns
        -------
        str
            The newly created position_id.

        Raises
        ------
        ValueError
            If a position for the same contract already exists.
        """
        if not self.can_open(symbol, expiry, strike, option_type):
            raise ValueError(
                f"Position already exists for {symbol} {expiry} {strike} {option_type}"
            )

        if lots != 1:
            logger.warning(
                "open_position called with lots=%d; hard constraint enforces 1 lot — using 1",
                lots,
            )
            lots = 1

        position_id = f"{symbol}_{strike}_{option_type}_{int(time.time())}"

        position: dict = {
            "position_id": position_id,
            "symbol": symbol,
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
            "tsym": tsym,
            "token": token,
            "exchange": exchange,
            "lots": lots,
            "lot_size": lot_size,
            "entry_price": entry_price,
            "entry_time": entry_time,
            "current_ltp": entry_price,
            "peak_ltp": entry_price,
            "trail_stop": None,
            "status": "OPEN",
            "exit_price": None,
            "exit_time": None,
            "exit_reason": None,
            "pnl": None,
        }

        self.positions[position_id] = position
        self.save_state()

        logger.info(
            "Opened virtual position %s | %s %s %s %s @ %.2f",
            position_id,
            symbol,
            expiry,
            strike,
            option_type,
            entry_price,
        )
        return position_id

    def update_ltp(self, position_id: str, current_ltp: float) -> None:
        """
        Update ``current_ltp`` for an open position and raise ``peak_ltp``
        if the new price is higher.  Persists state.

        Parameters
        ----------
        position_id:  ID of the position to update
        current_ltp:  Latest last-traded price
        """
        pos = self._get_position(position_id)
        pos["current_ltp"] = current_ltp
        if current_ltp > pos["peak_ltp"]:
            pos["peak_ltp"] = current_ltp
            logger.debug(
                "New peak LTP for %s: %.2f", position_id, current_ltp
            )
        self.save_state()

    def set_trail_stop(self, position_id: str, trail_stop: float) -> None:
        """
        Set or update the trailing stop for an open position.  Persists state.

        Parameters
        ----------
        position_id:  ID of the position to update
        trail_stop:   New trailing stop price
        """
        pos = self._get_position(position_id)
        old = pos.get("trail_stop")
        pos["trail_stop"] = trail_stop
        self.save_state()
        logger.debug(
            "Trail stop for %s: %.2f -> %.2f",
            position_id,
            old if old is not None else 0.0,
            trail_stop,
        )

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_time: str,
        exit_reason: str,
    ) -> float:
        """
        Close a virtual position, compute PnL, persist state, and return PnL.

        PnL formula (long option, BUY-side only):
            pnl = (exit_price - entry_price) * lots * lot_size

        Parameters
        ----------
        position_id:  ID of the position to close
        exit_price:   Exit LTP
        exit_time:    ISO-8601 string for the exit timestamp
        exit_reason:  Human-readable reason, e.g. "TRAIL_STOP_HIT"

        Returns
        -------
        float
            Realised PnL for this position.

        Raises
        ------
        ValueError
            If the position is not OPEN.
        """
        pos = self._get_position(position_id)
        if pos["status"] != "OPEN":
            raise ValueError(
                f"Cannot close position {position_id}: status is {pos['status']}"
            )

        pnl = (exit_price - pos["entry_price"]) * pos["lots"] * pos["lot_size"]

        pos["exit_price"] = exit_price
        pos["exit_time"] = exit_time
        pos["exit_reason"] = exit_reason
        pos["status"] = "CLOSED"
        pos["pnl"] = pnl
        # Snapshot the final LTP
        pos["current_ltp"] = exit_price

        self.save_state()

        logger.info(
            "Closed position %s | reason=%s exit=%.2f pnl=%.2f",
            position_id,
            exit_reason,
            exit_price,
            pnl,
        )
        return pnl

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def open_positions(self) -> list[dict]:
        """Return all positions whose status is OPEN."""
        return [p for p in self.positions.values() if p["status"] == "OPEN"]

    def all_positions(self) -> list[dict]:
        """Return all positions (OPEN and CLOSED)."""
        return list(self.positions.values())

    def total_pnl(self) -> float:
        """
        Sum of realised PnL across all CLOSED positions.
        Returns 0.0 when there are no closed positions.
        """
        return sum(
            p["pnl"]
            for p in self.positions.values()
            if p["status"] == "CLOSED" and p["pnl"] is not None
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_position(self, position_id: str) -> dict:
        """Return position dict or raise KeyError."""
        try:
            return self.positions[position_id]
        except KeyError:
            raise KeyError(f"Unknown position_id: {position_id}") from None
