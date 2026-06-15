#!/usr/bin/env python3
"""
main.py — GammaBlast virtual-only expiry-day options scanner.

Active loop: 09:15–15:15 IST on NIFTY Tuesdays and SENSEX Thursdays only.
No live orders ever. Virtual positions only.

Flow per tick (every SCAN_INTERVAL_SECONDS):
  1. Fetch index LTP
  2. Update ATM ladder recorder (activate new ATM±2 strikes)
  3. Sample due contracts (JSONL write)
  4. Push bucket data to candidate engine
  5. Evaluate candidates
  6. For RELEASED candidates: open virtual position if not already open
  7. For open positions: update LTP, check trailing exits
  8. Force-close all at 15:15 IST
  9. EOD: send summary, flush recorder
"""
from __future__ import annotations

import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config
config.setup_logging()

from broker.shoonya_client import ShoonyaClient, assert_client_identity  # noqa: E402
from tools.expiry_calendar import active_symbol_for_day, is_gammablast_day  # noqa: E402
from tools.options_chain import OptionsChain, fill_price  # noqa: E402
from tools.gamma_ladder_recorder import GammaLadderRecorder  # noqa: E402
from tools.gamma_candidate_engine import GammaCandidateEngine  # noqa: E402
from tools.virtual_position_book import VirtualPositionBook  # noqa: E402
from tools.trailing_exit import TrailingExitEngine  # noqa: E402
from tools.candidate_audit import CandidateAudit  # noqa: E402
from tools.journal_writer import JournalWriter  # noqa: E402
from tools.telegram_handler import TelegramHandler  # noqa: E402

logger = logging.getLogger("GammaBlast.Main")

IST = ZoneInfo("Asia/Kolkata")
_UTC = timezone.utc

assert_client_identity("GammaBlast")

_shutdown = False


def _handle_signal(sig, frame):
    global _shutdown
    logger.info("Signal %s received — shutting down", sig)
    _shutdown = True


def _ist_now() -> datetime:
    return datetime.now(IST)


def _ist_hhmm() -> str:
    return _ist_now().strftime("%H:%M")


def _after(hhmm: str) -> bool:
    return _ist_hhmm() >= hhmm


def _load_promoted_rules() -> dict:
    """Load promoted deterministic rule overrides from wiki/promoted_rules/."""
    overrides = {}
    rules_dir = config.PROMOTED_RULES_DIR
    for f in sorted(rules_dir.glob("*.json")):
        try:
            rule = json.loads(f.read_text())
            param = rule.get("parameter")
            value = rule.get("value")
            scope = rule.get("scope", "BOTH")
            if param and value is not None:
                overrides[f"{param}_{scope}"] = value
                overrides[param] = value  # also set global
                logger.info("Promoted rule: %s = %s (scope=%s)", param, value, scope)
        except Exception:
            logger.warning("Could not load promoted rule: %s", f)
    return overrides


def _build_bucket(recorder: GammaLadderRecorder, strike: int, option_type: str,
                  underlying_ltp: float) -> dict | None:
    """Collect a bucket summary from recorder's last sample for the engine."""
    rows = recorder.get_recent_rows(strike, option_type, n=1)
    if not rows:
        return None
    r = rows[-1]
    return {
        "ts": datetime.fromisoformat(r["timestamp_ist"]),
        "ltp": r.get("ltp"),
        "ltp_max": r.get("high") or r.get("ltp"),
        "vol_delta": r.get("volume") or 0,
        "oi": r.get("oi") or 0,
        "und_ltp": underlying_ltp,
        "bid_imbalance": _compute_bid_imbalance(r),
    }


