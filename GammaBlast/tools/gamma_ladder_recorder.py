"""
tools/gamma_ladder_recorder.py — GammaBlast ATM±2 option ladder recorder.

Records CE and PE quotes for ATM-2 through ATM+2 strikes on expiry day
(NIFTY Tuesdays, SENSEX Thursdays). Once a strike is activated it is tracked
for the entire session. Samples each tracked contract once per minute
(configurable) and writes one JSONL record per sample.

Output path:
    <base_dir>/data_exports/YYYYMMDD/gamma_ladder/<SYMBOL>/<tsym>.jsonl

JSONL record schema: see module docstring and GammaLadderRecorder.

This module does NOT import anything from BlitzTrader.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

from tools.options_chain import round_to_strike, atm_ladder, parse_depth

logger = logging.getLogger("GammaBlast.GammaLadderRecorder")

IST = pytz.timezone("Asia/Kolkata")

# Mapping from signed integer offset to role label
_ROLE_MAP: dict[int, str] = {
    -2: "ATM-2",
    -1: "ATM-1",
     0: "ATM",
     1: "ATM+1",
     2: "ATM+2",
}


def _get_role(offset_steps: int) -> str:
    """Return human-readable role string for an ATM offset."""
    return _ROLE_MAP.get(offset_steps, "TRACKED_PREVIOUS")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _iso_ist(dt: datetime) -> str:
    """Format a datetime as an ISO-8601 string without timezone suffix (IST implied)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _safe_float(value) -> Optional[float]:
    """Parse a float from a Shoonya quote field; return None on failure."""
    if value is None:
        return None
    try:
        f = float(value)
        return f if f != 0.0 else None
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> Optional[int]:
    """Parse an int from a Shoonya quote field; return None on failure."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class _TrackedContract:
    """Internal state for a single tracked CE or PE contract."""

    __slots__ = (
        "symbol",
        "expiry",
        "strike",
        "option_type",
        "token",
        "tsym",
        "source",
        "first_seen_at",
        "first_seen_role",
        "last_sampled_at",
        "previous_oi",
        "scrip",          # resolved scrip dict from OptionsChain
        "jsonl_path",
        "last_record",    # most recent written record (for main loop bucket building)
    )

    def __init__(
        self,
        symbol: str,
        expiry: str,
        strike: int,
        option_type: str,
        scrip: Optional[dict],
        first_seen_at: datetime,
        first_seen_role: str,
        jsonl_path: Path,
    ) -> None:
        self.symbol = symbol
        self.expiry = expiry
        self.strike = strike
        self.option_type = option_type
        self.scrip = scrip
        self.first_seen_at = first_seen_at
        self.first_seen_role = first_seen_role
        self.last_sampled_at: Optional[datetime] = None
        self.previous_oi: Optional[int] = None
        self.jsonl_path = jsonl_path

        if scrip:
            self.token = scrip.get("token") or None
            self.tsym = scrip.get("tsym") or None
        else:
            self.token = None
            self.tsym = None

        self.source = "shoonya"
        self.last_record: Optional[dict] = None


class GammaLadderRecorder:
    """
    ATM±2 option ladder recorder for GammaBlast expiry-day sessions.

    Tracks CE and PE contracts for a configurable set of ATM offsets.
    Once a strike is activated (first observed) it is tracked for the rest
    of the session — strikes are never removed from the tracked set.

    Usage::

        recorder = GammaLadderRecorder(
            base_dir="/home/user/BlitzTrader/GammaBlast",
            shoonya_client=client,
            options_chain=chain,
            symbol="NIFTY",
            expiry_str="10-JUN-2026",
            strike_step=50,
        )

        # Call every scan tick:
        recorder.update_atm(underlying_ltp=24487.5)

        # Call every scan tick (or on a timer):
        recorder.sample_due_contracts()

        # At EOD:
        recorder.flush()
    """

    def __init__(
        self,
        base_dir: str,
        shoonya_client,
        options_chain,
        symbol: str,
        expiry_str: str,
        strike_step: int,
        atm_offsets: list[int] | None = None,
        sample_interval: int = 60,
    ) -> None:
        """
        Parameters
        ----------
        base_dir:
            Root directory for the GammaBlast project.  JSONL files are
            written under ``<base_dir>/data_exports/``.
        shoonya_client:
            A connected GammaBlast ShoonyaClient instance.
        options_chain:
            An OptionsChain instance used to resolve tokens and fetch quotes.
        symbol:
            Underlying index symbol, e.g. "NIFTY" or "SENSEX".
        expiry_str:
            Expiry date string in Shoonya format, e.g. "10-JUN-2026".
        strike_step:
            Distance between adjacent strikes, e.g. 50 for NIFTY.
        atm_offsets:
            List of signed integer offsets to track relative to ATM.
            Defaults to [-2, -1, 0, 1, 2].
        sample_interval:
            Minimum seconds between successive samples of the same contract.
            Defaults to 60.
        """
        if atm_offsets is None:
            atm_offsets = [-2, -1, 0, 1, 2]

        self._base_dir = Path(base_dir)
        self._client = shoonya_client
        self._chain = options_chain
        self._symbol = symbol.upper()
        self._expiry_str = expiry_str
        self._strike_step = strike_step
        self._atm_offsets = list(atm_offsets)
        self._sample_interval = sample_interval

        # Current ATM strike (updated by update_atm)
        self._current_atm: Optional[int] = None

        # Tracked contracts: key = (strike, option_type)
        self._contracts: dict[tuple[int, str], _TrackedContract] = {}

        self._lock = threading.Lock()

        logger.info(
            "GammaLadderRecorder initialised: symbol=%s expiry=%s step=%d "
            "offsets=%s interval=%ds base_dir=%s",
            self._symbol, self._expiry_str, self._strike_step,
            self._atm_offsets, self._sample_interval, self._base_dir,
        )

    # ── public API ────────────────────────────────────────────────────────────

    def update_atm(self, underlying_ltp: float) -> None:
        """
        Compute the current ATM strike from ``underlying_ltp``, then activate
        any new strikes that have not been tracked before.

        Should be called on every scan tick.
        """
        new_atm = round_to_strike(underlying_ltp, self._symbol)

        with self._lock:
            atm_changed = new_atm != self._current_atm
            if atm_changed:
                logger.debug(
                    "ATM change: %s → %s (ltp=%.2f)",
                    self._current_atm, new_atm, underlying_ltp,
                )
            self._current_atm = new_atm
            strikes = atm_ladder(new_atm, self._symbol, self._atm_offsets)
            now = _now_ist()

            for offset, strike in zip(self._atm_offsets, strikes):
                role = _get_role(offset)
                for ot in ("CE", "PE"):
                    key = (strike, ot)
                    if key not in self._contracts:
                        self._activate_contract(
                            strike=strike,
                            option_type=ot,
                            first_seen_at=now,
                            first_seen_role=role,
                        )

    def sample_due_contracts(self) -> None:
        """
        For each tracked contract whose last sample is older than
        ``sample_interval`` seconds (or that has never been sampled), fetch a
        live quote and write a JSONL record.

        Should be called on every scan tick (the method itself gates on the
        elapsed time so it is safe to call frequently).
        """
        now = _now_ist()
        with self._lock:
            due = [
                c for c in self._contracts.values()
                if self._is_sample_due(c, now)
            ]
            current_atm = self._current_atm

        for c in due:
            self._sample_and_write(c, now, current_atm)

    def flush(self) -> None:
        """
        Force an immediate sample for every tracked contract regardless of
        when it was last sampled.  Intended for end-of-day teardown.
        """
        logger.info(
            "flush() called — sampling all %d tracked contracts",
            len(self._contracts),
        )
        now = _now_ist()
        with self._lock:
            all_contracts = list(self._contracts.values())
            current_atm = self._current_atm

        for c in all_contracts:
            self._sample_and_write(c, now, current_atm)

    def tracked_strikes(self) -> list[tuple[int, str]]:
        """Return a list of (strike, option_type) tuples currently tracked."""
        with self._lock:
            return list(self._contracts.keys())

    def get_recent_rows(self, strike: int, option_type: str, n: int = 1) -> list[dict]:
        """Return the last N written records for the given contract (from in-memory cache)."""
        with self._lock:
            c = self._contracts.get((strike, option_type.upper()))
            if c is None or c.last_record is None:
                return []
            return [c.last_record]

    # ── internal helpers ──────────────────────────────────────────────────────

    def _activate_contract(
        self,
        strike: int,
        option_type: str,
        first_seen_at: datetime,
        first_seen_role: str,
    ) -> None:
        """
        Resolve the contract token via OptionsChain and register it.
        Must be called while holding ``self._lock``.
        """
        scrip = None
        try:
            scrip = self._chain.resolve_option(
                self._symbol, self._expiry_str, strike, option_type
            )
        except Exception:
            logger.exception(
                "Exception resolving %s %s %s %s",
                self._symbol, self._expiry_str, strike, option_type,
            )

        if scrip:
            logger.info(
                "Activated: %s %s %s %s token=%s tsym=%s role=%s",
                self._symbol, self._expiry_str, strike, option_type,
                scrip.get("token"), scrip.get("tsym"), first_seen_role,
            )
        else:
            logger.warning(
                "Could not resolve token for %s %s %s %s — will still track",
                self._symbol, self._expiry_str, strike, option_type,
            )

        jsonl_path = self._build_jsonl_path(scrip, strike, option_type, first_seen_at)

        contract = _TrackedContract(
            symbol=self._symbol,
            expiry=self._expiry_str,
            strike=strike,
            option_type=option_type,
            scrip=scrip,
            first_seen_at=first_seen_at,
            first_seen_role=first_seen_role,
            jsonl_path=jsonl_path,
        )
        self._contracts[(strike, option_type)] = contract

    def _build_jsonl_path(
        self,
        scrip: Optional[dict],
        strike: int,
        option_type: str,
        ref_time: datetime,
    ) -> Path:
        """
        Construct the output JSONL path.

        Pattern: <base_dir>/data_exports/YYYYMMDD/gamma_ladder/<SYMBOL>/<tsym>.jsonl

        If the tsym could not be resolved we synthesise a filename from the
        symbol, expiry, strike, and option_type so that the file can still
        be written.
        """
        date_str = ref_time.strftime("%Y%m%d")
        if scrip and scrip.get("tsym"):
            filename = f"{scrip['tsym']}.jsonl"
        else:
            # Synthesise: NIFTY10JUN26C24500
            cp = "C" if option_type == "CE" else "P"
            try:
                from datetime import datetime as _dt
                exp_dt = _dt.strptime(self._expiry_str, "%d-%b-%Y")
                exp_code = exp_dt.strftime("%d%b%y").upper()
            except ValueError:
                exp_code = self._expiry_str.replace("-", "")
            filename = f"{self._symbol}{exp_code}{cp}{strike}.jsonl"

        path = (
            self._base_dir
            / "data_exports"
            / date_str
            / "gamma_ladder"
            / self._symbol
            / filename
        )
        return path

    def _is_sample_due(self, contract: _TrackedContract, now: datetime) -> bool:
        if contract.last_sampled_at is None:
            return True
        elapsed = (now - contract.last_sampled_at).total_seconds()
        return elapsed >= self._sample_interval

    def _sample_and_write(
        self,
        contract: _TrackedContract,
        now: datetime,
        current_atm: Optional[int],
    ) -> None:
        """
        Fetch a live quote for ``contract`` and append a JSONL record.
        Updates ``contract.last_sampled_at`` and ``contract.previous_oi``
        regardless of whether the fetch succeeds.
        """
        quote: Optional[dict] = None
        source_error: Optional[str] = None

        if contract.scrip:
            try:
                quote = self._chain.get_quote(contract.scrip)
                if quote is None:
                    source_error = "get_quote returned None"
            except Exception as exc:
                source_error = str(exc)
                logger.exception(
                    "Exception fetching quote for %s %s %s",
                    contract.symbol, contract.strike, contract.option_type,
                )
        else:
            source_error = "no_token_resolved"

        # Compute OI change before updating previous_oi
        current_oi: Optional[int] = None
        oi_change: Optional[int] = None
        if quote is not None:
            current_oi = _safe_int(quote.get("oi") or quote.get("OI"))
            if current_oi is not None and contract.previous_oi is not None:
                oi_change = current_oi - contract.previous_oi

        # Depth
        bids: list[dict] = []
        asks: list[dict] = []
        if quote is not None:
            try:
                depth = parse_depth(quote)
                bids = depth.get("bids", [])
                asks = depth.get("asks", [])
            except Exception:
                logger.exception("parse_depth failed for %s %s %s",
                                 contract.symbol, contract.strike, contract.option_type)

        # Current offset from ATM
        offset_steps: Optional[int] = None
        is_current_atm = False
        if current_atm is not None:
            offset_steps = (contract.strike - current_atm) // self._strike_step
            is_current_atm = contract.strike == current_atm

        role_at_time = _get_role(offset_steps) if offset_steps is not None else "TRACKED_PREVIOUS"

        # Underlying LTP — try to get from options_chain
        underlying_ltp: Optional[float] = None
        try:
            underlying_ltp = self._chain.get_index_ltp(contract.symbol)
        except Exception:
            pass

        record = {
            "timestamp_ist": _iso_ist(now),
            "symbol": contract.symbol,
            "expiry": contract.expiry,
            "strike": contract.strike,
            "option_type": contract.option_type,
            "token": contract.token,
            "tsym": contract.tsym,
            "source": contract.source,
            "underlying_ltp": underlying_ltp,
            "atm_strike_at_time": current_atm,
            "first_seen_at": _iso_ist(contract.first_seen_at),
            "first_seen_role": contract.first_seen_role,
            "strike_role_at_time": role_at_time,
            "strike_offset_steps_at_time": offset_steps,
            "is_current_atm_at_time": is_current_atm,
            "open": _safe_float(quote.get("o") or quote.get("op")) if quote else None,
            "high": _safe_float(quote.get("h") or quote.get("hp")) if quote else None,
            "low": _safe_float(quote.get("l") or quote.get("lp_lower")) if quote else None,
            "close": _safe_float(quote.get("c") or quote.get("cp")) if quote else None,
            "ltp": _safe_float(quote.get("lp")) if quote else None,
            "volume": _safe_int(quote.get("v") or quote.get("vol")) if quote else None,
            "oi": current_oi,
            "oi_change": oi_change,
            "previous_oi": contract.previous_oi,
            "best_5_bids": bids if bids else None,
            "best_5_asks": asks if asks else None,
            "source_error": source_error,
        }

        self._write_jsonl(contract.jsonl_path, record)

        # Update mutable state on the contract object after writing
        with self._lock:
            contract.last_sampled_at = now
            contract.last_record = record
            if current_oi is not None:
                contract.previous_oi = current_oi

    @staticmethod
    def _write_jsonl(path: Path, record: dict) -> None:
        """Append a single JSON record (one line) to the given JSONL file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            logger.exception("Failed to write JSONL record to %s", path)
