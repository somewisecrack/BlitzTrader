"""
GammaBlast — Trailing Exit Engine
Deterministic trailing-stop and hard-stop logic for virtual option positions.
No broker mutations: place_order / cancel_order are never called.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger("GammaBlast.TrailingExit")


class TrailingExitEngine:
    """
    Stateless evaluator that decides, on each scan tick, whether a virtual
    option position should be closed and what the new trailing-stop price
    should be.

    All decisions are deterministic given the position snapshot and the
    current market price — no internal state is mutated between calls.

    Parameters
    ----------
    trail_activation_mult:
        Start trailing only when ``current_ltp >= entry_price * trail_activation_mult``.
        Default 2.0 (i.e. the option has doubled).
    trail_initial_fraction:
        When trailing is active (but below the tight-trail threshold), set
        ``trail_stop = peak_ltp * (1 - trail_initial_fraction)``.
        Default 0.40 (allow a 40 % pullback from peak before exiting).
    trail_tight_mult:
        Switch to the tighter trail fraction when
        ``current_ltp >= entry_price * trail_tight_mult``.
        Default 4.0.
    trail_tight_fraction:
        Tighter fraction used above the tight-trail threshold.
        Default 0.25 (allow only a 25 % pullback from peak).
    hard_stop_fraction:
        Immediately exit if ``current_ltp <= entry_price * hard_stop_fraction``.
        Default 0.50 (i.e. option has lost half its value).
    stale_data_seconds:
        Exit if the last quote arrived more than this many seconds ago.
        Default 300 (5 minutes).
    """

    def __init__(
        self,
        trail_activation_mult: float = 2.0,
        trail_initial_fraction: float = 0.40,
        trail_tight_mult: float = 4.0,
        trail_tight_fraction: float = 0.25,
        hard_stop_fraction: float = 0.50,
        stale_data_seconds: float = 300,
    ) -> None:
        self.trail_activation_mult = trail_activation_mult
        self.trail_initial_fraction = trail_initial_fraction
        self.trail_tight_mult = trail_tight_mult
        self.trail_tight_fraction = trail_tight_fraction
        self.hard_stop_fraction = hard_stop_fraction
        self.stale_data_seconds = stale_data_seconds

        logger.info(
            "TrailingExitEngine initialised | activation=%.1fx tight=%.1fx "
            "initial_frac=%.2f tight_frac=%.2f hard_stop=%.2f stale=%ds",
            trail_activation_mult,
            trail_tight_mult,
            trail_initial_fraction,
            trail_tight_fraction,
            hard_stop_fraction,
            int(stale_data_seconds),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        position: dict,
        current_ltp: float,
        last_quote_time: float,
    ) -> tuple[bool, str]:
        """
        Decide whether the position should be closed on this tick.

        Exit conditions are checked in priority order:

        1. **HARD_STOP** — current_ltp has fallen to or below
           ``entry_price * hard_stop_fraction``.
        2. **STALE_DATA** — the last received quote is older than
           ``stale_data_seconds``.
        3. **TRAIL_STOP_HIT** — a trailing stop has been set and
           current_ltp has fallen to or below it.

        Parameters
        ----------
        position:
            Position dict as stored in VirtualPositionBook (must contain at
            least ``entry_price``, ``trail_stop`` keys).
        current_ltp:
            Latest last-traded price for the option.
        last_quote_time:
            Unix timestamp (float) of the most recent price quote received.

        Returns
        -------
        tuple[bool, str]
            ``(should_exit, reason)`` where ``reason`` is one of
            ``"HARD_STOP"``, ``"STALE_DATA"``, ``"TRAIL_STOP_HIT"``
            or ``""`` when no exit is triggered.
        """
        entry_price: float = position["entry_price"]
        trail_stop: Optional[float] = position.get("trail_stop")
        position_id: str = position.get("position_id", "<unknown>")

        # 1. Hard stop
        hard_stop_level = entry_price * self.hard_stop_fraction
        if current_ltp <= hard_stop_level:
            logger.info(
                "HARD_STOP triggered for %s | ltp=%.2f hard_stop_level=%.2f",
                position_id,
                current_ltp,
                hard_stop_level,
            )
            return True, "HARD_STOP"

        # 2. Stale data
        age_seconds = time.time() - last_quote_time
        if age_seconds > self.stale_data_seconds:
            logger.warning(
                "STALE_DATA exit for %s | last quote was %.0fs ago (threshold=%ds)",
                position_id,
                age_seconds,
                int(self.stale_data_seconds),
            )
            return True, "STALE_DATA"

        # 3. Trail stop hit
        if trail_stop is not None and current_ltp <= trail_stop:
            logger.info(
                "TRAIL_STOP_HIT for %s | ltp=%.2f trail_stop=%.2f",
                position_id,
                current_ltp,
                trail_stop,
            )
            return True, "TRAIL_STOP_HIT"

        return False, ""

    def compute_trail_stop(
        self,
        position: dict,
        current_ltp: float,
    ) -> Optional[float]:
        """
        Compute the new trailing-stop price to set for the position.

        Rules
        -----
        * If ``current_ltp < entry_price * trail_activation_mult``: trailing
          has not yet activated — return ``None``.
        * If ``current_ltp >= entry_price * trail_tight_mult``: use the tight
          fraction: ``trail = peak_ltp * (1 - trail_tight_fraction)``.
        * Otherwise: use the initial fraction:
          ``trail = peak_ltp * (1 - trail_initial_fraction)``.
        * The trail stop can only ever move UP (tighten), never down.  The
          returned value is ``max(new_trail, existing_trail_stop or 0)``.
        * The trail stop is always at least above the hard-stop floor
          (``entry_price * hard_stop_fraction``); if the computed level is at
          or below the hard-stop floor the value is still returned — the
          ``evaluate()`` method's hard-stop check will fire first.

        Parameters
        ----------
        position:
            Position dict with at minimum ``entry_price``, ``peak_ltp``, and
            ``trail_stop`` keys.
        current_ltp:
            Latest last-traded price for the option.

        Returns
        -------
        Optional[float]
            New trail-stop price, or ``None`` if trailing has not activated.
        """
        entry_price: float = position["entry_price"]
        peak_ltp: float = position["peak_ltp"]
        existing_trail: Optional[float] = position.get("trail_stop")
        position_id: str = position.get("position_id", "<unknown>")

        activation_level = entry_price * self.trail_activation_mult

        # Not yet activated
        if current_ltp < activation_level:
            logger.debug(
                "Trail not yet active for %s | ltp=%.2f activation_level=%.2f",
                position_id,
                current_ltp,
                activation_level,
            )
            return None

        # Determine which fraction to apply
        tight_level = entry_price * self.trail_tight_mult
        if current_ltp >= tight_level:
            fraction = self.trail_tight_fraction
            logger.debug(
                "Tight trail zone for %s | ltp=%.2f tight_level=%.2f fraction=%.2f",
                position_id,
                current_ltp,
                tight_level,
                fraction,
            )
        else:
            fraction = self.trail_initial_fraction
            logger.debug(
                "Initial trail zone for %s | ltp=%.2f fraction=%.2f",
                position_id,
                current_ltp,
                fraction,
            )

        new_trail = peak_ltp * (1.0 - fraction)

        # Only ever tighten — never loosen
        floor = existing_trail if existing_trail is not None else 0.0
        result = max(new_trail, floor)

        logger.debug(
            "Trail stop for %s: new=%.2f existing=%.2f -> %.2f",
            position_id,
            new_trail,
            floor,
            result,
        )
        return result

    def check_eod_close(
        self,
        current_ist_time_str: str,
        eod_close_time: str = "15:15",
    ) -> bool:
        """
        Return ``True`` if the current IST time is at or past the EOD
        close time, triggering a forced close of all open positions.

        Both arguments must be strings in ``"HH:MM"`` format.

        Parameters
        ----------
        current_ist_time_str:
            Current time in IST as ``"HH:MM"``, e.g. ``"15:10"``.
        eod_close_time:
            The threshold time after which no new positions should be
            held and all open positions must be closed.  Default ``"15:15"``.

        Returns
        -------
        bool
            ``True`` if ``current_ist_time_str >= eod_close_time``.

        Raises
        ------
        ValueError
            If either time string cannot be parsed as ``"HH:MM"``.
        """
        current_h, current_m = self._parse_hhmm(current_ist_time_str)
        eod_h, eod_m = self._parse_hhmm(eod_close_time)

        current_minutes = current_h * 60 + current_m
        eod_minutes = eod_h * 60 + eod_m

        should_close = current_minutes >= eod_minutes
        if should_close:
            logger.info(
                "EOD close triggered | current=%s >= eod=%s",
                current_ist_time_str,
                eod_close_time,
            )
        return should_close

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_hhmm(time_str: str) -> tuple[int, int]:
        """
        Parse a ``"HH:MM"`` string into ``(hours, minutes)``.

        Raises
        ------
        ValueError
            If the string is not in the expected format or values are
            outside valid ranges.
        """
        try:
            parts = time_str.strip().split(":")
            if len(parts) != 2:
                raise ValueError(f"Expected 'HH:MM', got '{time_str}'")
            hours, minutes = int(parts[0]), int(parts[1])
        except (AttributeError, TypeError) as exc:
            raise ValueError(
                f"Cannot parse time string '{time_str}': {exc}"
            ) from exc

        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            raise ValueError(
                f"Time values out of range in '{time_str}': hours={hours} minutes={minutes}"
            )
        return hours, minutes
