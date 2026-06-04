"""
tools/atm_option_recorder.py — Intraday ATM option OHLCV/OI/depth logger.

Records ATM call and put option data for NIFTY and BANKNIFTY throughout
the trading day. Data is saved to:
    data_exports/YYYYMMDD/atm_options/<SYMBOL>/

Key behaviours:
- ATM strike is detected on each tick using the current underlying/futures LTP.
- When ATM changes, both old and new strike contracts continue logging.
- Sampling rate: once per minute per tracked contract (rate-limit safe).
- Data source: Shoonya REST quotes only — no yfinance, no fallback invented data.
- Missing fields result in null values, never guessed values.

Record schema (JSONL):
  timestamp_ist, symbol, expiry, strike, option_type, token, tsym,
  source, underlying_ltp, atm_strike_at_time, first_seen_at,
  is_current_atm_at_time,
  open, high, low, ltp, volume, oi, oi_change, previous_oi,
  best_5_bids, best_5_asks, source_error

Depth fields (best_5_bids / best_5_asks) are parsed from bp1-bp5/sp1-sp5
in the Shoonya GetQuotes response.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

logger = logging.getLogger("BlitzTrader.ATMOptionRecorder")

IST = pytz.timezone("Asia/Kolkata")

# Strike rounding increments matching options_chain.py
_STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
}

# Minimum seconds between samples for each tracked contract
_SAMPLE_INTERVAL_SECONDS = 60


def round_to_strike(price: float, symbol: str) -> int:
    step = _STRIKE_STEP.get(symbol.upper(), 50)
    return int(round(price / step) * step)


@dataclass
class _TrackedContract:
    """One tracked ATM option contract."""
    symbol: str        # NIFTY or BANKNIFTY
    expiry: str        # e.g. "26-JUN-2026"
    strike: int
    option_type: str   # CE or PE
    token: str
    tsym: str
    first_seen_at: str # IST ISO datetime
    last_sampled_at: float = 0.0  # POSIX epoch of last sample


class ATMOptionRecorder:
    """
    Tracks ATM option contracts for NIFTY and BANKNIFTY and records
    intraday OHLCV/OI/depth snapshots to local JSONL files.

    Usage:
        recorder = ATMOptionRecorder(
            base_dir=Path("data_exports"),
            shoonya_client=client,
            options_chain=chain,
        )
        # On each scan tick (called by main.py):
        recorder.update_atm(symbol="NIFTY", underlying_ltp=24550.0)
        recorder.sample_due_contracts()
        # At EOD:
        recorder.flush()
    """

    def __init__(
        self,
        base_dir: Path,
        shoonya_client,
        options_chain,
        sample_interval: int = _SAMPLE_INTERVAL_SECONDS,
    ):
        self._base_dir = Path(base_dir)
        self._client = shoonya_client
        self._chain = options_chain
        self._sample_interval = sample_interval
        self._lock = threading.Lock()

        # Keyed by (symbol, strike, option_type) → _TrackedContract
        self._tracked: dict[tuple, _TrackedContract] = {}

        # Current ATM strike per symbol
        self._current_atm: dict[str, int] = {}

        self._date_str = datetime.now(IST).strftime("%Y%m%d")

    # ── Public API ────────────────────────────────────────────────────────────

    def update_atm(self, symbol: str, underlying_ltp: float) -> None:
        """
        Detect ATM strike for symbol given current underlying LTP.
        If ATM has changed or is new, resolve option tokens and start tracking.
        Old ATM contracts continue logging — they are never removed.
        """
        sym = symbol.upper()
        if sym not in ("NIFTY", "BANKNIFTY"):
            return
        if underlying_ltp is None or underlying_ltp <= 0:
            return

        new_atm = round_to_strike(underlying_ltp, sym)
        old_atm = self._current_atm.get(sym)

        with self._lock:
            self._current_atm[sym] = new_atm

        if new_atm == old_atm:
            return  # ATM unchanged — tracked set is up to date

        logger.info(
            "ATMOptionRecorder: %s ATM changed %s → %s (underlying ₹%.2f)",
            sym, old_atm, new_atm, underlying_ltp,
        )
        self._resolve_and_track(sym, new_atm, underlying_ltp)

    def sample_due_contracts(self) -> None:
        """
        For each tracked contract that hasn't been sampled in the last
        _sample_interval seconds, fetch a quote and write a record.
        Call this on each scan tick.
        """
        now = time.time()
        with self._lock:
            due = [
                c for c in self._tracked.values()
                if now - c.last_sampled_at >= self._sample_interval
            ]

        for contract in due:
            self._sample_one(contract)

    def flush(self) -> None:
        """Force-sample all tracked contracts once (called at EOD)."""
        with self._lock:
            all_contracts = list(self._tracked.values())
        for contract in all_contracts:
            self._sample_one(contract, force=True)
        logger.info(
            "ATMOptionRecorder: EOD flush complete — %d contracts sampled",
            len(all_contracts),
        )

    @property
    def export_dir(self) -> Path:
        return self._base_dir / self._date_str / "atm_options"

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_and_track(self, symbol: str, atm_strike: int, underlying_ltp: float) -> None:
        """Resolve NFO tokens for ATM CE and PE; add them to tracked set."""
        if not self._chain:
            logger.warning("ATMOptionRecorder: no OptionsChain — cannot resolve ATM tokens")
            return

        expiry = self._chain.get_nearest_expiry(symbol)
        if not expiry:
            logger.warning("ATMOptionRecorder: no expiry found for %s", symbol)
            return

        now_ist_str = datetime.now(IST).isoformat()

        for opt_type in ("CE", "PE"):
            key = (symbol, atm_strike, opt_type)
            with self._lock:
                if key in self._tracked:
                    continue  # already tracked

            result = self._chain.resolve_option(
                symbol=symbol,
                expiry=expiry,
                strike=atm_strike,
                option_type=opt_type,
            )
            if not result:
                logger.warning(
                    "ATMOptionRecorder: could not resolve %s %s %s %s",
                    symbol, expiry, atm_strike, opt_type,
                )
                continue

            token = result.get("token", "")
            tsym = result.get("tsym", "")
            if not token or not tsym:
                logger.warning(
                    "ATMOptionRecorder: missing token/tsym for %s %s %s",
                    symbol, atm_strike, opt_type,
                )
                continue

            contract = _TrackedContract(
                symbol=symbol,
                expiry=expiry,
                strike=atm_strike,
                option_type=opt_type,
                token=token,
                tsym=tsym,
                first_seen_at=now_ist_str,
            )
            with self._lock:
                self._tracked[key] = contract
            logger.info(
                "ATMOptionRecorder: tracking %s %s %s %s (token=%s)",
                symbol, expiry, atm_strike, opt_type, token,
            )

    def _sample_one(self, contract: _TrackedContract, force: bool = False) -> None:
        """Fetch Shoonya quote for one contract and write a JSONL record."""
        now = time.time()
        if not force and now - contract.last_sampled_at < self._sample_interval:
            return

        record = self._build_record(contract)
        self._write_record(contract, record)

        with self._lock:
            contract.last_sampled_at = now

    def _build_record(self, contract: _TrackedContract) -> dict:
        """Fetch quote from Shoonya and build the record dict."""
        now_ist = datetime.now(IST)
        timestamp_ist = now_ist.isoformat()

        with self._lock:
            current_atm = self._current_atm.get(contract.symbol, 0)
        is_current_atm = (contract.strike == current_atm)

        base = {
            "timestamp_ist": timestamp_ist,
            "symbol": contract.symbol,
            "expiry": contract.expiry,
            "strike": contract.strike,
            "option_type": contract.option_type,
            "token": contract.token,
            "tsym": contract.tsym,
            "source": "shoonya",
            "underlying_ltp": None,
            "atm_strike_at_time": current_atm or None,
            "first_seen_at": contract.first_seen_at,
            "is_current_atm_at_time": is_current_atm,
            "open": None,
            "high": None,
            "low": None,
            "ltp": None,
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

        try:
            resp = self._client.get_quotes("NFO", contract.token)
        except Exception as exc:
            base["source_error"] = f"get_quotes_exception: {exc}"
            return base

        if not resp:
            base["source_error"] = "get_quotes_returned_none"
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

        base["ltp"] = _f("lp") or _f("c")
        base["open"] = _f("o")
        base["high"] = _f("h")
        base["low"] = _f("l")
        base["volume"] = _i("v") or _i("volume")
        base["oi"] = _i("oi")
        base["previous_oi"] = _i("poi")
        if base["oi"] is not None and base["previous_oi"] is not None:
            base["oi_change"] = base["oi"] - base["previous_oi"]

        # Parse up to 5 bid levels (bp1-bp5 / bq1-bq5 / bo1-bo5)
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

        # Parse up to 5 ask levels (sp1-sp5 / sq1-sq5 / so1-so5)
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
        """Append one JSONL record to the appropriate export file."""
        try:
            sym_dir = self.export_dir / contract.symbol
            sym_dir.mkdir(parents=True, exist_ok=True)

            # OHLCV/OI file
            ohlcv_path = sym_dir / f"{contract.tsym}_ohlcv.jsonl"
            ohlcv_row = {
                k: record[k]
                for k in (
                    "timestamp_ist", "symbol", "expiry", "strike", "option_type",
                    "token", "tsym", "source", "underlying_ltp",
                    "atm_strike_at_time", "first_seen_at", "is_current_atm_at_time",
                    "open", "high", "low", "ltp", "volume",
                    "oi", "oi_change", "previous_oi", "source_error",
                )
            }
            with open(ohlcv_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ohlcv_row, default=str) + "\n")

            # Depth file (only if depth data is present)
            if record.get("best_5_bids") or record.get("best_5_asks"):
                depth_path = sym_dir / f"{contract.tsym}_depth.jsonl"
                depth_row = {
                    k: record[k]
                    for k in (
                        "timestamp_ist", "symbol", "expiry", "strike", "option_type",
                        "token", "tsym", "source",
                        "atm_strike_at_time", "is_current_atm_at_time",
                        "ltp", "best_5_bids", "best_5_asks", "source_error",
                    )
                }
                with open(depth_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(depth_row, default=str) + "\n")

            # ATM strikes log (one entry per unique strike first-seen)
            self._update_strikes_log(contract)

        except Exception:
            logger.exception(
                "ATMOptionRecorder: failed to write record for %s %s",
                contract.symbol, contract.tsym,
            )

    def _update_strikes_log(self, contract: _TrackedContract) -> None:
        """Write/update the ATM strikes summary file for the symbol."""
        try:
            sym_dir = self.export_dir / contract.symbol
            strikes_path = sym_dir / f"{contract.symbol}_ATM_STRIKES.jsonl"
            entry = {
                "symbol": contract.symbol,
                "strike": contract.strike,
                "expiry": contract.expiry,
                "option_type": contract.option_type,
                "token": contract.token,
                "tsym": contract.tsym,
                "first_seen_at": contract.first_seen_at,
            }
            # Only append once per contract (check file for existing entry)
            if strikes_path.exists():
                existing = strikes_path.read_text(encoding="utf-8")
                marker = f'"tsym": "{contract.tsym}"'
                if marker in existing:
                    return
            with open(strikes_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            logger.exception("ATMOptionRecorder: strikes log update failed (non-fatal)")
