"""
tools/sensex_atm_recorder.py — Intraday SENSEX ATM-ladder logger.

Records CE and PE data for SENSEX ATM, ATM-1 and ATM+1 throughout the
trading day.  Data is saved to:
    data_exports/YYYYMMDD/atm_options/SENSEX/

*** RECORDING ONLY — SENSEX IS NEVER TRADED ***

This module must never be imported from any trading, order-execution,
SpreadBuilder, Gemini-gatekeeper, or portfolio-management code path.
Only main.py may instantiate SensexATMOptionRecorder for data collection.

Key behaviours:
- ATM strike is detected from the SENSEX spot index LTP supplied by main.py.
- Exchange is BFO (BSE Futures & Options) — verified at runtime against Shoonya.
- Strike step is discovered dynamically from live Shoonya BFO contract data.
  A fallback of 100 points is used ONLY if Shoonya returns zero contracts,
  and a warning is emitted so the operator knows to investigate.
- Every ATM transition adds ATM-1, ATM and ATM+1 for both CE and PE.
- Previously activated strikes continue logging after ATM changes.
- Sampling rate: once per minute per tracked contract.
- Data source: Shoonya REST quotes via BFO exchange — no yfinance, no fallback
  invented data.
- Missing fields result in null values, never guessed values.
- Option LTP and underlying LTP have distinct code paths and fields.
  Underlying LTP is NEVER substituted for option LTP.

Shoonya SENSEX option tsym formats on BFO:
  Weekly:  SENSEX + YY + M (1-9, O, N, D) + DD + strike + CE/PE
           e.g. SENSEX2661174000CE  = SENSEX 11-JUN-2026 74000 CE
  Monthly: SENSEX + YY + MMM + strike + CE/PE
           e.g. SENSEX26JUN74000CE  = SENSEX JUN-2026    74000 CE

  symname for option search: BSXOPT
  exchange: BFO

Record schema (JSONL) — identical to NIFTY:
  timestamp_ist, symbol, expiry, exchange, strike, option_type, token, tsym,
  source, underlying_ltp, atm_strike_at_time, first_seen_at,
  first_seen_role, strike_role_at_time, strike_offset_steps_at_time,
  is_current_atm_at_time,
  open, high, low, close, ltp, volume, oi, oi_change, previous_oi,
  best_5_bids, best_5_asks, source_error
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz

logger = logging.getLogger("BlitzTrader.SensexATMOptionRecorder")

IST = pytz.timezone("Asia/Kolkata")

_SYMBOL = "SENSEX"
_EXCHANGE = "BFO"           # BSE Futures & Options
_INSTNAME = "OPTIDX"        # Shoonya instrument class for index options

# Minimum seconds between samples for each tracked contract
_SAMPLE_INTERVAL_SECONDS = 60

# Fallback strike step only if dynamic discovery returns nothing.
# Emits a warning log so the operator knows to investigate.
_FALLBACK_STRIKE_STEP = 100

# Format used in Shoonya expiry strings returned by SearchScrip: "11-JUN-2026"
_EXPIRY_FORMAT = "%d-%b-%Y"

# Shoonya option tsym regexes for SENSEX on BFO.
# Weekly:  SENSEX + YY + M(1-9/O/N/D) + DD + strike + CE/PE
# Monthly: SENSEX + YY + MMM + strike + CE/PE
_SENSEX_WEEKLY_RE  = re.compile(r"^SENSEX(\d{2})([0-9OND])(\d{2})(\d+)(CE|PE)$")
_SENSEX_MONTHLY_RE = re.compile(r"^SENSEX(\d{2})([A-Z]{3})(\d+)(CE|PE)$")

# Shoonya symname for SENSEX option contracts — use as search term
_BSXOPT_SYMNAME = "BSXOPT"

# Single-digit month code used in weekly SENSEX option tsym
_MONTH_WEEKLY_CODE = {
    1: "1", 2: "2", 3: "3", 4: "4", 5: "5",
    6: "6", 7: "7", 8: "8", 9: "9",
    10: "O", 11: "N", 12: "D",
}


# ──────────────────────────────────────────────────────────────────────────────
#   SAFETY NOTE
# ──────────────────────────────────────────────────────────────────────────────
# SensexATMOptionRecorder is for DATA COLLECTION ONLY.
# The trading-isolation guarantee is enforced by:
#   1. options_chain._ALLOWED_UNDERLYINGS does NOT include SENSEX.
#   2. SpreadExecutionEngine._check_guardrails rejects any symbol not in
#      ("NIFTY", "BANKNIFTY").
#   3. This module never returns SpreadCandidate, OpenSpread, or any type
#      consumed by order execution or Gemini gatekeeper.
# Tests in test_sensex_atm_recorder.py::TestSensexTradingIsolation verify these.


# ──────────────────────────────────────────────────────────────────────────────
#   HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _role_for_offset(offset_steps: int) -> str:
    return {-1: "ATM-1", 0: "ATM", 1: "ATM+1"}.get(offset_steps, "TRACKED_PREVIOUS")


def round_to_sensex_strike(price: float, step: int) -> int:
    """Round spot price to nearest valid SENSEX strike."""
    if step <= 0:
        step = _FALLBACK_STRIKE_STEP
    return int(((float(price) + step / 2) // step) * step)


def _build_sensex_tsym(expiry_date: date, strike: int, option_type: str) -> list[str]:
    """
    Build candidate Shoonya tsyms for a SENSEX option.

    Returns a list of possible tsyms to try (weekly format first, then monthly),
    because we cannot always know which format Shoonya used at instrument creation.
    """
    ot = option_type.upper()   # "CE" or "PE"
    yy = str(expiry_date.year % 100).zfill(2)
    m_code = _MONTH_WEEKLY_CODE[expiry_date.month]
    dd = str(expiry_date.day).zfill(2)
    mmm = expiry_date.strftime("%b").upper()   # "JUN"

    weekly  = f"SENSEX{yy}{m_code}{dd}{strike}{ot}"
    monthly = f"SENSEX{yy}{mmm}{strike}{ot}"
    return [weekly, monthly]


def _parse_strike_from_sensex_tsym(tsym: str) -> Optional[int]:
    """Extract strike price from a SENSEX BFO tsym."""
    m = _SENSEX_WEEKLY_RE.match(tsym)
    if m:
        try:
            return int(m.group(4))
        except (ValueError, IndexError):
            return None
    m = _SENSEX_MONTHLY_RE.match(tsym)
    if m:
        try:
            return int(m.group(3))
        except (ValueError, IndexError):
            return None
    return None


def _is_sensex_optidx_tsym(tsym: str) -> bool:
    return bool(_SENSEX_WEEKLY_RE.match(tsym) or _SENSEX_MONTHLY_RE.match(tsym))


# ──────────────────────────────────────────────────────────────────────────────
#   DATA MODEL
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _TrackedContract:
    """One tracked SENSEX option contract."""
    symbol: str           # always "SENSEX"
    exchange: str         # always "BFO"
    expiry: str           # e.g. "11-JUN-2026"
    strike: int
    option_type: str      # "CE" or "PE"
    token: str
    tsym: str
    first_seen_at: str    # IST ISO datetime
    first_seen_role: str
    last_sampled_at: float = 0.0   # POSIX epoch of last sample


# ──────────────────────────────────────────────────────────────────────────────
#   BFO CONTRACT RESOLVER
# ──────────────────────────────────────────────────────────────────────────────

class SensexOptionChain:
    """
    Read-only resolver for SENSEX options on BFO.

    Completely separate from OptionsChain (which is for NIFTY/BANKNIFTY on NFO).
    Never used by trading code.

    Shoonya SENSEX option tsym formats (verified from live BFO data):
      Weekly:  SENSEX{YY}{M}{DD}{strike}{CE/PE}  e.g. SENSEX2661174000CE
      Monthly: SENSEX{YY}{MMM}{strike}{CE/PE}    e.g. SENSEX26JUN74000CE
    """

    def __init__(self, shoonya_client) -> None:
        self._client = shoonya_client
        self._expiry_cache: dict[str, list[date]] = {}
        self._token_cache: dict[str, dict] = {}
        self._strike_step: Optional[int] = None

    def discover_strike_step(self) -> int:
        """
        Discover the SENSEX strike interval by examining live BFO contracts.

        Searches BFO for 'BSXOPT' (the Shoonya symname for SENSEX options),
        extracts strike prices from tsyms, and computes the GCD of gaps.
        Falls back to _FALLBACK_STRIKE_STEP with a warning if nothing found.
        """
        if self._strike_step is not None:
            return self._strike_step

        if not self._client:
            logger.warning(
                "SensexOptionChain.discover_strike_step: no client — using fallback %d",
                _FALLBACK_STRIKE_STEP,
            )
            self._strike_step = _FALLBACK_STRIKE_STEP
            return self._strike_step

        # Search using the Shoonya symname for SENSEX options
        results = self._client.search_scrip(_EXCHANGE, _BSXOPT_SYMNAME) or []
        strikes: list[int] = []
        for r in results:
            if r.get("instname") != _INSTNAME:
                continue
            tsym = str(r.get("tsym", "")).upper()
            strike = _parse_strike_from_sensex_tsym(tsym)
            if strike and strike > 0:
                strikes.append(strike)

        if len(strikes) >= 2:
            strikes_sorted = sorted(set(strikes))
            gaps = [
                strikes_sorted[i + 1] - strikes_sorted[i]
                for i in range(len(strikes_sorted) - 1)
                if strikes_sorted[i + 1] > strikes_sorted[i]
            ]
            if gaps:
                step = gaps[0]
                for g in gaps[1:]:
                    step = math.gcd(step, g)
                if step > 0:
                    logger.info(
                        "SensexOptionChain: discovered SENSEX strike step = %d "
                        "from %d contracts",
                        step, len(strikes),
                    )
                    self._strike_step = step
                    return self._strike_step

        logger.warning(
            "SensexOptionChain: could not determine strike step from %d results; "
            "using fallback %d — verify BFO BSXOPT contract data is available",
            len(results), _FALLBACK_STRIKE_STEP,
        )
        self._strike_step = _FALLBACK_STRIKE_STEP
        return self._strike_step

    def get_nearest_expiry(self) -> Optional[str]:
        """
        Return the nearest upcoming SENSEX BFO weekly/monthly expiry as
        'D-MMM-YYYY' string.  Includes today so same-day options are tracked
        on expiry day.
        """
        expiries = self._get_available_expiries(after_today=True, exclude_today=False)
        if not expiries:
            return None
        return expiries[0].strftime("%-d-%b-%Y").upper()

    def _get_available_expiries(
        self, after_today: bool = True, exclude_today: bool = False
    ) -> list[date]:
        """Fetch SENSEX expiries from BFO via Shoonya SearchScrip for BSXOPT."""
        today = date.today()
        cache_key = f"SENSEX|{today}"
        if cache_key in self._expiry_cache:
            cached = self._expiry_cache[cache_key]
            return [
                e for e in cached
                if (not after_today or e >= today)
                and (not exclude_today or e != today)
            ]

        if not self._client:
            logger.warning("SensexOptionChain: no client — cannot resolve expiries")
            return []

        expiry_set: set[date] = set()

        # Pass 1: generic BSXOPT symname search
        results = self._client.search_scrip(_EXCHANGE, _BSXOPT_SYMNAME) or []
        self._parse_expiries_from_results(results, today, after_today, exclude_today, expiry_set)

        # Pass 2: date-specific fallback using SENSEX weekly tsym prefixes
        # Needed when BSXOPT generic search only returns current-expiry contracts.
        if not expiry_set or len(expiry_set) < 2:
            logger.info(
                "SensexOptionChain: BSXOPT generic search found %d expiries — "
                "trying date-specific fallback",
                len(expiry_set),
            )
            for days_ahead in range(1, 57):
                candidate = today + timedelta(days=days_ahead)
                # Build weekly tsym prefix for this candidate date
                yy = str(candidate.year % 100).zfill(2)
                m_code = _MONTH_WEEKLY_CODE[candidate.month]
                dd = str(candidate.day).zfill(2)
                weekly_prefix = f"SENSEX{yy}{m_code}{dd}"
                rows = self._client.search_scrip(_EXCHANGE, weekly_prefix) or []
                self._parse_expiries_from_results(
                    rows, today, after_today, exclude_today, expiry_set
                )
                # Also try monthly format prefix for the month
                if candidate.day == 1:   # only probe month prefix once per month
                    mmm = candidate.strftime("%b").upper()
                    monthly_prefix = f"SENSEX{yy}{mmm}"
                    rows2 = self._client.search_scrip(_EXCHANGE, monthly_prefix) or []
                    self._parse_expiries_from_results(
                        rows2, today, after_today, exclude_today, expiry_set
                    )
                if len(expiry_set) >= 2:
                    break

        if expiry_set:
            logger.info(
                "SensexOptionChain: found SENSEX BFO expiries: %s",
                sorted(expiry_set),
            )
        else:
            logger.warning("SensexOptionChain: no SENSEX BFO expiries found")

        result = sorted(expiry_set)
        self._expiry_cache[cache_key] = result
        return [
            e for e in result
            if (not after_today or e >= today)
            and (not exclude_today or e != today)
        ]

    @staticmethod
    def _parse_expiries_from_results(
        results: list, today: date, after_today: bool,
        exclude_today: bool, expiry_set: set,
    ) -> None:
        for r in results:
            if r.get("instname") != _INSTNAME:
                continue
            tsym = str(r.get("tsym", "")).upper()
            if not _is_sensex_optidx_tsym(tsym):
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

    def resolve_option(
        self,
        expiry: str,
        strike: int,
        option_type: str,   # "CE" or "PE"
    ) -> Optional[dict]:
        """
        Resolve a SENSEX BFO option contract.

        Tries both weekly and monthly tsym formats as Shoonya may use either.
        Returns dict with {token, tsym, exchange, symbol, strike, option_type,
        expiry, expiry_str} or None on failure.
        """
        ot = option_type.upper()
        if ot not in ("CE", "PE"):
            logger.error("resolve_option: option_type must be CE/PE, got %r", ot)
            return None

        try:
            expiry_date = datetime.strptime(expiry, _EXPIRY_FORMAT).date()
        except ValueError:
            logger.error("resolve_option: bad expiry string %r", expiry)
            return None

        cache_key = f"SENSEX|{expiry}|{strike}|{ot}"
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        if not self._client:
            return None

        # Build candidate tsyms and try each
        candidate_tsyms = _build_sensex_tsym(expiry_date, strike, ot)
        for expected_tsym in candidate_tsyms:
            results = self._client.search_scrip(_EXCHANGE, expected_tsym) or []
            for r in results:
                if not self._row_matches(r, expiry_date, strike, ot, expected_tsym):
                    continue

                tsym = r.get("tsym", "")
                token = r.get("token", "")
                if not tsym or not token:
                    continue

                info = {
                    "token": token,
                    "tsym": tsym,
                    "exchange": _EXCHANGE,
                    "symbol": _SYMBOL,
                    "strike": strike,
                    "option_type": ot,
                    "expiry": expiry_date,
                    "expiry_str": expiry,
                }
                self._token_cache[cache_key] = info
                logger.info(
                    "SensexOptionChain: resolved %s strike=%d %s expiry=%s "
                    "token=%s exchange=%s",
                    tsym, strike, ot, expiry, token, _EXCHANGE,
                )
                return info

        logger.warning(
            "SensexOptionChain: no matching OPTIDX for SENSEX %s %s %s; "
            "tried_tsyms=%s",
            expiry, strike, ot, candidate_tsyms,
        )
        return None

    @staticmethod
    def _row_matches(
        row: dict, expiry: date, strike: int, option_type: str, expected_tsym: str
    ) -> bool:
        if row.get("instname") != _INSTNAME:
            return False

        tsym = str(row.get("tsym", "")).upper()
        if not _is_sensex_optidx_tsym(tsym):
            return False

        # Expiry check via exd field
        r_expiry = row.get("exd", "")
        if r_expiry:
            try:
                if datetime.strptime(str(r_expiry), _EXPIRY_FORMAT).date() != expiry:
                    return False
            except ValueError:
                return False

        # Option type check
        r_otype = row.get("optt", "")
        if r_otype and r_otype.upper() != option_type.upper():
            return False

        # Strike check: parse from tsym since strprc is absent in Shoonya response
        parsed_strike = _parse_strike_from_sensex_tsym(tsym)
        if parsed_strike is not None and parsed_strike != strike:
            return False

        # Exact tsym match as final fallback
        if tsym == expected_tsym:
            return True

        # If strike and expiry both match, accept
        if parsed_strike == strike and r_expiry:
            return True

        return False


# ──────────────────────────────────────────────────────────────────────────────
#   SENSEX ATM OPTION RECORDER
# ──────────────────────────────────────────────────────────────────────────────

class SensexATMOptionRecorder:
    """
    Tracks the cumulative SENSEX ATM/ATM±1 option ladder and records
    intraday OHLCV/OI/depth snapshots to local JSONL files.

    *** RECORDING ONLY — never produces trading signals ***

    Usage (from main.py only):
        recorder = SensexATMOptionRecorder(
            base_dir=Path("data_exports"),
            shoonya_client=client,
        )
        # On each scan tick:
        recorder.update_atm(underlying_ltp=74000.0)
        recorder.sample_due_contracts()
        # At EOD:
        recorder.flush()
    """

    def __init__(
        self,
        base_dir: Path,
        shoonya_client,
        sample_interval: int = _SAMPLE_INTERVAL_SECONDS,
    ):
        self._base_dir = Path(base_dir)
        self._client = shoonya_client
        self._chain = SensexOptionChain(shoonya_client)
        self._sample_interval = sample_interval
        self._lock = threading.Lock()

        self._tracked: dict[tuple, _TrackedContract] = {}
        self._current_atm: Optional[int] = None
        self._latest_underlying_ltp: Optional[float] = None
        self._strike_step: Optional[int] = None
        self._expiry: Optional[str] = None

        self._date_str = datetime.now(IST).strftime("%Y%m%d")

    # ── Public API ────────────────────────────────────────────────────────────

    def initialise(self) -> None:
        """
        Discover strike step and expiry at session start.
        Call once before the first update_atm().
        """
        self._strike_step = self._chain.discover_strike_step()
        self._expiry = self._chain.get_nearest_expiry()
        if self._expiry:
            logger.info(
                "SensexATMOptionRecorder: initialised — "
                "exchange=%s strike_step=%d expiry=%s",
                _EXCHANGE, self._strike_step, self._expiry,
            )
        else:
            logger.warning(
                "SensexATMOptionRecorder: could not resolve SENSEX expiry from BFO; "
                "recording will be skipped until expiry is available"
            )

    def update_atm(self, underlying_ltp: float) -> None:
        """
        Detect SENSEX ATM from spot LTP and ensure ATM-1/ATM/ATM+1 CE and PE
        contracts are tracked. Old contracts are never removed.
        """
        if underlying_ltp is None or underlying_ltp <= 0:
            return
        if not self._expiry:
            self._expiry = self._chain.get_nearest_expiry()
            if not self._expiry:
                return
        if not self._strike_step:
            self._strike_step = self._chain.discover_strike_step()

        new_atm = round_to_sensex_strike(underlying_ltp, self._strike_step)
        old_atm = self._current_atm

        with self._lock:
            self._current_atm = new_atm
            self._latest_underlying_ltp = float(underlying_ltp)

        if new_atm != old_atm:
            logger.info(
                "SensexATMOptionRecorder: ATM changed %s → %d (spot ₹%.2f); "
                "window=[%d, %d, %d]",
                old_atm, new_atm, underlying_ltp,
                new_atm - self._strike_step,
                new_atm,
                new_atm + self._strike_step,
            )
            self._ensure_active_window(new_atm)

    def sample_due_contracts(self) -> None:
        """Sample all contracts due for their next snapshot."""
        now = time.time()
        with self._lock:
            due = [
                c for c in self._tracked.values()
                if now - c.last_sampled_at >= self._sample_interval
            ]
        for contract in due:
            self._sample_one(contract)

    def flush(self) -> None:
        """Force-sample all tracked contracts (called at EOD)."""
        with self._lock:
            all_contracts = list(self._tracked.values())
        for contract in all_contracts:
            self._sample_one(contract, force=True)
        logger.info(
            "SensexATMOptionRecorder: EOD flush — %d contracts sampled",
            len(all_contracts),
        )

    @property
    def export_dir(self) -> Path:
        return self._base_dir / self._date_str / "atm_options" / _SYMBOL

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_active_window(self, atm_strike: int) -> None:
        """Resolve and retain CE/PE contracts for ATM-1, ATM and ATM+1."""
        if not self._expiry or not self._strike_step:
            return

        for offset in (-1, 0, 1):
            strike = atm_strike + offset * self._strike_step
            first_seen_role = _role_for_offset(offset)
            for opt_type in ("CE", "PE"):
                key = (_SYMBOL, strike, opt_type)
                with self._lock:
                    if key in self._tracked:
                        continue

                result = self._chain.resolve_option(
                    expiry=self._expiry,
                    strike=strike,
                    option_type=opt_type,
                )
                if not result:
                    logger.warning(
                        "SensexATMOptionRecorder: could not resolve %s %s %s %s (%s)",
                        _SYMBOL, self._expiry, strike, opt_type, first_seen_role,
                    )
                    continue

                token = result.get("token", "")
                tsym = result.get("tsym", "")
                if not token or not tsym:
                    logger.warning(
                        "SensexATMOptionRecorder: missing token/tsym for %s %s %s",
                        _SYMBOL, strike, opt_type,
                    )
                    continue

                contract = _TrackedContract(
                    symbol=_SYMBOL,
                    exchange=_EXCHANGE,
                    expiry=self._expiry,
                    strike=strike,
                    option_type=opt_type,
                    token=token,
                    tsym=tsym,
                    first_seen_at=datetime.now(IST).isoformat(),
                    first_seen_role=first_seen_role,
                )
                with self._lock:
                    self._tracked[key] = contract

                logger.info(
                    "SensexATMOptionRecorder: tracking %s %s %s %s "
                    "as %s exchange=%s token=%s",
                    _SYMBOL, self._expiry, strike, opt_type,
                    first_seen_role, _EXCHANGE, token,
                )

    def _sample_one(self, contract: _TrackedContract, force: bool = False) -> None:
        now = time.time()
        if not force and now - contract.last_sampled_at < self._sample_interval:
            return
        record = self._build_record(contract)
        self._write_record(contract, record)
        with self._lock:
            contract.last_sampled_at = now

    def _build_record(self, contract: _TrackedContract) -> dict:
        """Fetch quote from Shoonya BFO and build the record dict.

        Underlying LTP and option LTP are distinct fields with distinct code
        paths.  Underlying LTP is NEVER written to the option ltp field.
        """
        now_ist = datetime.now(IST)
        timestamp_ist = now_ist.isoformat()

        with self._lock:
            current_atm = self._current_atm
            underlying_ltp: Optional[float] = self._latest_underlying_ltp

        is_current_atm = (contract.strike == current_atm) if current_atm else False
        step = self._strike_step or _FALLBACK_STRIKE_STEP
        offset_steps = (
            int((contract.strike - current_atm) / step)
            if current_atm else None
        )
        strike_role = (
            _role_for_offset(offset_steps)
            if offset_steps is not None else "TRACKED_PREVIOUS"
        )

        base: dict = {
            "timestamp_ist": timestamp_ist,
            "symbol": contract.symbol,
            "exchange": contract.exchange,
            "expiry": contract.expiry,
            "strike": contract.strike,
            "option_type": contract.option_type,
            "token": contract.token,
            "tsym": contract.tsym,
            "source": "shoonya_bfo",
            "underlying_ltp": underlying_ltp,          # SENSEX spot — NOT option price
            "atm_strike_at_time": current_atm,
            "first_seen_at": contract.first_seen_at,
            "first_seen_role": contract.first_seen_role,
            "strike_role_at_time": strike_role,
            "strike_offset_steps_at_time": offset_steps,
            "is_current_atm_at_time": is_current_atm,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "ltp": None,            # option LTP — populated only from option quote
            "volume": None,
            "oi": None,
            "oi_change": None,
            "previous_oi": None,
            "best_5_bids": None,
            "best_5_asks": None,
            "source_error": None,
        }

        if not self._client:
            base["source_error"] = "no_shoonya_client"
            return base

        # Fetch quote using BFO exchange and the option's own token.
        # This is the ONLY place option LTP is populated — from the option's
        # own Shoonya quote, never from underlying_ltp.
        try:
            resp = self._client.get_quotes(_EXCHANGE, contract.token)
        except Exception as exc:
            base["source_error"] = f"get_quotes_exception: {exc}"
            return base

        if not resp:
            base["source_error"] = "get_quotes_returned_none"
            return base
        if str(resp.get("stat", "Ok")).lower() not in {"ok", ""}:
            base["source_error"] = (
                f"get_quotes_not_ok: {resp.get('emsg', 'unknown error')}"
            )
            return base

        # Validate that the response token matches the contract.
        # Prevents index-price contamination if the wrong instrument is returned.
        resp_token = str(resp.get("token", resp.get("tok", ""))).strip()
        resp_exch = str(resp.get("exch", resp.get("exchange", ""))).strip().upper()
        if resp_token and resp_token != str(contract.token):
            base["source_error"] = (
                f"token_mismatch: expected={contract.token} got={resp_token}"
            )
            return base
        if resp_exch and resp_exch not in (_EXCHANGE, ""):
            base["source_error"] = (
                f"exchange_mismatch: expected={_EXCHANGE} got={resp_exch}"
            )
            return base

        def _f(key: str) -> Optional[float]:
            v = resp.get(key)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def _i(key: str) -> Optional[int]:
            v = resp.get(key)
            if v is None or v == "":
                return None
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        # option_ltp is the LTP from the option contract quote — distinct from
        # underlying_ltp which holds the SENSEX spot price.
        option_ltp = _f("lp") or _f("c")

        # Sanity guard: SENSEX spot is ~74000+. A valid SENSEX option price
        # should be well under 10,000. Values above 15,000 almost certainly
        # indicate an index value leaking into the option quote path.
        if option_ltp is not None and option_ltp > 15_000:
            logger.warning(
                "SensexATMOptionRecorder: suspicious option LTP %.2f for %s (token=%s) "
                "— value exceeds plausible option price; recording as source_error",
                option_ltp, contract.tsym, contract.token,
            )
            base["source_error"] = (
                f"implausible_option_ltp: {option_ltp} (may be index value leaking)"
            )
            return base

        base["ltp"] = option_ltp       # option price only
        base["open"] = _f("o")
        base["high"] = _f("h")
        base["low"] = _f("l")
        base["close"] = _f("c")
        base["volume"] = _i("v") or _i("volume")
        base["oi"] = _i("oi")
        base["previous_oi"] = _i("poi")
        if base["oi"] is not None and base["previous_oi"] is not None:
            base["oi_change"] = base["oi"] - base["previous_oi"]

        bids = []
        for i in range(1, 6):
            price = _f(f"bp{i}")
            qty = _i(f"bq{i}")
            if price is not None and qty is not None and price > 0 and qty > 0:
                entry: dict = {"price": price, "qty": qty}
                orders = _i(f"bo{i}")
                if orders is not None:
                    entry["orders"] = orders
                bids.append(entry)
        base["best_5_bids"] = bids if bids else None

        asks = []
        for i in range(1, 6):
            price = _f(f"sp{i}")
            qty = _i(f"sq{i}")
            if price is not None and qty is not None and price > 0 and qty > 0:
                entry = {"price": price, "qty": qty}
                orders = _i(f"so{i}")
                if orders is not None:
                    entry["orders"] = orders
                asks.append(entry)
        base["best_5_asks"] = asks if asks else None

        return base

    def _write_record(self, contract: _TrackedContract, record: dict) -> None:
        """Append OHLCV, depth and strikes-log JSONL records."""
        try:
            sym_dir = self.export_dir
            sym_dir.mkdir(parents=True, exist_ok=True)

            ohlcv_path = sym_dir / f"{contract.tsym}_ohlcv.jsonl"
            ohlcv_keys = (
                "timestamp_ist", "symbol", "exchange", "expiry", "strike",
                "option_type", "token", "tsym", "source", "underlying_ltp",
                "atm_strike_at_time", "first_seen_at", "first_seen_role",
                "strike_role_at_time", "strike_offset_steps_at_time",
                "is_current_atm_at_time",
                "open", "high", "low", "close", "ltp", "volume",
                "oi", "oi_change", "previous_oi", "source_error",
            )
            ohlcv_row = {k: record[k] for k in ohlcv_keys}
            with open(ohlcv_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ohlcv_row, default=str) + "\n")

            depth_path = sym_dir / f"{contract.tsym}_depth.jsonl"
            depth_keys = (
                "timestamp_ist", "symbol", "exchange", "expiry", "strike",
                "option_type", "token", "tsym", "source",
                "atm_strike_at_time", "first_seen_role",
                "strike_role_at_time", "strike_offset_steps_at_time",
                "is_current_atm_at_time",
                "ltp", "best_5_bids", "best_5_asks", "source_error",
            )
            depth_row = {k: record[k] for k in depth_keys}
            with open(depth_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(depth_row, default=str) + "\n")

            self._update_strikes_log(contract)

        except Exception:
            logger.exception(
                "SensexATMOptionRecorder: failed to write record for %s %s",
                contract.symbol, contract.tsym,
            )

    def _update_strikes_log(self, contract: _TrackedContract) -> None:
        """Append new contract to ATM strikes metadata log (once per contract)."""
        try:
            strikes_path = self.export_dir / f"{_SYMBOL}_ATM_STRIKES.jsonl"
            entry = {
                "symbol": contract.symbol,
                "exchange": contract.exchange,
                "strike": contract.strike,
                "expiry": contract.expiry,
                "option_type": contract.option_type,
                "token": contract.token,
                "tsym": contract.tsym,
                "first_seen_at": contract.first_seen_at,
                "first_seen_role": contract.first_seen_role,
            }
            if strikes_path.exists():
                existing = strikes_path.read_text(encoding="utf-8")
                if f'"tsym": "{contract.tsym}"' in existing:
                    return
            with open(strikes_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            logger.exception(
                "SensexATMOptionRecorder: strikes log update failed (non-fatal)"
            )
