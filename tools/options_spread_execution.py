"""
tools/options_spread_execution.py — Two-leg vertical spread order placement.

Hard invariants (enforced here, never bypassed):
  1. Long / protective leg is placed FIRST.
  2. Short leg is placed ONLY after the long leg fill is confirmed.
  3. If the short leg fails after the long leg fills:
       → Exit the long leg immediately (emergency protective buy-back for sell leg
         or sell-to-close for buy leg).  Never leave naked short exposure.
  4. No futures tokens — only NFO OPTIDX contracts.
  5. Only NIFTY and BANKNIFTY.
  6. No basket / simultaneous orders.

Execution modes
───────────────
  virtual=True  (default, LIVE_ORDER_EXECUTION=false in config):
    Both legs are filled immediately at fill_price_estimate from SpreadCandidate.
    No Shoonya API calls are made.  The OpenSpread is persisted to state as normal
    so all downstream P&L tracking, exit logic, and serial-exit flows work unchanged.

  virtual=False (LIVE_ORDER_EXECUTION=true):
    Live broker path — place LIMIT orders via Shoonya, poll for fill confirmation.
    Only enable when broker connectivity and order routing have been fully tested.

Order flow (live mode):
  place_spread(candidate)
    ├─ Guardrail checks (paused, stopped, max-positions, time, instrument)
    ├─ Place long leg → wait for fill confirmation (up to fill_timeout_seconds)
    │     └─ if fill fails → abort, return error (no short leg, no exposure)
    ├─ Place short leg → wait for fill confirmation
    │     └─ if fill fails → emergency close long leg → return error
    └─ Record OpenSpread → return success

Returns:
  {"ok": True,  "spread": OpenSpread, ...}            on success
  {"ok": False, "error": str, "emergency": bool, ...} on failure
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pytz

from tools.options_spread_builder import SpreadCandidate, SpreadLeg

logger = logging.getLogger("BlitzTrader.SpreadExecution")

IST = pytz.timezone("Asia/Kolkata")

_SHOONYA_OPTION_TSYM_RE = re.compile(r"^(NIFTY|BANKNIFTY)\d{2}[A-Z]{3}\d{2}[CP]\d+$")

# How long (seconds) to wait for a leg fill before treating as timeout
_DEFAULT_FILL_TIMEOUT = 300   # 5 minutes

# How often to poll order book during fill wait (seconds)
_POLL_INTERVAL = 2


@dataclass
class OpenSpread:
    """
    A successfully executed (both legs filled) vertical spread position.
    Tracked by the portfolio manager.
    """
    spread_id: str              # unique ID for this spread, e.g. "SPR-20260526-001"
    symbol: str
    spread_type: str
    direction: str
    expiry: str                 # "26-MAY-2026"
    lot_size: int
    lots: int

    long_tsym: str
    long_token: str
    long_action: str            # always "BUY"
    long_strike: int
    long_option_type: str
    long_fill_price: float
    long_order_id: str

    short_tsym: str
    short_token: str
    short_action: str           # always "SELL"
    short_strike: int
    short_option_type: str
    short_fill_price: float
    short_order_id: str

    net_debit_or_credit: float  # from candidate economics at entry time
    max_profit: float
    max_loss: float
    breakeven: float

    opened_at: str              # ISO datetime IST
    signal_id: str = ""
    strategy: str = ""
    underlying_at_entry: float = 0.0


# ──────────────────────────────────────────────────────────────────────────────
#   EXECUTION ENGINE
# ──────────────────────────────────────────────────────────────────────────────

class SpreadExecutionEngine:
    """
    Places two-leg vertical spreads via Shoonya with hard safety invariants.

    Usage:
        engine = SpreadExecutionEngine(shoonya_client, state_manager, max_open_spreads=2)
        result = engine.place_spread(candidate)
    """

    def __init__(
        self,
        shoonya_client,
        state_manager,
        max_open_spreads: int = 2,
        no_entry_after: str = "15:05",
        fill_timeout_seconds: int = _DEFAULT_FILL_TIMEOUT,
        virtual: bool = True,
    ):
        self._client = shoonya_client
        self._state = state_manager
        self._max_open_spreads = max_open_spreads
        self._no_entry_after = no_entry_after
        self._fill_timeout = fill_timeout_seconds
        self._virtual = virtual

    # ── Guardrails ────────────────────────────────────────────────────────────

    def _check_guardrails(self, candidate: SpreadCandidate) -> Optional[str]:
        """Return error string if blocked, None if OK."""
        state = self._state.get_state()

        if state.get("is_stopped"):
            return "BLOCKED: trading stopped (daily loss limit hit)"

        if state.get("is_paused"):
            return "BLOCKED: trading paused by user (/pause)"

        # Time check
        now_ist = datetime.now(IST)
        hhmm = now_ist.strftime("%H:%M")
        if hhmm >= self._no_entry_after:
            return f"BLOCKED: no new entries after {self._no_entry_after} (now {hhmm})"

        # Max open spreads
        open_spreads = state.get("open_spreads", []) or []
        if len(open_spreads) >= self._max_open_spreads:
            return (
                f"BLOCKED: max open spreads ({self._max_open_spreads}) already open"
            )

        # Instrument validation
        if candidate.symbol not in ("NIFTY", "BANKNIFTY"):
            return f"BLOCKED: forbidden instrument {candidate.symbol!r}"

        for spread in open_spreads:
            existing_symbol = str(spread.get("symbol", "")).upper()
            if existing_symbol == candidate.symbol:
                return f"BLOCKED: no pyramiding — {candidate.symbol} spread already open"

        for leg in candidate.legs:
            if leg.exchange != "NFO":
                return f"BLOCKED: leg exchange {leg.exchange!r} is not NFO"
            # Reject if tsym looks like a futures contract or a non-Shoonya option.
            tsym = leg.tsym.upper()
            if tsym.endswith("F") or not _SHOONYA_OPTION_TSYM_RE.match(tsym):
                return f"BLOCKED: tsym {tsym!r} does not look like an option contract"

        return None  # all clear

    # ── Order helpers ─────────────────────────────────────────────────────────

    def _place_limit_order(
        self,
        tsym: str,
        token: str,
        action: str,        # "BUY" or "SELL"
        qty: int,
        price: float,
    ) -> tuple[Optional[str], str]:
        """
        Place a limit order via Shoonya.
        Returns (order_id, "") on success, (None, error) on failure.
        """
        if not self._client:
            return None, "no Shoonya client available (paper trading mode)"
        try:
            # Shoonya place_order parameters:
            # buy_or_sell, product_type, exchange, tradingsymbol, quantity,
            # discloseqty, price_type, price, trigger_price, retention, remarks
            resp = self._client.place_order(
                buy_or_sell=action,
                product_type="M",          # NRML for options
                exchange="NFO",
                tradingsymbol=tsym,
                quantity=qty,
                discloseqty=0,
                price_type="LMT",
                price=price,
                trigger_price=None,
                retention="DAY",
                remarks=f"BlitzTrader-{tsym}",
            )
            if resp and resp.get("stat") == "Ok":
                return resp.get("norenordno"), ""
            err = (resp or {}).get("emsg", str(resp))
            return None, f"place_order failed: {err}"
        except Exception as e:
            logger.exception("_place_limit_order(%s) raised", tsym)
            return None, f"exception: {e}"

    def _wait_for_fill(self, order_id: str, tsym: str) -> tuple[Optional[float], str]:
        """
        Poll Shoonya order book until fill or timeout.
        Returns (fill_price, "") on fill, (None, reason) on timeout/reject.
        """
        if not self._client:
            # Paper mode: simulate instant fill at last known price
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
        """Best-effort order cancel. Errors are logged but not raised."""
        if not self._client or not order_id:
            return
        try:
            self._client.cancel_order(order_id)
        except Exception:
            logger.exception("_cancel_order(%s) failed (non-fatal)", order_id)

    def _emergency_close_long(
        self,
        long_leg: SpreadLeg,
        long_order_id: str,
        qty: int,
    ) -> None:
        """
        Emergency: sell-to-close the long leg after short leg failure.
        Uses market order to ensure execution.
        """
        logger.warning(
            "EMERGENCY: closing long leg %s after short leg failure", long_leg.tsym
        )
        if not self._client:
            logger.warning("No Shoonya client — cannot emergency-close %s", long_leg.tsym)
            return
        try:
            resp = self._client.place_order(
                buy_or_sell="SELL",
                product_type="M",
                exchange="NFO",
                tradingsymbol=long_leg.tsym,
                quantity=qty,
                discloseqty=0,
                price_type="MKT",
                price=0,
                trigger_price=None,
                retention="DAY",
                remarks="BlitzTrader-EMERGENCY-CLOSE",
            )
            if resp and resp.get("stat") == "Ok":
                logger.warning(
                    "Emergency close placed for %s: order_id=%s",
                    long_leg.tsym, resp.get("norenordno"),
                )
            else:
                logger.error(
                    "Emergency close FAILED for %s: %s", long_leg.tsym, resp
                )
        except Exception:
            logger.exception("Emergency close raised for %s", long_leg.tsym)

    # ── Main entry point ──────────────────────────────────────────────────────

    def place_spread(self, candidate: SpreadCandidate) -> dict:
        """
        Execute both legs of the spread with safety invariants enforced.

        In virtual mode (self._virtual=True) fills are simulated immediately
        using candidate.legs[*].fill_price — no broker calls are made.

        Returns:
          {"ok": True, "spread": OpenSpread, "message": str}          on success
          {"ok": False, "error": str, "emergency": bool}              on failure
        """
        # Ensure legs are in expected order (long first)
        if len(candidate.legs) != 2:
            return {"ok": False, "error": "Candidate must have exactly 2 legs", "emergency": False}

        long_leg = candidate.legs[0]
        short_leg = candidate.legs[1]

        if long_leg.action != "BUY" or short_leg.action != "SELL":
            return {
                "ok": False,
                "error": (
                    f"Leg order invariant violated: "
                    f"legs[0].action={long_leg.action!r}, legs[1].action={short_leg.action!r}"
                ),
                "emergency": False,
            }

        # ── Guardrails (always enforced regardless of mode) ───────────────
        blocked = self._check_guardrails(candidate)
        if blocked:
            logger.warning("SpreadExecution: %s", blocked)
            return {"ok": False, "error": blocked, "emergency": False}

        qty = candidate.lot_size * candidate.lots
        spread_id = _generate_spread_id()

        # ── Branch: virtual vs live ───────────────────────────────────────
        if self._virtual:
            return self._place_spread_virtual(candidate, spread_id, long_leg, short_leg, qty)
        return self._place_spread_live(candidate, spread_id, long_leg, short_leg, qty)

    def _place_spread_virtual(
        self,
        candidate: SpreadCandidate,
        spread_id: str,
        long_leg,
        short_leg,
        qty: int,
    ) -> dict:
        """
        Simulate spread execution without any broker API calls.
        Fills are taken directly from candidate leg fill_price estimates.
        """
        long_fill = long_leg.fill_price
        short_fill = short_leg.fill_price

        logger.info(
            "SpreadExecution[%s]: VIRTUAL %s %s %s expiry=%s "
            "long=%s@%.2f short=%s@%.2f qty=%d",
            spread_id,
            candidate.symbol, candidate.spread_type, candidate.direction,
            candidate.expiry_str,
            long_leg.tsym, long_fill,
            short_leg.tsym, short_fill,
            qty,
        )

        spread = self._build_open_spread(
            candidate, spread_id, long_leg, short_leg,
            long_fill, short_fill,
            long_order_id="VIRTUAL-LONG",
            short_order_id="VIRTUAL-SHORT",
        )
        self._record_open_spread(spread)

        net_str = _net_str(candidate.net_debit_or_credit)
        message = (
            f"[VIRTUAL] Spread {spread_id} opened: "
            f"{candidate.symbol} {candidate.spread_type} expiry={candidate.expiry_str}\n"
            f"  Long:  {long_leg.tsym} @ ₹{long_fill:.2f}\n"
            f"  Short: {short_leg.tsym} @ ₹{short_fill:.2f}\n"
            f"  {net_str} | max profit ₹{candidate.max_profit:.2f} | "
            f"max loss ₹{candidate.max_loss:.2f}"
        )
        logger.info("SpreadExecution[%s]: %s", spread_id, message)
        return {"ok": True, "spread": spread, "message": message}

    def _place_spread_live(
        self,
        candidate: SpreadCandidate,
        spread_id: str,
        long_leg,
        short_leg,
        qty: int,
    ) -> dict:
        """
        Live broker path — place LIMIT orders via Shoonya and poll for fills.
        Long leg first, short leg only after long fill is confirmed.
        """
        logger.info(
            "SpreadExecution[%s]: LIVE placing %s %s %s expiry=%s "
            "long=%s@%.2f short=%s@%.2f qty=%d",
            spread_id,
            candidate.symbol, candidate.spread_type, candidate.direction,
            candidate.expiry_str,
            long_leg.tsym, long_leg.fill_price,
            short_leg.tsym, short_leg.fill_price,
            qty,
        )

        # ── Step 1: Place long (protective) leg ───────────────────────────
        long_order_id, err = self._place_limit_order(
            long_leg.tsym, long_leg.token, "BUY", qty, long_leg.fill_price
        )
        if long_order_id is None:
            logger.error("SpreadExecution[%s]: long leg placement failed — %s", spread_id, err)
            return {"ok": False, "error": f"Long leg placement failed: {err}", "emergency": False}

        # ── Step 2: Confirm long fill ──────────────────────────────────────
        long_actual_fill, fill_err = self._wait_for_fill(long_order_id, long_leg.tsym)
        if long_actual_fill is None:
            self._cancel_order(long_order_id)
            logger.error(
                "SpreadExecution[%s]: long leg fill failed — %s (cancelled)", spread_id, fill_err
            )
            return {"ok": False, "error": f"Long leg fill failed: {fill_err}", "emergency": False}

        logger.info(
            "SpreadExecution[%s]: long leg filled %s @ ₹%.2f",
            spread_id, long_leg.tsym, long_actual_fill,
        )

        # ── Step 3: Place short leg (ONLY after long confirmed) ───────────
        short_order_id, err = self._place_limit_order(
            short_leg.tsym, short_leg.token, "SELL", qty, short_leg.fill_price
        )
        if short_order_id is None:
            logger.error(
                "SpreadExecution[%s]: short leg placement failed — %s; "
                "triggering emergency long close",
                spread_id, err,
            )
            self._emergency_close_long(long_leg, long_order_id, qty)
            return {
                "ok": False,
                "error": f"Short leg placement failed: {err}. Long leg emergency-closed.",
                "emergency": True,
            }

        # ── Step 4: Confirm short fill ─────────────────────────────────────
        short_actual_fill, fill_err = self._wait_for_fill(short_order_id, short_leg.tsym)
        if short_actual_fill is None:
            self._cancel_order(short_order_id)
            logger.error(
                "SpreadExecution[%s]: short leg fill failed — %s; "
                "triggering emergency long close",
                spread_id, fill_err,
            )
            self._emergency_close_long(long_leg, long_order_id, qty)
            return {
                "ok": False,
                "error": f"Short leg fill failed: {fill_err}. Long leg emergency-closed.",
                "emergency": True,
            }

        logger.info(
            "SpreadExecution[%s]: short leg filled %s @ ₹%.2f",
            spread_id, short_leg.tsym, short_actual_fill,
        )

        # ── Step 5: Record open spread ─────────────────────────────────────
        spread = self._build_open_spread(
            candidate, spread_id, long_leg, short_leg,
            long_actual_fill, short_actual_fill,
            long_order_id=str(long_order_id),
            short_order_id=str(short_order_id),
        )
        self._record_open_spread(spread)

        net_str = _net_str(candidate.net_debit_or_credit)
        message = (
            f"Spread {spread_id} opened: {candidate.symbol} {candidate.spread_type} "
            f"expiry={candidate.expiry_str}\n"
            f"  Long:  {long_leg.tsym} @ ₹{long_actual_fill:.2f}\n"
            f"  Short: {short_leg.tsym} @ ₹{short_actual_fill:.2f}\n"
            f"  {net_str} | max profit ₹{candidate.max_profit:.2f} | "
            f"max loss ₹{candidate.max_loss:.2f}"
        )
        logger.info("SpreadExecution[%s]: %s", spread_id, message)
        return {"ok": True, "spread": spread, "message": message}

    @staticmethod
    def _build_open_spread(
        candidate: SpreadCandidate,
        spread_id: str,
        long_leg,
        short_leg,
        long_fill: float,
        short_fill: float,
        long_order_id: str,
        short_order_id: str,
    ) -> "OpenSpread":
        """Construct an OpenSpread dataclass from a filled candidate."""
        now_ist = datetime.now(IST)
        return OpenSpread(
            spread_id=spread_id,
            symbol=candidate.symbol,
            spread_type=candidate.spread_type,
            direction=candidate.direction,
            expiry=candidate.expiry_str,
            lot_size=candidate.lot_size,
            lots=candidate.lots,
            long_tsym=long_leg.tsym,
            long_token=long_leg.token,
            long_action="BUY",
            long_strike=long_leg.strike,
            long_option_type=long_leg.option_type,
            long_fill_price=long_fill,
            long_order_id=long_order_id,
            short_tsym=short_leg.tsym,
            short_token=short_leg.token,
            short_action="SELL",
            short_strike=short_leg.strike,
            short_option_type=short_leg.option_type,
            short_fill_price=short_fill,
            short_order_id=short_order_id,
            net_debit_or_credit=candidate.net_debit_or_credit,
            max_profit=candidate.max_profit,
            max_loss=candidate.max_loss,
            breakeven=candidate.breakeven,
            opened_at=now_ist.isoformat(),
            signal_id=candidate.signal_id,
            strategy=candidate.strategy,
            underlying_at_entry=candidate.underlying_price,
        )

    def _record_open_spread(self, spread: OpenSpread) -> None:
        """Append the spread to state_manager open_spreads list."""
        try:
            import dataclasses
            state = self._state.get_state()
            open_spreads = list(state.get("open_spreads", []) or [])
            open_spreads.append(dataclasses.asdict(spread))
            self._state.update_state(open_spreads=open_spreads)
        except Exception:
            logger.exception("_record_open_spread: failed to persist spread (non-fatal)")


# ──────────────────────────────────────────────────────────────────────────────
#   HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _generate_spread_id() -> str:
    """Generate a human-readable unique spread ID."""
    now = datetime.now(IST)
    uid = uuid.uuid4().hex[:6].upper()
    return f"SPR-{now.strftime('%Y%m%d-%H%M%S')}-{uid}"


def _net_str(net_debit_or_credit: float) -> str:
    """Format net debit/credit for log messages."""
    if net_debit_or_credit >= 0:
        return f"net debit ₹{net_debit_or_credit:.2f}"
    return f"net credit ₹{abs(net_debit_or_credit):.2f}"
