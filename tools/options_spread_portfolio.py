"""
tools/options_spread_portfolio.py — Open spread tracking, P&L monitoring, and exit management.

Responsibilities:
  - Load open spreads from state_manager on startup.
  - Compute live P&L for each spread from Shoonya quotes.
  - Check exit conditions on every scan tick:
      • Max-loss exit:  current_loss >= SPREAD_MAX_LOSS_EXIT_FRACTION * max_loss
      • Credit TP:      current_profit >= SPREAD_CREDIT_TP_FRACTION * max_profit
      • Debit TP:       current_profit >= SPREAD_DEBIT_TP_FRACTION * max_profit
      • EOD forced close: caller passes force=True at 3:15 PM IST
  - Close a spread: buy-back short leg first (reduce risk), then sell long leg.
    (On close, short leg goes first — the opposite of entry — to reduce net risk
     as fast as possible. Both legs are always closed; never leave single leg open.)
  - Return structured result dicts; never raises.
  - Thread-safe: all state mutations go through state_manager.

Exit order (close spread):
  Step 1: Buy-to-close short leg (LIMIT, best ask; market fallback after timeout)
  Step 2: Sell-to-close long leg (LIMIT, best bid; market fallback after timeout)

If step 1 fails:  log error, do NOT proceed to step 2 (position still intact).
If step 2 fails after step 1:  emergency market order for step 2. Log critical.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime
from typing import Optional

import pytz

from tools.options_spread_execution import OpenSpread

logger = logging.getLogger("BlitzTrader.SpreadPortfolio")

IST = pytz.timezone("Asia/Kolkata")

# Polling interval when waiting for close-leg fill (seconds)
_POLL_INTERVAL = 2
_DEFAULT_FILL_TIMEOUT = 300  # seconds


# ──────────────────────────────────────────────────────────────────────────────
#   LIVE P&L
# ──────────────────────────────────────────────────────────────────────────────

# ── P&L rounding tolerance ────────────────────────────────────────────────────
# A small absolute rupee tolerance added to max_profit / max_loss before
# treating a P&L as outside defined-risk bounds.  This covers tick-size
# rounding only (NIFTY tick=₹0.05, BANKNIFTY tick=₹0.05).
# NOT an option-premium cap — this does not limit what price an option may have.
_PNL_ROUNDING_TOLERANCE_RS = 25.0   # ₹25 absolute — handles tick-size noise


def get_verified_option_quote(
    exchange: str,
    token: str,
    expected_tsym: str,
    shoonya_client,
    live_feed,
) -> dict:
    """
    Fetch and identity-verify the current LTP for an NFO option contract.

    Returns a structured result dict:
      {
        "ok":       bool,     # True if quote obtained and identity verified
        "ltp":      float | None,
        "source":   "websocket" | "rest" | "none",
        "exchange": exchange,
        "token":    token,
        "tsym":     expected_tsym,
        "raw":      dict | None,   # raw Shoonya response (REST) or cache entry (WS)
        "reason":   ""             # non-empty on failure
      }

    Identity verification rules:
    ──────────────────────────────
    WebSocket path:
      - Only accepted if the live_feed was subscribed via subscribe_with_tsym()
        for this exact (exchange, token, tsym) triple.
      - If metadata is absent or mismatched, the WebSocket value is skipped and
        REST is tried.

    REST path:
      - Calls shoonya_client.get_quotes(exchange, token).
      - If the response includes a "tsym" field, it must match expected_tsym.
      - If the response includes a "token"/"tok" field, it must match token.
      - If the response includes an "exch" field, it must match exchange.
      - If any present identity field conflicts, the quote is rejected.
      - If identity fields are absent from the response, the lp value is
        accepted (no conflicting evidence of mismatch).
      - lp must be > 0.

    There are no hardcoded price caps.  The only numerical rejection criterion
    is ltp <= 0.  Implausibly large prices (e.g. underlying index level) are
    caught downstream by the defined-risk P&L bounds check.
    """
    # ── WebSocket path (verified) ─────────────────────────────────────────────
    if live_feed:
        try:
            entry = live_feed.get_live_quote_verified(token, exchange, expected_tsym)
            if entry:
                ltp = float(entry.get("ltp", 0))
                if ltp > 0:
                    return {
                        "ok": True, "ltp": ltp, "source": "websocket",
                        "exchange": exchange, "token": token, "tsym": expected_tsym,
                        "raw": entry, "reason": "",
                    }
        except Exception as e:
            logger.debug("get_verified_option_quote[%s]: WebSocket path error: %s", token, e)

    # ── REST path (with identity verification from response fields) ───────────
    if not shoonya_client:
        return {
            "ok": False, "ltp": None, "source": "none",
            "exchange": exchange, "token": token, "tsym": expected_tsym,
            "raw": None, "reason": "no_quote_source_available",
        }

    try:
        resp = shoonya_client.get_quotes(exchange, token)
    except Exception as e:
        return {
            "ok": False, "ltp": None, "source": "rest",
            "exchange": exchange, "token": token, "tsym": expected_tsym,
            "raw": None, "reason": f"rest_exception: {e}",
        }

    if not resp or resp.get("stat") != "Ok":
        emsg = (resp or {}).get("emsg", "no response")
        return {
            "ok": False, "ltp": None, "source": "rest",
            "exchange": exchange, "token": token, "tsym": expected_tsym,
            "raw": resp, "reason": f"rest_failed: {emsg}",
        }

    # Extract lp
    ltp_raw = resp.get("lp") or resp.get("ltp")
    if ltp_raw is None:
        return {
            "ok": False, "ltp": None, "source": "rest",
            "exchange": exchange, "token": token, "tsym": expected_tsym,
            "raw": resp, "reason": "rest_missing_lp_field",
        }
    try:
        ltp = float(ltp_raw)
    except (TypeError, ValueError):
        return {
            "ok": False, "ltp": None, "source": "rest",
            "exchange": exchange, "token": token, "tsym": expected_tsym,
            "raw": resp, "reason": f"rest_lp_not_numeric: {ltp_raw!r}",
        }
    if ltp <= 0:
        return {
            "ok": False, "ltp": ltp, "source": "rest",
            "exchange": exchange, "token": token, "tsym": expected_tsym,
            "raw": resp, "reason": f"rest_non_positive_ltp: {ltp}",
        }

    # Identity fields — only reject if present AND conflicting
    resp_token = str(resp.get("token") or resp.get("tok") or "").strip()
    resp_tsym  = str(resp.get("tsym") or "").strip()
    resp_exch  = str(resp.get("exch") or resp.get("exchange") or "").strip().upper()

    if resp_token and resp_token != str(token):
        return {
            "ok": False, "ltp": ltp, "source": "rest",
            "exchange": exchange, "token": token, "tsym": expected_tsym,
            "raw": resp,
            "reason": f"rest_token_mismatch: expected {token!r}, got {resp_token!r}",
        }
    if resp_tsym and resp_tsym != expected_tsym:
        return {
            "ok": False, "ltp": ltp, "source": "rest",
            "exchange": exchange, "token": token, "tsym": expected_tsym,
            "raw": resp,
            "reason": f"rest_tsym_mismatch: expected {expected_tsym!r}, got {resp_tsym!r}",
        }
    if resp_exch and resp_exch != exchange.upper():
        return {
            "ok": False, "ltp": ltp, "source": "rest",
            "exchange": exchange, "token": token, "tsym": expected_tsym,
            "raw": resp,
            "reason": f"rest_exchange_mismatch: expected {exchange!r}, got {resp_exch!r}",
        }

    return {
        "ok": True, "ltp": ltp, "source": "rest",
        "exchange": exchange, "token": token, "tsym": expected_tsym,
        "raw": resp, "reason": "",
    }


def compute_spread_pnl(
    spread: OpenSpread,
    shoonya_client,
    live_feed,
) -> dict:
    """
    Compute current P&L for an open spread with exact quote identity verification.

    P&L formula (uniform for debit and credit):
      pnl = (long_ltp  - long_fill)  * qty
          - (short_ltp - short_fill) * qty

    Two layers of validation are applied before returning data_ok=True:
      1. Quote identity: each leg's LTP must come from the exact NFO option contract
         (verified via Shoonya response fields: token, tsym, exchange).
         No hardcoded price caps — any positive lp from a verified contract is accepted.
      2. Defined-risk P&L bounds: computed P&L must lie within
         [-(max_loss + tolerance), +(max_profit + tolerance)]
         where tolerance = ₹25 (tick-size rounding only, not a trading limit).

    If either check fails: data_ok=False, unrealized_pnl=None, full details logged.

    Returns dict with keys:
      long_ltp, short_ltp, unrealized_pnl, data_ok[, reason, source_long, source_short]
    """
    qty = spread.lot_size * spread.lots

    long_q  = get_verified_option_quote("NFO", spread.long_token,  spread.long_tsym,  shoonya_client, live_feed)
    short_q = get_verified_option_quote("NFO", spread.short_token, spread.short_tsym, shoonya_client, live_feed)

    long_ltp    = long_q["ltp"]
    short_ltp   = short_q["ltp"]
    source_long  = long_q["source"]
    source_short = short_q["source"]

    # ── Guard 1: quote identity / availability ────────────────────────────────
    if not long_q["ok"] or not short_q["ok"]:
        reasons = []
        if not long_q["ok"]:
            reasons.append(f"long({spread.long_tsym}): {long_q['reason']}")
        if not short_q["ok"]:
            reasons.append(f"short({spread.short_tsym}): {short_q['reason']}")
        reason_str = "; ".join(reasons)
        logger.error(
            "compute_spread_pnl[%s]: QUOTE VERIFICATION FAILED — %s",
            spread.spread_id, reason_str,
        )
        return {
            "long_ltp": long_ltp, "short_ltp": short_ltp,
            "unrealized_pnl": None, "data_ok": False,
            "reason": f"quote_verification_failed: {reason_str}",
            "source_long": source_long, "source_short": source_short,
        }

    # ── Raw P&L ───────────────────────────────────────────────────────────────
    raw_pnl = (long_ltp - spread.long_fill_price) * qty \
            - (short_ltp - spread.short_fill_price) * qty

    # ── Guard 2: defined-risk P&L bounds ─────────────────────────────────────
    # Small rounding tolerance (₹25) for tick-size noise — not a price cap.
    tol = _PNL_ROUNDING_TOLERANCE_RS
    if raw_pnl > spread.max_profit + tol or raw_pnl < -(spread.max_loss + tol):
        reason_str = (
            f"pnl ₹{raw_pnl:+.2f} outside defined-risk bounds "
            f"[₹{-(spread.max_loss + tol):.2f}, ₹{spread.max_profit + tol:.2f}] "
            f"(max_profit=₹{spread.max_profit:.2f}, max_loss=₹{spread.max_loss:.2f}, "
            f"tolerance=₹{tol:.2f})"
        )
        logger.error(
            "compute_spread_pnl[%s]: P&L OUTSIDE DEFINED RISK — %s "
            "(long=%s ltp=%.2f src=%s, short=%s ltp=%.2f src=%s)",
            spread.spread_id, reason_str,
            spread.long_tsym,  long_ltp,  source_long,
            spread.short_tsym, short_ltp, source_short,
        )
        return {
            "long_ltp": long_ltp, "short_ltp": short_ltp,
            "unrealized_pnl": None, "data_ok": False,
            "reason": f"invalid_pnl_outside_defined_risk: {reason_str}",
            "source_long": source_long, "source_short": source_short,
        }

    logger.debug(
        "compute_spread_pnl[%s]: long=%s ltp=%.2f(%s) fill=%.2f | "
        "short=%s ltp=%.2f(%s) fill=%.2f | pnl=₹%+.2f | "
        "max_profit=₹%.2f max_loss=₹%.2f",
        spread.spread_id,
        spread.long_tsym,  long_ltp,  source_long,  spread.long_fill_price,
        spread.short_tsym, short_ltp, source_short, spread.short_fill_price,
        raw_pnl, spread.max_profit, spread.max_loss,
    )

    return {
        "long_ltp":       long_ltp,
        "short_ltp":      short_ltp,
        "unrealized_pnl": raw_pnl,
        "data_ok":        True,
        "source_long":    source_long,
        "source_short":   source_short,
    }


def should_exit(
    spread: OpenSpread,
    pnl_data: dict,
    max_loss_exit_fraction: float = 0.60,
    credit_tp_fraction: float = 0.60,
    debit_tp_fraction: float = 0.70,
    force: bool = False,
) -> tuple[bool, str]:
    """
    Determine whether an open spread should be exited.

    Returns (True, reason) if exit is warranted, (False, "") otherwise.
    """
    if force:
        return True, "EOD forced close"

    if not pnl_data.get("data_ok"):
        return False, ""  # can't decide without data

    pnl = pnl_data["unrealized_pnl"]
    max_loss = spread.max_loss
    max_profit = spread.max_profit
    is_credit = spread.net_debit_or_credit < 0

    # Max-loss exit (loss side)
    if pnl is not None and max_loss > 0:
        loss = -pnl  # positive = losing money
        if loss >= max_loss_exit_fraction * max_loss:
            return True, (
                f"max-loss exit: loss ₹{loss:.2f} >= "
                f"{max_loss_exit_fraction:.0%} × max_loss ₹{max_loss:.2f}"
            )

    # Take-profit
    if pnl is not None and max_profit > 0:
        tp_fraction = credit_tp_fraction if is_credit else debit_tp_fraction
        if pnl >= tp_fraction * max_profit:
            return True, (
                f"take-profit: profit ₹{pnl:.2f} >= "
                f"{tp_fraction:.0%} × max_profit ₹{max_profit:.2f}"
            )

    return False, ""


# ──────────────────────────────────────────────────────────────────────────────
#   CLOSE ENGINE
# ──────────────────────────────────────────────────────────────────────────────

class SpreadPortfolio:
    """
    Manages all open spreads: P&L monitoring, exit checks, and close execution.

    Usage:
        portfolio = SpreadPortfolio(shoonya_client, state_manager, live_feed)
        # On each scan tick:
        exits = portfolio.check_and_exit_spreads(force_close_all=False)
        # At EOD:
        exits = portfolio.check_and_exit_spreads(force_close_all=True)
    """

    def __init__(
        self,
        shoonya_client,
        state_manager,
        live_feed=None,
        max_loss_exit_fraction: float = 0.60,
        credit_tp_fraction: float = 0.60,
        debit_tp_fraction: float = 0.70,
        fill_timeout_seconds: int = _DEFAULT_FILL_TIMEOUT,
        virtual: bool = True,
    ):
        self._client = shoonya_client
        self._state = state_manager
        self._feed = live_feed
        self._max_loss_frac = max_loss_exit_fraction
        self._credit_tp_frac = credit_tp_fraction
        self._debit_tp_frac = debit_tp_fraction
        self._fill_timeout = fill_timeout_seconds
        self._virtual = virtual
        # Track invalid-quote incidents for EOD reporting
        self._invalid_quote_count: int = 0

    # ── Portfolio accessors ───────────────────────────────────────────────────

    def get_open_spreads(self) -> list[OpenSpread]:
        """Load current open spreads from state."""
        try:
            state = self._state.get_state()
            raw = state.get("open_spreads", []) or []
            spreads = []
            for d in raw:
                try:
                    spreads.append(_dict_to_open_spread(d))
                except Exception:
                    logger.exception("Failed to deserialize spread: %r", d)
            return spreads
        except Exception:
            logger.exception("get_open_spreads: state read failed")
            return []

    def count_open_spreads(self) -> int:
        return len(self.get_open_spreads())

    def subscribe_spread_legs(self, spread: "OpenSpread") -> None:
        """
        Subscribe both option leg tokens to the live WebSocket feed WITH tsym metadata.

        Using subscribe_with_tsym() registers (exchange, token, tsym) triples so that
        get_live_quote_verified() can confirm any WebSocket tick actually belongs to
        the intended option contract before using it for P&L.
        """
        if not self._feed:
            return
        triples = [
            ("NFO", spread.long_token,  spread.long_tsym),
            ("NFO", spread.short_token, spread.short_tsym),
        ]
        try:
            self._feed.subscribe_with_tsym(triples)
            logger.info(
                "subscribe_spread_legs[%s]: subscribed NFO|%s(%s), NFO|%s(%s)",
                spread.spread_id,
                spread.long_token, spread.long_tsym,
                spread.short_token, spread.short_tsym,
            )
        except Exception:
            logger.exception("subscribe_spread_legs[%s]: failed", spread.spread_id)

    def unsubscribe_spread_legs(self, spread: "OpenSpread") -> None:
        """
        Unsubscribe option leg tokens from the live feed after a spread closes,
        provided no other open spread still needs those tokens.
        """
        if not self._feed:
            return
        open_spreads = self.get_open_spreads()
        still_needed = {
            tok
            for s in open_spreads
            if s.spread_id != spread.spread_id
            for tok in (s.long_token, s.short_token)
        }
        to_unsub = [
            ("NFO", tok)
            for tok in (spread.long_token, spread.short_token)
            if tok not in still_needed
        ]
        if to_unsub:
            try:
                self._feed.unsubscribe(to_unsub)
                logger.info(
                    "unsubscribe_spread_legs[%s]: unsubscribed %s",
                    spread.spread_id, to_unsub,
                )
            except Exception:
                logger.exception("unsubscribe_spread_legs[%s]: failed", spread.spread_id)

    # ── Scan tick ─────────────────────────────────────────────────────────────

    def check_and_exit_spreads(self, force_close_all: bool = False) -> list[dict]:
        """
        Check all open spreads and exit those that meet exit criteria.

        Called on every scan tick. At EOD, pass force_close_all=True.
        Returns list of exit result dicts (one per exited spread).
        """
        open_spreads = self.get_open_spreads()
        results = []

        for spread in open_spreads:
            pnl_data = compute_spread_pnl(spread, self._client, self._feed)

            # Track invalid-quote incidents for session reporting
            if not pnl_data.get("data_ok") and not force_close_all:
                self._invalid_quote_count += 1
                logger.warning(
                    "check_and_exit_spreads[%s]: quote invalid — %s — skip exit check",
                    spread.spread_id, pnl_data.get("reason", "unknown"),
                )

            do_exit, reason = should_exit(
                spread, pnl_data,
                max_loss_exit_fraction=self._max_loss_frac,
                credit_tp_fraction=self._credit_tp_frac,
                debit_tp_fraction=self._debit_tp_frac,
                force=force_close_all,
            )
            if do_exit:
                result = self.close_spread(spread, reason=reason, pnl_data=pnl_data)
                results.append(result)

        return results

    # ── Close a spread ────────────────────────────────────────────────────────

    def close_spread(
        self,
        spread: OpenSpread,
        reason: str = "",
        pnl_data: Optional[dict] = None,
    ) -> dict:
        """
        Close both legs of an open spread.

        In virtual mode fills are taken from live quotes (pnl_data) if available,
        otherwise from entry prices.  No broker API calls are made.

        In live mode close order is: buy-to-close short leg FIRST (reduces naked
        risk), then sell-to-close long leg.

        Returns structured result dict.
        """
        qty = spread.lot_size * spread.lots
        logger.info(
            "close_spread[%s]: closing %s %s reason=%r qty=%d mode=%s",
            spread.spread_id, spread.symbol, spread.spread_type, reason, qty,
            "VIRTUAL" if self._virtual else "LIVE",
        )

        if self._virtual and pnl_data is None:
            pnl_data = compute_spread_pnl(spread, self._client, self._feed)

        if self._virtual:
            return self._close_spread_virtual(spread, reason, pnl_data, qty)
        return self._close_spread_live(spread, reason, pnl_data, qty)

    def _close_spread_virtual(
        self,
        spread: OpenSpread,
        reason: str,
        pnl_data: Optional[dict],
        qty: int,
    ) -> dict:
        """
        Simulate spread close without broker API calls.

        Close prices are taken from pnl_data LTPs ONLY if pnl_data passed full
        quote identity verification AND defined-risk P&L bounds (data_ok=True).

        There is NO fallback to entry prices for any type of close — not for
        automatic exits, not for manual exits, not for EOD forced close.
        Using invented prices would corrupt realized P&L and daily_pnl.

        If pnl_data is invalid/unavailable:
          - For threshold exits and manual exits: refuse, spread stays open.
          - For EOD forced close: mark spread close_failed_quote_unavailable
            WITHOUT updating daily_pnl or removing the spread from state. This
            keeps the failure visible instead of erasing a position with
            invented prices.
        """
        data_valid = pnl_data and pnl_data.get("data_ok")

        if not data_valid:
            reason_detail = (pnl_data or {}).get("reason", "no_pnl_data")
            is_eod = "eod" in reason.lower() or "force" in reason.lower()

            if is_eod:
                logger.error(
                    "close_spread[%s]: EOD close — quote unavailable/invalid: %s. "
                    "Spread left open and marked close_failed, daily_pnl NOT updated.",
                    spread.spread_id, reason_detail,
                )
                self._mark_spread_close_failed(
                    spread.spread_id,
                    reason=f"eod_close_failed_quote_unavailable: {reason_detail}",
                )
                return {
                    "ok": False,
                    "spread_id": spread.spread_id,
                    "eod_close_failed": True,
                    "error": f"EOD close failed: quote unavailable ({reason_detail})",
                    "emergency": False,
                }
            else:
                # Any other close with invalid data: refuse, spread stays open
                logger.error(
                    "close_spread[%s]: REFUSED — quote invalid/unavailable: %s. "
                    "Spread remains open, daily_pnl unchanged.",
                    spread.spread_id, reason_detail,
                )
                return {
                    "ok": False,
                    "spread_id": spread.spread_id,
                    "error": f"close refused: invalid quote data ({reason_detail})",
                    "emergency": False,
                }

        btc_fill = pnl_data["short_ltp"]
        stc_fill = pnl_data["long_ltp"]

        realized_pnl = (
            (stc_fill - spread.long_fill_price) * qty
            - (btc_fill - spread.short_fill_price) * qty
        )

        # realized_pnl already within defined-risk bounds (guaranteed by compute_spread_pnl
        # which is the only source of data_ok=True pnl_data that reaches here).
        self._remove_spread_from_state(spread.spread_id, realized_pnl=realized_pnl, reason=reason)
        self._update_daily_pnl(realized_pnl)
        self.unsubscribe_spread_legs(spread)

        message = (
            f"[VIRTUAL] Spread {spread.spread_id} closed ({reason}): "
            f"{spread.symbol} {spread.spread_type}\n"
            f"  Short closed: {spread.short_tsym} @ ₹{btc_fill:.2f}\n"
            f"  Long  closed: {spread.long_tsym} @ ₹{stc_fill:.2f}\n"
            f"  Realized P&L: ₹{realized_pnl:+.2f}"
        )
        logger.info("close_spread[%s]: %s", spread.spread_id, message)

        return {
            "ok": True,
            "spread_id": spread.spread_id,
            "symbol": spread.symbol,
            "spread_type": spread.spread_type,
            "reason": reason,
            "realized_pnl": realized_pnl,
            "short_close_price": btc_fill,
            "long_close_price": stc_fill,
            "message": message,
        }

    def _close_spread_live(
        self,
        spread: OpenSpread,
        reason: str,
        pnl_data: Optional[dict],
        qty: int,
    ) -> dict:
        """
        Live broker path: buy-to-close short leg first, then sell-to-close long leg.
        """
        # ── Step 1: Buy-to-close short leg ────────────────────────────────
        btc_order_id, err = self._place_close_order(
            tsym=spread.short_tsym,
            action="BUY",   # buy back the short
            qty=qty,
            ref_price=pnl_data.get("short_ltp") if pnl_data else None,
        )
        if btc_order_id is None:
            logger.error(
                "close_spread[%s]: short leg close failed — %s; leaving spread open",
                spread.spread_id, err,
            )
            return {
                "ok": False,
                "spread_id": spread.spread_id,
                "error": f"Short leg close failed: {err}",
                "emergency": False,
            }

        btc_fill, fill_err = self._wait_for_fill(btc_order_id, spread.short_tsym)
        if btc_fill is None:
            self._cancel_order(btc_order_id)
            logger.error(
                "close_spread[%s]: short leg fill timeout — %s; leaving spread open",
                spread.spread_id, fill_err,
            )
            return {
                "ok": False,
                "spread_id": spread.spread_id,
                "error": f"Short leg close fill failed: {fill_err}",
                "emergency": False,
            }

        logger.info(
            "close_spread[%s]: short leg closed %s @ ₹%.2f",
            spread.spread_id, spread.short_tsym, btc_fill,
        )

        # ── Step 2: Sell-to-close long leg ─────────────────────────────────
        stc_order_id, err = self._place_close_order(
            tsym=spread.long_tsym,
            action="SELL",
            qty=qty,
            ref_price=pnl_data.get("long_ltp") if pnl_data else None,
        )
        if stc_order_id is None:
            logger.critical(
                "close_spread[%s]: long leg close FAILED after short already closed — "
                "NAKED LONG EXPOSURE: %s. Attempting market order.",
                spread.spread_id, spread.long_tsym,
            )
            self._emergency_close_long(spread.long_tsym, qty)
            self._remove_spread_from_state(spread.spread_id)
            return {
                "ok": False,
                "spread_id": spread.spread_id,
                "error": f"Long leg close failed (emergency market attempted): {err}",
                "emergency": True,
            }

        stc_fill, fill_err = self._wait_for_fill(stc_order_id, spread.long_tsym)
        if stc_fill is None:
            self._cancel_order(stc_order_id)
            logger.critical(
                "close_spread[%s]: long leg fill timeout after short closed — "
                "emergency market close of %s",
                spread.spread_id, spread.long_tsym,
            )
            self._emergency_close_long(spread.long_tsym, qty)
            self._remove_spread_from_state(spread.spread_id)
            return {
                "ok": False,
                "spread_id": spread.spread_id,
                "error": f"Long leg fill timeout (emergency market attempted): {fill_err}",
                "emergency": True,
            }

        logger.info(
            "close_spread[%s]: long leg closed %s @ ₹%.2f",
            spread.spread_id, spread.long_tsym, stc_fill,
        )

        realized_pnl = (
            (stc_fill - spread.long_fill_price) * qty
            - (btc_fill - spread.short_fill_price) * qty
        )

        self._remove_spread_from_state(spread.spread_id, realized_pnl=realized_pnl, reason=reason)
        self._update_daily_pnl(realized_pnl)

        message = (
            f"Spread {spread.spread_id} closed ({reason}): "
            f"{spread.symbol} {spread.spread_type}\n"
            f"  Short closed: {spread.short_tsym} @ ₹{btc_fill:.2f}\n"
            f"  Long  closed: {spread.long_tsym} @ ₹{stc_fill:.2f}\n"
            f"  Realized P&L: ₹{realized_pnl:+.2f}"
        )
        logger.info("close_spread[%s]: %s", spread.spread_id, message)

        return {
            "ok": True,
            "spread_id": spread.spread_id,
            "symbol": spread.symbol,
            "spread_type": spread.spread_type,
            "reason": reason,
            "realized_pnl": realized_pnl,
            "short_close_price": btc_fill,
            "long_close_price": stc_fill,
            "message": message,
        }

    # ── Order helpers ─────────────────────────────────────────────────────────

    def _place_close_order(
        self,
        tsym: str,
        action: str,
        qty: int,
        ref_price: Optional[float],
    ) -> tuple[Optional[str], str]:
        """Place a LIMIT close order. Falls back to market if ref_price unavailable."""
        if not self._client:
            return None, "no Shoonya client (paper mode)"
        price_type = "LMT" if ref_price and ref_price > 0 else "MKT"
        price = ref_price if price_type == "LMT" else 0
        # Shoonya API: buy_or_sell uses 'B' / 'S' (not full words)
        shoonya_side = "B" if action == "BUY" else "S"
        try:
            resp = self._client.place_order(
                buy_or_sell=shoonya_side,
                product_type="M",
                exchange="NFO",
                tradingsymbol=tsym,
                quantity=qty,
                discloseqty=0,
                price_type=price_type,
                price=price,
                trigger_price=None,
                retention="DAY",
                remarks=f"BlitzTrader-CLOSE-{tsym}",
            )
            if resp and resp.get("stat") == "Ok":
                return resp.get("norenordno"), ""
            err = (resp or {}).get("emsg", str(resp))
            return None, f"place_order failed: {err}"
        except Exception as e:
            logger.exception("_place_close_order(%s) raised", tsym)
            return None, f"exception: {e}"

    def _wait_for_fill(self, order_id: str, tsym: str) -> tuple[Optional[float], str]:
        if not self._client:
            return None, "no Shoonya client (paper mode)"
        deadline = time.time() + self._fill_timeout
        while time.time() < deadline:
            try:
                orders = self._client.get_order_book()
                for o in (orders or []):
                    if str(o.get("norenordno")) == str(order_id):
                        status = (o.get("status") or "").upper()
                        if status in ("COMPLETE", "FILLED"):
                            try:
                                return float(o.get("avgprc") or o.get("price") or 0), ""
                            except (TypeError, ValueError):
                                return None, "fill price parse error"
                        if status in ("REJECTED", "CANCELLED", "CANCELED"):
                            return None, f"order {status}: {o.get('rejreason', '')}"
            except Exception:
                logger.exception("_wait_for_fill: order book poll failed")
            time.sleep(_POLL_INTERVAL)
        return None, f"fill timeout after {self._fill_timeout}s for {tsym}"

    def _cancel_order(self, order_id: str) -> None:
        if not self._client or not order_id:
            return
        try:
            self._client.cancel_order(order_id)
        except Exception:
            logger.exception("_cancel_order(%s) failed (non-fatal)", order_id)

    def _emergency_close_long(self, tsym: str, qty: int) -> None:
        if not self._client:
            logger.error("No Shoonya client — cannot emergency-close %s", tsym)
            return
        try:
            resp = self._client.place_order(
                buy_or_sell="S",           # Shoonya API: 'S' not 'SELL'
                product_type="M",
                exchange="NFO",
                tradingsymbol=tsym,
                quantity=qty,
                discloseqty=0,
                price_type="MKT",
                price=0,
                trigger_price=None,
                retention="DAY",
                remarks="BlitzTrader-EMERGENCY-CLOSE",
            )
            if resp and resp.get("stat") == "Ok":
                logger.warning("Emergency close placed for %s", tsym)
            else:
                logger.critical("Emergency close FAILED for %s: %s", tsym, resp)
        except Exception:
            logger.exception("_emergency_close_long raised for %s", tsym)

    # ── State mutations ───────────────────────────────────────────────────────

    def _remove_spread_from_state(
        self, spread_id: str, realized_pnl: Optional[float] = None, reason: str = ""
    ) -> None:
        try:
            state = self._state.get_state()
            open_spreads = [
                s for s in (state.get("open_spreads", []) or [])
                if s.get("spread_id") != spread_id
            ]
            # Mark closed in spreads_traded ledger
            spreads_traded = list(state.get("spreads_traded", []) or [])
            for t in spreads_traded:
                if t.get("spread_id") == spread_id:
                    t["closed"] = True
                    if realized_pnl is not None:
                        t["realized_pnl"] = realized_pnl
                    if reason:
                        t["close_reason"] = reason
                    break
            self._state.update_state(
                open_spreads=open_spreads,
                spreads_traded=spreads_traded,
            )
        except Exception:
            logger.exception("_remove_spread_from_state failed for %s (non-fatal)", spread_id)

    def _mark_spread_close_failed(self, spread_id: str, reason: str) -> None:
        """Record a failed close attempt without removing the spread or mutating P&L."""
        try:
            state = self._state.get_state()
            open_spreads = list(state.get("open_spreads", []) or [])
            for spread in open_spreads:
                if spread.get("spread_id") == spread_id:
                    spread["close_failed"] = True
                    spread["close_failed_reason"] = reason
                    spread["close_failed_at"] = datetime.now(IST).isoformat()
                    break

            spreads_traded = list(state.get("spreads_traded", []) or [])
            for traded in spreads_traded:
                if traded.get("spread_id") == spread_id:
                    traded["close_failed"] = True
                    traded["close_failed_reason"] = reason
                    traded["close_failed_at"] = datetime.now(IST).isoformat()
                    break

            self._state.update_state(
                open_spreads=open_spreads,
                spreads_traded=spreads_traded,
            )
        except Exception:
            logger.exception("_mark_spread_close_failed failed for %s (non-fatal)", spread_id)

    def _update_daily_pnl(self, realized_pnl: float) -> None:
        """Update daily P&L in state. Guards against NaN/inf corruption."""
        try:
            import math
            if not math.isfinite(realized_pnl):
                logger.error(
                    "_update_daily_pnl: non-finite P&L ₹%s rejected", realized_pnl
                )
                return
            state = self._state.get_state()
            current = float(state.get("daily_pnl", 0) or 0)
            new_pnl = current + realized_pnl
            virtual_capital = float(state.get("virtual_capital", 1_000_000) or 1_000_000)
            new_pct = (new_pnl / virtual_capital * 100) if virtual_capital else 0.0
            self._state.update_state(
                daily_pnl=new_pnl,
                daily_pnl_pct=round(new_pct, 4),
            )
        except Exception:
            logger.exception("_update_daily_pnl failed (non-fatal)")

    def get_session_stats(self) -> dict:
        """
        Return structured session statistics for EOD reporting.
        Reads spread trade history from state (spreads_traded list).
        """
        try:
            state = self._state.get_state()
            spreads_traded = state.get("spreads_traded", []) or []
            realized = sum(t.get("realized_pnl", 0) for t in spreads_traded if isinstance(t.get("realized_pnl"), (int, float)))
            open_spreads = self.get_open_spreads()
            unrealized = 0.0
            for sp in open_spreads:
                pnl_data = compute_spread_pnl(sp, self._client, self._feed)
                if pnl_data.get("data_ok") and pnl_data["unrealized_pnl"] is not None:
                    unrealized += pnl_data["unrealized_pnl"]
            return {
                "spreads_opened":   len(spreads_traded),
                "spreads_closed":   len([t for t in spreads_traded if t.get("closed")]),
                "spreads_open_now": len(open_spreads),
                "realized_pnl":     realized,
                "unrealized_pnl":   unrealized,
                "invalid_quote_incidents": self._invalid_quote_count,
                "daily_pnl":        float(state.get("daily_pnl", 0) or 0),
            }
        except Exception:
            logger.exception("get_session_stats failed")
            return {}

    # ── Status summary ────────────────────────────────────────────────────────

    def build_status_lines(self, shoonya_client=None, live_feed=None) -> list[str]:
        """
        Return human-readable status lines for all open spreads (for Telegram /status).
        """
        client = shoonya_client or self._client
        feed = live_feed or self._feed
        spreads = self.get_open_spreads()
        if not spreads:
            return ["- No open spreads"]

        lines = []
        for i, sp in enumerate(spreads, 1):
            pnl_data = compute_spread_pnl(sp, client, feed)
            pnl_str = (
                f"₹{pnl_data['unrealized_pnl']:+.2f}"
                if pnl_data.get("data_ok") and pnl_data["unrealized_pnl"] is not None
                else "N/A"
            )
            lines.append(
                f"{i}. [{sp.spread_id}] {sp.symbol} {sp.spread_type} "
                f"expiry={sp.expiry} "
                f"long={sp.long_tsym}@{sp.long_fill_price:.2f} "
                f"short={sp.short_tsym}@{sp.short_fill_price:.2f} "
                f"unrealized={pnl_str}"
            )
        return lines


# ──────────────────────────────────────────────────────────────────────────────
#   DESERIALIZATION
# ──────────────────────────────────────────────────────────────────────────────

def _dict_to_open_spread(d: dict) -> OpenSpread:
    """Reconstruct an OpenSpread from a state dict."""
    return OpenSpread(
        spread_id=d["spread_id"],
        symbol=d["symbol"],
        spread_type=d["spread_type"],
        direction=d["direction"],
        expiry=d["expiry"],
        lot_size=int(d["lot_size"]),
        lots=int(d["lots"]),
        long_tsym=d["long_tsym"],
        long_token=d["long_token"],
        long_action=d.get("long_action", "BUY"),
        long_strike=int(d["long_strike"]),
        long_option_type=d["long_option_type"],
        long_fill_price=float(d["long_fill_price"]),
        long_order_id=d["long_order_id"],
        short_tsym=d["short_tsym"],
        short_token=d["short_token"],
        short_action=d.get("short_action", "SELL"),
        short_strike=int(d["short_strike"]),
        short_option_type=d["short_option_type"],
        short_fill_price=float(d["short_fill_price"]),
        short_order_id=d["short_order_id"],
        net_debit_or_credit=float(d["net_debit_or_credit"]),
        max_profit=float(d["max_profit"]),
        max_loss=float(d["max_loss"]),
        breakeven=float(d["breakeven"]),
        opened_at=d["opened_at"],
        signal_id=d.get("signal_id", ""),
        strategy=d.get("strategy", ""),
        underlying_at_entry=float(d.get("underlying_at_entry", 0)),
    )