def _compute_bid_imbalance(row: dict) -> float:
    bids = row.get("best_5_bids") or []
    asks = row.get("best_5_asks") or []
    bq = sum(d.get("qty", 0) for d in bids)
    aq = sum(d.get("qty", 0) for d in asks)
    total = bq + aq
    return (bq - aq) / total if total > 0 else 0.0


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    today = _ist_now().date()
    symbol = active_symbol_for_day(today)
    if not symbol:
        logger.info("Not a GammaBlast day (%s) — exiting", today)
        return 0

    logger.info("=== GammaBlast starting — %s %s ===", today, symbol)

    telegram = TelegramHandler(
        bot_token=config.GAMMABLAST_TELEGRAM_BOT_TOKEN,
        chat_id=config.GAMMABLAST_TELEGRAM_CHAT_ID,
    )
    audit = CandidateAudit(config.CANDIDATE_AUDIT_DIR)
    journal = JournalWriter(config.JOURNALS_DIR, config.VIRTUAL_CAPITAL)

    # Shoonya login
    client = ShoonyaClient()
    login_ok = client.login(
        user_id=config.SHOONYA_USER_ID,
        password=config.SHOONYA_PASSWORD,
        totp_secret=config.SHOONYA_TOTP_SECRET,
        api_key=config.SHOONYA_API_KEY,
        secret_code=config.SHOONYA_SECRET_CODE,
        vendor_code=config.SHOONYA_VENDOR_CODE,
        imei=config.SHOONYA_IMEI,
    )
    telegram.send_login_result(login_ok)
    if not login_ok:
        logger.error("Shoonya login failed — aborting")
        return 1

    chain = OptionsChain(client)
    lot_sizes = {symbol: config.LOT_SIZE.get(symbol, 25)}
    positions = VirtualPositionBook(config.STATE_FILE, lot_sizes)
    exit_engine = TrailingExitEngine(
        trail_activation_mult=config.TRAIL_ACTIVATION_MULT,
        trail_initial_fraction=config.TRAIL_INITIAL_FRACTION,
        trail_tight_mult=config.TRAIL_TIGHT_MULT,
        trail_tight_fraction=config.TRAIL_TIGHT_FRACTION,
        hard_stop_fraction=config.HARD_STOP_FRACTION,
        stale_data_seconds=config.STALE_DATA_SECONDS,
    )

    # Resolve expiry
    expiry_str = chain.resolve_expiry(symbol, today)
    if not expiry_str:
        logger.error("Could not resolve expiry for %s %s", symbol, today)
        telegram.send(f"⚠️ GammaBlast: Could not resolve {symbol} expiry for {today}")
        return 1

    strike_step = config.STRIKE_STEP[symbol]
    candidate_engine = GammaCandidateEngine(
        symbol=symbol,
        expiry_date=today,
        strike_step=strike_step,
        atm_offsets=config.ATM_OFFSETS,
    )
    recorder = GammaLadderRecorder(
        base_dir=config.DATA_EXPORTS_DIR,
        shoonya_client=client,
        options_chain=chain,
        symbol=symbol,
        expiry_str=expiry_str,
        strike_step=strike_step,
        atm_offsets=config.ATM_OFFSETS,
        sample_interval=config.SCAN_INTERVAL_SECONDS,
    )

    _load_promoted_rules()

    telegram.send_startup(symbol=symbol, expiry=expiry_str)
    journal.log_event("STARTUP", symbol=symbol, reason=f"expiry={expiry_str}")

    logger.info("Waiting for market open (%s)...", config.MARKET_OPEN_IST)
    while not _after(config.MARKET_OPEN_IST) and not _shutdown:
        time.sleep(10)

    logger.info("Market open — scanning loop started")
    last_quote_times: dict[str, float] = {}

    while not _shutdown:
        now_str = _ist_hhmm()

        # EOD force-close
        if now_str >= config.EOD_FORCE_CLOSE_IST:
            logger.info("EOD force-close at %s", now_str)
            open_pos = positions.open_positions()
            for pos in open_pos:
                pid = pos["position_id"]
                scrip = {
                    "token": pos["token"],
                    "exchange": pos["exchange"],
                }
                q = chain.get_quote(scrip)
                exit_p = fill_price(q, "SELL") if q else pos["current_ltp"]
                exit_time = _ist_now().isoformat()
                pnl = positions.close_position(pid, exit_p, exit_time, "EOD_FORCE_CLOSE")
                audit.record(
                    candidate_id=pid, stage="EOD_FORCE_CLOSE",
                    symbol=pos["symbol"], expiry=pos["expiry"],
                    strike=pos["strike"], option_type=pos["option_type"],
                    details={"exit_price": exit_p, "pnl": pnl},
                )
                telegram.send_virtual_exit(
                    pos["symbol"], pos["strike"], pos["option_type"],
                    pos["entry_price"], exit_p, pnl, "EOD"
                )
            recorder.flush()
            total_pnl = positions.total_pnl()
            closed = len(open_pos)
            candidates_seen = len(candidate_engine._board)
            telegram.send_eod_summary(symbol, closed, total_pnl, candidates_seen)
            journal.write_eod_summary(total_pnl, closed, candidates_seen)
            break

        # Session end (past 15:20)
        if now_str >= config.SESSION_END_IST:
            logger.info("Session end (%s) — exiting", now_str)
            break

        # ── main scan tick ────────────────────────────────────────────────────
        try:
            underlying_ltp = chain.get_index_ltp(symbol)
            if underlying_ltp:
                recorder.update_atm(underlying_ltp)
                recorder.sample_due_contracts()

                # push buckets to candidate engine
                for (strike, ot) in recorder.tracked_strikes():
                    bucket = _build_bucket(recorder, strike, ot, underlying_ltp)
                    if bucket:
                        candidate_engine.push_bucket(strike, ot, bucket)

                candidates = candidate_engine.evaluate_all(
                    current_ist_time=_ist_now(),
                    underlying_ltp=underlying_ltp,
                )

                # audit candidate state changes
                for c in candidates:
                    audit.record(
                        candidate_id=c.candidate_id, stage=f"{c.status}_DETECTED",
                        symbol=c.symbol, expiry=expiry_str,
                        strike=c.strike, option_type=c.option_type,
                        confidence_score=c.confidence_score,
                        details=c.observed_features,
                    )

                # open virtual positions for RELEASED candidates
                if not _after(config.ENTRY_CUTOFF_IST):
                    armed = candidate_engine.get_armed_candidates(config.MIN_CANDIDATE_SCORE)
                    for c in armed:
                        if c.status == "RELEASED" and positions.can_open(
                            symbol, expiry_str, c.strike, c.option_type
                        ):
                            scrip = chain.resolve_option(symbol, expiry_str, c.strike, c.option_type)
                            if scrip:
                                q = chain.get_quote(scrip)
                                if q:
                                    entry_p = fill_price(q, "BUY")
                                    if entry_p > 0:
                                        lot_size = config.LOT_SIZE.get(symbol, 25)
                                        pid = positions.open_position(
                                            symbol=symbol, expiry=expiry_str,
                                            strike=c.strike, option_type=c.option_type,
                                            tsym=scrip["tsym"], token=scrip["token"],
                                            exchange=scrip["exchange"],
                                            lots=1, lot_size=lot_size,
                                            entry_price=entry_p,
                                            entry_time=_ist_now().isoformat(),
                                        )
                                        last_quote_times[pid] = time.time()
                                        audit.record(
                                            candidate_id=pid, stage="VIRTUAL_ENTRY",
                                            symbol=symbol, expiry=expiry_str,
                                            strike=c.strike, option_type=c.option_type,
                                            confidence_score=c.confidence_score,
                                            details={"entry_price": entry_p},
                                        )
                                        telegram.send_virtual_entry(
                                            symbol, c.strike, c.option_type, entry_p, 1
                                        )
                                        journal.log_event(
                                            "VIRTUAL_ENTRY", symbol,
                                            f"{c.option_type} {c.strike} @ {entry_p:.2f}",
                                        )

                # update and check open positions
                for pos in positions.open_positions():
                    pid = pos["position_id"]
                    scrip = {"token": pos["token"], "exchange": pos["exchange"]}
                    q = chain.get_quote(scrip)
                    if q:
                        ltp = fill_price(q, "SELL") or pos["current_ltp"]
                        last_quote_times[pid] = time.time()
                    else:
                        ltp = pos["current_ltp"]

                    positions.update_ltp(pid, ltp)

                    new_trail = exit_engine.compute_trail_stop(pos, ltp)
                    if new_trail and (pos.get("trail_stop") is None or new_trail > pos["trail_stop"]):
                        positions.set_trail_stop(pid, new_trail)
                        audit.record(
                            candidate_id=pid, stage="TRAILING_UPDATE",
                            symbol=pos["symbol"], expiry=pos["expiry"],
                            strike=pos["strike"], option_type=pos["option_type"],
                            details={"trail_stop": new_trail, "current_ltp": ltp},
                        )

                    should_exit, reason = exit_engine.evaluate(
                        pos, ltp, last_quote_times.get(pid, time.time())
                    )
                    if should_exit:
                        exit_p = ltp
                        exit_time = _ist_now().isoformat()
                        pnl = positions.close_position(pid, exit_p, exit_time, reason)
                        audit.record(
                            candidate_id=pid, stage="VIRTUAL_EXIT",
                            symbol=pos["symbol"], expiry=pos["expiry"],
                            strike=pos["strike"], option_type=pos["option_type"],
                            details={"exit_price": exit_p, "pnl": pnl, "reason": reason},
                        )
                        telegram.send_virtual_exit(
                            pos["symbol"], pos["strike"], pos["option_type"],
                            pos["entry_price"], exit_p, pnl, reason
                        )

        except Exception:
            logger.exception("Scan tick error")

        time.sleep(config.SCAN_INTERVAL_SECONDS)

    logger.info("GammaBlast session ended — %s", _ist_now().isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
