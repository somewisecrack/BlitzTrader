"""
Virtual NIFTY50 pair credit-spread replacement engine.

OmniSpread is imported read-only from OMNISPREAD_BACKEND_PATH. All position
state and trade-ledger writes stay inside BlitzTrader's runtime directory.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytz

from tools.blitz_schedule import is_pair_credit_expiry_close_time
from tools.pair_vrp_selector import NsePairVolatilityProvider, StructureDecision, select_pair_leg_structures

logger = logging.getLogger("BlitzTrader.PairCredit")
IST = pytz.timezone("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


def _date_key(value: date | None = None) -> str:
    return (value or _now_ist().date()).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _parse_expiry(value: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).upper(), fmt).date()
        except ValueError:
            continue
    return None


class _LegacyStrikeFallbackDetector(logging.Handler):
    """Detect OmniSpread downgrading vol strikes to the legacy rule."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "using the legacy rule" in message:
            self.messages.append(message)


class PairCreditLedger:
    """Atomic JSON state plus JSONL trade ledger for virtual pair spreads."""

    def __init__(self, state_file: Path, ledger_file: Path, capital: float):
        self._state_file = state_file
        self._ledger_file = ledger_file
        self._capital = float(capital)
        self._state: dict[str, Any] = {}
        self.load()

    def load(self) -> dict[str, Any]:
        if not self._state_file.exists():
            self._state = self._default_state()
            self.save()
            return self._state
        try:
            self._state = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._state.setdefault("capital", self._capital)
            self._state.setdefault("open_positions", [])
            self._state.setdefault("closed_positions", [])
            self._state.setdefault("manual_exit_dates", {})
            self._state.setdefault("notifications_sent", {})
            return self._state
        except Exception:
            logger.exception("PairCreditLedger: corrupt state, reinitializing")
            self._state = self._default_state()
            self.save()
            return self._state

    def _default_state(self) -> dict[str, Any]:
        return {
            "session_id": uuid.uuid4().hex[:8],
            "created_at": _now_ist().isoformat(),
            "capital": self._capital,
            "last_scan_date": "",
            "open_positions": [],
            "closed_positions": [],
            "manual_exit_dates": {},
            "notifications_sent": {},
        }

    def save(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, indent=2, default=_json_default), encoding="utf-8")
        os.replace(tmp, self._state_file)

    @property
    def state(self) -> dict[str, Any]:
        return self._state

    def open_positions(self) -> list[dict[str, Any]]:
        return [p for p in self._state.get("open_positions", []) if p.get("status") == "OPEN"]

    def allocated_margin(self) -> float:
        return sum(float(p.get("entry_margin", 0) or 0) for p in self.open_positions())

    def remaining_capital(self) -> float:
        return max(0.0, float(self._state.get("capital", self._capital)) - self.allocated_margin())

    def append_event(self, event: dict[str, Any]) -> None:
        self._ledger_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": _now_ist().isoformat(), **event}
        with self._ledger_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=_json_default) + "\n")

    def add_position(self, position: dict[str, Any]) -> None:
        self._state.setdefault("open_positions", []).append(position)
        self.append_event({"event": "OPEN", "position": position})
        self.save()

    def close_position(self, position_id: str, close_payload: dict[str, Any]) -> dict[str, Any] | None:
        open_positions = self._state.setdefault("open_positions", [])
        for idx, pos in enumerate(open_positions):
            if pos.get("position_id") != position_id:
                continue
            closed = deepcopy(pos)
            closed.update(close_payload)
            closed["status"] = "CLOSED"
            open_positions.pop(idx)
            self._state.setdefault("closed_positions", []).append(closed)
            self.append_event({"event": "CLOSE", "position": closed})
            self.save()
            return closed
        return None

    def mark_manual_exit_today(self) -> None:
        self._state.setdefault("manual_exit_dates", {})[_date_key()] = _now_ist().isoformat()
        self.save()

    def set_last_scan_today(self) -> None:
        self._state["last_scan_date"] = _date_key()
        self.save()


