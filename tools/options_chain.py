"""
tools/options_chain.py — NSE index option chain resolution for BlitzTrader.

Responsibilities:
  - Find available weekly/monthly expiries for NIFTY and BANKNIFTY via Shoonya.
  - Resolve option tokens (CE/PE) for a given underlying, expiry, and strike.
  - Validate live bid/ask quotes for each leg.
  - Provide ATM/OTM strike arithmetic helpers.
  - Strict filtering: only NIFTY and BANKNIFTY OPTIDX on NFO.

All methods return None / empty on failure — never raise.
All hard checks (naked short, bad token, stale quote) are enforced HERE.

Shoonya option symbol format (NSE F&O):
  NIFTY26MAY2624500CE  →  SYMBOL + DDMMMYY + STRIKE(int) + CE/PE
  e.g. NIFTY 26-MAY-2026 strike 24500 Call = NIFTY26MAY2624500CE
  Instname for index options: OPTIDX
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

logger = logging.getLogger("BlitzTrader.OptionsChain")

IST = pytz.timezone("Asia/Kolkata")

# Only these underlyings are allowed for live option trades
_ALLOWED_UNDERLYINGS = {"NIFTY", "BANKNIFTY"}

# Strike rounding increments (NSE standard)
_STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
}

# Shoonya expiry date string format in search results
_EXPIRY_FORMAT = "%d-%b-%Y"   # e.g. "26-MAY-2026"


def round_to_strike(price: float, symbol: str) -> int:
    """Round price to nearest valid NSE option strike for the given index."""
    step = _STRIKE_STEP.get(symbol.upper(), 50)
    return int(round(price / step) * step)


def strikes_range(atm: int, symbol: str, count: int = 10) -> list[int]:
    """Return a sorted list of valid strikes around ATM for given index."""
    step = _STRIKE_STEP.get(symbol.upper(), 50)
    return sorted([atm + i * step for i in range(-count, count + 1)])


# ──────────────────────────────────────────────────────────────────────────────
#   QUOTE VALIDATION
# ──────────────────────────────────────────────────────────────────────────────

def _is_quote_valid(
    quote: dict,
    side: str,                      # "BUY" or "SELL"
    min_sell_bid: float = 5.0,
    max_spread_fraction: float = 0.30,
    min_ltp: float = 2.0,
) -> tuple[bool, str]:
    """
    Validate a single option leg quote.

    Returns (True, "") if valid, (False, reason) otherwise.
    """
    if not quote:
        return False, "quote missing"

    ltp = quote.get("lp") or quote.get("ltp")
    bid = quote.get("bp1") or quote.get("bid")
    ask = quote.get("sp1") or quote.get("ask")

    # Convert to float safely
    try:
        ltp = float(ltp) if ltp else 0.0
    except (TypeError, ValueError):
        ltp = 0.0
    try:
        bid = float(bid) if bid else 0.0
    except (TypeError, ValueError):
        bid = 0.0
    try:
        ask = float(ask) if ask else 0.0
    except (TypeError, ValueError):
        ask = 0.0

    if ltp < min_ltp and bid < min_ltp:
        return False, f"LTP {ltp:.2f} and bid {bid:.2f} below min_ltp {min_ltp}"

    if side == "SELL":
        if bid < min_sell_bid:
            return False, f"sell-leg bid {bid:.2f} below min {min_sell_bid}"

    if bid > 0 and ask > 0 and (ask - bid) / max(ask, 0.01) > max_spread_fraction:
        return False, (
            f"bid-ask spread too wide: bid={bid:.2f} ask={ask:.2f} "
            f"({(ask - bid) / ask:.0%} > {max_spread_fraction:.0%} threshold)"
        )

    return True, ""


def fill_price_estimate(quote: dict, side: str) -> float:
    """
    Conservative fill-price estimate from a quote dict.

    Entry:  BUY → use ask;  SELL → use bid
    Exit:   BUY-to-close → use ask;  SELL-to-close → use bid
    """
    bid = quote.get("bp1") or quote.get("bid") or 0
    ask = quote.get("sp1") or quote.get("ask") or 0
    ltp = quote.get("lp") or quote.get("ltp") or 0
    try:
        bid = float(bid)
        ask = float(ask)
        ltp = float(ltp)
    except (TypeError, ValueError):
        return 0.0

    if side == "BUY":
        return ask if ask > 0 else ltp
    else:
        return bid if bid > 0 else ltp


# ──────────────────────────────────────────────────────────────────────────────
#   MAIN RESOLVER
# ──────────────────────────────────────────────────────────────────────────────

class OptionsChain:
    """
    Resolves NSE index option tokens and validates quotes via Shoonya.

    Usage:
        chain = OptionsChain(shoonya_client)
        expiries = chain.get_available_expiries("NIFTY")
        token_info = chain.resolve_option_token("NIFTY", "26-MAY-2026", 24500, "CE")
        quote = chain.get_option_quote(token_info["token"])
    """

    def __init__(self, shoonya_client):
        self._client = shoonya_client
        self._token_cache: dict[str, dict] = {}  # key: "NIFTY|26-MAY-2026|24500|CE"

    # ── Expiry discovery ──────────────────────────────────────────────────────

    def get_available_expiries(
        self,
        symbol: str,
        after_today: bool = True,
        exclude_today: bool = True,
    ) -> list[date]:
        """
        Return sorted list of expiry dates for index options of the given symbol.

        Filters to OPTIDX instname only. Excludes expired and (optionally) today.
        Returns [] if no results.
        """
        sym = symbol.upper()
        if sym not in _ALLOWED_UNDERLYINGS:
            logger.error("get_available_expiries: %s not in allowed underlyings", sym)
            return []

        results = self._client.search_scrip("NFO", sym) if self._client else None
        if not results:
            logger.warning("get_available_expiries(%s): no results from search_scrip", sym)
            return []

        today = date.today()
        expiry_set: set[date] = set()
        prefix_digit_re = re.compile(r"^" + re.escape(sym) + r"\d")

        for r in results:
            if r.get("instname") != "OPTIDX":
                continue
            tsym = r.get("tsym", "")
            if not prefix_digit_re.match(tsym):
                continue
            exd = r.get("exd", "")
            try:
                expiry = datetime.strptime(exd, _EXPIRY_FORMAT).date()
            except ValueError:
                continue
            if after_today and expiry < today:
                continue
            if exclude_today and expiry == today:
                continue
            expiry_set.add(expiry)

        return sorted(expiry_set)

    def select_expiry(
        self,
        symbol: str,
        spread_type: str,
        allow_same_day: bool = False,
        min_days_credit: int = 1,
    ) -> Optional[date]:
        """
        Select expiry for the spread type:
          - Credit spreads: nearest expiry (prefer theta decay), respect min_days_credit.
          - Debit spreads:  nearest liquid expiry giving room for intraday move.

        Returns None if no suitable expiry found.
        """
        is_credit = spread_type in ("BULL_PUT", "BEAR_CALL")
        exclude_today = not allow_same_day

        expiries = self.get_available_expiries(
            symbol,
            after_today=True,
            exclude_today=exclude_today,
        )
        if not expiries:
            logger.warning("select_expiry(%s, %s): no expiries available", symbol, spread_type)
            return None

        today = date.today()
        if is_credit:
            # Nearest expiry, but at least min_days_credit days away
            for exp in expiries:
                days = (exp - today).days
                if days >= min_days_credit:
                    return exp
            return None
        else:
            # Debit spread: nearest expiry (already excludes same-day by default)
            return expiries[0] if expiries else None

    # ── Token resolution ─────────────────────────────────────────────────────

    def resolve_option_token(
        self,
        symbol: str,
        expiry: date,
        strike: int,
        option_type: str,       # "CE" or "PE"
    ) -> Optional[dict]:
        """
        Resolve the Shoonya NFO token for an option contract.

        Returns dict with: token, tsym, exchange, lot_size, expiry_str
        Returns None on any failure.
        """
        sym = symbol.upper()
        ot = option_type.upper()
        if sym not in _ALLOWED_UNDERLYINGS:
            logger.error("resolve_option_token: %s not allowed", sym)
            return None
        if ot not in ("CE", "PE"):
            logger.error("resolve_option_token: option_type must be CE/PE, got %r", ot)
            return None

        expiry_str = expiry.strftime(_EXPIRY_FORMAT)   # "26-MAY-2026"
        cache_key = f"{sym}|{expiry_str}|{strike}|{ot}"
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        # Search by expiry prefix: e.g. "NIFTY26MAY26" for 26-MAY-2026
        exp_suffix = expiry.strftime("%d%b%y").upper()     # "26MAY26"
        search_text = f"{sym}{exp_suffix}"                  # "NIFTY26MAY26"
        results = self._client.search_scrip("NFO", search_text) if self._client else None
        if not results:
            logger.warning(
                "resolve_option_token: no results for %s %s %d%s",
                sym, expiry_str, strike, ot,
            )
            return None

        for r in results:
            if r.get("instname") != "OPTIDX":
                continue
            tsym = r.get("tsym", "")
            # tsym format: NIFTY26MAY2624500CE
            # Check it ends with STRIKE + OPTION_TYPE
            expected_suffix = f"{strike}{ot}"
            # Also verify symname/optiontype if present
            r_strike = r.get("strprc") or r.get("strike")
            r_otype  = r.get("optt") or r.get("option_type") or r.get("optiontype")

            # Primary: match tsym suffix directly
            if tsym.endswith(expected_suffix):
                # Verify expiry field too
                r_expiry = r.get("exd", "")
                try:
                    r_exp_date = datetime.strptime(r_expiry, _EXPIRY_FORMAT).date()
                except ValueError:
                    r_exp_date = None
                if r_exp_date and r_exp_date != expiry:
                    continue

                lot_size = None
                for key in ("ls", "lotsize", "lot_size"):
                    raw = r.get(key)
                    if raw is not None:
                        try:
                            lot_size = int(float(raw))
                            break
                        except (TypeError, ValueError):
                            pass

                info = {
                    "token": r["token"],
                    "tsym": tsym,
                    "exchange": "NFO",
                    "symbol": sym,
                    "strike": strike,
                    "option_type": ot,
                    "expiry": expiry,
                    "expiry_str": expiry_str,
                    "lot_size": lot_size,
                }
                self._token_cache[cache_key] = info
                logger.debug("Resolved option token: %s → token=%s", tsym, r["token"])
                return info

        logger.warning(
            "resolve_option_token: no matching OPTIDX found for %s %s %d%s",
            sym, expiry_str, strike, ot,
        )
        return None

    # ── Live quotes ──────────────────────────────────────────────────────────

    def get_option_quote(self, token: str, exchange: str = "NFO") -> Optional[dict]:
        """
        Fetch live quote for an option token via Shoonya REST.
        Returns the raw quote dict, or None on failure.
        """
        if not self._client:
            return None
        try:
            return self._client.get_quotes(exchange, token)
        except Exception:
            logger.exception("get_option_quote(%s) failed", token)
            return None

    def get_validated_quote(
        self,
        token: str,
        side: str,
        exchange: str = "NFO",
        min_sell_bid: float = 5.0,
        max_spread_fraction: float = 0.30,
        min_ltp: float = 2.0,
    ) -> tuple[Optional[dict], str]:
        """
        Fetch and validate a quote.
        Returns (quote, "") on success, (None, reason) on failure.
        """
        quote = self.get_option_quote(token, exchange)
        if not quote:
            return None, "quote fetch failed"
        ok, reason = _is_quote_valid(
            quote, side, min_sell_bid, max_spread_fraction, min_ltp
        )
        if not ok:
            return None, reason
        return quote, ""

    # ── ATM/OTM helpers ──────────────────────────────────────────────────────

    def atm_strike(self, underlying_price: float, symbol: str) -> int:
        """Return ATM strike (nearest valid strike to current price)."""
        return round_to_strike(underlying_price, symbol)

    def itm_strike(self, underlying_price: float, symbol: str, option_type: str, steps: int = 1) -> int:
        """
        Return a strike that is `steps` increments ITM relative to current price.
        ITM for CE = below price; ITM for PE = above price.
        """
        atm = self.atm_strike(underlying_price, symbol)
        step = _STRIKE_STEP.get(symbol.upper(), 50)
        ot = option_type.upper()
        if ot == "CE":
            return atm - steps * step
        else:
            return atm + steps * step

    def otm_strike(self, underlying_price: float, symbol: str, option_type: str, steps: int = 1) -> int:
        """
        Return a strike that is `steps` increments OTM relative to current price.
        OTM for CE = above price; OTM for PE = below price.
        """
        atm = self.atm_strike(underlying_price, symbol)
        step = _STRIKE_STEP.get(symbol.upper(), 50)
        ot = option_type.upper()
        if ot == "CE":
            return atm + steps * step
        else:
            return atm - steps * step

    def width_to_steps(self, symbol: str, width_points: int) -> int:
        """Convert a spread width in points to number of strike steps."""
        step = _STRIKE_STEP.get(symbol.upper(), 50)
        return max(1, round(width_points / step))

    # ── ATMOptionRecorder adapter methods ─────────────────────────────────────

    def get_nearest_expiry(self, symbol: str) -> Optional[str]:
        """Return the nearest upcoming expiry as 'DD-MMM-YYYY' string, or None.

        Used by ATMOptionRecorder.  Includes today's expiry so same-day options
        can be tracked on expiry day.
        """
        expiries = self.get_available_expiries(symbol, after_today=True, exclude_today=False)
        if not expiries:
            return None
        return expiries[0].strftime("%-d-%b-%Y").upper()

    def resolve_option(
        self,
        symbol: str,
        expiry: str,
        strike: int,
        option_type: str,
    ) -> Optional[dict]:
        """Resolve a single option contract by expiry string.

        expiry: 'DD-MMM-YYYY' string (e.g. '26-JUN-2026')
        Returns dict with at minimum {"token": ..., "tsym": ...}, or None.
        Used by ATMOptionRecorder.
        """
        try:
            expiry_date = datetime.strptime(expiry, _EXPIRY_FORMAT).date()
        except ValueError:
            logger.error("resolve_option: bad expiry string %r", expiry)
            return None
        return self.resolve_option_token(symbol, expiry_date, strike, option_type)
