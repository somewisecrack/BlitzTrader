from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from config import (
    JOURNALS_DIR,
    PAIRS_BASE_CAPITAL,
    PAIRS_GROSS_CAPITAL,
    PAIRS_EXCHANGE,
    PAIRS_PRODUCT,
    PAIRS_STATE_FILE,
)
from broker.shoonya_client import ResolvedScrip, ShoonyaClient
from pairs.scanner import PairCandidate

logger = logging.getLogger("BlitzTrader.PairsPortfolio")


@dataclass
class Leg:
    symbol: str
    tradingsymbol: str
    token: str
    side: str
    qty: int
    entry_price: float
    exit_price: float | None = None
    stop_price: float | None = None
    stop_armed_at_profit_pct: float | None = None
    realized_pnl: float | None = None
    closed_at: str | None = None


@dataclass
class PairPosition:
    pair_name: str
    timeframe: str
    method: str
    z_score: float
    beta: float
    prob_profit: float
    prob_profit_low: float
    prob_profit_high: float
    long_leg: Leg
    short_leg: Leg
    margin_used: float
    capital_reserved: float
    opened_at: str
    closed_at: str | None = None
    pnl: float | None = None
    matched_timeframes: list[str] = field(default_factory=list)
    half_life: int = 0


class PairPortfolio:
    def __init__(self, state_file: Path = PAIRS_STATE_FILE):
        self._state_file = state_file
        self.capital = PAIRS_GROSS_CAPITAL  # gross deployable capital (base * leverage)
        self.positions: list[PairPosition] = []

    @staticmethod
    def _rank_candidates(candidates: list[PairCandidate]) -> list[PairCandidate]:
        """Rank by prob_profit desc, abs(z_score) desc, half_life asc (deterministic)."""
        return sorted(
            candidates,
            key=lambda c: (-c.prob_profit, -abs(c.z_score), c.half_life),
        )

    @staticmethod
    def _deduplicate_unordered(candidates: list[PairCandidate]) -> list[PairCandidate]:
        """Keep only the better-ranked candidate for each unordered pair (A/B == B/A).

        Candidates must be pre-ranked; first occurrence wins (it is the better rank).
        """
        seen: set[tuple[str, str]] = set()
        deduped: list[PairCandidate] = []
        for c in candidates:
            key = tuple(sorted((c.x_symbol, c.y_symbol)))
            if key not in seen:
                seen.add(key)
                deduped.append(c)
        return deduped

    @staticmethod
    def _apply_concentration_filter(
        candidates: list[PairCandidate], max_per_stock: int = 2
    ) -> list[PairCandidate]:
        """Greedy selection: no ticker may appear in more than max_per_stock final pairs."""
        usage: dict[str, int] = {}
        selected: list[PairCandidate] = []
        for c in candidates:
            x_count = usage.get(c.x_symbol, 0)
            y_count = usage.get(c.y_symbol, 0)
            if x_count < max_per_stock and y_count < max_per_stock:
                selected.append(c)
                usage[c.x_symbol] = x_count + 1
                usage[c.y_symbol] = y_count + 1
        return selected

    def allocate_and_open(
        self, client: ShoonyaClient, candidates: list[PairCandidate]
    ) -> list[PairPosition]:
        if not candidates:
            logger.warning("No candidates to allocate")
            return []

        # 1. Rank deterministically
        ranked = self._rank_candidates(candidates)
        # 2. Deduplicate unordered pairs (INFY/SBIN == SBIN/INFY — keep better-ranked)
        deduped = self._deduplicate_unordered(ranked)
        # 3. Stock concentration filter: no ticker in > 2 final selected pairs
        selected = self._apply_concentration_filter(deduped, max_per_stock=2)

        if not selected:
            logger.warning("No pairs survived ranking/dedup/concentration filters")
            return []

        # 4. Dynamic per-pair allocation: never exceed gross capital
        per_pair_capital = float(PAIRS_GROSS_CAPITAL / len(selected))
        logger.info(
            "Selected %d pairs after filters; per-pair gross = ₹%,.0f",
            len(selected),
            per_pair_capital,
        )

        opened: list[PairPosition] = []
        for candidate in selected:
            pos = self._open_candidate(client, candidate, per_pair_capital)
            if pos:
                opened.append(pos)
        self.positions = opened
        self._persist()
        self._write_journal(opened)
        return opened

    def monitor_open_positions(self, client: ShoonyaClient) -> list[dict]:
        """
        Check each open leg for trailing-stop arms, raises, and hits.
        Returns a list of event dicts; caller is responsible for Telegram.
        """
        events: list[dict] = []
        changed = False
        for pos in self.positions:
            if pos.closed_at:
                continue
            for leg_name, exit_action in (("long_leg", "SELL"), ("short_leg", "BUY")):
                leg = getattr(pos, leg_name)
                if leg.closed_at:
                    continue
                scrip = ResolvedScrip(leg.symbol, leg.tradingsymbol, leg.token)
                exit_price = self._entry_price(client, scrip, exit_action)
                if exit_price is None:
                    continue
                profit_pct = self._profit_pct(leg, exit_price)
                if profit_pct > 1.0:
                    new_stop = self._trailing_stop_price(leg, profit_pct)
                    if leg.stop_price is None:
                        leg.stop_armed_at_profit_pct = round(profit_pct, 3)
                        leg.stop_price = new_stop
                        changed = True
                        events.append({
                            "type": "STOP_ARMED",
                            "pair": pos.pair_name,
                            "leg": leg.symbol,
                            "profit_pct": round(profit_pct, 3),
                            "stop_price": leg.stop_price,
                        })
                    elif self._should_raise_stop(leg, new_stop):
                        leg.stop_price = new_stop
                        changed = True
                        events.append({
                            "type": "STOP_MOVED",
                            "pair": pos.pair_name,
                            "leg": leg.symbol,
                            "profit_pct": round(profit_pct, 3),
                            "stop_price": leg.stop_price,
                        })
                if leg.stop_price is not None and self._stop_hit(leg, exit_price):
                    realized = self._close_leg(leg, exit_price)
                    changed = True
                    events.append({
                        "type": "LEG_EXIT",
                        "pair": pos.pair_name,
                        "leg": leg.symbol,
                        "exit_price": exit_price,
                        "pnl": realized,
                    })
            if pos.long_leg.closed_at and pos.short_leg.closed_at and not pos.closed_at:
                pos.closed_at = datetime.now().isoformat()
                pos.pnl = round(
                    (pos.long_leg.realized_pnl or 0.0) + (pos.short_leg.realized_pnl or 0.0), 2
                )
                events.append({"type": "PAIR_CLOSED", "pair": pos.pair_name, "pnl": pos.pnl})
                changed = True
        if changed:
            self._persist()
            self._append_monitor_journal(events)
        return events

    def close_all(self, client: ShoonyaClient) -> dict:
        total_pnl = 0.0
        closed = []
        for pos in self.positions:
            pair_pnl = 0.0
            if not pos.long_leg.closed_at:
                long_exit = self._entry_price(
                    client,
                    ResolvedScrip(pos.long_leg.symbol, pos.long_leg.tradingsymbol, pos.long_leg.token),
                    "SELL",
                )
                if long_exit is not None:
                    pair_pnl += self._close_leg(pos.long_leg, long_exit)
            else:
                pair_pnl += pos.long_leg.realized_pnl or 0.0
            if not pos.short_leg.closed_at:
                short_exit = self._entry_price(
                    client,
                    ResolvedScrip(pos.short_leg.symbol, pos.short_leg.tradingsymbol, pos.short_leg.token),
                    "BUY",
                )
                if short_exit is not None:
                    pair_pnl += self._close_leg(pos.short_leg, short_exit)
            else:
                pair_pnl += pos.short_leg.realized_pnl or 0.0
            if not pos.long_leg.closed_at or not pos.short_leg.closed_at:
                continue
            pos.closed_at = datetime.now().isoformat()
            pos.pnl = round(pair_pnl, 2)
            total_pnl += pos.pnl
            closed.append(pos)
            logger.info("CLOSE %s pnl=%.2f", pos.pair_name, pos.pnl)
        self._persist()
        self._append_eod_journal(closed, total_pnl)
        return {"closed": len(closed), "total_pnl": round(total_pnl, 2)}

    def get_status(self, client: ShoonyaClient | None = None) -> dict:
        total_realized = 0.0
        total_unrealized = 0.0
        positions: list[dict] = []
        for pos in self.positions:
            pair_realized = (pos.long_leg.realized_pnl or 0.0) + (pos.short_leg.realized_pnl or 0.0)
            pair_unrealized = 0.0
            for leg_name, exit_action in (("long_leg", "SELL"), ("short_leg", "BUY")):
                leg = getattr(pos, leg_name)
                if leg.closed_at:
                    continue
                price = None
                if client:
                    price = self._entry_price(
                        client,
                        ResolvedScrip(leg.symbol, leg.tradingsymbol, leg.token),
                        exit_action,
                    )
                if price is None:
                    continue
                pair_unrealized += self._leg_pnl(leg, price)
            total_realized += pair_realized
            total_unrealized += pair_unrealized
            positions.append({
                "pair": pos.pair_name,
                "timeframe": pos.timeframe,
                "realized_pnl": round(pair_realized, 2),
                "unrealized_pnl": round(pair_unrealized, 2),
                "closed": bool(pos.closed_at),
            })
        return {
            "capital": self.capital,
            "realized_pnl": round(total_realized, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "net_pnl": round(total_realized + total_unrealized, 2),
            "positions": positions,
            "open_pairs": sum(1 for p in self.positions if not p.closed_at),
        }

    # ──────────────────────────────────────────────────────────
    #   INTERNALS
    # ──────────────────────────────────────────────────────────

    def _open_candidate(
        self,
        client: ShoonyaClient,
        candidate: PairCandidate,
        pair_capital: float,
    ) -> PairPosition | None:
        x = client.resolve_equity_symbol(candidate.x_symbol)
        y = client.resolve_equity_symbol(candidate.y_symbol)
        if not x or not y:
            logger.warning(
                "Skipping %s/%s: could not resolve Shoonya tokens",
                candidate.x_symbol,
                candidate.y_symbol,
            )
            return None

        if candidate.direction == "SHORT_SPREAD":
            long_scrip, short_scrip = x, y
            long_weight, short_weight = candidate.beta, 1.0
        else:
            long_scrip, short_scrip = y, x
            long_weight, short_weight = 1.0, candidate.beta

        long_entry = self._entry_price(client, long_scrip, "BUY")
        short_entry = self._entry_price(client, short_scrip, "SELL")
        if long_entry is None or short_entry is None:
            logger.warning("Skipping %s/%s: missing entry prices", candidate.x_symbol, candidate.y_symbol)
            return None

        long_margin_1 = self._margin_per_share(client, long_scrip, long_entry, "B")
        short_margin_1 = self._margin_per_share(client, short_scrip, short_entry, "S")
        if long_margin_1 is None or short_margin_1 is None:
            logger.warning(
                "Skipping %s/%s: margin lookup failed — skipping pair",
                candidate.x_symbol,
                candidate.y_symbol,
            )
            return None

        base_margin = long_weight * long_margin_1 + short_weight * short_margin_1
        if base_margin <= 0:
            return None
        scale = max(1, int(pair_capital // base_margin))
        long_qty = max(1, int(math.floor(long_weight * scale)))
        short_qty = max(1, int(math.floor(short_weight * scale)))
        used_margin = (long_qty * long_margin_1) + (short_qty * short_margin_1)

        position = PairPosition(
            pair_name=f"{candidate.x_symbol}/{candidate.y_symbol}",
            timeframe=candidate.timeframe,
            method=candidate.method,
            z_score=candidate.z_score,
            beta=round(candidate.beta, 4),
            prob_profit=round(candidate.prob_profit, 1),
            prob_profit_low=round(candidate.prob_profit_low, 1),
            prob_profit_high=round(candidate.prob_profit_high, 1),
            long_leg=Leg(
                symbol=long_scrip.symbol,
                tradingsymbol=long_scrip.tradingsymbol,
                token=long_scrip.token,
                side="BUY",
                qty=long_qty,
                entry_price=long_entry,
            ),
            short_leg=Leg(
                symbol=short_scrip.symbol,
                tradingsymbol=short_scrip.tradingsymbol,
                token=short_scrip.token,
                side="SELL",
                qty=short_qty,
                entry_price=short_entry,
            ),
            margin_used=round(used_margin, 2),
            capital_reserved=round(pair_capital, 2),
            opened_at=datetime.now().isoformat(),
            matched_timeframes=sorted(candidate.matched_timeframes),
            half_life=candidate.half_life,
        )
        logger.info(
            "OPEN %s long=%s x%s @ %.2f short=%s x%s @ %.2f margin=%.2f",
            position.pair_name,
            position.long_leg.symbol,
            position.long_leg.qty,
            position.long_leg.entry_price,
            position.short_leg.symbol,
            position.short_leg.qty,
            position.short_leg.entry_price,
            position.margin_used,
        )
        return position

    def _entry_price(self, client: ShoonyaClient, scrip: ResolvedScrip, action: str) -> float | None:
        book = client.get_best_bid_ask(PAIRS_EXCHANGE, scrip.token)
        if not book:
            last = client.get_last_price(PAIRS_EXCHANGE, scrip.token)
            return round(last, 2) if last is not None else None
        bid, ask = book
        return round(ask if action == "BUY" else bid, 2)

    def _margin_per_share(
        self,
        client: ShoonyaClient,
        scrip: ResolvedScrip,
        price: float,
        side: str,
    ) -> float | None:
        resp = client.get_order_margin(
            exchange=PAIRS_EXCHANGE,
            tradingsymbol=scrip.tradingsymbol,
            quantity=1,
            price=price,
            transaction_type=side,
            product=PAIRS_PRODUCT,
            price_type="LMT",
        )
        if not resp or resp.get("stat") != "Ok":
            return None
        raw = resp.get("ordermargin") or resp.get("margin") or resp.get("span")
        try:
            return float(raw)
        except Exception:
            return None

    @staticmethod
    def _leg_pnl(leg: Leg, exit_price: float) -> float:
        if leg.side == "BUY":
            return round((exit_price - leg.entry_price) * leg.qty, 2)
        return round((leg.entry_price - exit_price) * leg.qty, 2)

    def _profit_pct(self, leg: Leg, exit_price: float) -> float:
        if leg.entry_price <= 0:
            return 0.0
        if leg.side == "BUY":
            return ((exit_price - leg.entry_price) / leg.entry_price) * 100
        return ((leg.entry_price - exit_price) / leg.entry_price) * 100

    def _stop_hit(self, leg: Leg, exit_price: float) -> bool:
        if leg.stop_price is None:
            return False
        if leg.side == "BUY":
            return exit_price <= leg.stop_price
        return exit_price >= leg.stop_price

    @staticmethod
    def _trailing_stop_price(leg: Leg, profit_pct: float) -> float:
        locked_profit_pct = max(0.5, profit_pct - 0.5)
        if leg.side == "BUY":
            return round(leg.entry_price * (1 + locked_profit_pct / 100.0), 2)
        return round(leg.entry_price * (1 - locked_profit_pct / 100.0), 2)

    @staticmethod
    def _should_raise_stop(leg: Leg, new_stop: float) -> bool:
        if leg.stop_price is None:
            return True
        if leg.side == "BUY":
            return new_stop > leg.stop_price
        return new_stop < leg.stop_price

    def _close_leg(self, leg: Leg, exit_price: float) -> float:
        leg.exit_price = round(exit_price, 2)
        leg.closed_at = datetime.now().isoformat()
        leg.realized_pnl = self._leg_pnl(leg, leg.exit_price)
        return leg.realized_pnl

    def _persist(self) -> None:
        payload = {
            "capital": self.capital,
            "positions": [asdict(p) for p in self.positions],
        }
        self._state_file.write_text(json.dumps(payload, indent=2))

    # ──────────────────────────────────────────────────────────
    #   JOURNALING (writes to BlitzTrader JOURNALS_DIR)
    # ──────────────────────────────────────────────────────────

    def _write_journal(self, positions: list[PairPosition]) -> None:
        day = datetime.now().strftime("%Y%m%d")
        path = JOURNALS_DIR / f"{day}_pairs.md"
        lines = [
            f"# BlitzTrader — Pairs Journal — {datetime.now().strftime('%d %b %Y')}",
            "",
            "## Opened Pairs",
            "",
            f"- **Pairs Capital:** ₹{self.capital:,.0f}",
            f"- **Pairs Opened:** {len(positions)}",
            "",
        ]
        for pos in positions:
            lines.extend([
                f"### {pos.pair_name}",
                f"- Timeframe: `{pos.timeframe}` (matched: {', '.join(pos.matched_timeframes)})",
                f"- Method: `{pos.method}`",
                f"- Z-score: `{pos.z_score}` | Beta: `{pos.beta}` | P(profit): `{pos.prob_profit}%` (`{pos.prob_profit_low}%–{pos.prob_profit_high}%`)",
                f"- Half-life: `{pos.half_life}` bars",
                f"- Capital reserved: `₹{pos.capital_reserved:,.2f}`",
                f"- Long: `{pos.long_leg.symbol}` x {pos.long_leg.qty} @ ₹{pos.long_leg.entry_price:.2f}",
                f"- Short: `{pos.short_leg.symbol}` x {pos.short_leg.qty} @ ₹{pos.short_leg.entry_price:.2f}",
                f"- Margin used: `₹{pos.margin_used:,.2f}`",
                "",
            ])
        path.write_text("\n".join(lines))

    def _append_eod_journal(self, positions: list[PairPosition], total_pnl: float) -> None:
        day = datetime.now().strftime("%Y%m%d")
        path = JOURNALS_DIR / f"{day}_pairs.md"
        lines = [
            "",
            "## End of Day",
            "",
            f"- **Closed Pairs:** {len(positions)}",
            f"- **Net P&L:** ₹{total_pnl:+,.2f} ({(total_pnl / self.capital) * 100:+.2f}%)",
            "",
        ]
        for pos in positions:
            lines.extend([
                f"### Closed {pos.pair_name}",
                f"- Half-life: `{pos.half_life}` bars",
                f"- Long exit: `{pos.long_leg.symbol}` @ ₹{pos.long_leg.exit_price:.2f}",
                f"- Short exit: `{pos.short_leg.symbol}` @ ₹{pos.short_leg.exit_price:.2f}",
                f"- P&L: `₹{pos.pnl:+,.2f}`",
                "",
            ])
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

    def _append_monitor_journal(self, events: list[dict]) -> None:
        if not events:
            return
        day = datetime.now().strftime("%Y%m%d")
        path = JOURNALS_DIR / f"{day}_pairs.md"
        lines = ["", "## Intraday Leg Updates", ""]
        for event in events:
            if event["type"] == "STOP_ARMED":
                lines.append(
                    f"- Armed `{event['leg']}` stop at `₹{event['stop_price']:.2f}` "
                    f"after `{event['profit_pct']:.2f}%` profit in `{event['pair']}`"
                )
            elif event["type"] == "STOP_MOVED":
                lines.append(
                    f"- Trailed `{event['leg']}` stop to `₹{event['stop_price']:.2f}` "
                    f"at `{event['profit_pct']:.2f}%` profit in `{event['pair']}`"
                )
            elif event["type"] == "LEG_EXIT":
                lines.append(
                    f"- Closed `{event['leg']}` in `{event['pair']}` "
                    f"at `₹{event['exit_price']:.2f}` for `₹{event['pnl']:+,.2f}`"
                )
            elif event["type"] == "PAIR_CLOSED":
                lines.append(f"- Pair `{event['pair']}` fully closed for `₹{event['pnl']:+,.2f}`")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
