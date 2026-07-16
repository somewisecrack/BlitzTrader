"""
tools/gamma_replay.py — GammaBlast "cheap-ticket" rule engine + replay backtester.

This module is a self-contained, data-driven backtester for the GammaBlast
options strategy. It reads a day's per-strike gamma-ladder JSONL exports and
replays a rule configuration tick-by-tick, producing virtual entries, exits and
P&L. It is used to *validate* rule changes against real recorded market data
before those rules are promoted to the live GammaBlast box.

Why this exists
---------------
The live GammaBlast runs on a separate trading VM. It loads "promoted rules"
(numeric thresholds, scoped per symbol) at startup — the same mechanism
BlitzTrader uses for wiki/promoted_filters/*.json. Rather than edit the live
engine blind, we express the new rules here as a RuleConfig, prove them against
historical ladder data, and emit a promoted-rules JSON the VM can consume.

The rules encoded here (the "cheap-ticket" overhaul)
----------------------------------------------------
Motivated by three losing sessions (Jun 23 feed-freeze, Jul 9 SENSEX -3,392,
Jul 14 NIFTY -321) whose common failure modes were: counter-trend entries with
no direction confirmation, mid-premium entries that bleed on the hard stop, and
late entries that always came *after* the move.

  1. DIRECTION GATE      — only enter when the underlying's N-min move points
                           TOWARD the strike (rebuilds direction_move_5m_ok and
                           makes it mandatory; kills blind "terminal anticipation").
  2. VELOCITY GATE       — the underlying must have moved at least
                           min_underlying_move_5m points toward the strike.
  3. CHEAP-ENTRY CAP     — entry premium <= entry_max_premium (default Rs 2),
                           with an optional tiered allowance up to
                           entry_max_premium_tier for first-OTM strikes when the
                           underlying is accelerating toward them.
  4. PROXIMITY / OTM     — strike must be on the correct OTM side and within
                           proximity_pct of the underlying (avoid dead strikes).
  5. SCALE + TRAIL EXIT  — scale out scale_out_frac at scale_out_mult, then trail
                           the remainder at trail_frac of the peak. Cheap tickets
                           are not hard-stopped on a fraction of a 1-rupee premium.
  6. TIME STOP           — record the feed to 15:30 (the NSE/BSE close), allow new
                           entries up to 15:25, and flatten everything at 15:30.
  7. RE-ENTRY COOLDOWN   — no re-entry on the same strike+side within
                           cooldown_min minutes of an exit.

Ladder record schema (per line, one file per strike)
----------------------------------------------------
    {
      "timestamp_ist": "2026-07-14T14:47:30.123456",
      "symbol": "NIFTY", "expiry": "14-JUL-2026",
      "strike": 24000, "option_type": "PE",
      "underlying_ltp": 24044.8,
      "ltp": 14.45, "oi": ..., "oi_change": ..., "volume": ...,
      ...
    }

Sizing: virtual 25-lot; P&L in rupees = (exit - entry) * lot_size.

CLI
---
    python3 tools/gamma_replay.py --ladder-dir <dir> [--config default]
    python3 tools/gamma_replay.py --ladder-dir <dir> --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ── Configuration ───────────────────────────────────────────────────────────────


@dataclass
class RuleConfig:
    """Tunable thresholds for the cheap-ticket rule engine.

    All values here map 1:1 onto promoted rules on the GammaBlast box. Field
    names are the promoted-rule keys (upper-cased) the live engine reads.
    """

    # Entry premium caps (rupees)
    entry_max_premium: float = 2.0          # ENTRY_MAX_PREMIUM
    entry_max_premium_tier: float = 6.0     # ENTRY_MAX_PREMIUM_TIER (first-OTM only)
    tier_enabled: bool = True               # ENTRY_TIER_ENABLED

    # Direction / velocity gates
    direction_window_min: int = 5           # DIRECTION_WINDOW_MIN
    min_underlying_move_5m: float = 12.0     # MIN_UNDERLYING_MOVE_5M (points toward strike)

    # Proximity / OTM side
    proximity_pct: float = 0.006            # PROXIMITY_PCT (0.6% of underlying)

    # Exit management
    scale_out_mult: float = 3.0             # SCALE_OUT_MULT
    scale_out_frac: float = 0.50            # SCALE_OUT_FRAC
    trail_frac: float = 0.25                # TRAIL_FRAC (of running peak)
    trail_activate_mult: float = 2.0        # TRAIL_ACTIVATE_MULT (arm trail at 2x)

    # Time controls (IST HH:MM). Both NSE (NIFTY) and BSE (SENSEX) trade the
    # continuous session to 15:30, so the feed is recorded to the close and the
    # last entry is allowed at 15:25 — the previous 15:00 cutoff / 15:12 stop
    # threw away the wildest 15 minutes of expiry-day gamma. Positions are
    # flattened at 15:30 (the close).
    entry_cutoff_hhmm: str = "15:25"        # ENTRY_CUTOFF  (last new entry)
    time_stop_hhmm: str = "15:30"           # TIME_STOP     (flatten at close)
    recorder_end_hhmm: str = "15:30"        # RECORDER_END  (feed recorded to close)

    # Re-entry cooldown
    cooldown_min: int = 15                  # COOLDOWN_MIN

    # Ladder width — how many strikes each side of ATM the recorder tracks.
    # Cheap (<=Rs 2) tickets that can blast 4-6x live well OTM; the legacy ±2
    # ladder never tracks them, so no cheap entry ever exists. Widen to ±8.
    ladder_offsets: int = 8                 # LADDER_OFFSETS

    # Sizing
    lot_size: int = 25                      # LOT_SIZE

    name: str = "cheap_ticket_v2"

    def promoted_rules(self) -> dict:
        """Return the numeric thresholds as a promoted-rules dict (VM format)."""
        return {
            "ENTRY_MAX_PREMIUM": self.entry_max_premium,
            "ENTRY_MAX_PREMIUM_TIER": self.entry_max_premium_tier,
            "ENTRY_TIER_ENABLED": self.tier_enabled,
            "DIRECTION_WINDOW_MIN": self.direction_window_min,
            "MIN_UNDERLYING_MOVE_5M": self.min_underlying_move_5m,
            "PROXIMITY_PCT": self.proximity_pct,
            "SCALE_OUT_MULT": self.scale_out_mult,
            "SCALE_OUT_FRAC": self.scale_out_frac,
            "TRAIL_FRAC": self.trail_frac,
            "TRAIL_ACTIVATE_MULT": self.trail_activate_mult,
            "ENTRY_CUTOFF": self.entry_cutoff_hhmm,
            "TIME_STOP": self.time_stop_hhmm,
            "RECORDER_END": self.recorder_end_hhmm,
            "COOLDOWN_MIN": self.cooldown_min,
            "LADDER_OFFSETS": self.ladder_offsets,
            "LOT_SIZE": self.lot_size,
        }


# Named presets. "strict2" enforces a hard Rs 2 cap; "tiered6" allows first-OTM
# up to Rs 6 when accelerating (recommended — catches both the 0.90 and 5.50
# tickets seen on Jul 14).
#
# SENSEX runs at ~77000 vs NIFTY ~24000 (≈3.2x), so its point-moves and
# strike-step (100 vs 50) are larger. The velocity threshold is scaled to keep
# the *percentage* move comparable; proximity stays a percentage so it needs no
# scaling. These map to the scope=SENSEX promoted rules on the live box.
CONFIGS = {
    "default": RuleConfig(),
    "tiered6": RuleConfig(name="cheap_ticket_v2_tiered6", tier_enabled=True),
    "strict2": RuleConfig(name="cheap_ticket_v2_strict2", tier_enabled=False),
    "nifty": RuleConfig(name="cheap_ticket_v2_nifty", tier_enabled=True,
                        min_underlying_move_5m=12.0),
    "sensex": RuleConfig(name="cheap_ticket_v2_sensex", tier_enabled=True,
                         min_underlying_move_5m=38.0),
}


# ── Data loading ────────────────────────────────────────────────────────────────


@dataclass
class Tick:
    ts: str            # full timestamp_ist
    hhmm: str          # "HH:MM"
    hhmmss: str        # "HH:MM:SS"
    ltp: float
    underlying: float


@dataclass
class Strike:
    key: str           # e.g. "C24050"
    strike: int
    opt_type: str      # "CE" or "PE"
    ticks: list = field(default_factory=list)  # list[Tick], time-ordered

    def ltp_at_index(self, i: int) -> float:
        return self.ticks[i].ltp


def _hhmm(ts: str) -> str:
    return ts[11:16]


def _hhmmss(ts: str) -> str:
    return ts[11:19]


def load_ladder_dir(ladder_dir: Path) -> tuple[dict, str]:
    """Load all per-strike JSONL files in a directory.

    Returns (strikes_by_key, symbol) where strikes_by_key maps "C24050" -> Strike.
    Only ticks with a positive ltp and a valid underlying are retained.
    """
    strikes: dict[str, Strike] = {}
    symbol = ""
    for path in sorted(Path(ladder_dir).glob("*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ltp = r.get("ltp")
                underlying = r.get("underlying_ltp")
                opt_type = r.get("option_type")
                strike_val = r.get("strike")
                if ltp is None or underlying is None or not opt_type or not strike_val:
                    continue
                if ltp <= 0 or underlying <= 0:
                    continue
                symbol = symbol or r.get("symbol", "")
                key = ("C" if opt_type == "CE" else "P") + str(strike_val)
                if key not in strikes:
                    strikes[key] = Strike(key=key, strike=int(strike_val), opt_type=opt_type)
                ts = r["timestamp_ist"]
                strikes[key].ticks.append(
                    Tick(ts=ts, hhmm=_hhmm(ts), hhmmss=_hhmmss(ts),
                         ltp=float(ltp), underlying=float(underlying))
                )
    for s in strikes.values():
        s.ticks.sort(key=lambda t: t.ts)
    return strikes, symbol


# ── Rule engine ─────────────────────────────────────────────────────────────────


@dataclass
class Trade:
    strike_key: str
    opt_type: str
    strike: int
    entry_ts: str
    entry_price: float
    exit_ts: str = ""
    exit_price: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""
    peak_price: float = 0.0
    entry_reason: str = ""


def _direction_ok(strike: Strike, i: int, cfg: RuleConfig) -> tuple[bool, float]:
    """Is the underlying moving TOWARD this strike over the direction window?

    Returns (ok, move_points) where move_points is the signed move toward the
    strike (positive = toward). Ticks are ~60s apart, so the window is
    direction_window_min ticks back (clamped to available history).
    """
    if i < 1:
        return False, 0.0
    back = min(cfg.direction_window_min, i)
    now_u = strike.ticks[i].underlying
    then_u = strike.ticks[i - back].underlying
    delta = now_u - then_u  # positive = underlying rising
    if strike.opt_type == "CE":
        toward = delta            # CE blasts when underlying rises
    else:
        toward = -delta           # PE blasts when underlying falls
    return (toward >= cfg.min_underlying_move_5m), toward


def _is_otm_and_near(strike: Strike, i: int, cfg: RuleConfig) -> bool:
    """Strike is on the correct OTM side and within proximity_pct of underlying."""
    u = strike.ticks[i].underlying
    band = u * cfg.proximity_pct
    if strike.opt_type == "CE":
        # OTM call: strike above spot, but not further than the proximity band
        return 0 < (strike.strike - u) <= band
    else:
        # OTM put: strike below spot, within band
        return 0 < (u - strike.strike) <= band


def _entry_cap_for(strike: Strike, i: int, move_pts: float, cfg: RuleConfig) -> float:
    """Resolve the entry premium cap for this strike at this tick.

    Base cap is entry_max_premium. The tier allowance (up to
    entry_max_premium_tier) applies only to the *first* OTM strike (nearest to
    ATM) when the underlying is accelerating toward it (move >= 1.5x threshold).
    """
    cap = cfg.entry_max_premium
    if not cfg.tier_enabled:
        return cap
    u = strike.ticks[i].underlying
    # first OTM = within one strike-step; approximate as within 0.3% of spot
    first_otm = abs(strike.strike - u) <= u * 0.003
    accelerating = move_pts >= 1.5 * cfg.min_underlying_move_5m
    if first_otm and accelerating:
        return cfg.entry_max_premium_tier
    return cap


def _hhmm_to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _manage_exit(strike: Strike, entry_i: int, entry_price: float,
                 cfg: RuleConfig) -> tuple[int, float, str, float]:
    """Walk forward from entry and determine exit index/price/reason.

    Exit logic:
      - Arm a trailing stop once price >= trail_activate_mult * entry.
      - Once armed, exit if price falls below trail_frac-of-peak retrace, i.e.
        exit when price <= peak - trail_frac * (peak - entry) ... expressed as a
        drawdown from peak. We trail at trail_frac of the *gain*.
      - Scale-out is modelled as a realised partial at scale_out_mult; for a
        single-P&L backtest we blend: realise scale_out_frac at the scale price
        and the remainder at the trailed exit.
      - Hard time stop at time_stop_hhmm.
    Returns (exit_index, blended_exit_price, reason, peak_price).
    """
    time_stop = cfg.time_stop_hhmm
    peak = entry_price
    scaled = False
    scale_price = 0.0
    armed = False

    n = len(strike.ticks)
    for j in range(entry_i + 1, n):
        px = strike.ticks[j].ltp
        hhmm = strike.ticks[j].hhmm
        if px > peak:
            peak = px

        # time stop — flatten remainder here
        if hhmm >= time_stop:
            return j, _blend(entry_price, scale_price if scaled else 0.0,
                             px, cfg.scale_out_frac if scaled else 0.0), "TIME_STOP", peak

        # scale out
        if not scaled and px >= cfg.scale_out_mult * entry_price:
            scaled = True
            scale_price = px

        # arm trail
        if not armed and px >= cfg.trail_activate_mult * entry_price:
            armed = True

        # trailing exit on the remainder
        if armed:
            gain = peak - entry_price
            trail_level = peak - cfg.trail_frac * gain
            if px <= trail_level and px < peak:
                return j, _blend(entry_price, scale_price if scaled else 0.0,
                                 px, cfg.scale_out_frac if scaled else 0.0), \
                    ("TRAIL_AFTER_SCALE" if scaled else "TRAIL_STOP"), peak

    # ran to end of data with no trail/stop hit — exit at last tick
    last = strike.ticks[n - 1]
    return n - 1, _blend(entry_price, scale_price if scaled else 0.0,
                         last.ltp, cfg.scale_out_frac if scaled else 0.0), "EOD", peak


def _blend(entry: float, scale_price: float, final_price: float, scale_frac: float) -> float:
    """Blend a partial scale-out with the final exit into one effective exit px.

    If no scale happened (scale_frac 0), effective exit == final_price.
    Otherwise effective exit = scale_frac*scale_price + (1-scale_frac)*final_price.
    """
    if scale_frac <= 0 or scale_price <= 0:
        return final_price
    return scale_frac * scale_price + (1.0 - scale_frac) * final_price


def replay(strikes: dict, cfg: RuleConfig) -> list:
    """Replay the rule config across all strikes, return list[Trade].

    Single-position model: at most one open position at a time (matches the live
    engine's virtual book behaviour). We iterate a merged timeline; at each tick
    we evaluate candidate entries across all strikes and take the nearest-ATM
    qualifying strike. Cooldown is enforced per strike+side.
    """
    # Build a global time-ordered index of (hhmmss, strike_key, tick_index)
    timeline = []
    for key, s in strikes.items():
        for i, t in enumerate(s.ticks):
            timeline.append((t.hhmmss, key, i))
    timeline.sort()

    trades: list[Trade] = []
    cooldown_until: dict[str, int] = {}  # strike_key -> minute-of-day
    open_until_min = -1  # minute-of-day the current position exits (block new entries)

    for hhmmss, key, i in timeline:
        s = strikes[key]
        t = s.ticks[i]
        cur_min = _hhmm_to_min(t.hhmm)

        # respect an open position: no new entries until it has exited
        if cur_min < open_until_min:
            continue

        # entry cutoff
        if t.hhmm >= cfg.entry_cutoff_hhmm:
            continue

        # cooldown for this strike+side
        if cur_min < cooldown_until.get(key, -1):
            continue

        # gates
        if not _is_otm_and_near(s, i, cfg):
            continue
        dir_ok, move_pts = _direction_ok(s, i, cfg)
        if not dir_ok:
            continue
        cap = _entry_cap_for(s, i, move_pts, cfg)
        if not (0 < t.ltp <= cap):
            continue

        # qualifying entry — take it
        entry_price = t.ltp
        exit_i, exit_px, reason, peak = _manage_exit(s, i, entry_price, cfg)
        pnl = (exit_px - entry_price) * cfg.lot_size
        tr = Trade(
            strike_key=key, opt_type=s.opt_type, strike=s.strike,
            entry_ts=t.hhmmss, entry_price=round(entry_price, 2),
            exit_ts=s.ticks[exit_i].hhmmss, exit_price=round(exit_px, 2),
            pnl=round(pnl, 2), exit_reason=reason, peak_price=round(peak, 2),
            entry_reason=f"dir_move={move_pts:+.1f}pts cap={cap:.1f}",
        )
        trades.append(tr)

        # block re-entry until exit + cooldown
        exit_min = _hhmm_to_min(s.ticks[exit_i].hhmm)
        open_until_min = exit_min
        cooldown_until[key] = exit_min + cfg.cooldown_min

    return trades


# ── Reporting ───────────────────────────────────────────────────────────────────


def summarise(trades: list, cfg: RuleConfig) -> dict:
    total_pnl = round(sum(t.pnl for t in trades), 2)
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl <= 0)
    best = max((t.pnl for t in trades), default=0.0)
    worst = min((t.pnl for t in trades), default=0.0)
    return {
        "config": cfg.name,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100 * wins / len(trades), 1) if trades else 0.0,
        "total_pnl": total_pnl,
        "best_trade": round(best, 2),
        "worst_trade": round(worst, 2),
    }


def format_report(trades: list, summary: dict) -> str:
    lines = []
    lines.append(f"GammaBlast replay — config '{summary['config']}'")
    lines.append("=" * 68)
    if trades:
        lines.append(f"{'Strike':9} {'Entry':>16} {'Exit':>16} {'Peak':>7} {'PnL':>10} {'Reason'}")
        lines.append("-" * 68)
        for t in trades:
            lines.append(
                f"{t.opt_type} {t.strike:<6} "
                f"{t.entry_price:>6.2f}@{t.entry_ts}  "
                f"{t.exit_price:>6.2f}@{t.exit_ts}  "
                f"{t.peak_price:>6.2f} {t.pnl:>+10.2f} {t.exit_reason}"
            )
        lines.append("-" * 68)
    else:
        lines.append("(no qualifying entries)")
    lines.append(
        f"Trades: {summary['trades']} | Wins: {summary['wins']} | "
        f"Losses: {summary['losses']} | Win rate: {summary['win_rate_pct']}%"
    )
    lines.append(
        f"Total P&L: Rs {summary['total_pnl']:+,.2f} | "
        f"Best: Rs {summary['best_trade']:+,.2f} | Worst: Rs {summary['worst_trade']:+,.2f}"
    )
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────────


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="GammaBlast cheap-ticket rule replay backtester")
    parser.add_argument("--ladder-dir", required=True,
                        help="Directory of per-strike gamma-ladder JSONL files")
    parser.add_argument("--config", default="tiered6", choices=sorted(CONFIGS.keys()),
                        help="Rule config preset (default: tiered6)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--emit-promoted-rules", default=None,
                        help="Write the config's promoted-rules JSON to this path")
    args = parser.parse_args(argv)

    cfg = CONFIGS[args.config]
    ladder_dir = Path(args.ladder_dir).expanduser().resolve()
    if not ladder_dir.is_dir():
        print(f"ERROR: ladder dir not found: {ladder_dir}", file=sys.stderr)
        return 1

    strikes, symbol = load_ladder_dir(ladder_dir)
    if not strikes:
        print(f"ERROR: no valid ladder ticks found in {ladder_dir}", file=sys.stderr)
        return 1

    trades = replay(strikes, cfg)
    summary = summarise(trades, cfg)

    if args.emit_promoted_rules:
        out = {
            "id": cfg.name,
            "scope": symbol or "NIFTY",
            "rules": cfg.promoted_rules(),
        }
        Path(args.emit_promoted_rules).write_text(
            json.dumps(out, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps({
            "symbol": symbol,
            "summary": summary,
            "trades": [asdict(t) for t in trades],
        }, indent=2))
    else:
        print(format_report(trades, summary))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
