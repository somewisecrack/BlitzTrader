"""
tools/options_chain.py — GammaBlast option token resolver for NIFTY and SENSEX.

Responsible for:
  - Resolving expiry dates from Shoonya search results.
  - Resolving CE/PE tokens for a given symbol, expiry, strike.
  - ATM strike arithmetic for NIFTY (50-pt step) and SENSEX (100-pt step).

Shoonya symbol format:
  NIFTY  (NFO):  NIFTY26JUN26C24500  → SYMBOL + DDMMMYY + C/P + STRIKE
  SENSEX (BFO):  SENSEX26JUN26C80000  → same format on BFO exchange

Exchange mappings:
  NIFTY  → NFO (NSE F&O)
  SENSEX → BFO (BSE F&O)

All methods return None / [] on failure — never raise.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Optional

import pytz

logger = logging.getLogger("GammaBlast.OptionsChain")

IST = pytz.timezone("Asia/Kolkata")

# Shoonya exchange for each underlying
_EXCHANGE = {
    "NIFTY":  "NFO",
    "SENSEX": "BFO",
}

# Index spot quote tokens (Shoonya)
_INDEX_TOKEN = {
    "NIFTY":  ("NSE", "26000"),
    "SENSEX": ("BSE", "1"),
}

_STRIKE_STEP = {
    "NIFTY":  50,
    "SENSEX": 100,
}

_EXPIRY_FORMAT = "%d-%b-%Y"   # "26-JUN-2026"


def exchange_for(symbol: str) -> str:
    return _EXCHANGE.get(symbol.upper(), "NFO")


def index_token_for(symbol: str) -> tuple[str, str]:
    """Return (exchange, token) for the index spot quote."""
    return _INDEX_TOKEN.get(symbol.upper(), ("NSE", "26000"))


def strike_step_for(symbol: str) -> int:
    return _STRIKE_STEP.get(symbol.upper(), 50)


def round_to_strike(price: float, symbol: str) -> int:
    step = strike_step_for(symbol)
    return int(round(price / step) * step)


def atm_ladder(atm_strike: int, symbol: str, offsets: list[int]) -> list[int]:
    """Return absolute strike values for the given ATM offsets."""
    step = strike_step_for(symbol)
    return [atm_strike + o * step for o in offsets]


class OptionsChain:
    """
    Resolves option tokens and expiry dates for NIFTY and SENSEX via Shoonya.
    """

    def __init__(self, shoonya_client):
        self._client = shoonya_client
        # Cache: (symbol, expiry_str, strike, option_type) → ResolvedScrip
        self._cache: dict[tuple, dict] = {}
        # Cache: symbol → expiry_str (nearest same-day expiry)
        self._expiry_cache: dict[str, str] = {}

    def get_index_ltp(self, symbol: str) -> Optional[float]:
        """Return current index spot LTP."""
        exch, token = index_token_for(symbol)
        return self._client.get_index_ltp(exch, token)

    def resolve_expiry(self, symbol: str, expiry_date: date) -> Optional[str]:
        """
        Return the Shoonya expiry string for the given symbol and expiry date,
        e.g. "26-JUN-2026". Validates by searching for at least one contract.
        """
        sym = symbol.upper()
        expiry_str = expiry_date.strftime("%-d-%b-%Y").upper()  # "9-JUN-2026"
        # Shoonya uses zero-padded day in some results; try both
        expiry_str_padded = expiry_date.strftime("%d-%b-%Y").upper()  # "09-JUN-2026"

        cache_key = f"{sym}_{expiry_date.isoformat()}"
        if cache_key in self._expiry_cache:
            return self._expiry_cache[cache_key]

        exch = exchange_for(sym)
        atm_approx = 24000 if sym == "NIFTY" else 80000
        search_text = f"{sym}{expiry_date.strftime('%d%b%y').upper()}C{atm_approx}"
        results = self._client.search_scrip(exch, search_text[:15])

        for r in results:
            exd = r.get("exd", "")
            if exd and (expiry_str in exd or expiry_str_padded in exd):
                self._expiry_cache[cache_key] = exd
                return exd

        # Fallback: use padded format without validation
        logger.warning("Could not confirm expiry for %s %s via Shoonya; using %s",
                       sym, expiry_date, expiry_str_padded)
        return expiry_str_padded

    def resolve_option(
        self,
        symbol: str,
        expiry_str: str,
        strike: int,
        option_type: str,   # "CE" or "PE"
    ) -> Optional[dict]:
        """
        Return a dict with {token, tsym, exchange} for the given option contract.
        Returns None if not found or quote invalid.
        """
        sym = symbol.upper()
        ot = option_type.upper()
        cache_key = (sym, expiry_str, strike, ot)
        if cache_key in self._cache:
            return self._cache[cache_key]

        exch = exchange_for(sym)
        # Build Shoonya tsym: NIFTY26JUN26C24500
        try:
            exp_dt = datetime.strptime(expiry_str, _EXPIRY_FORMAT)
        except ValueError:
            # Try zero-padded variant
            try:
                exp_dt = datetime.strptime(expiry_str, "%d-%b-%Y")
            except ValueError:
                logger.warning("Cannot parse expiry string: %r", expiry_str)
                return None

        exp_code = exp_dt.strftime("%d%b%y").upper()  # "26JUN26"
        cp = "C" if ot == "CE" else "P"
        tsym_guess = f"{sym}{exp_code}{cp}{strike}"

        results = self._client.search_scrip(exch, tsym_guess)
        for r in results:
            r_tsym = r.get("tsym", "")
            r_strike = r.get("strprc", "")
            r_optt = r.get("optt", "")
            r_exd = r.get("exd", "")

            if (
                _strike_matches(r_strike, strike)
                and _ot_matches(r_optt, ot)
                and expiry_str in (r_exd or "")
            ):
                result = {
                    "token": r.get("token", ""),
                    "tsym": r_tsym,
                    "exchange": exch,
                    "symbol": sym,
                    "expiry": expiry_str,
                    "strike": strike,
                    "option_type": ot,
                }
                self._cache[cache_key] = result
                return result

        # Fallback: try with guessed tsym and get token directly
        for r in results:
            if tsym_guess.replace(cp, "") in r.get("tsym", "").upper():
                result = {
                    "token": r.get("token", ""),
                    "tsym": r.get("tsym", tsym_guess),
                    "exchange": exch,
                    "symbol": sym,
                    "expiry": expiry_str,
                    "strike": strike,
                    "option_type": ot,
                }
                self._cache[cache_key] = result
                return result

        logger.debug("No token found for %s %s %s %s", sym, expiry_str, strike, ot)
        return None

    def get_quote(self, scrip: dict) -> Optional[dict]:
        """Fetch live quote for a resolved scrip dict."""
        token = scrip.get("token")
        exch = scrip.get("exchange")
        if not token or not exch:
            return None
        return self._client.get_quotes(exch, token)

    def clear_cache(self):
        self._cache.clear()
        self._expiry_cache.clear()


def _strike_matches(raw, expected: int) -> bool:
    try:
        return int(float(raw)) == int(expected)
    except (TypeError, ValueError):
        return False


def _ot_matches(raw: object, expected: str) -> bool:
    if raw is None:
        return False
    v = str(raw).upper().strip()
    code = "C" if expected == "CE" else "P"
    return v in {expected, code}


def parse_depth(quote: dict) -> dict:
    """
    Extract best-5 bid/ask depth from a Shoonya GetQuotes response.
    Returns {"bids": [...], "asks": [...]} each a list of {price, qty} dicts.
    """
    bids, asks = [], []
    for i in range(1, 6):
        try:
            bp = float(quote.get(f"bp{i}") or 0)
            bq = int(quote.get(f"bq{i}") or 0)
            if bp > 0:
                bids.append({"price": bp, "qty": bq})
        except (TypeError, ValueError):
            pass
        try:
            sp = float(quote.get(f"sp{i}") or 0)
            sq = int(quote.get(f"sq{i}") or 0)
            if sp > 0:
                asks.append({"price": sp, "qty": sq})
        except (TypeError, ValueError):
            pass
    return {"bids": bids, "asks": asks}


def bid_imbalance(depth: dict) -> float:
    """
    Compute best-5 bid imbalance: (bid_qty - ask_qty) / (bid_qty + ask_qty).
    Returns 0.0 if no depth data.
    """
    bq = sum(d["qty"] for d in depth.get("bids", []))
    aq = sum(d["qty"] for d in depth.get("asks", []))
    total = bq + aq
    return (bq - aq) / total if total > 0 else 0.0


def fill_price(quote: dict, side: str) -> float:
    """BUY → best ask (sp1); SELL → best bid (bp1). Falls back to ltp."""
    try:
        if side == "BUY":
            return float(quote.get("sp1") or quote.get("lp") or 0)
        return float(quote.get("bp1") or quote.get("lp") or 0)
    except (TypeError, ValueError):
        return 0.0
