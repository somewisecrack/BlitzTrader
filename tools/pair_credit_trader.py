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
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytz

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

    def __init__(self, backend_path: Path):
        self.backend_path = Path(backend_path)
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self.backend_path.exists():
            raise RuntimeError(f"OmniSpread backend not found: {self.backend_path}")
        if str(self.backend_path) not in sys.path:
            sys.path.insert(0, str(self.backend_path))
        from derivatives_backtest import _clean, build_credit_spread_structure, instrument_types
        from engine import OmniSpreadEngine
        from nse_client import fetch_future, fetch_option
        from presets import PRESETS

        self._clean = _clean
        self._build_credit_spread_structure = build_credit_spread_structure
        self._instrument_types = instrument_types
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
        return self._build_credit_spread_structure(
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

    def latest_option_price(self, leg: dict[str, Any]) -> float | None:
        self._ensure_loaded()
        symbol = str(leg["symbol"])
        option_type = str(leg["instrument"]).upper()
        expiry = _parse_expiry(str(leg["expiry"]))
        if not expiry:
            return None
        strike = float(leg["strike"])
        _, option_instrument = self._instrument_types(symbol)
        end = _now_ist()
        start = end - timedelta(days=21)
        raw = self._fetch_option(
            symbol=symbol,
            instrument=option_instrument,
            option_type=None,
            from_date=start.strftime("%d-%m-%Y"),
            to_date=end.strftime("%d-%m-%Y"),
        )
        import pandas as pd

        df = self._clean(raw)
        if df.empty:
            return None
        rows = df[(df["expiry"] == pd.Timestamp(expiry)) & (df["OPTION_TYPE"] == option_type) & (df["STRIKE_PRICE"] == strike)]
        if rows.empty:
            return None
        latest = rows.sort_values("date").iloc[-1]
        price = float(latest["CLOSING_PRICE"])
        return price if math.isfinite(price) and price >= 0 else None


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
    strike_rule: str = "vol"
    sold_sd: float = 1.0
    hedge_sd: float = 2.5


class PairCreditTrader:
    """Daily virtual allocation, status, manual exit, and expiry exit engine."""

    def __init__(self, config: PairCreditConfig, telegram=None, adapter: OmniSpreadReadOnlyAdapter | None = None):
        self.config = config
        self.telegram = telegram
        self.ledger = PairCreditLedger(config.state_file, config.ledger_file, config.capital)
        self.adapter = adapter or OmniSpreadReadOnlyAdapter(config.backend_path)

    def send(self, message: str) -> None:
        if self.telegram:
            self.telegram.send_telegram(message)
        logger.info("TELEGRAM: %s", message.replace("\n", " | "))

    def run_opening_allocation(self) -> dict[str, Any]:
        today = _date_key()
        if self.ledger.state.get("last_scan_date") == today:
            return {"status": "skipped", "reason": "opening scan already completed today"}

        self.ledger.set_last_scan_today()
        remaining = self.ledger.remaining_capital()
        opened: list[dict[str, Any]] = []
        insufficient: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

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
        for candidate in candidates:
            pair = str(candidate.get("pair") or f"{candidate.get('x')}/{candidate.get('y')}")
            if pair in open_pairs:
                continue
            try:
                structure = self.adapter.build_credit_structure(
                    candidate,
                    strike_rule=self.config.strike_rule,
                    sold_sd=self.config.sold_sd,
                    hedge_sd=self.config.hedge_sd,
                )
                margin = float((structure.get("margin") or {}).get("estimated_margin") or 0)
                if margin <= 0:
                    rejected.append({"pair": pair, "reason": "non-positive margin estimate"})
                    continue
                if margin > remaining:
                    insufficient.append({"pair": pair, "margin": margin, "remaining": remaining})
                    continue
                position = self._position_from(candidate, structure, margin)
                self.ledger.add_position(position)
                open_pairs.add(pair)
                remaining -= margin
                opened.append(position)
                self.send(self._format_open_message(position, remaining))
            except Exception as exc:
                logger.exception("Could not structure pair %s", pair)
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

    def _position_from(self, candidate: dict[str, Any], structure: dict[str, Any], margin: float) -> dict[str, Any]:
        position_id = f"PCR-{_now_ist().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        legs = deepcopy(structure.get("legs") or [])
        net_credit = 0.0
        for leg in legs:
            qty = int(leg.get("lots", 0) or 0) * int(leg.get("lot_size", 0) or 0)
            leg["quantity"] = qty
            price = float(leg.get("price", 0) or 0)
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
            "earliest_expiry": min(expiries).isoformat() if expiries else "",
            "legs": legs,
        }

    def mark_position(self, position: dict[str, Any]) -> dict[str, Any]:
        total = 0.0
        legs = []
        data_ok = True
        for leg in position.get("legs", []):
            current = self.adapter.latest_option_price(leg)
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
        today = _now_ist().date()
        results = []
        for position in list(self.ledger.open_positions()):
            expiry = _parse_expiry(position.get("earliest_expiry", ""))
            if not expiry or today < expiry:
                continue
            mark = self.mark_position(position)
            if not mark["data_ok"]:
                results.append({"ok": False, "position": position, "error": "expiry prices unavailable"})
                continue
            closed = self.ledger.close_position(
                position["position_id"],
                {"closed_at": _now_ist().isoformat(), "close_reason": "automatic expiry exit", "exit_legs": mark["legs"], "realized_pnl": mark["unrealized_pnl"]},
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
        for idx, position in enumerate(open_positions, start=1):
            mark = self.mark_position(position)
            pnl = mark["unrealized_pnl"]
            if pnl is None:
                total_ok = False
                pnl_txt = "P&L unavailable"
            else:
                total_pnl += pnl
                pnl_txt = f"P&L Rs {pnl:+,.2f}"
            lines.append(
                f"{idx}. {position['pair']} | {position['direction']} | "
                f"margin Rs {float(position.get('entry_margin', 0)):,.2f} | "
                f"expiry {position.get('earliest_expiry', '?')} | {pnl_txt}"
            )
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
        return (
            "PAIR CREDIT ENTRY [VIRTUAL]\n"
            f"{position['position_id']} | {position['pair']} | {position['direction']}\n"
            f"Prob profit: {float(position.get('prob_profit') or 0):.1%} | "
            f"z: {float(position.get('z_score') or 0):+.2f} | "
            f"hurst: {float(position.get('hurst') or 0):.2f}\n"
            f"Margin: Rs {float(position.get('entry_margin', 0)):,.2f} | "
            f"Net credit: Rs {float(position.get('entry_net_credit', 0)):,.2f} | "
            f"Unallocated left: Rs {remaining:,.2f}\n"
            + "\n".join(leg_lines)
        )


def make_pair_credit_trader_from_config(telegram=None) -> PairCreditTrader:
    from config import (
        OMNISPREAD_BACKEND_PATH,
        PAIR_CREDIT_CAPITAL,
        PAIR_CREDIT_HEDGE_SD,
        PAIR_CREDIT_INTERVAL,
        PAIR_CREDIT_LEDGER_FILE,
        PAIR_CREDIT_PERIOD,
        PAIR_CREDIT_PRESET,
        PAIR_CREDIT_SOLD_SD,
        PAIR_CREDIT_STATE_FILE,
        PAIR_CREDIT_STRIKE_RULE,
        PAIR_CREDIT_TOP_N,
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
        strike_rule=PAIR_CREDIT_STRIKE_RULE,
        sold_sd=PAIR_CREDIT_SOLD_SD,
        hedge_sd=PAIR_CREDIT_HEDGE_SD,
    )
    return PairCreditTrader(cfg, telegram=telegram)
