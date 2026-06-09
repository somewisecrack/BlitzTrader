"""
tools/options_spread_builder.py — Build NSE index option vertical spread candidates.

Responsibilities:
  - Accept a directional signal + underlying spot price + OptionsChain resolver.
  - Classify signal direction (BULLISH / BEARISH).
  - Select spread type: debit for momentum strategies, credit for mean-reversion/range.
  - Select expiry via OptionsChain.select_expiry().
  - Compute both strikes (long/short legs) from ATM ± N steps.
  - Resolve tokens for both legs; validate live quotes.
  - Compute net_debit_or_credit, max_profit, max_loss, breakeven, risk_reward.
  - Apply hard guardrails: only NIFTY/BANKNIFTY, no naked shorts, no futures tokens,
    max-loss budget check, stale/zero-bid reject.
  - Return SpreadCandidate dataclass on success, None on any failure.

Spread types:
  BULL_CALL  — debit spread  (buy lower CE, sell higher CE)
  BEAR_PUT   — debit spread  (buy higher PE, sell lower PE)
  BULL_PUT   — credit spread (sell higher PE, buy lower PE)
  BEAR_CALL  — credit spread (sell lower CE, buy higher CE)

Hard invariants (never violated here):
  - Long / protective leg is listed FIRST in .legs; execution layer places it first.
  - Short leg is SECOND; execution layer places it only after long fill confirmed.
  - No naked shorts; both legs are always present or candidate is rejected.
  - Instruments: NIFTY and BANKNIFTY OPTIDX on NFO only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pytz

from tools.options_chain import OptionsChain, fill_price_estimate

logger = logging.getLogger("BlitzTrader.SpreadBuilder")

IST = pytz.timezone("Asia/Kolkata")

# Strategy codes that imply momentum (use debit spreads — direction-aligned)
_MOMENTUM_STRATEGIES = {
    "VP-05",  # 3EMA Trend
    "VP-07",  # Wicks Pullback
    "VP-01",  # Counter Bull Trap (momentum reversal)
    "VP-02",  # Counter Bear Trap (momentum reversal)
}

# Strategy codes that imply mean-reversion / range (use credit spreads)
_CREDIT_STRATEGIES = {
    "VP-14",  # Morning Star — exhaustion reversal at support
    "VP-15",  # Evening Star — exhaustion reversal at resistance
    "VP-18",  # M-Pattern Double Top
    "VP-19",  # W-Pattern Double Bottom
    "VP-21",  # Extreme Candle Reversal
}

# Allowed underlyings
_ALLOWED_UNDERLYINGS = {"NIFTY", "BANKNIFTY"}


@dataclass
class SpreadLeg:
    """One leg of a vertical spread."""
    action: str           # "BUY" or "SELL"
    option_type: str      # "CE" or "PE"
    strike: int
    expiry: date
    token: str
    tsym: str
    exchange: str
    lot_size: int
    quote: dict           # raw Shoonya quote at build time
    fill_price: float     # conservative fill estimate


@dataclass
class SpreadCandidate:
    """
    Fully-validated vertical spread candidate ready for execution.

    Both legs are present; long/protective leg is legs[0], short leg is legs[1].
    Execution layer MUST place legs[0] first, legs[1] only after fill confirmed.
    """
    symbol: str               # NIFTY or BANKNIFTY
    spread_type: str          # BULL_CALL | BULL_PUT | BEAR_PUT | BEAR_CALL
    direction: str            # BULLISH or BEARISH
    expiry: date
    expiry_str: str           # "26-MAY-2026"
    legs: list[SpreadLeg]     # [long_leg, short_leg] — order matters
    lot_size: int             # from long leg
    lots: int                 # 1 always (single lot per trade)

    # Economics (per lot, excluding brokerage)
    net_debit_or_credit: float    # positive = net debit (paid), negative = net credit (received)
    max_profit: float             # best-case P&L per lot
    max_loss: float               # worst-case P&L per lot (positive = rupee loss amount)
    breakeven: float              # spot price at breakeven
    risk_reward: float            # max_profit / max_loss

    # Provenance
    signal_id: str = ""
    strategy: str = ""
    underlying_price: float = 0.0


# ──────────────────────────────────────────────────────────────────────────────
#   SPREAD TYPE SELECTION
# ──────────────────────────────────────────────────────────────────────────────

def _select_spread_type(direction: str, strategy_code: str) -> str:
    """
    Determine spread type from signal direction and strategy.

    Rules:
      Momentum strategies  → debit spread (BULL_CALL for bullish, BEAR_PUT for bearish)
      Credit strategies    → credit spread (BULL_PUT for bullish, BEAR_CALL for bearish)
      Unknown strategies   → default to credit (lower capital outlay, defined risk)
    """
    is_bullish = direction.upper() == "BULLISH"
    code = strategy_code.upper() if strategy_code else ""

    if code in _MOMENTUM_STRATEGIES:
        return "BULL_CALL" if is_bullish else "BEAR_PUT"
    else:
        # credit strategies + unknown default → credit spreads
        return "BULL_PUT" if is_bullish else "BEAR_CALL"


# ──────────────────────────────────────────────────────────────────────────────
#   STRIKE SELECTION
# ──────────────────────────────────────────────────────────────────────────────

def _build_strikes(
    spread_type: str,
    atm: int,
    symbol: str,
    width_steps: int,
) -> tuple[int, int]:
    """
    Return (long_strike, short_strike) for a given spread type.

    BULL_CALL debit:  long lower CE (ATM), short higher CE (ATM + width)
    BEAR_PUT  debit:  long higher PE (ATM), short lower PE (ATM - width)
    BULL_PUT  credit: short higher PE (ATM), long lower PE (ATM - width)
    BEAR_CALL credit: short lower CE (ATM), long higher CE (ATM + width)
    """
    from tools.options_chain import _STRIKE_STEP
    step = _STRIKE_STEP.get(symbol.upper(), 50)
    w = width_steps * step

    if spread_type == "BULL_CALL":
        return atm, atm + w                       # long ATM CE, short OTM CE
    elif spread_type == "BEAR_PUT":
        return atm, atm - w                       # long ATM PE, short OTM PE
    elif spread_type == "BULL_PUT":
        return atm - w, atm                       # long OTM PE, short ATM PE
    elif spread_type == "BEAR_CALL":
        return atm + w, atm                       # long OTM CE, short ATM CE
    else:
        raise ValueError(f"Unknown spread_type: {spread_type!r}")


def _option_type_for_spread(spread_type: str) -> str:
    """Return the option type (CE/PE) for a given spread type."""
    if spread_type in ("BULL_CALL", "BEAR_CALL"):
        return "CE"
    return "PE"


# ──────────────────────────────────────────────────────────────────────────────
#   ECONOMICS
# ──────────────────────────────────────────────────────────────────────────────

def _compute_economics(
    spread_type: str,
    long_fill: float,
    short_fill: float,
    long_strike: int,
    short_strike: int,
    lot_size: int,
) -> tuple[float, float, float, float, float]:
    """
    Compute (net_debit_or_credit, max_profit, max_loss, breakeven, risk_reward).

    Debit spreads  (BULL_CALL, BEAR_PUT):
      net_debit         = long_fill - short_fill   (positive, cash paid out)
      max_profit / lot  = (spread_width - net_debit) * lot_size
      max_loss   / lot  = net_debit * lot_size
      breakeven (BULL_CALL): long_strike + net_debit
      breakeven (BEAR_PUT):  long_strike - net_debit

    Credit spreads (BULL_PUT, BEAR_CALL):
      net_credit        = short_fill - long_fill   (positive, cash received)
      net_debit_or_credit = -net_credit  (negative = inflow)
      max_profit / lot  = net_credit * lot_size
      max_loss   / lot  = (spread_width - net_credit) * lot_size
      breakeven (BULL_PUT):  short_strike - net_credit
      breakeven (BEAR_CALL): short_strike + net_credit
    """
    spread_width = abs(short_strike - long_strike)

    if spread_type in ("BULL_CALL", "BEAR_PUT"):
        # Debit
        net = long_fill - short_fill
        net_debit_or_credit = net
        max_profit = (spread_width - net) * lot_size
        max_loss = net * lot_size
        if spread_type == "BULL_CALL":
            breakeven = long_strike + net
        else:  # BEAR_PUT
            breakeven = long_strike - net
    else:
        # Credit (BULL_PUT or BEAR_CALL)
        net_credit = short_fill - long_fill
        net_debit_or_credit = -net_credit
        max_profit = net_credit * lot_size
        max_loss = (spread_width - net_credit) * lot_size
        if spread_type == "BULL_PUT":
            breakeven = short_strike - net_credit
        else:  # BEAR_CALL
            breakeven = short_strike + net_credit

    risk_reward = (max_profit / max_loss) if max_loss > 0 else 0.0
    return net_debit_or_credit, max_profit, max_loss, breakeven, risk_reward


# ──────────────────────────────────────────────────────────────────────────────
#   MAIN BUILDER
# ──────────────────────────────────────────────────────────────────────────────

class SpreadBuilder:
    """
    Builds SpreadCandidate objects from directional trading signals.

    Usage:
        builder = SpreadBuilder(options_chain, max_risk_rupees=10000)
        candidate = builder.build(signal, underlying_price)
    """

    def __init__(
        self,
        options_chain: OptionsChain,
        max_risk_rupees: float = 10000.0,
        min_risk_reward: float = 0.3,
        min_sell_bid: float = 5.0,
        max_spread_fraction: float = 0.30,
        min_ltp: float = 2.0,
    ):
        self._chain = options_chain
        self._max_risk = max_risk_rupees
        self._min_rr = min_risk_reward
        self._min_sell_bid = min_sell_bid
        self._max_spread_fraction = max_spread_fraction
        self._min_ltp = min_ltp

    def build(
        self,
        signal: dict,
        underlying_price: float,
        lots: int = 1,
    ) -> Optional[SpreadCandidate]:
        """
        Build a spread candidate from a signal and current spot price.

        Signal dict must contain:
          - symbol:     "NIFTY" or "BANKNIFTY"
          - direction:  "BUY" / "BULLISH" (bullish) or "SELL" / "BEARISH" (bearish)
          - strategy:   strategy code e.g. "VP-05"  (used for spread type selection)

        Returns SpreadCandidate on success, None on any validation failure.
        All failures are logged at WARNING level; never raises.
        """
        try:
            return self._build_inner(signal, underlying_price, lots)
        except Exception:
            logger.exception("SpreadBuilder.build raised unexpectedly for signal=%r", signal)
            return None

    def _build_inner(
        self,
        signal: dict,
        underlying_price: float,
        lots: int,
    ) -> Optional[SpreadCandidate]:
        # ── 1. Parse and validate inputs ───────────────────────────────────
        symbol = (signal.get("symbol") or "").upper().strip()
        if symbol not in _ALLOWED_UNDERLYINGS:
            logger.warning("SpreadBuilder: symbol %r not in allowed underlyings", symbol)
            return None

        raw_direction = (signal.get("direction") or "").upper().strip()
        # Normalize BUY/SELL to BULLISH/BEARISH
        if raw_direction in ("BUY", "BULLISH", "LONG"):
            direction = "BULLISH"
        elif raw_direction in ("SELL", "BEARISH", "SHORT"):
            direction = "BEARISH"
        else:
            logger.warning("SpreadBuilder: unrecognised direction %r", raw_direction)
            return None

        if underlying_price <= 0:
            logger.warning("SpreadBuilder: invalid underlying_price %s", underlying_price)
            return None

        strategy_code = (signal.get("strategy") or "").upper().strip()
        signal_id = signal.get("_signal_id", "")

        # ── 2. Select spread type ──────────────────────────────────────────
        spread_type = _select_spread_type(direction, strategy_code)

        # ── 3. Select expiry ───────────────────────────────────────────────
        from config import (
            ALLOW_SAME_DAY_EXPIRY_CREDIT_SPREADS,
            MIN_DAYS_TO_EXPIRY_CREDIT_SPREAD,
        )
        expiry = self._chain.select_expiry(
            symbol,
            spread_type,
            allow_same_day=ALLOW_SAME_DAY_EXPIRY_CREDIT_SPREADS,
            min_days_credit=MIN_DAYS_TO_EXPIRY_CREDIT_SPREAD,
        )
        if expiry is None:
            logger.warning(
                "SpreadBuilder: no suitable expiry for %s %s", symbol, spread_type
            )
            return None

        # ── 4. Compute strikes ─────────────────────────────────────────────
        from config import NIFTY_SPREAD_WIDTH, BANKNIFTY_SPREAD_WIDTH
        width_pts = NIFTY_SPREAD_WIDTH if symbol == "NIFTY" else BANKNIFTY_SPREAD_WIDTH
        width_steps = self._chain.width_to_steps(symbol, width_pts)

        atm = self._chain.atm_strike(underlying_price, symbol)
        long_strike, short_strike = _build_strikes(spread_type, atm, symbol, width_steps)
        option_type = _option_type_for_spread(spread_type)

        # ── 5. Resolve tokens ──────────────────────────────────────────────
        long_token_info = self._chain.resolve_option_token(symbol, expiry, long_strike, option_type)
        if long_token_info is None:
            logger.warning(
                "SpreadBuilder: could not resolve long leg token %s %s %d%s",
                symbol, expiry, long_strike, option_type,
            )
            return None

        short_token_info = self._chain.resolve_option_token(symbol, expiry, short_strike, option_type)
        if short_token_info is None:
            logger.warning(
                "SpreadBuilder: could not resolve short leg token %s %s %d%s",
                symbol, expiry, short_strike, option_type,
            )
            return None

        lot_size = long_token_info.get("lot_size")
        if not lot_size or lot_size <= 0:
            logger.warning("SpreadBuilder: invalid lot_size %r for %s", lot_size, symbol)
            return None

        # ── 6. Validate quotes ─────────────────────────────────────────────
        long_quote, long_err = self._chain.get_validated_quote(
            long_token_info["token"],
            side="BUY",
            min_sell_bid=self._min_sell_bid,
            max_spread_fraction=self._max_spread_fraction,
            min_ltp=self._min_ltp,
        )
        if long_quote is None:
            logger.warning(
                "SpreadBuilder: long leg quote invalid for %s %d%s — %s",
                symbol, long_strike, option_type, long_err,
            )
            return None

        short_quote, short_err = self._chain.get_validated_quote(
            short_token_info["token"],
            side="SELL",
            min_sell_bid=self._min_sell_bid,
            max_spread_fraction=self._max_spread_fraction,
            min_ltp=self._min_ltp,
        )
        if short_quote is None:
            logger.warning(
                "SpreadBuilder: short leg quote invalid for %s %d%s — %s",
                symbol, short_strike, option_type, short_err,
            )
            return None

        # ── 7. Fill price estimates ────────────────────────────────────────
        long_fill = fill_price_estimate(long_quote, "BUY")
        short_fill = fill_price_estimate(short_quote, "SELL")

        if long_fill <= 0 or short_fill <= 0:
            logger.warning(
                "SpreadBuilder: zero fill estimate — long=%s short=%s",
                long_fill, short_fill,
            )
            return None

        # ── 8. Economics ───────────────────────────────────────────────────
        net_dc, max_profit, max_loss, breakeven, rr = _compute_economics(
            spread_type, long_fill, short_fill, long_strike, short_strike, lot_size
        )

        # Clamp negatives from stale/illogical quotes
        max_loss = max(max_loss, 0.0)
        max_profit = max(max_profit, 0.0)

        # ── 9. Hard guardrails ─────────────────────────────────────────────
        if max_loss <= 0:
            logger.warning(
                "SpreadBuilder: max_loss=%.2f ≤ 0 — degenerate spread rejected", max_loss
            )
            return None

        total_risk = max_loss * lots
        if total_risk > self._max_risk:
            logger.warning(
                "SpreadBuilder: total_risk ₹%.2f exceeds budget ₹%.2f for %s",
                total_risk, self._max_risk, symbol,
            )
            return None

        if rr < self._min_rr:
            logger.warning(
                "SpreadBuilder: risk/reward %.2f below minimum %.2f for %s",
                rr, self._min_rr, symbol,
            )
            return None

        # ── 10. Build legs (long first — INVARIANT) ────────────────────────
        expiry_str = expiry.strftime("%d-%b-%Y").upper()

        long_leg = SpreadLeg(
            action="BUY",
            option_type=option_type,
            strike=long_strike,
            expiry=expiry,
            token=long_token_info["token"],
            tsym=long_token_info["tsym"],
            exchange="NFO",
            lot_size=lot_size,
            quote=long_quote,
            fill_price=long_fill,
        )

        short_leg = SpreadLeg(
            action="SELL",
            option_type=option_type,
            strike=short_strike,
            expiry=expiry,
            token=short_token_info["token"],
            tsym=short_token_info["tsym"],
            exchange="NFO",
            lot_size=lot_size,
            quote=short_quote,
            fill_price=short_fill,
        )

        candidate = SpreadCandidate(
            symbol=symbol,
            spread_type=spread_type,
            direction=direction,
            expiry=expiry,
            expiry_str=expiry_str,
            legs=[long_leg, short_leg],
            lot_size=lot_size,
            lots=lots,
            net_debit_or_credit=net_dc,
            max_profit=max_profit,
            max_loss=max_loss,
            breakeven=breakeven,
            risk_reward=rr,
            signal_id=signal_id,
            strategy=strategy_code,
            underlying_price=underlying_price,
        )

        logger.info(
            "SpreadBuilder: %s %s %s expiry=%s long=%d short=%d "
            "net=%.2f max_profit=%.2f max_loss=%.2f RR=%.2f",
            symbol, spread_type, direction, expiry_str,
            long_strike, short_strike,
            net_dc, max_profit, max_loss, rr,
        )
        return candidate