class OmniSpreadReadOnlyAdapter:
    """Read-only adapter over OmniSpread's scanner and credit-spread builder."""

    def __init__(self, backend_path: Path, shoonya_client=None):
        self.backend_path = Path(backend_path)
        self._loaded = False
        self._shoonya_client = shoonya_client
        self._option_token_cache: dict[str, dict[str, Any]] = {}
        self._future_token_cache: dict[str, dict[str, Any]] = {}
        self._iv_cache: dict[tuple[str, str, float, str], float | None] = {}
        self._hv_cache: dict[tuple[str, date, int, int, int], dict[str, Any] | None] = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self.backend_path.exists():
            raise RuntimeError(f"OmniSpread backend not found: {self.backend_path}")
        if str(self.backend_path) not in sys.path:
            sys.path.insert(0, str(self.backend_path))
        from derivatives_backtest import (
            _clean,
            _fetch_credit_snapshot,
            _snapshot_price,
            build_credit_spread_structure,
            instrument_types,
            nse_symbol,
            whole_lot_hedge,
        )
        from engine import OmniSpreadEngine
        from margin_estimator import estimate_margin
        from nse_client import fetch_future, fetch_option
        from presets import PRESETS

        self._clean = _clean
        self._fetch_credit_snapshot = _fetch_credit_snapshot
        self._snapshot_price = _snapshot_price
        self._build_credit_spread_structure = build_credit_spread_structure
        self._instrument_types = instrument_types
        self._nse_symbol = nse_symbol
        self._whole_lot_hedge = whole_lot_hedge
        self._estimate_margin = estimate_margin
        self._engine_cls = OmniSpreadEngine
        self._fetch_future = fetch_future
        self._fetch_option = fetch_option
        self._presets = PRESETS
        self._loaded = True

    def scan(self, preset: str, period: str, interval: str, top_n: int) -> list[dict[str, Any]]:
        self._ensure_loaded()
        tickers = list(self._presets[preset])
        engine = self._engine_cls(tickers=tickers, period=period, interval=interval, top_n=top_n)
        return list(engine.run_scan())

    def build_credit_structure(self, candidate: dict[str, Any], strike_rule: str, sold_sd: float, hedge_sd: float) -> dict[str, Any]:
        self._ensure_loaded()
        detector = _LegacyStrikeFallbackDetector()
        backend_logger = logging.getLogger("derivatives_backtest")
        backend_logger.addHandler(detector)
        try:
            structure = self._build_credit_spread_structure(
                x=candidate["x"],
                y=candidate["y"],
                qty=float(candidate["qty"]),
                direction=candidate["direction"],
                fetch_future=self._fetch_future,
                fetch_option=self._fetch_option,
                strike_rule=strike_rule,
                sold_sd=sold_sd,
                hedge_sd=hedge_sd,
            )
        finally:
            backend_logger.removeHandler(detector)
        if detector.messages:
            raise RuntimeError(
                "volatility strike selection fell back to legacy rule; rejecting candidate: "
                + " | ".join(detector.messages)
            )
        structure["strike_rule"] = strike_rule
        structure["sold_sd"] = sold_sd
        structure["hedge_sd"] = hedge_sd
        return structure

    @staticmethod
    def _normal_cdf(value: float) -> float:
        return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))

    @classmethod
    def _black_scholes_price(
        cls,
        spot: float,
        strike: float,
        years: float,
        rate: float,
        sigma: float,
        option_type: str,
    ) -> float:
        if spot <= 0 or strike <= 0 or years <= 0 or sigma <= 0:
            return 0.0
        sqrt_t = math.sqrt(years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
        discounted_strike = strike * math.exp(-rate * years)
        if option_type == "CE":
            return spot * cls._normal_cdf(d1) - discounted_strike * cls._normal_cdf(d2)
        if option_type == "PE":
            return discounted_strike * cls._normal_cdf(-d2) - spot * cls._normal_cdf(-d1)
        return 0.0

    @classmethod
    def _implied_volatility(
        cls,
        price: float,
        spot: float,
        strike: float,
        years: float,
        option_type: str,
        rate: float = 0.065,
    ) -> float | None:
        if price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
            return None
        low = 0.0001
        high = 5.0
        intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
        if price < intrinsic:
            return None
        for _ in range(80):
            mid = (low + high) / 2.0
            estimate = cls._black_scholes_price(spot, strike, years, rate, mid, option_type)
            if estimate > price:
                high = mid
            else:
                low = mid
        iv = (low + high) / 2.0
        return iv if math.isfinite(iv) and iv > 0 else None

    @staticmethod
    def _snapshot_entry_price(snapshot, strike: float, option_type: str) -> float | None:
        row = snapshot[
            (snapshot["STRIKE_PRICE"].astype(float) == float(strike))
            & (snapshot["OPTION_TYPE"] == option_type)
        ]
        if row.empty:
            return None
        price = float(row.iloc[0]["CLOSING_PRICE"])
        return price if math.isfinite(price) and price > 0 else None

    @staticmethod
    def _snapshot_traded(snapshot, strike: float, option_type: str) -> bool:
        row = snapshot[
            (snapshot["STRIKE_PRICE"].astype(float) == float(strike))
            & (snapshot["OPTION_TYPE"] == option_type)
        ]
        if row.empty:
            return False
        try:
            volume = float(row.iloc[0].get("TOT_TRADED_QTY") or 0)
        except (TypeError, ValueError):
            volume = 0.0
        return volume > 0

    def _atm_iv_expected_move(self, asset: dict[str, Any]) -> dict[str, Any]:
        snapshot = asset["option_snapshot"]
        spot = float(asset["spot"])
        expiry = asset["expiry"]
        as_of = asset.get("as_of")
        if hasattr(as_of, "date"):
            as_of_date = as_of.date()
        else:
            as_of_date = _now_ist().date()
        calendar_days = max((expiry.date() - as_of_date).days, 1)
        years = calendar_days / 365.0
        strikes = sorted(float(value) for value in snapshot["STRIKE_PRICE"].dropna().unique())
        if not strikes:
            raise ValueError(f"No option strikes available for {asset['symbol']} IV sizing.")
        atm = min(strikes, key=lambda strike: abs(strike - spot))

        ivs: list[float] = []
        for option_type in ("CE", "PE"):
            if not self._snapshot_traded(snapshot, atm, option_type):
                continue
            price = self._snapshot_entry_price(snapshot, atm, option_type)
            if price is None:
                continue
            iv = self._implied_volatility(price, spot, atm, years, option_type)
            if iv is not None:
                ivs.append(iv)
        if not ivs:
            raise ValueError(
                f"No traded ATM option with valid price for {asset['symbol']} {expiry.strftime('%d-%b-%Y')}."
            )
        iv = sum(ivs) / len(ivs)
        expected_move = spot * iv * math.sqrt(years)
        if not math.isfinite(expected_move) or expected_move <= 0:
            raise ValueError(f"Invalid IV expected move for {asset['symbol']}.")
        return {
            "atm_strike": atm,
            "atm_iv": iv,
            "calendar_days": calendar_days,
            "expected_move": expected_move,
        }

    def _select_iv_credit_leg(
        self,
        asset_key: str,
        asset: dict[str, Any],
        option_type: str,
        lots: int,
        sell_iv_move: float,
        hedge_max_iv_move: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        snapshot = asset["option_snapshot"]
        spot = float(asset["spot"])
        expiry = asset["expiry"]
        iv_metrics = self._atm_iv_expected_move(asset)
        expected_move = float(iv_metrics["expected_move"])
        away = -1.0 if option_type == "PE" else 1.0
        sold_target = spot + away * float(sell_iv_move) * expected_move
        hedge_limit = spot + away * float(hedge_max_iv_move) * expected_move

        typed = snapshot[(snapshot["OPTION_TYPE"] == option_type) & snapshot["STRIKE_PRICE"].notna()].copy()
        if typed.empty:
            raise ValueError(f"No {option_type} option strikes available for {asset['symbol']}.")
        strikes = sorted(float(value) for value in typed["STRIKE_PRICE"].unique())
        otm_sold = [
            strike for strike in strikes
            if away * (strike - spot) > 0 and self._snapshot_traded(typed, strike, option_type)
        ]
        if not otm_sold:
            raise ValueError(f"No traded OTM {option_type} sell strike for {asset['symbol']}.")
        sold_strike = min(otm_sold, key=lambda strike: abs(strike - sold_target))
        sold_price = self._snapshot_entry_price(typed, sold_strike, option_type)
        if sold_price is None:
            raise ValueError(f"No valid sold-leg price for {asset['symbol']} {sold_strike:g}{option_type}.")

        best: tuple[float, dict[str, Any], float, float] | None = None
        for hedge_strike in strikes:
            if away * (hedge_strike - sold_strike) <= 0:
                continue
            if away * (hedge_strike - hedge_limit) > 0:
                continue
            if not self._snapshot_traded(typed, hedge_strike, option_type):
                continue
            hedge_price = self._snapshot_entry_price(typed, hedge_strike, option_type)
            if hedge_price is None:
                continue
            credit_per_share = sold_price - hedge_price
            if credit_per_share <= 0:
                continue
            probe_legs = [
                {
                    "asset": asset_key,
                    "symbol": asset["symbol"],
                    "instrument": option_type,
                    "side": "SELL",
                    "lots": 1,
                    "lot_size": int(asset["future_lot"]),
                    "strike": sold_strike,
                    "spot": spot,
                    "price": sold_price,
                    "is_index": asset["is_index"],
                },
                {
                    "asset": asset_key,
                    "symbol": asset["symbol"],
                    "instrument": option_type,
                    "side": "BUY",
                    "lots": 1,
                    "lot_size": int(asset["future_lot"]),
                    "strike": hedge_strike,
                    "spot": spot,
                    "price": hedge_price,
                    "is_index": asset["is_index"],
                },
            ]
            margin = float(self._estimate_margin(probe_legs).get("estimated_margin") or 0)
            if margin <= 0:
                continue
            score = credit_per_share * int(asset["future_lot"]) / margin
            if best is None or score > best[0]:
                hedge_info = {
                    "strike": hedge_strike,
                    "price": hedge_price,
                    "margin_per_lot": margin,
                    "credit_to_margin": score,
                }
                best = (score, hedge_info, credit_per_share, margin)
        if best is None:
            raise ValueError(
                f"No viable traded hedge for {asset['symbol']} {sold_strike:g}{option_type} "
                f"inside {hedge_max_iv_move:g}x IV move."
            )

        _, hedge_info, credit_per_share, margin_per_lot = best
        common = {
            "asset": asset_key,
            "symbol": asset["symbol"],
            "instrument": option_type,
            "lots": int(lots),
            "lot_size": int(asset["future_lot"]),
            "expiry": asset["expiry"].strftime("%d-%b-%Y"),
            "spot": round(spot, 2),
            "is_index": asset["is_index"],
        }
        legs = [
            {**common, "side": "SELL", "strike": sold_strike, "price": sold_price},
            {**common, "side": "BUY", "strike": hedge_info["strike"], "price": hedge_info["price"]},
        ]
        metrics = {
            **iv_metrics,
            "symbol": asset["symbol"],
            "option_type": option_type,
            "sold_target": sold_target,
            "hedge_limit": hedge_limit,
            "sold_strike": sold_strike,
            "hedge_strike": hedge_info["strike"],
            "credit_per_share": credit_per_share,
            "margin_per_lot": margin_per_lot,
            "credit_to_margin": hedge_info["credit_to_margin"],
        }
        return legs, metrics

    def build_iv_expected_move_credit_structure(
        self,
        candidate: dict[str, Any],
        sell_iv_move: float,
        hedge_max_iv_move: float,
    ) -> dict[str, Any]:
        self._ensure_loaded()
        from pandas import Timestamp

        as_of = datetime.now()
        start = as_of - timedelta(days=14)
        required_expiry = Timestamp(as_of.date())
        assets = {
            "x": self._fetch_credit_snapshot(
                candidate["x"], start, as_of, required_expiry, self._fetch_future, self._fetch_option
            ),
            "y": self._fetch_credit_snapshot(
                candidate["y"], start, as_of, required_expiry, self._fetch_future, self._fetch_option
            ),
        }
        x_lots, y_lots = self._whole_lot_hedge(
            float(candidate["qty"]), assets["x"]["future_lot"], assets["y"]["future_lot"]
        )
        lot_counts = {"x": x_lots, "y": y_lots}
        short_spread = candidate["direction"] in {"SHORT_SPREAD", "long_x_short_y"}
        signs = {"x": 1 if short_spread else -1, "y": -1 if short_spread else 1}

        legs: list[dict[str, Any]] = []
        iv_leg_selection: list[dict[str, Any]] = []
        for key, asset in assets.items():
            option_type = "PE" if signs[key] > 0 else "CE"
            selected_legs, metrics = self._select_iv_credit_leg(
                key,
                asset,
                option_type,
                lot_counts[key],
                sell_iv_move=sell_iv_move,
                hedge_max_iv_move=hedge_max_iv_move,
            )
            legs.extend(selected_legs)
            iv_leg_selection.append(metrics)

        actual_ratio = x_lots * assets["x"]["future_lot"] / (y_lots * assets["y"]["future_lot"])
        margin = self._estimate_margin(legs)
        return {
            "pair": f"{self._nse_symbol(candidate['x'])}/{self._nse_symbol(candidate['y'])}",
            "qty": candidate["qty"],
            "direction": candidate["direction"],
            "as_of": min(asset["as_of"] for asset in assets.values()).strftime("%d-%b-%Y"),
            "x_lots": x_lots,
            "y_lots": y_lots,
            "actual_ratio": round(actual_ratio, 4),
            "legs": legs,
            "margin": margin,
            "leg_selection": "iv_expected_move",
            "sell_iv_move": sell_iv_move,
            "hedge_max_iv_move": hedge_max_iv_move,
            "iv_leg_selection": iv_leg_selection,
            "note": "Current structure only; prices and available strikes can change before execution.",
        }

    def build_iv_expected_move_futures_options_structure(
        self,
        candidate: dict[str, Any],
        protection_iv_move: float,
    ) -> dict[str, Any]:
        """Build the established long-future plus protective-option shape.

        This is separate from the credit builder: it reuses its ATM-IV expected
        move convention but never changes its sold/hedge strike selection.
        """
        self._ensure_loaded()
        from pandas import Timestamp

        as_of = datetime.now()
        start = as_of - timedelta(days=14)
        required_expiry = Timestamp(as_of.date())
        assets = {
            "x": self._fetch_credit_snapshot(candidate["x"], start, as_of, required_expiry, self._fetch_future, self._fetch_option),
            "y": self._fetch_credit_snapshot(candidate["y"], start, as_of, required_expiry, self._fetch_future, self._fetch_option),
        }
        future_prices = {
            key: self._current_future_snapshot_price(asset, start, as_of)
            for key, asset in assets.items()
        }
        x_lots, y_lots = self._whole_lot_hedge(float(candidate["qty"]), assets["x"]["future_lot"], assets["y"]["future_lot"])
        counts = {"x": x_lots, "y": y_lots}
        short_spread = candidate["direction"] in {"SHORT_SPREAD", "long_x_short_y"}
        signs = {"x": 1 if short_spread else -1, "y": -1 if short_spread else 1}
        legs: list[dict[str, Any]] = []
        selections: list[dict[str, Any]] = []
        for key, asset in assets.items():
            count = counts[key]
            option_type = "PE" if signs[key] > 0 else "CE"
            metrics = self._atm_iv_expected_move(asset)
            target = float(asset["spot"]) + (-1.0 if option_type == "PE" else 1.0) * protection_iv_move * float(metrics["expected_move"])
            typed = asset["option_snapshot"][(asset["option_snapshot"]["OPTION_TYPE"] == option_type) & asset["option_snapshot"]["STRIKE_PRICE"].notna()]
            traded = [float(s) for s in typed["STRIKE_PRICE"].unique() if self._snapshot_traded(typed, float(s), option_type)]
            direction = -1.0 if option_type == "PE" else 1.0
            traded = [s for s in traded if direction * (s - float(asset["spot"])) > 0]
            if not traded:
                raise ValueError(f"No traded OTM protective {option_type} for {asset['symbol']}.")
            strike = min(traded, key=lambda value: abs(value - target))
            premium = self._snapshot_entry_price(typed, strike, option_type)
            if premium is None:
                raise ValueError(f"No valid protective option price for {asset['symbol']} {strike:g}{option_type}.")
            common = {
                "asset": key, "symbol": asset["symbol"], "lots": int(count), "lot_size": int(asset["future_lot"]),
                "expiry": asset["expiry"].strftime("%d-%b-%Y"), "spot": round(float(asset["spot"]), 2), "is_index": asset["is_index"],
            }
            legs.append({**common, "instrument": "FUT", "side": "BUY" if signs[key] > 0 else "SELL", "price": future_prices[key]})
            legs.append({**common, "instrument": option_type, "side": "BUY", "strike": strike, "price": premium})
            selections.append({**metrics, "symbol": asset["symbol"], "option_type": option_type, "protection_target": target, "protection_strike": strike, "protection_price": premium})
        actual_ratio = x_lots * assets["x"]["future_lot"] / (y_lots * assets["y"]["future_lot"])
        return {
            "pair": f"{self._nse_symbol(candidate['x'])}/{self._nse_symbol(candidate['y'])}", "qty": candidate["qty"], "direction": candidate["direction"],
            "as_of": min(asset["as_of"] for asset in assets.values()).strftime("%d-%b-%Y"),
            "x_lots": x_lots, "y_lots": y_lots, "actual_ratio": round(actual_ratio, 4), "legs": legs,
            "margin": self._estimate_margin(legs), "leg_selection": "iv_expected_move", "structure_type": "FUTURES_PLUS_OPTIONS",
            "protection_iv_move": protection_iv_move, "iv_leg_selection": selections,
            "note": "Current structure only; prices and available strikes can change before execution.",
        }

    def build_iv_expected_move_per_leg_structure(
        self,
        candidate: dict[str, Any],
        leg_structures: dict[str, str],
        sell_iv_move: float,
        hedge_max_iv_move: float,
    ) -> dict[str, Any]:
        """Build a pair whose two legs may use different approved structures."""
        self._ensure_loaded()
        from pandas import Timestamp

        if set(leg_structures) != {"x", "y"} or not set(leg_structures.values()) <= {"CREDIT_SPREAD", "FUTURES_PLUS_OPTIONS"}:
            raise ValueError("Per-leg structure selection must specify x and y approved structures.")
        as_of = datetime.now()
        start = as_of - timedelta(days=14)
        required_expiry = Timestamp(as_of.date())
        assets = {
            "x": self._fetch_credit_snapshot(candidate["x"], start, as_of, required_expiry, self._fetch_future, self._fetch_option),
            "y": self._fetch_credit_snapshot(candidate["y"], start, as_of, required_expiry, self._fetch_future, self._fetch_option),
        }
        x_lots, y_lots = self._whole_lot_hedge(float(candidate["qty"]), assets["x"]["future_lot"], assets["y"]["future_lot"])
        counts = {"x": x_lots, "y": y_lots}
        short_spread = candidate["direction"] in {"SHORT_SPREAD", "long_x_short_y"}
        signs = {"x": 1 if short_spread else -1, "y": -1 if short_spread else 1}
        legs: list[dict[str, Any]] = []
        selections: list[dict[str, Any]] = []
        for key, asset in assets.items():
            option_type = "PE" if signs[key] > 0 else "CE"
            if leg_structures[key] == "CREDIT_SPREAD":
                selected, metrics = self._select_iv_credit_leg(
                    key, asset, option_type, counts[key], sell_iv_move, hedge_max_iv_move,
                )
                legs.extend(selected)
                selections.append({"structure_type": "CREDIT_SPREAD", **metrics})
                continue

            future_price = self._current_future_snapshot_price(asset, start, as_of)
            metrics = self._atm_iv_expected_move(asset)
            direction = -1.0 if option_type == "PE" else 1.0
            target = float(asset["spot"]) + direction * sell_iv_move * float(metrics["expected_move"])
            typed = asset["option_snapshot"][(asset["option_snapshot"]["OPTION_TYPE"] == option_type) & asset["option_snapshot"]["STRIKE_PRICE"].notna()]
            strikes = [float(value) for value in typed["STRIKE_PRICE"].unique() if self._snapshot_traded(typed, float(value), option_type)]
            strikes = [strike for strike in strikes if direction * (strike - float(asset["spot"])) > 0]
            if not strikes:
                raise ValueError(f"No traded OTM protective {option_type} for {asset['symbol']}.")
            strike = min(strikes, key=lambda value: abs(value - target))
            premium = self._snapshot_entry_price(typed, strike, option_type)
            if premium is None:
                raise ValueError(f"No valid protective option price for {asset['symbol']} {strike:g}{option_type}.")
            common = {
                "asset": key, "symbol": asset["symbol"], "lots": int(counts[key]), "lot_size": int(asset["future_lot"]),
                "expiry": asset["expiry"].strftime("%d-%b-%Y"), "spot": round(float(asset["spot"]), 2), "is_index": asset["is_index"],
            }
            legs.extend([
                {**common, "instrument": "FUT", "side": "BUY" if signs[key] > 0 else "SELL", "price": future_price},
                {**common, "instrument": option_type, "side": "BUY", "strike": strike, "price": premium},
            ])
            selections.append({"structure_type": "FUTURES_PLUS_OPTIONS", **metrics, "symbol": asset["symbol"], "option_type": option_type, "protection_target": target, "protection_strike": strike, "protection_price": premium})
        actual_ratio = x_lots * assets["x"]["future_lot"] / (y_lots * assets["y"]["future_lot"])
        return {
            "pair": f"{self._nse_symbol(candidate['x'])}/{self._nse_symbol(candidate['y'])}", "qty": candidate["qty"], "direction": candidate["direction"],
            "as_of": min(asset["as_of"] for asset in assets.values()).strftime("%d-%b-%Y"),
            "x_lots": x_lots, "y_lots": y_lots, "actual_ratio": round(actual_ratio, 4), "legs": legs,
            "margin": self._estimate_margin(legs), "leg_selection": "iv_expected_move", "structure_type": "HYBRID",
            "leg_structure_types": dict(leg_structures), "sell_iv_move": sell_iv_move, "hedge_max_iv_move": hedge_max_iv_move,
            "iv_leg_selection": selections, "note": "Current structure only; prices and available strikes can change before execution.",
        }

    def _current_future_snapshot_price(self, asset: dict[str, Any], start: datetime, end: datetime) -> float:
        """Read the actual same-contract future close; never substitute spot."""
        raw = self._fetch_future(
            symbol=asset["symbol"], instrument="FUTIDX" if asset["is_index"] else "FUTSTK",
            from_date=start.strftime("%d-%m-%Y"), to_date=end.strftime("%d-%m-%Y"),
        )
        frame = self._clean(raw)
        rows = frame[frame["expiry"] == asset["expiry"]]
        if rows.empty:
            raise ValueError(f"No current future snapshot was available for {asset['symbol']}.")
        latest_day = rows["date"].max()
        latest = rows[rows["date"] == latest_day]
        prices = latest["CLOSING_PRICE"].dropna()
        if prices.empty:
            raise ValueError(f"No current future closing price was available for {asset['symbol']}.")
        price = float(prices.iloc[0])
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"Invalid current future closing price for {asset['symbol']}.")
        return price

    @staticmethod
    def _strike_text(strike: Any) -> str:
        value = float(strike)
        return str(int(value)) if value.is_integer() else f"{value:g}"

    @staticmethod
    def _option_code(option_type: str) -> str | None:
        option_type = str(option_type).upper()
        if option_type == "CE":
            return "C"
        if option_type == "PE":
            return "P"
        return None

    def _expected_option_tsym(self, leg: dict[str, Any]) -> str | None:
        expiry = _parse_expiry(str(leg.get("expiry", "")))
        option_code = self._option_code(str(leg.get("instrument", "")))
        if not expiry or not option_code:
            return None
        symbol = str(leg.get("symbol", "")).upper()
        expiry_code = expiry.strftime("%d%b%y").upper()
        strike = self._strike_text(leg.get("strike"))
        return f"{symbol}{expiry_code}{option_code}{strike}"

    def _ensure_shoonya_client(self):
        if self._shoonya_client is not None:
            return self._shoonya_client
        try:
            from broker.shoonya_client import ShoonyaClient
            from config import (
                SHOONYA_API_KEY,
                SHOONYA_AUTH_CODE,
                SHOONYA_IMEI,
                SHOONYA_PASSWORD,
                SHOONYA_SECRET_CODE,
                SHOONYA_TOTP_SECRET,
                SHOONYA_USER_ID,
                SHOONYA_VENDOR_CODE,
            )

            client = ShoonyaClient()
            ok, msg = client.login(
                SHOONYA_USER_ID,
                SHOONYA_PASSWORD,
                SHOONYA_TOTP_SECRET,
                SHOONYA_API_KEY,
                SHOONYA_VENDOR_CODE,
                SHOONYA_IMEI,
                SHOONYA_SECRET_CODE,
                SHOONYA_AUTH_CODE,
            )
            if not ok:
                logger.warning("Pair-credit MTM: Shoonya login failed: %s", msg)
                return None
            self._shoonya_client = client
            return client
        except Exception:
            logger.exception("Pair-credit MTM: Shoonya client unavailable")
            return None

    def _resolve_live_option(self, leg: dict[str, Any]) -> dict[str, Any] | None:
        expected_tsym = self._expected_option_tsym(leg)
        if not expected_tsym:
            return None
        cached = self._option_token_cache.get(expected_tsym)
        if cached:
            return cached

        client = self._ensure_shoonya_client()
        if client is None:
            return None
        results = client.search_scrip("NFO", expected_tsym) or []
        expiry = _parse_expiry(str(leg.get("expiry", "")))
        option_type = str(leg.get("instrument", "")).upper()
        symbol = str(leg.get("symbol", "")).upper()
        expected_exd = expiry.strftime("%d-%b-%Y").upper() if expiry else ""

        for row in results:
            tsym = str(row.get("tsym", "")).upper()
            if tsym != expected_tsym:
                continue
            if str(row.get("instname", "")).upper() != "OPTSTK":
                continue
            if str(row.get("symname", "")).upper() != symbol:
                continue
            if str(row.get("optt", "")).upper() != option_type:
                continue
            if str(row.get("exd", "")).upper() != expected_exd:
                continue
            token = str(row.get("token", "")).strip()
            if not token:
                continue
            resolved = {"exchange": "NFO", "token": token, "tsym": row.get("tsym", expected_tsym)}
            self._option_token_cache[expected_tsym] = resolved
            return resolved

        logger.warning(
            "Pair-credit MTM: exact option not resolved for %s; sample_tsyms=%s",
            expected_tsym,
            [row.get("tsym") for row in results[:5]],
        )
        return None

    def latest_option_price(self, leg: dict[str, Any]) -> float | None:
        resolved = self._resolve_live_option(leg)
        if not resolved:
            return None
        client = self._ensure_shoonya_client()
        if client is None:
            return None
        # Shoonya can intermittently return an NSE equity quote for an NFO
        # token. Re-query the same exact token; never substitute another price.
        for attempt in range(2):
            quote = client.get_quotes(resolved["exchange"], resolved["token"]) or {}
            if self._is_valid_option_quote(leg, resolved, quote):
                try:
                    price = float(quote.get("lp"))
                except (TypeError, ValueError):
                    logger.warning(
                        "Pair-credit MTM: quote missing lp for %s token=%s quote=%s",
                        resolved["tsym"],
                        resolved["token"],
                        quote,
                    )
                    return None
                return price if math.isfinite(price) and price >= 0 else None
            if attempt == 0:
                time.sleep(0.05)
        return None

    def expiry_settlement_price(self, leg: dict[str, Any], expiry: date) -> float | None:
        """Return an expired option's intrinsic value from its expiry-day close.

        This is deliberately not a live-quote fallback.  It is available only
        after expiry, when the exact option contract is no longer tradeable and
        its settlement value is intrinsic value.  Missing source data fails
        closed so a position is never booked at an invented price.
        """
        option_type = str(leg.get("instrument", "")).upper()
        symbol = str(leg.get("symbol", "")).upper().strip()
        try:
            strike = float(leg["strike"])
        except (KeyError, TypeError, ValueError):
            return None
        if option_type not in {"CE", "PE"} or not symbol or not math.isfinite(strike):
            return None

        try:
            import yfinance as yf

            history = yf.Ticker(f"{symbol}.NS").history(
                start=expiry.isoformat(),
                end=(expiry + timedelta(days=1)).isoformat(),
                auto_adjust=False,
            )
            if history.empty or "Close" not in history:
                logger.warning("Expiry settlement close unavailable for %s on %s", symbol, expiry)
                return None
            close = float(history["Close"].iloc[-1])
        except Exception:
            logger.exception("Expiry settlement close lookup failed for %s on %s", symbol, expiry)
            return None

        if not math.isfinite(close) or close <= 0:
            logger.warning("Invalid expiry settlement close for %s on %s: %s", symbol, expiry, close)
            return None
        intrinsic = max(strike - close, 0.0) if option_type == "PE" else max(close - strike, 0.0)
        return round(intrinsic, 8)

    def _resolve_live_future(self, leg: dict[str, Any]) -> dict[str, Any] | None:
        expiry = _parse_expiry(str(leg.get("expiry", "")))
        symbol = str(leg.get("symbol", "")).upper()
        if not expiry or not symbol:
            return None
        key = f"{symbol}:{expiry.isoformat()}"
        if key in self._future_token_cache:
            return self._future_token_cache[key]
        client = self._ensure_shoonya_client()
        if client is None:
            return None
        expected_expiry = expiry.strftime("%d-%b-%Y").upper()
        for row in client.search_scrip("NFO", symbol) or []:
            if str(row.get("instname", "")).upper() not in {"FUTSTK", "FUTIDX"}:
                continue
            if str(row.get("symname", "")).upper() != symbol or str(row.get("exd", "")).upper() != expected_expiry:
                continue
            token = str(row.get("token", "")).strip()
            if token:
                result = {"exchange": "NFO", "token": token, "tsym": row.get("tsym", "")}
                self._future_token_cache[key] = result
                return result
        logger.warning("Pair-credit MTM: exact future not resolved for %s %s", symbol, expected_expiry)
        return None

    def latest_future_price(self, leg: dict[str, Any]) -> float | None:
        resolved = self._resolve_live_future(leg)
        client = self._ensure_shoonya_client()
        if not resolved or client is None:
            return None
        quote = client.get_quotes(resolved["exchange"], resolved["token"]) or {}
        if (
            str(quote.get("tsym", "")).upper() != str(resolved["tsym"]).upper()
            or str(quote.get("token", "")) != str(resolved["token"])
            or str(quote.get("exch", "")).upper() != "NFO"
        ):
            logger.warning("Pair-credit MTM: future quote identity mismatch for %s", resolved["tsym"])
            return None
        try:
            price = float(quote["lp"])
        except (KeyError, TypeError, ValueError):
            return None
        return price if math.isfinite(price) and price > 0 else None

    @staticmethod
    def _is_valid_option_quote(
        leg: dict[str, Any],
        resolved: dict[str, Any],
        quote: dict[str, Any],
    ) -> bool:
        """Accept only an internally coherent quote for the exact option contract.

        A broker response can occasionally contain an underlying value in ``lp``.
        MTM and virtual exits must fail closed in that case rather than booking an
        impossible option premium.
        """
        expected_tsym = str(resolved.get("tsym", "")).upper()
        expected_token = str(resolved.get("token", ""))
        if (
            str(quote.get("tsym", "")).upper() != expected_tsym
            or str(quote.get("token", "")) != expected_token
            or str(quote.get("exch", "")).upper() != str(resolved.get("exchange", "")).upper()
        ):
            logger.warning(
                "Pair-credit MTM: quote identity mismatch for %s token=%s quote=%s",
                expected_tsym,
                expected_token,
                quote,
            )
            return False

        try:
            price = float(quote["lp"])
            tick_size = float(quote["ti"])
            strike = float(leg["strike"])
            underlying = float(quote["sptprc"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Pair-credit MTM: incomplete option quote for %s: %s", expected_tsym, quote)
            return False

        values = (price, tick_size, strike, underlying)
        if not all(math.isfinite(value) for value in values) or tick_size <= 0 or strike <= 0 or underlying <= 0:
            logger.warning("Pair-credit MTM: non-finite or invalid option quote for %s: %s", expected_tsym, quote)
            return False

        # Market-open responses contain no intraday high/low. They are still
        # valid exact-contract quotes. Validate the range only when Shoonya
        # actually supplies both fields.
        if "l" in quote or "h" in quote:
            try:
                day_low = float(quote["l"])
                day_high = float(quote["h"])
            except (KeyError, TypeError, ValueError):
                logger.warning("Pair-credit MTM: malformed daily range for %s: %s", expected_tsym, quote)
                return False
            if not all(math.isfinite(value) for value in (day_low, day_high)) or price < day_low - tick_size or price > day_high + tick_size:
                logger.warning("Pair-credit MTM: lp outside daily range for %s: %s", expected_tsym, quote)
                return False

        option_type = str(leg.get("instrument", "")).upper()
        upper_bound = strike if option_type == "PE" else underlying if option_type == "CE" else None
        if upper_bound is None or price > upper_bound + tick_size:
            logger.warning("Pair-credit MTM: economically impossible premium for %s: %s", expected_tsym, quote)
            return False
        return True

    @staticmethod
    def _trading_days_to_expiry(expiry: date, today: date | None = None) -> int:
        from tools.market_calendar import is_nse_trading_day

        today = today or _now_ist().date()
        if expiry <= today:
            return 1
        days = 0
        cursor = today + timedelta(days=1)
        while cursor <= expiry:
            if is_nse_trading_day(cursor):
                days += 1
            cursor += timedelta(days=1)
        return max(1, days)

    @staticmethod
    def _hv_lookback_days(
        expiry: date,
        min_days: int,
        max_days: int,
        multiplier: int,
        today: date | None = None,
    ) -> int:
        trading_days = OmniSpreadReadOnlyAdapter._trading_days_to_expiry(expiry, today=today)
        return max(int(min_days), min(int(max_days), trading_days * int(multiplier)))

    def historical_volatility(
        self,
        symbol: str,
        expiry: date,
        min_days: int,
        max_days: int,
        multiplier: int,
    ) -> dict[str, Any] | None:
        lookback = self._hv_lookback_days(expiry, min_days, max_days, multiplier)
        ticker = symbol if str(symbol).upper().endswith(".NS") else f"{str(symbol).upper()}.NS"
        cache_key = (ticker, expiry, int(min_days), int(max_days), int(multiplier))
        if cache_key in self._hv_cache:
            return self._hv_cache[cache_key]
        try:
            import numpy as np
            import yfinance as yf

            period_days = max(45, lookback * 4)
            df = yf.download(
                ticker,
                period=f"{period_days}d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if df is None or df.empty:
                self._hv_cache[cache_key] = None
                return None
            close = df["Close"].dropna()
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            if len(close) < lookback + 1:
                self._hv_cache[cache_key] = None
                return None
            returns = np.log(close / close.shift(1)).dropna().tail(lookback)
            if len(returns) < lookback:
                self._hv_cache[cache_key] = None
                return None
            hv = float(returns.std(ddof=1) * math.sqrt(252) * 100.0)
            if not math.isfinite(hv) or hv <= 0:
                self._hv_cache[cache_key] = None
                return None
            result = {
                "symbol": ticker,
                "hv": hv,
                "lookback_days": lookback,
                "trading_days_to_expiry": self._trading_days_to_expiry(expiry),
                "source": "yfinance",
            }
            self._hv_cache[cache_key] = result
            return result
        except Exception:
            logger.exception("Pair-credit vol gate: HV unavailable for %s", ticker)
            self._hv_cache[cache_key] = None
            return None

    def option_iv(self, leg: dict[str, Any]) -> float | None:
        expiry = _parse_expiry(str(leg.get("expiry", "")))
        if not expiry:
            return None
        symbol = str(leg.get("symbol", "")).upper()
        option_type = str(leg.get("instrument", "")).upper()
        strike = float(leg.get("strike"))
        cache_key = (symbol, expiry.isoformat(), strike, option_type)
        if cache_key in self._iv_cache:
            return self._iv_cache[cache_key]
        try:
            from nselib import derivatives

            chain = derivatives.nse_live_option_chain(
                symbol=symbol,
                expiry_date=expiry.strftime("%d-%m-%Y"),
                oi_mode="compact",
            )
            if chain is None or chain.empty:
                self._iv_cache[cache_key] = None
                return None
            rows = chain[chain["Strike_Price"].astype(float) == strike]
            if rows.empty:
                self._iv_cache[cache_key] = None
                return None
            col = "CALLS_IV" if option_type == "CE" else "PUTS_IV" if option_type == "PE" else ""
            if not col:
                self._iv_cache[cache_key] = None
                return None
            iv = float(rows.iloc[0][col])
            if not math.isfinite(iv) or iv <= 0:
                self._iv_cache[cache_key] = None
                return None
            self._iv_cache[cache_key] = iv
            return iv
        except Exception:
            logger.exception("Pair-credit vol gate: IV unavailable for %s", self._expected_option_tsym(leg))
            self._iv_cache[cache_key] = None
            return None

    def evaluate_credit_volatility(
        self,
        structure: dict[str, Any],
        min_ratio: float,
        min_days: int,
        max_days: int,
        multiplier: int,
    ) -> dict[str, Any]:
        leg_metrics: list[dict[str, Any]] = []
        ratios: list[float] = []
        for leg in structure.get("legs", []):
            if str(leg.get("side", "")).upper() != "SELL":
                continue
            expiry = _parse_expiry(str(leg.get("expiry", "")))
            if not expiry:
                return {"ok": False, "preferred_structure": "UNKNOWN", "reason": "missing expiry", "legs": leg_metrics}
            iv = self.option_iv(leg)
            hv_info = self.historical_volatility(
                str(leg.get("symbol", "")),
                expiry,
                min_days=min_days,
                max_days=max_days,
                multiplier=multiplier,
            )
            hv = float(hv_info["hv"]) if hv_info else None
            ratio = (iv / hv) if iv and hv else None
            metric = {
                "symbol": leg.get("symbol"),
                "strike": leg.get("strike"),
                "option_type": leg.get("instrument"),
                "expiry": leg.get("expiry"),
                "side": leg.get("side"),
                "iv": iv,
                "hv": hv,
                "iv_hv_ratio": ratio,
                "hv_lookback_days": hv_info.get("lookback_days") if hv_info else None,
                "trading_days_to_expiry": hv_info.get("trading_days_to_expiry") if hv_info else None,
            }
            leg_metrics.append(metric)
            if ratio is None:
                return {
                    "ok": False,
                    "preferred_structure": "UNKNOWN",
                    "reason": "IV/HV unavailable for sold leg",
                    "legs": leg_metrics,
                }
            ratios.append(float(ratio))

        if not ratios:
            return {"ok": False, "preferred_structure": "UNKNOWN", "reason": "no sold option legs", "legs": leg_metrics}
        min_observed = min(ratios)
        if min_observed >= float(min_ratio):
            return {
                "ok": True,
                "preferred_structure": "CREDIT_SPREAD",
                "min_iv_hv_ratio": min_observed,
                "legs": leg_metrics,
            }
        return {
            "ok": False,
            "preferred_structure": "LONG_VOL",
            "reason": f"IV/HV below credit threshold: {min_observed:.2f} < {float(min_ratio):.2f}",
            "min_iv_hv_ratio": min_observed,
            "legs": leg_metrics,
        }


@dataclass
class PairCreditConfig:
    backend_path: Path
    state_file: Path
    ledger_file: Path
    capital: float
    preset: str = "nifty_50"
    period: str = "1y"
    interval: str = "1d"
    top_n: int = 50
    leg_selection: str = "iv_expected_move"
    strike_rule: str = "vol"
    sold_sd: float = 1.0
    hedge_sd: float = 2.5
    sell_iv_move: float = 1.0
    hedge_max_iv_move: float = 2.5
    vol_gate_enabled: bool = False
    iv_hv_min_ratio: float = 1.0
    hv_lookback_multiplier: int = 2
    hv_min_lookback_days: int = 5
    hv_max_lookback_days: int = 30
    vrp_structure_selection_enabled: bool = False
    vrp_default_structure: str = "CREDIT_SPREAD"
    vrp_sell_threshold: float = 0.03
    vrp_buy_threshold: float = -0.03
    vrp_ivp_guard_enabled: bool = False
    vrp_ivp_sell_floor: float = 50.0
    vrp_ivp_lookback_days: int = 250
    vrp_min_valid_observations: int = 60
    vrp_fetch_calendar_days: int = 400
    vrp_min_dte_days: int = 7
    vrp_realized_window: int = 21
    vrp_risk_free_rate: float = 0.065
    vrp_cache_dir: Path | None = None


class PairCreditTrader:
    """Daily virtual allocation, status, manual exit, and expiry exit engine."""

    def __init__(self, config: PairCreditConfig, telegram=None, adapter: OmniSpreadReadOnlyAdapter | None = None, shoonya_client=None):
        self.config = config
        self.telegram = telegram
        self.ledger = PairCreditLedger(config.state_file, config.ledger_file, config.capital)
        self.adapter = adapter or OmniSpreadReadOnlyAdapter(config.backend_path, shoonya_client=shoonya_client)
        self._vrp_provider = NsePairVolatilityProvider(
            config.vrp_cache_dir or config.state_file.parent / "pair_vrp_cache",
            ivp_lookback_days=config.vrp_ivp_lookback_days,
            min_valid_observations=config.vrp_min_valid_observations,
            fetch_calendar_days=config.vrp_fetch_calendar_days,
            min_dte_days=config.vrp_min_dte_days,
            realized_window=config.vrp_realized_window,
            risk_free_rate=config.vrp_risk_free_rate,
        )

    def send(self, message: str) -> None:
        if self.telegram:
            self.telegram.send_telegram(message)
        logger.info("TELEGRAM: %s", message.replace("\n", " | "))

    @staticmethod
    def _clean_pair_symbol(value: Any) -> str:
        symbol = str(value or "").upper().strip()
        return symbol.removesuffix(".NS").removesuffix(".BO")

    @classmethod
    def _candidate_symbols(cls, candidate: dict[str, Any]) -> set[str]:
        return {
            cls._clean_pair_symbol(candidate.get("x")),
            cls._clean_pair_symbol(candidate.get("y")),
        } - {""}

    @classmethod
    def _position_symbols(cls, position: dict[str, Any]) -> set[str]:
        symbols = {
            cls._clean_pair_symbol(position.get("x")),
            cls._clean_pair_symbol(position.get("y")),
        } - {""}
        if symbols:
            return symbols
        return {
            cls._clean_pair_symbol(leg.get("symbol"))
            for leg in position.get("legs", [])
            if cls._clean_pair_symbol(leg.get("symbol"))
        }

    def run_opening_allocation(self) -> dict[str, Any]:
        today = _date_key()
        if self.ledger.state.get("last_scan_date") == today:
            return {"status": "skipped", "reason": "opening scan already completed today"}

        self.ledger.set_last_scan_today()
        remaining = self.ledger.remaining_capital()
        opened: list[dict[str, Any]] = []
        insufficient: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        structured_candidates: list[dict[str, Any]] = []

        try:
            candidates = self.adapter.scan(
                preset=self.config.preset,
                period=self.config.period,
                interval=self.config.interval,
                top_n=self.config.top_n,
            )
        except Exception as exc:
            self.send(f"Pair-credit scan failed before open: {exc}")
            raise

        open_pairs = {p.get("pair") for p in self.ledger.open_positions()}
        used_symbols: set[str] = set()
        for position in self.ledger.open_positions():
            used_symbols.update(self._position_symbols(position))
        for candidate in candidates:
            pair = str(candidate.get("pair") or f"{candidate.get('x')}/{candidate.get('y')}")
            if pair in open_pairs:
                continue
            symbols = self._candidate_symbols(candidate)
            try:
                decision = self._structure_decision(candidate)
                if self.config.vrp_structure_selection_enabled:
                    structure = self.adapter.build_iv_expected_move_per_leg_structure(
                        candidate,
                        leg_structures=decision.leg_structures or {"x": "CREDIT_SPREAD", "y": "CREDIT_SPREAD"},
                        sell_iv_move=self.config.sell_iv_move,
                        hedge_max_iv_move=self.config.hedge_max_iv_move,
                    )
                elif self.config.leg_selection == "iv_expected_move":
                    structure = self.adapter.build_iv_expected_move_credit_structure(
                        candidate,
                        sell_iv_move=self.config.sell_iv_move,
                        hedge_max_iv_move=self.config.hedge_max_iv_move,
                    )
                else:
                    structure = self.adapter.build_credit_structure(
                        candidate,
                        strike_rule=self.config.strike_rule,
                        sold_sd=self.config.sold_sd,
                        hedge_sd=self.config.hedge_sd,
                    )
                structure["structure_type"] = decision.structure_type
                structure["leg_structure_types"] = decision.leg_structures
                structure["vrp_decision"] = decision.audit()
                vol_check = {"ok": True, "preferred_structure": "CREDIT_SPREAD", "legs": []}
                if self.config.vol_gate_enabled and decision.structure_type == "CREDIT_SPREAD":
                    vol_check = self.adapter.evaluate_credit_volatility(
                        structure,
                        min_ratio=self.config.iv_hv_min_ratio,
                        min_days=self.config.hv_min_lookback_days,
                        max_days=self.config.hv_max_lookback_days,
                        multiplier=self.config.hv_lookback_multiplier,
                    )
                    if not vol_check.get("ok"):
                        rejected.append({
                            "pair": pair,
                            "reason": vol_check.get("reason", "IV/HV credit gate rejected"),
                            "preferred_structure": vol_check.get("preferred_structure"),
                            "volatility": vol_check,
                        })
                        continue
                    structure["volatility"] = vol_check
                margin = float((structure.get("margin") or {}).get("estimated_margin") or 0)
                if margin <= 0:
                    rejected.append({"pair": pair, "reason": "non-positive margin estimate"})
                    continue
                structured_candidates.append({
                    "pair": pair,
                    "candidate": candidate,
                    "structure": structure,
                    "margin": margin,
                    "symbols": symbols,
                })
            except Exception as exc:
                logger.exception("Could not structure pair %s", pair)
                rejected.append({"pair": pair, "reason": str(exc)})

        structured_candidates.sort(key=lambda item: (item["margin"], item["pair"]))
        for item in structured_candidates:
            pair = item["pair"]
            margin = item["margin"]
            symbols = item["symbols"]
            repeated = sorted(symbols & used_symbols)
            if repeated:
                rejected.append({
                    "pair": pair,
                    "reason": "stock already allocated in another open pair",
                    "symbols": repeated,
                })
                continue
            try:
                if margin > remaining:
                    insufficient.append({"pair": pair, "margin": margin, "remaining": remaining})
                    continue
                position = self._position_from(item["candidate"], item["structure"], margin)
                self.ledger.add_position(position)
                open_pairs.add(pair)
                used_symbols.update(symbols)
                remaining -= margin
                opened.append(position)
                self.send(self._format_open_message(position, remaining))
            except Exception as exc:
                logger.exception("Could not allocate pair %s", pair)
                rejected.append({"pair": pair, "reason": str(exc)})

        if insufficient:
            top = insufficient[0]
            self.send(
                "Insufficient margin for additional pair-credit trades.\n"
                f"First unaffordable pair: {top['pair']}\n"
                f"Needed: Rs {top['margin']:,.2f} | Remaining: Rs {top['remaining']:,.2f}\n"
                "Continuing to monitor existing positions only."
            )
        if not opened and not insufficient:
            self.send("Opening scan complete: no new affordable NIFTY50 pair-credit entries.")
        return {"status": "completed", "opened": opened, "insufficient": insufficient, "rejected": rejected, "remaining": self.ledger.remaining_capital()}

    def _structure_decision(self, candidate: dict[str, Any]) -> StructureDecision:
        """Keep feature-off behavior exact: do not even invoke metrics then."""
        if not self.config.vrp_structure_selection_enabled:
            return select_pair_leg_structures(
                None, None, enabled=False, default_structure="CREDIT_SPREAD",
                credit_threshold=self.config.vrp_sell_threshold,
            )
        x = self._vrp_provider.metrics(str(candidate.get("x", "")))
        y = self._vrp_provider.metrics(str(candidate.get("y", "")))
        return select_pair_leg_structures(
            x, y, enabled=True, default_structure=self.config.vrp_default_structure,
            credit_threshold=self.config.vrp_sell_threshold,
        )

    def _position_from(self, candidate: dict[str, Any], structure: dict[str, Any], margin: float) -> dict[str, Any]:
        position_id = f"PCR-{_now_ist().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        legs = deepcopy(structure.get("legs") or [])
        net_credit = 0.0
        for leg in legs:
            qty = int(leg.get("lots", 0) or 0) * int(leg.get("lot_size", 0) or 0)
            leg["quantity"] = qty
            price = float(leg.get("price", 0) or 0)
            if leg.get("instrument") in {"CE", "PE"}:
                net_credit += price * qty * (1 if leg.get("side") == "SELL" else -1)
        expiries = [_parse_expiry(str(leg.get("expiry", ""))) for leg in legs]
        expiries = [e for e in expiries if e]
        return {
            "position_id": position_id,
            "status": "OPEN",
            "opened_at": _now_ist().isoformat(),
            "pair": structure.get("pair") or candidate.get("pair"),
            "x": candidate.get("x"),
            "y": candidate.get("y"),
            "qty": candidate.get("qty"),
            "direction": candidate.get("direction"),
            "method": candidate.get("method"),
            "z_score": candidate.get("z_score"),
            "hurst": candidate.get("hurst"),
            "prob_profit": candidate.get("prob_profit"),
            "half_life": candidate.get("half_life"),
            "x_lots": structure.get("x_lots"),
            "y_lots": structure.get("y_lots"),
            "actual_ratio": structure.get("actual_ratio"),
            "entry_margin": margin,
            "margin": structure.get("margin"),
            "entry_net_credit": round(net_credit, 2),
            "structure_type": structure.get("structure_type", "CREDIT_SPREAD"),
            "leg_structure_types": structure.get("leg_structure_types"),
            "vrp_decision": structure.get("vrp_decision"),
            "protection_iv_move": structure.get("protection_iv_move"),
            "leg_selection": structure.get("leg_selection"),
            "sell_iv_move": structure.get("sell_iv_move"),
            "hedge_max_iv_move": structure.get("hedge_max_iv_move"),
            "iv_leg_selection": structure.get("iv_leg_selection"),
            "strike_rule": structure.get("strike_rule"),
            "sold_sd": structure.get("sold_sd"),
            "hedge_sd": structure.get("hedge_sd"),
            "volatility": structure.get("volatility"),
            "earliest_expiry": min(expiries).isoformat() if expiries else "",
            "legs": legs,
        }

    def mark_position(self, position: dict[str, Any]) -> dict[str, Any]:
        total = 0.0
        legs = []
        data_ok = True
        for leg in position.get("legs", []):
            current = (
                self.adapter.latest_future_price(leg)
                if str(leg.get("instrument", "")).upper() == "FUT"
                else self.adapter.latest_option_price(leg)
            )
            entry = float(leg.get("price", 0) or 0)
            qty = int(leg.get("quantity", 0) or 0)
            if current is None:
                data_ok = False
                pnl = None
            else:
                sign = -1 if leg.get("side") == "SELL" else 1
                pnl = sign * (current - entry) * qty
                total += pnl
            legs.append({**leg, "current_price": current, "pnl": pnl})
        return {"data_ok": data_ok, "unrealized_pnl": round(total, 2) if data_ok else None, "legs": legs}

    def close_by_serial(self, serial: int, reason: str = "manual Telegram exit") -> dict[str, Any]:
        positions = self.ledger.open_positions()
        if serial < 1 or serial > len(positions):
            return {"ok": False, "error": f"Serial #{serial} not found"}
        position = positions[serial - 1]
        mark = self.mark_position(position)
        if not mark["data_ok"]:
            return {"ok": False, "error": f"Could not fetch current prices for #{serial}; exit not recorded"}
        closed = self.ledger.close_position(
            position["position_id"],
            {"closed_at": _now_ist().isoformat(), "close_reason": reason, "exit_legs": mark["legs"], "realized_pnl": mark["unrealized_pnl"]},
        )
        self.ledger.mark_manual_exit_today()
        return {"ok": True, "position": closed, "realized_pnl": mark["unrealized_pnl"]}

    def close_expired_positions(self) -> list[dict[str, Any]]:
        now = _now_ist()
        today = now.date()
        results = []
        for position in list(self.ledger.open_positions()):
            expiry = _parse_expiry(position.get("earliest_expiry", ""))
            if not expiry or today < expiry:
                continue
            if today == expiry and not is_pair_credit_expiry_close_time(now):
                continue
            mark = self.mark_position(position)
            if not mark["data_ok"]:
                settlement_legs = []
                settlement_pnl = 0.0
                settlement_ok = True
                for leg in position.get("legs", []):
                    price = self.adapter.expiry_settlement_price(leg, expiry)
                    if price is None:
                        settlement_ok = False
                        break
                    entry = float(leg.get("price", 0) or 0)
                    quantity = int(leg.get("quantity", 0) or 0)
                    sign = -1 if leg.get("side") == "SELL" else 1
                    pnl = sign * (price - entry) * quantity
                    settlement_pnl += pnl
                    settlement_legs.append({
                        **leg,
                        "current_price": price,
                        "pnl": round(pnl, 2),
                        "valuation": "expiry_intrinsic_settlement",
                    })
                if not settlement_ok:
                    key = f"expiry_pending:{position.get('position_id')}:{today.isoformat()}"
                    sent = self.ledger.state.setdefault("notifications_sent", {})
                    if sent.get(key):
                        continue
                    sent[key] = now.isoformat()
                    self.ledger.save()
                    results.append({"ok": False, "position": position, "error": "expiry settlement unavailable"})
                    continue
                mark = {
                    "data_ok": True,
                    "unrealized_pnl": round(settlement_pnl, 2),
                    "legs": settlement_legs,
                }
            closed = self.ledger.close_position(
                position["position_id"],
                {
                    "closed_at": _now_ist().isoformat(),
                    "close_reason": "automatic expiry settlement",
                    "exit_legs": mark["legs"],
                    "realized_pnl": mark["unrealized_pnl"],
                },
            )
            results.append({"ok": True, "position": closed, "realized_pnl": mark["unrealized_pnl"]})
        return results

    def status_message(self) -> str:
        open_positions = self.ledger.open_positions()
        remaining = self.ledger.remaining_capital()
        allocated = self.ledger.allocated_margin()
        lines = [
            "Pair-credit virtual portfolio",
            f"Capital: Rs {self.config.capital:,.2f}",
            f"Allocated margin: Rs {allocated:,.2f}",
            f"Unallocated: Rs {remaining:,.2f}",
            "",
            "Open Positions:",
        ]
        if not open_positions:
            lines.append("- None")
            return "\n".join(lines)
        total_pnl = 0.0
        total_ok = True
        marks_updated = False
        for idx, position in enumerate(open_positions, start=1):
            mark = self.mark_position(position)
            pnl = mark["unrealized_pnl"]
            if pnl is None:
                total_ok = False
                pnl_txt = "P&L unavailable"
            else:
                total_pnl += pnl
                pnl_txt = f"P&L Rs {pnl:+,.2f}"
                position["unrealized_pnl"] = pnl
                position["marked_at"] = _now_ist().isoformat()
                position["mark_legs"] = mark["legs"]
                marks_updated = True
            lines.append(
                f"{idx}. {position['pair']} | {position['direction']} | "
                f"margin Rs {float(position.get('entry_margin', 0)):,.2f} | "
                f"expiry {position.get('earliest_expiry', '?')} | {pnl_txt}"
            )
        if marks_updated:
            self.ledger.save()
        if total_ok:
            lines.extend(["", f"Total unrealized P&L: Rs {total_pnl:+,.2f}"])
        return "\n".join(lines)

    @staticmethod
    def _format_open_message(position: dict[str, Any], remaining: float) -> str:
        leg_lines = []
        for leg in position.get("legs", []):
            leg_lines.append(
                f"{leg['side']} {leg['symbol']} {leg['expiry']} "
                f"{leg['strike']:g}{leg['instrument']} x{leg['lots']} @ Rs {float(leg['price']):.2f}"
            )
        prob_profit = float(position.get("prob_profit") or 0)
        prob_text = f"{prob_profit:.1f}%" if prob_profit > 1 else f"{prob_profit:.1%}"
        return (
            f"PAIR {position.get('structure_type', 'CREDIT_SPREAD')} ENTRY [VIRTUAL]\n"
            f"{position['position_id']} | {position['pair']} | {position['direction']}\n"
            f"Prob profit: {prob_text} | "
            f"z: {float(position.get('z_score') or 0):+.2f} | "
            f"hurst: {float(position.get('hurst') or 0):.2f}\n"
            f"Margin: Rs {float(position.get('entry_margin', 0)):,.2f} | "
            f"Net option cashflow: Rs {float(position.get('entry_net_credit', 0)):,.2f} | "
            f"Unallocated left: Rs {remaining:,.2f}\n"
            + PairCreditTrader._format_volatility_line(position)
            + "\n".join(leg_lines)
        )

    @staticmethod
    def _format_volatility_line(position: dict[str, Any]) -> str:
        volatility = position.get("volatility") or {}
        legs = volatility.get("legs") or []
        if not legs:
            return ""
        parts = []
        for leg in legs:
            try:
                parts.append(
                    f"{leg.get('symbol')} {float(leg.get('iv')):.1f}/{float(leg.get('hv')):.1f} "
                    f"({float(leg.get('iv_hv_ratio')):.2f}x)"
                )
            except (TypeError, ValueError):
                continue
        return "IV/HV: " + "; ".join(parts) + "\n" if parts else ""


def make_pair_credit_trader_from_config(telegram=None, shoonya_client=None) -> PairCreditTrader:
    from config import (
        OMNISPREAD_BACKEND_PATH,
        PAIR_CREDIT_CAPITAL,
        PAIR_CREDIT_HEDGE_SD,
        PAIR_CREDIT_HEDGE_MAX_IV_MOVE,
        PAIR_CREDIT_INTERVAL,
        PAIR_CREDIT_LEG_SELECTION,
        PAIR_CREDIT_LEDGER_FILE,
        PAIR_CREDIT_PERIOD,
        PAIR_CREDIT_PRESET,
        PAIR_CREDIT_SELL_IV_MOVE,
        PAIR_CREDIT_SOLD_SD,
        PAIR_CREDIT_STATE_FILE,
        PAIR_CREDIT_STRIKE_RULE,
        PAIR_CREDIT_TOP_N,
        PAIR_CREDIT_VOL_GATE_ENABLED,
        PAIR_CREDIT_IV_HV_MIN_RATIO,
        PAIR_CREDIT_HV_LOOKBACK_MULTIPLIER,
        PAIR_CREDIT_HV_MIN_LOOKBACK_DAYS,
        PAIR_CREDIT_HV_MAX_LOOKBACK_DAYS,
        PAIR_VRP_STRUCTURE_SELECTION_ENABLED,
        PAIR_VRP_DEFAULT_STRUCTURE,
        PAIR_VRP_SELL_THRESHOLD,
        PAIR_VRP_BUY_THRESHOLD,
        PAIR_VRP_IVP_GUARD_ENABLED,
        PAIR_VRP_IVP_SELL_FLOOR,
        PAIR_VRP_IVP_LOOKBACK_DAYS,
        PAIR_VRP_MIN_VALID_OBSERVATIONS,
        PAIR_VRP_FETCH_CALENDAR_DAYS,
        PAIR_VRP_MIN_DTE_CALENDAR_DAYS,
        PAIR_VRP_REALIZED_VOL_WINDOW,
        PAIR_VRP_RISK_FREE_RATE,
        PAIR_VRP_CACHE_DIR,
    )

    cfg = PairCreditConfig(
        backend_path=OMNISPREAD_BACKEND_PATH,
        state_file=PAIR_CREDIT_STATE_FILE,
        ledger_file=PAIR_CREDIT_LEDGER_FILE,
        capital=PAIR_CREDIT_CAPITAL,
        preset=PAIR_CREDIT_PRESET,
        period=PAIR_CREDIT_PERIOD,
        interval=PAIR_CREDIT_INTERVAL,
        top_n=PAIR_CREDIT_TOP_N,
        leg_selection=PAIR_CREDIT_LEG_SELECTION,
        strike_rule=PAIR_CREDIT_STRIKE_RULE,
        sold_sd=PAIR_CREDIT_SOLD_SD,
        hedge_sd=PAIR_CREDIT_HEDGE_SD,
        sell_iv_move=PAIR_CREDIT_SELL_IV_MOVE,
        hedge_max_iv_move=PAIR_CREDIT_HEDGE_MAX_IV_MOVE,
        vol_gate_enabled=PAIR_CREDIT_VOL_GATE_ENABLED,
        iv_hv_min_ratio=PAIR_CREDIT_IV_HV_MIN_RATIO,
        hv_lookback_multiplier=PAIR_CREDIT_HV_LOOKBACK_MULTIPLIER,
        hv_min_lookback_days=PAIR_CREDIT_HV_MIN_LOOKBACK_DAYS,
        hv_max_lookback_days=PAIR_CREDIT_HV_MAX_LOOKBACK_DAYS,
        vrp_structure_selection_enabled=PAIR_VRP_STRUCTURE_SELECTION_ENABLED,
        vrp_default_structure=PAIR_VRP_DEFAULT_STRUCTURE,
        vrp_sell_threshold=PAIR_VRP_SELL_THRESHOLD,
        vrp_buy_threshold=PAIR_VRP_BUY_THRESHOLD,
        vrp_ivp_guard_enabled=PAIR_VRP_IVP_GUARD_ENABLED,
        vrp_ivp_sell_floor=PAIR_VRP_IVP_SELL_FLOOR,
        vrp_ivp_lookback_days=PAIR_VRP_IVP_LOOKBACK_DAYS,
        vrp_min_valid_observations=PAIR_VRP_MIN_VALID_OBSERVATIONS,
        vrp_fetch_calendar_days=PAIR_VRP_FETCH_CALENDAR_DAYS,
        vrp_min_dte_days=PAIR_VRP_MIN_DTE_CALENDAR_DAYS,
        vrp_realized_window=PAIR_VRP_REALIZED_VOL_WINDOW,
        vrp_risk_free_rate=PAIR_VRP_RISK_FREE_RATE,
        vrp_cache_dir=PAIR_VRP_CACHE_DIR,
    )
    return PairCreditTrader(cfg, telegram=telegram, shoonya_client=shoonya_client)
