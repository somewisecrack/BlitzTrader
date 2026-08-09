"""
NIFTY first-hour Portfolio A momentum engine.

Ranks current NIFTY50 equities by the first trading hour's return, then opens
Portfolio A: long the strongest names and short the weakest names. The engine
owns entry timing, state, P&L, trailing-stop exits, EOD exits, and Telegram
status text. It is virtual by default and only places Shoonya orders when the
NIFTY_FIRST_HOUR_MOMENTUM_LIVE_ORDERS switch is enabled.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import pytz

logger = logging.getLogger("BlitzTrader.NiftyFirstHourMomentum")
IST = pytz.timezone("Asia/Kolkata")

NIFTY50_SYMBOLS: tuple[str, ...] = (
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN",
    "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH",
    "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE",
    "SHRIRAMFIN", "SBIN", "SUNPHARMA", "TCS", "TATACONSUM", "TMPV",
    "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
)


@dataclass(frozen=True)
class NiftyFirstHourMomentumConfig:
    state_file: Path
    capital: float = 100000.0
    leverage: float = 3.0
    basket_size: int = 4
    trailing_stop_pct: float = 0.01
    entry_time: str = "10:16"
    eod_exit_time: str = "15:15"
    product_type: str = "I"
    live_orders: bool = False
    symbols: tuple[str, ...] = NIFTY50_SYMBOLS


def _now_ist() -> datetime:
    return datetime.now(IST)


def _date_key(now: datetime | None = None) -> str:
    return (now or _now_ist()).date().isoformat()


def _parse_hhmm(value: str) -> dtime:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return dtime(hour=hour, minute=minute)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class NiftyFirstHourMomentumTrader:
    """One-day virtual/live Portfolio A long-short trader for NIFTY equities."""

    def __init__(self, config: NiftyFirstHourMomentumConfig, shoonya_client, telegram=None):
        self.config = config
        self.client = shoonya_client
        self.telegram = telegram
        self.state = self._load_state()
        self._resolved: dict[str, Any] = {}

    def send(self, message: str) -> None:
        if self.telegram:
            self.telegram.send_telegram(message)
        logger.info("TELEGRAM: %s", message.replace("\n", " | "))

    def _load_state(self) -> dict[str, Any]:
        path = self.config.state_file
        if path.exists():
            try:
                data = json.loads(path.read_text())
                data.setdefault("open_positions", [])
                data.setdefault("closed_positions", [])
                return data
            except Exception:
                logger.exception("Could not load %s; starting with empty state", path)
        return {"open_positions": [], "closed_positions": [], "last_entry_date": ""}

    def save(self) -> None:
        self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.state_file.write_text(json.dumps(self.state, indent=2, sort_keys=True))

    def open_positions(self) -> list[dict[str, Any]]:
        return [p for p in self.state.get("open_positions", []) if p.get("status") == "OPEN"]

    def run_entry_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or _now_ist()
        if now.tzinfo is None:
            now = IST.localize(now)
        today = _date_key(now)
        if now.time() < _parse_hhmm(self.config.entry_time):
            return {"status": "skipped", "reason": "before entry time"}
        if now.time() >= _parse_hhmm(self.config.eod_exit_time):
            return {"status": "skipped", "reason": "after EOD exit time"}
        if self.state.get("last_entry_date") == today:
            return {"status": "skipped", "reason": "already attempted today"}
        if self.open_positions():
            self.state["last_entry_date"] = today
            self.save()
            return {"status": "skipped", "reason": "existing first-hour positions are open"}
        retry_after = self._entry_retry_after(now)
        if retry_after and now < retry_after:
            return {"status": "skipped", "reason": "waiting for retry"}

        ranked = self._rank_first_hour(now)
        if len(ranked) < self.config.basket_size * 2:
            self.state["next_entry_retry_at"] = (now + timedelta(seconds=60)).isoformat()
            if self.state.get("last_insufficient_data_notice_date") != today:
                self.state["last_insufficient_data_notice_date"] = today
                self.save()
                self.send(
                    "NIFTY first-hour momentum: not enough clean first-hour equity data "
                    f"({len(ranked)} usable symbols). Will retry silently."
                )
            return {"status": "skipped", "reason": "insufficient ranked symbols", "ranked": len(ranked)}

        winners = [item for item in ranked[-self.config.basket_size:]][::-1]
        losers = [item for item in ranked[:self.config.basket_size]]
        self.state.pop("next_entry_retry_at", None)
        opened: list[dict[str, Any]] = []
        errors: list[str] = []
        side_notional = self.config.capital * self.config.leverage * 0.5
        per_leg_notional = side_notional / self.config.basket_size

        for direction, basket in (("LONG", winners), ("SHORT", losers)):
            for item in basket:
                leg = self._open_leg(item, direction, per_leg_notional)
                if leg.get("ok"):
                    opened.append(leg["position"])
                else:
                    errors.append(f"{item['symbol']} {direction}: {leg.get('error')}")

        if opened:
            self.state.setdefault("open_positions", []).extend(opened)
            self.state["last_entry_date"] = today
            self.save()
            self.send(self._entry_message(opened, winners, losers, errors))
        else:
            self.state["last_entry_date"] = today
            self.save()
            self.send(
                "NIFTY first-hour momentum: ranked baskets were found, but no positions opened.\n"
                + "\n".join(errors[:8])
            )
        return {"status": "completed", "opened": opened, "errors": errors}

    def _rank_first_hour(self, now: datetime) -> list[dict[str, Any]]:
        day = now.date()
        start = IST.localize(datetime(day.year, day.month, day.day, 9, 15))
        end = IST.localize(datetime(day.year, day.month, day.day, 10, 15))
        ranked: list[dict[str, Any]] = []
        for symbol in self.config.symbols:
            resolved = self._resolve(symbol)
            if not resolved:
                continue
            candles = self.client.get_time_price_series(
                exchange="NSE",
                token=resolved.token,
                starttime=int(start.timestamp()),
                endtime=int(end.timestamp()),
                interval="60",
            )
            candle = self._first_candle(candles)
            if not candle:
                continue
            open_price = _float_or_none(candle.get("into"))
            close_price = _float_or_none(candle.get("intc"))
            if not open_price or not close_price or open_price <= 0:
                continue
            ranked.append({
                "symbol": symbol,
                "tradingsymbol": resolved.tradingsymbol,
                "token": resolved.token,
                "first_hour_return": close_price / open_price - 1.0,
                "first_hour_open": open_price,
                "first_hour_close": close_price,
            })
        ranked.sort(key=lambda item: item["first_hour_return"])
        return ranked

    @staticmethod
    def _first_candle(candles: Any) -> dict[str, Any] | None:
        if not candles or not isinstance(candles, list):
            return None
        usable = [c for c in candles if isinstance(c, dict) and c.get("into") and c.get("intc")]
        if not usable:
            return None
        return sorted(usable, key=NiftyFirstHourMomentumTrader._candle_sort_key)[0]

    @staticmethod
    def _candle_sort_key(candle: dict[str, Any]) -> datetime:
        raw = str(candle.get("time") or candle.get("ssboe") or "").strip()
        for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                pass
        try:
            return datetime.fromtimestamp(float(raw), IST).replace(tzinfo=None)
        except (TypeError, ValueError):
            return datetime.max

    def _entry_retry_after(self, now: datetime) -> datetime | None:
        raw = self.state.get("next_entry_retry_at")
        if not raw:
            return None
        try:
            retry_after = datetime.fromisoformat(str(raw))
        except ValueError:
            self.state.pop("next_entry_retry_at", None)
            self.save()
            return None
        if retry_after.tzinfo is None:
            retry_after = IST.localize(retry_after)
        return retry_after.astimezone(now.tzinfo or IST)

    def _resolve(self, symbol: str):
        cached = self._resolved.get(symbol)
        if cached:
            return cached
        resolved = self.client.resolve_equity_symbol(symbol)
        if resolved:
            self._resolved[symbol] = resolved
        return resolved

    def _quote_price(self, token: str) -> float | None:
        quote = self.client.get_quotes("NSE", token) or {}
        return _float_or_none(quote.get("lp") or quote.get("c"))

    def _open_leg(self, item: dict[str, Any], direction: str, notional: float) -> dict[str, Any]:
        entry_price = self._quote_price(item["token"])
        if not entry_price or entry_price <= 0:
            return {"ok": False, "error": "entry quote unavailable"}
        quantity = int(notional // entry_price)
        if quantity <= 0:
            return {"ok": False, "error": f"notional too small for price Rs {entry_price:.2f}"}

        order_response = None
        if self.config.live_orders:
            side = "BUY" if direction == "LONG" else "SELL"
            order_response = self.client.place_order(
                buy_or_sell=side,
                product_type=self.config.product_type,
                exchange="NSE",
                tradingsymbol=item["tradingsymbol"],
                quantity=quantity,
                price_type="MKT",
                price=0.0,
                remarks="NIFTY_FIRST_HOUR_MOMENTUM_A",
            )
            if not order_response or order_response.get("stat") != "Ok":
                return {"ok": False, "error": f"broker order rejected: {order_response}"}

        position = {
            "position_id": f"NFH-{_now_ist().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}",
            "status": "OPEN",
            "opened_at": _now_ist().isoformat(),
            "strategy": "NIFTY_FIRST_HOUR_MOMENTUM_A",
            "symbol": item["symbol"],
            "tradingsymbol": item["tradingsymbol"],
            "exchange": "NSE",
            "token": item["token"],
            "direction": direction,
            "quantity": quantity,
            "entry_price": round(entry_price, 2),
            "first_hour_return": item["first_hour_return"],
            "notional": round(entry_price * quantity, 2),
            "best_price": round(entry_price, 2),
            "trailing_stop_pct": self.config.trailing_stop_pct,
            "trailing_stop": round(
                entry_price * (1.0 - self.config.trailing_stop_pct)
                if direction == "LONG"
                else entry_price * (1.0 + self.config.trailing_stop_pct),
                2,
            ),
            "live_order": bool(self.config.live_orders),
            "entry_order_response": order_response,
        }
        return {"ok": True, "position": position}

    def check_trailing_stops(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or _now_ist()
        exits: list[dict[str, Any]] = []
        updated = False
        for pos in list(self.open_positions()):
            current = self._quote_price(pos["token"])
            if not current or current <= 0:
                continue
            direction = pos["direction"]
            trail = float(pos.get("trailing_stop_pct") or self.config.trailing_stop_pct)
            if direction == "LONG":
                if current > float(pos.get("best_price") or pos["entry_price"]):
                    pos["best_price"] = round(current, 2)
                    pos["trailing_stop"] = round(current * (1.0 - trail), 2)
                    updated = True
                if current <= float(pos.get("trailing_stop") or 0):
                    exits.append(self.close_position(pos["position_id"], "1% trailing stop"))
            else:
                if current < float(pos.get("best_price") or pos["entry_price"]):
                    pos["best_price"] = round(current, 2)
                    pos["trailing_stop"] = round(current * (1.0 + trail), 2)
                    updated = True
                if current >= float(pos.get("trailing_stop") or 0):
                    exits.append(self.close_position(pos["position_id"], "1% trailing stop"))
        if updated or exits:
            self.save()
        return exits

    def close_all(self, reason: str = "manual Telegram exit") -> list[dict[str, Any]]:
        return [self.close_position(pos["position_id"], reason) for pos in list(self.open_positions())]

    def close_by_serial(self, serial: int, reason: str = "manual Telegram exit") -> dict[str, Any]:
        positions = self.open_positions()
        if serial < 1 or serial > len(positions):
            return {"ok": False, "error": f"Serial #{serial} not found"}
        return self.close_position(positions[serial - 1]["position_id"], reason)

    def close_position(self, position_id: str, reason: str) -> dict[str, Any]:
        positions = self.state.setdefault("open_positions", [])
        for idx, pos in enumerate(positions):
            if pos.get("position_id") != position_id or pos.get("status") != "OPEN":
                continue
            current = self._quote_price(pos["token"])
            if not current or current <= 0:
                return {"ok": False, "error": f"quote unavailable for {pos.get('symbol')}", "position": pos}

            order_response = None
            if self.config.live_orders:
                exit_side = "SELL" if pos["direction"] == "LONG" else "BUY"
                order_response = self.client.place_order(
                    buy_or_sell=exit_side,
                    product_type=self.config.product_type,
                    exchange=pos["exchange"],
                    tradingsymbol=pos["tradingsymbol"],
                    quantity=int(pos["quantity"]),
                    price_type="MKT",
                    price=0.0,
                    remarks="NIFTY_FIRST_HOUR_MOMENTUM_A_EXIT",
                )
                if not order_response or order_response.get("stat") != "Ok":
                    return {"ok": False, "error": f"exit order rejected: {order_response}", "position": pos}

            pnl = self._position_pnl(pos, current)
            closed = {
                **pos,
                "status": "CLOSED",
                "closed_at": _now_ist().isoformat(),
                "close_reason": reason,
                "exit_price": round(current, 2),
                "realized_pnl": round(pnl, 2),
                "exit_order_response": order_response,
            }
            positions.pop(idx)
            self.state.setdefault("closed_positions", []).append(closed)
            self.save()
            return {"ok": True, "position": closed, "realized_pnl": pnl}
        return {"ok": False, "error": f"position_id {position_id} not found"}

    def close_for_eod_if_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or _now_ist()
        if now.time() < _parse_hhmm(self.config.eod_exit_time):
            return []
        return self.close_all("EOD forced exit")

    def status_message(self) -> str:
        positions = self.open_positions()
        lines = [
            "NIFTY first-hour momentum A",
            f"Capital: Rs {self.config.capital:,.2f} | Gross {self.config.leverage:.1f}x | Size {self.config.basket_size}+{self.config.basket_size}",
            f"Mode: {'LIVE BROKER ORDERS' if self.config.live_orders else 'VIRTUAL'} | Trail: {self.config.trailing_stop_pct:.1%}",
            "",
            "Open Positions:",
        ]
        if not positions:
            lines.append("- None")
            return "\n".join(lines)
        total = 0.0
        total_ok = True
        for idx, pos in enumerate(positions, start=1):
            current = self._quote_price(pos["token"])
            pnl_txt = "P&L unavailable"
            if current and current > 0:
                pnl = self._position_pnl(pos, current)
                total += pnl
                pnl_txt = f"P&L Rs {pnl:+,.2f} @ Rs {current:.2f}"
            else:
                total_ok = False
            lines.append(
                f"{idx}. {pos['direction']} {pos['tradingsymbol']} x{pos['quantity']} | "
                f"entry Rs {float(pos['entry_price']):.2f} | trail Rs {float(pos.get('trailing_stop') or 0):.2f} | {pnl_txt}"
            )
        if total_ok:
            lines.extend(["", f"Total unrealized P&L: Rs {total:+,.2f}"])
        else:
            lines.extend(["", "Total unrealized P&L unavailable: one or more leg quotes failed."])
        return "\n".join(lines)

    @staticmethod
    def _position_pnl(pos: dict[str, Any], current: float) -> float:
        entry = float(pos.get("entry_price") or 0)
        qty = int(pos.get("quantity") or 0)
        if pos.get("direction") == "SHORT":
            return (entry - current) * qty
        return (current - entry) * qty

    def _entry_message(
        self,
        opened: list[dict[str, Any]],
        winners: list[dict[str, Any]],
        losers: list[dict[str, Any]],
        errors: list[str],
    ) -> str:
        lines = [
            "NIFTY first-hour momentum A triggered",
            f"Capital Rs {self.config.capital:,.0f} | gross {self.config.leverage:.1f}x | virtual={not self.config.live_orders}",
            "",
            "Long first-hour winners:",
        ]
        for item in winners:
            lines.append(f"- {item['symbol']}: {item['first_hour_return'] * 100:+.2f}%")
        lines.append("")
        lines.append("Short first-hour losers:")
        for item in losers:
            lines.append(f"- {item['symbol']}: {item['first_hour_return'] * 100:+.2f}%")
        lines.append("")
        lines.append("Opened legs:")
        for pos in opened:
            lines.append(
                f"- {pos['direction']} {pos['tradingsymbol']} x{pos['quantity']} "
                f"@ Rs {float(pos['entry_price']):.2f} | trail Rs {float(pos['trailing_stop']):.2f}"
            )
        if errors:
            lines.extend(["", "Skipped/errors:"])
            lines.extend(f"- {err}" for err in errors[:8])
        lines.extend(["", "Commands: momentum status, momentum pnl, exit momentum"])
        return "\n".join(lines)


def make_nifty_first_hour_momentum_trader_from_config(telegram=None, shoonya_client=None):
    from config import (
        NIFTY_FIRST_HOUR_MOMENTUM_LIVE_ORDERS,
        NIFTY_FIRST_HOUR_MOMENTUM_CAPITAL,
        NIFTY_FIRST_HOUR_MOMENTUM_EOD_EXIT_TIME,
        NIFTY_FIRST_HOUR_MOMENTUM_ENTRY_TIME,
        NIFTY_FIRST_HOUR_MOMENTUM_LEVERAGE,
        NIFTY_FIRST_HOUR_MOMENTUM_PRODUCT_TYPE,
        NIFTY_FIRST_HOUR_MOMENTUM_SIZE,
        NIFTY_FIRST_HOUR_MOMENTUM_STATE_FILE,
        NIFTY_FIRST_HOUR_MOMENTUM_TRAILING_STOP_PCT,
    )

    config = NiftyFirstHourMomentumConfig(
        state_file=NIFTY_FIRST_HOUR_MOMENTUM_STATE_FILE,
        capital=NIFTY_FIRST_HOUR_MOMENTUM_CAPITAL,
        leverage=NIFTY_FIRST_HOUR_MOMENTUM_LEVERAGE,
        basket_size=NIFTY_FIRST_HOUR_MOMENTUM_SIZE,
        trailing_stop_pct=NIFTY_FIRST_HOUR_MOMENTUM_TRAILING_STOP_PCT,
        entry_time=NIFTY_FIRST_HOUR_MOMENTUM_ENTRY_TIME,
        eod_exit_time=NIFTY_FIRST_HOUR_MOMENTUM_EOD_EXIT_TIME,
        product_type=NIFTY_FIRST_HOUR_MOMENTUM_PRODUCT_TYPE,
        live_orders=NIFTY_FIRST_HOUR_MOMENTUM_LIVE_ORDERS,
    )
    return NiftyFirstHourMomentumTrader(config, shoonya_client=shoonya_client, telegram=telegram)
