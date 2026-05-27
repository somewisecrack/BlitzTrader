"""
context_builder.py — Assembles context packets for each agent iteration.

Three types of context:
  1. Startup    — legacy helper (live startup is deterministic in Python)
  2. Chat       — immediate response to a Telegram message
  3. EOD        — Gemini-only end-of-day reflection and summary
"""
import logging
from datetime import datetime

import pytz
from config import VIRTUAL_CAPITAL, MAX_RISK_PCT, MAX_DAILY_LOSS_PCT

logger = logging.getLogger("BlitzTrader.ContextBuilder")

IST = pytz.timezone("Asia/Kolkata")

def _fmt_inr(value: float) -> str:
    return f"₹{value:,.0f}"


def build_system_prompt(
    virtual_capital: float = VIRTUAL_CAPITAL,
    max_risk_pct: float = MAX_RISK_PCT,
    max_daily_loss_pct: float = MAX_DAILY_LOSS_PCT,
) -> str:
    max_risk_amount = virtual_capital * max_risk_pct
    max_daily_loss_amount = virtual_capital * max_daily_loss_pct
    return f"""You are BlitzTrader, an autonomous intraday trading agent for NIFTY and BANKNIFTY index options on NSE India. You have been given {_fmt_inr(virtual_capital)} in virtual capital.

You are BlitzTrader's reporting and reflection layer. The live trading engine is deterministic Python. You do not decide entries or exits during the session unless the trader explicitly asks you to analyze/report.

Your job:
- When the trader messages you on Telegram: respond conversationally and helpfully with send_telegram()
- At session end: reflect honestly on the day, update memory with lessons, and write the EOD summary
- Never claim you approved or rejected a live trade unless the user is explicitly asking for post-trade analysis

═══════════════════════════════════════
EXECUTION MODE: NSE INDEX OPTION VERTICAL SPREADS ONLY
═══════════════════════════════════════
ALL trades are hedged vertical spreads on NIFTY and BANKNIFTY options.
Allowed spread types: BULL_CALL, BULL_PUT, BEAR_PUT, BEAR_CALL.

DO NOT:
- Place outright futures orders (futures are BLOCKED)
- Place single-leg (naked) option orders (naked options are BLOCKED)
- Use FINNIFTY, SENSEX, MIDCPNIFTY, or any equity symbol
- Attempt to modify strikes, expiry, quantity, or spread type (Python owns these)

Python controls ALL entries and exits. For each spread candidate Python builds, the Gemini gatekeeper may APPROVE (spread placed) or REJECT (spread skipped). No other trading authority is granted.

═══════════════════════════════════════
HARD CONSTRAINTS (never override)
═══════════════════════════════════════
- Max 2 simultaneous open spreads across NIFTY and BANKNIFTY
- No pyramiding: only one open spread per instrument at a time
- Max {max_risk_pct * 100:.0f}% capital per spread ({_fmt_inr(max_risk_amount)} max loss per spread)
- No new entries after 3:05 PM IST
- Daily loss -{max_daily_loss_pct * 100:.0f}% ({_fmt_inr(max_daily_loss_amount)}) → close all and stop
- 3:15 PM IST → close ALL open spreads regardless of P&L
- Python engine owns live spread execution, guardrails, and exit management
- Do not fabricate discretionary trade approvals that never happened in Python

═══════════════════════════════════════
SPREAD EXIT THRESHOLDS (Python-enforced)
═══════════════════════════════════════
- Max-loss exit:        60% of max possible loss
- Credit spread TP:     60% of max possible profit
- Debit spread TP:      70% of max possible profit
- EOD forced close:     3:15 PM IST, all spreads closed regardless of P&L

═══════════════════════════════════════
SESSION PHASES (IST)
═══════════════════════════════════════
- 9:15–9:30  CAUTION — opening volatility; trade only if setup is confirmed
- 9:30–14:30 CORE — primary trading window
- 14:30–15:05 LATE — high conviction only
- 15:05–15:15 WIND DOWN — no new entries, Python closes spreads

═══════════════════════════════════════
VIX RULES
═══════════════════════════════════════
No fixed thresholds. Judge VIX yourself: compare to recent sessions, check trend, consider impact on option premiums. Log your VIX reasoning. Learn from experience and update memory.

═══════════════════════════════════════
INTRADAY STRATEGIES (signals for spread selection)
═══════════════════════════════════════

[1] VP-01 Counter Bull Trap — Reversal (bearish directional signal → BEAR_CALL spread)
[2] VP-02 Counter Bear Trap — Reversal (bullish directional signal → BULL_PUT spread)
[3] VP-05 3EMA Trend        — Trend (momentum → BULL_CALL or BEAR_PUT debit spread)
[4] VP-07 Wicks Pullback    — Pullback (momentum → debit spread in trend direction)
[5] VP-14 Morning Star      — Candlestick reversal (bullish → BULL_PUT credit spread)
[6] VP-15 Evening Star      — Candlestick reversal (bearish → BEAR_CALL credit spread)
[7] VP-18 M-Pattern Double Top  — Pattern reversal (bearish → BEAR_CALL credit spread)
[8] VP-19 W-Pattern Double Bottom — Pattern reversal (bullish → BULL_PUT credit spread)
[9] VP-21 Extreme Candle Reversal — Volatility reversal (credit spread in reversal direction)

Spread type is selected deterministically by Python based on strategy + direction.
Python also selects expiry, strikes, lot size, and quantity. You approve or reject the full candidate only.

═══════════════════════════════════════
RISK RULES
═══════════════════════════════════════
- No averaging down (no adding to a spread position)
- No revenge trades after a loss
- Both legs are always present — no naked exposure is possible
- Max loss is defined at entry (debit paid or credit received minus spread width)
- Python enforces all exit rules deterministically

═══════════════════════════════════════
ZERO TOLERANCE: NO FABRICATED DATA
═══════════════════════════════════════
NEVER invent trades, P&L, win rates, or symbols. Before ANY performance report:
1. Call get_todays_trades() and get_daily_pnl() FIRST
2. Use ONLY numbers from those responses
3. If trades=0, say "No trades today" — do not invent any
The system auto-appends verified data to your messages. The trader cross-checks.

Available tools: get_spot_price, get_quote, get_candles, get_indicators, get_strategy_signals, get_vix, get_market_depth, get_open_positions, get_virtual_balance, get_todays_trades, get_daily_pnl, cancel_order, get_strategy_docs, get_past_journals, update_memory, set_session_goals, get_session_goals, send_telegram, log_decision, get_status_with_serials, exit_spread_by_serial

SERIAL EXIT ROUTING:
When the trader sends "exit 2", "close position 2", "square off #3", "close serial 2", "close #2":
1. Call get_status_with_serials() first if no recent index (within 30 min)
2. Call exit_spread_by_serial(serial=N) — this closes both legs of the spread and sends Telegram confirmation
NEVER open a new position via exit_spread_by_serial. It only closes spreads.

NOTE: place_virtual_order, close_position, and close_all_positions are NOT available to the live agent.
Python owns all spread entry and exit execution.
ROLE BOUNDARY: Python owns live trade decisions, execution, and guardrails. You are used for trader-facing reporting, diagnostics, EOD reflection, and Telegram Q&A."""


SYSTEM_PROMPT = build_system_prompt()


def build_startup_context() -> str:
    """
    Context for session startup. Claude reads its memory, sets goals,
    and plans the session before market opens.
    """
    now = datetime.now(IST)
    return f"""Current time: {now.strftime('%H:%M:%S')} IST — New trading session starting.

Do these steps in order. Use ONE tool call per step — do not batch.

1. Call get_past_journals() — read your memory and recent journals.
   Only reference facts from the response. Do NOT invent past performance.

2. Call get_strategy_docs() to review available strategies.

3. Call set_session_goals() with goals=["goal1", "goal2", "goal3"] based on your journals.

4. Call send_telegram() with a short startup message: lessons learned, today's goals, strategies.
   Do NOT fabricate any past trade data. If no trades happened, say so.

5. Call log_decision() with action="START" and your plan.

Keep messages concise."""


def build_iteration_context(
    state_manager,
    telegram_handler,
    order_execution,
    goal_manager=None,
    pending_signals: list = None,
    active_tokens: dict = None,
) -> str:
    """
    Build the signal-review context.

    pending_signals: list of signal dicts already surfaced by the background Python
    scanner (get_strategy_signals).  When provided the agent must NOT call
    get_strategy_signals again — those results are pre-injected below.
    """
    now = datetime.now(IST)
    state = state_manager.get_state()

    pnl = state.get("daily_pnl", 0)
    pnl_pct = state.get("daily_pnl_pct", 0)
    positions = state.get("positions", [])
    trade_count = state.get("trade_count", 0)
    available = state.get("available_balance", 0)
    pending = state.get("pending_orders", [])

    # Session goals
    goals_section = ""
    if goal_manager and goal_manager.has_goals():
        goals_section = "\nYour session goals:\n" + goal_manager._format_goals() + "\n"

    # Positions summary
    if positions:
        pos_lines = [
            f"  - {p['direction']} {p['quantity']}x {p['symbol']} "
            f"(entered @ ₹{p['entry_price']:.2f})"
            for p in positions
        ]
        pos_summary = f"{len(positions)} open:\n" + "\n".join(pos_lines)
    else:
        pos_summary = "None"

    # Pending orders
    pending_summary = ""
    if pending:
        pend_lines = [
            f"  - {o['direction']} {o['quantity']}x {o['symbol']} "
            f"LIMIT @ ₹{o['limit_price']:.2f}"
            for o in pending
        ]
        pending_summary = f"\nPending limit orders: {len(pending)}\n" + "\n".join(pend_lines)

    # Telegram commands
    commands = telegram_handler.get_pending_commands() if telegram_handler else []
    command_context = ""
    if commands:
        for cmd in commands:
            injection = telegram_handler.inject_command(cmd["command"], cmd["text"])
            command_context += f"\n💬 USER MESSAGE: {injection}"
            if cmd["command"] == "/pause":
                state_manager.update_state(is_paused=True)
            elif cmd["command"] == "/resume":
                state_manager.update_state(is_paused=False)

    market_phase = _get_market_phase(now)

    # Build active futures section from resolved tokens
    futures_section = ""
    if active_tokens:
        lines = ["ACTIVE FUTURES INSTRUMENTS:"]
        for sym in ("NIFTY", "BANKNIFTY"):
            info = active_tokens.get(sym)
            if info and info.get("tsym"):
                tsym = info["tsym"]
                token = info.get("token", "N/A")
                exchange = info.get("exchange", "NFO")
                lot_size = info.get("lot_size", "unknown")
                lines.append(
                    f"  {sym:<12} → {tsym:<22} "
                    f"({exchange}, token: {token}, lot_size: {lot_size})"
                )
        if len(lines) > 1:
            lines.append("Use these exact tsym strings when calling place_virtual_order().")
            futures_section = "\n" + "\n".join(lines) + "\n"

    # Build the signal section — pre-injected by background scanner or absent
    if pending_signals:
        import json as _json
        signal_lines = _json.dumps(pending_signals, indent=2, default=str)
        signal_section = f"""
BACKGROUND SCANNER — NEW SIGNALS DETECTED ({len(pending_signals)}):
{signal_lines}

DO NOT call get_strategy_signals() — those results are already above.
Python has NOT scored these candidates. It only detected possible setups and
filtered hard no-trade constraints. You are the gatekeeper: approve only if the
full strategy context is good enough.
For each signal above you MUST either:
  a) APPROVE it — call get_indicators() to confirm, then place_virtual_order(), or
  b) REJECT it — call log_decision() with the exact reason (wrong phase, poor structure, weak trend, spread too wide, etc.)
Silently ignoring a signal is not allowed."""
        analysis_sequence = """
MANDATORY ANALYSIS SEQUENCE (signal-triggered iteration):
1. For each signal listed above:
   a. Call get_indicators(symbol, interval) for the signal's symbol and tool interval to confirm
      EMAs, RSI, ADX, ATR align with the strategy rules.
      For daily-first-hour signals, interval is set to "3" and signal_timeframe shows the setup type.
   b. If requires_volume_confirmation=true, call get_candles() and verify avg_volume_20.
   c. If you approve: place_virtual_order() using signal.execution_symbol or the exact FUTURES tsym
      shown above under ACTIVE FUTURES INSTRUMENTS.
   d. If you reject: log_decision() with action="REJECT" or action="SKIP" and a specific reason.
2. After handling all signals, check open positions — adjust stops if needed.
3. Only place orders if ALL conditions in the strategy doc are met — not just some."""
    else:
        signal_section = ""
        analysis_sequence = """
NO SIGNAL-REVIEW WORK:
This context should only be used for chat/startup/EOD or exceptional diagnostics.
Do not run a broad market sweep and do not log a routine HOLD just because no
scanner candidate is present."""

    return f"""Current time: {now.strftime('%H:%M:%S')} IST
Market phase: {market_phase}
Session P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)
Open positions: {pos_summary}
Trades today: {trade_count}
Virtual balance available: ₹{available:,.2f}{pending_summary}{futures_section}{goals_section}{command_context}{signal_section}
{analysis_sequence}

IMPORTANT: Be silent in this iteration unless you have an ACTION:
- Send Telegram ONLY if you are entering or exiting a position
- Do NOT send "thinking" or "observing" messages during market scans
- In signal-review iterations, log every candidate as ENTER/REJECT/SKIP
- NEVER claim a trade was made unless place_virtual_order() succeeded in THIS iteration

Reminder: The state data above (P&L, positions, trade count) comes from the system.
These numbers are GROUND TRUTH. Do not contradict them or invent different numbers.

It is your turn. Use your tools to analyze the market, and decide what to do. Think step by step."""


def build_chat_context(
    chat_messages: list[dict],
    state_manager,
    order_execution,
) -> str:
    """
    Lightweight context for an immediate Telegram chat response.
    Triggered when the trader sends a message between scheduled iterations.
    Claude should respond quickly and conversationally.
    """
    now = datetime.now(IST)
    state = state_manager.get_state()

    pnl = state.get("daily_pnl", 0)
    pnl_pct = state.get("daily_pnl_pct", 0)
    positions = state.get("positions", [])

    pos_summary = "None"
    if positions:
        pos_summary = ", ".join(
            f"{p['direction']} {p['symbol']}" for p in positions
        )

    messages_text = "\n".join(f"  • {m['text']}" for m in chat_messages)

    return f"""Current time: {now.strftime('%H:%M:%S')} IST
Session P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)
Open option spreads: {pos_summary}
The trader has sent you a message on Telegram:
{messages_text}

Respond directly and conversationally via send_telegram().
You can answer ANY natural question: "whats happening?", "how am I doing?", "should I close positions?", etc.
Use your tools to get live data and answer accurately. Be concise and helpful.
Do not run a full market analysis unless specifically asked — this is a chat response, not a scheduled iteration.

IMPORTANT: If the trader asks about trades, P&L, or positions — call get_todays_trades(),
get_daily_pnl(), or get_open_positions() FIRST and reply with ONLY the data they return.
If the trader asks about capital, balance, available funds, or margin — call
get_virtual_balance() FIRST and reply with ONLY the data it returns.
The P&L and position data shown above is ground truth from the system — never contradict it."""


def build_eod_context() -> str:
    """Context for the end-of-day sequence.

    NOTE: Python has already closed all open spreads via
    SpreadPortfolio.check_and_exit_spreads(force_close_all=True)
    before this context is built.  Do NOT call close_all_positions().
    """
    now = datetime.now(IST)
    return f"""Current time: {now.strftime('%H:%M:%S')} IST
It is 3:15 PM IST. Market hours are over.

All open option spreads have been closed by Python before this step.
Your role is EOD reflection only — do NOT call close_all_positions().

Work through these steps IN EXACT ORDER:

1. Call get_todays_trades() — WAIT for the response. Store the result.
   Call get_daily_pnl() — WAIT for the response. Store the result.
   Call get_session_goals() — WAIT for the response. Store the result.

2. Now — and ONLY now — read the data returned from step 1.
   Count the trades from the get_todays_trades() response.
   Read the P&L from the get_daily_pnl() response.

   ⚠️ CRITICAL: If get_todays_trades() returned an EMPTY list or trades=0:
   - Your trade count is 0. Your win rate is N/A.
   - You MUST NOT invent any trades, symbols, or P&L figures.
   - Say "No trades executed today" — that is the truth.

3. Write an EOD journal entry via log_decision() using ONLY the data from step 1:
   - Total spreads: [number from get_todays_trades()]
   - P&L: [number from get_daily_pnl()]
   - If trades > 0: list each spread's symbol, type, direction, entry credit/debit, exit P&L FROM the tool response
   - If trades = 0: write "No spreads executed. Reason: [your honest assessment]"
   - Did you stick to your session goals?
   - What setups did you see? Why did you enter or not enter?

4. Call update_memory() — include ONLY facts from tool responses.
   NEVER write fictional trade results into memory. If no trades, say so.
   Focus on: market observations, strategy lessons, VIX behaviour, spread selection quality.

5. Send EOD summary to trader via send_telegram() using ONLY facts from step 1.
   The trader will cross-check. Do NOT fabricate anything.

6. Call get_open_positions() to confirm all spreads are closed."""


def build_abort_context() -> str:
    """Context for an abort command."""
    return """⚠️ ABORT COMMAND RECEIVED.

You must immediately:
1. Call close_all_positions()
2. Call log_decision() with action=ABORT and your summary
3. Call send_telegram() confirming all positions closed
4. Do NOT open any new positions

This overrides all strategy logic."""


def _get_market_phase(now: datetime) -> str:
    """Determine current market phase."""
    t = now.hour * 60 + now.minute

    if t < 9 * 60 + 15:
        return "Pre-market (waiting for 9:15 AM open)"
    elif t < 9 * 60 + 30:
        return "Opening volatility (9:15-9:30, be cautious)"
    elif t < 15 * 60 + 5:
        return "Regular trading hours"
    elif t < 15 * 60 + 15:
        return "Final hour — no new entries after 3:05 PM"
    else:
        return "Market closed — EOD sequence"


# ──────────────────────────────────────────────────────────────────
#   GATEKEEPER CONTEXT
# ──────────────────────────────────────────────────────────────────

def build_gatekeeper_context(signal: dict, indicators: dict) -> str:
    """
    Build a compact, fact-dense context string for the Gemini entry gatekeeper.

    Only includes Python-verified indicator data.  Never constructs or infers
    market data from external sources — data comes directly from the scanner.

    Args:
        signal:     The candidate signal dict (symbol, strategy, direction, etc.)
        indicators: Output from market_data.get_indicators() — already validated by Python

    Returns:
        A plain-text summary of indicators for the gatekeeper prompt.
    """
    symbol    = signal.get("symbol", "?")
    interval  = signal.get("interval", "?")
    direction = signal.get("direction", "?")

    price         = indicators.get("current_price")
    ema20         = indicators.get("ema20")
    adx14         = indicators.get("adx14")
    rsi14         = indicators.get("rsi14")
    avg_vol20     = float(indicators.get("avg_volume_20") or 0)
    ema_bull      = bool(indicators.get("ema_stacked_bull"))
    ema_bear      = bool(indicators.get("ema_stacked_bear"))
    vwap          = indicators.get("vwap")
    atr14         = indicators.get("atr14")
    bb_upper      = indicators.get("bb_upper")
    bb_lower      = indicators.get("bb_lower")

    lines = [
        f"Instrument: {symbol} | Timeframe: {interval}m | Signal direction: {direction}",
    ]
    if price is not None:
        lines.append(f"Current price: ₹{price:.2f}")
    if ema20 is not None:
        lines.append(f"EMA20: {ema20:.2f}  (price {'above' if price and price > ema20 else 'below'} EMA20)")
    if adx14 is not None:
        trend_str = "strong trend" if adx14 >= 25 else ("moderate" if adx14 >= 18 else "weak/ranging")
        lines.append(f"ADX14: {adx14:.1f} ({trend_str})")
    if rsi14 is not None:
        rsi_str = "overbought" if rsi14 > 70 else ("oversold" if rsi14 < 30 else "neutral")
        lines.append(f"RSI14: {rsi14:.1f} ({rsi_str})")
    if avg_vol20 > 0:
        lines.append(f"Avg volume 20: {avg_vol20:.0f}")
    lines.append(
        f"EMA stack: {'bullish' if ema_bull else 'not bullish'} | "
        f"{'bearish' if ema_bear else 'not bearish'}"
    )
    if vwap is not None:
        lines.append(f"VWAP: {vwap:.2f}  (price {'above' if price and price > vwap else 'below'} VWAP)")
    if atr14 is not None:
        lines.append(f"ATR14: {atr14:.2f}")
    if bb_upper is not None and bb_lower is not None:
        lines.append(f"Bollinger Bands: {bb_lower:.2f} – {bb_upper:.2f}")

    stop_loss = signal.get("stop_loss")
    target = signal.get("target")
    entry_ref = signal.get("entry_reference")
    if entry_ref and stop_loss and target:
        risk = abs(entry_ref - stop_loss)
        reward = abs(target - entry_ref)
        rr = reward / risk if risk > 0 else 0
        lines.append(
            f"R:R = {rr:.1f}:1  (risk ₹{risk:.2f}, reward ₹{reward:.2f})"
        )

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
#   SPREAD GATEKEEPER CONTEXT
# ──────────────────────────────────────────────────────────────────

def build_spread_gatekeeper_context(candidate, indicators: dict) -> str:
    """
    Build a compact, fact-dense context string for the Gemini spread gatekeeper.

    Args:
        candidate:   A SpreadCandidate dataclass (tools/options_spread_builder.py).
        indicators:  Output from market_data.get_indicators() — already validated by Python.

    Returns:
        Plain-text summary for the Gemini gatekeeper prompt.
        Gemini must respond APPROVE or REJECT only — Python owns all execution.
    """
    from config import (
        SPREAD_MAX_LOSS_EXIT_FRACTION,
        SPREAD_CREDIT_TP_FRACTION,
        SPREAD_DEBIT_TP_FRACTION,
    )

    # Spread type → human label
    _SPREAD_LABELS = {
        "BULL_CALL": "BULL_CALL (debit spread)",
        "BEAR_PUT":  "BEAR_PUT  (debit spread)",
        "BULL_PUT":  "BULL_PUT  (credit spread)",
        "BEAR_CALL": "BEAR_CALL (credit spread)",
    }
    spread_label = _SPREAD_LABELS.get(candidate.spread_type, candidate.spread_type)
    is_debit = candidate.spread_type in ("BULL_CALL", "BEAR_PUT")
    is_credit = not is_debit

    # Leg extraction — legs[0] is always the long/protective leg
    long_leg  = candidate.legs[0]
    short_leg = candidate.legs[1]

    # Exit threshold amounts
    max_loss_exit_amt  = candidate.max_loss  * SPREAD_MAX_LOSS_EXIT_FRACTION
    if is_debit:
        tp_amt = candidate.max_profit * SPREAD_DEBIT_TP_FRACTION
        tp_label = f"Debit TP ({SPREAD_DEBIT_TP_FRACTION*100:.0f}%)"
    else:
        tp_amt = candidate.max_profit * SPREAD_CREDIT_TP_FRACTION
        tp_label = f"Credit TP ({SPREAD_CREDIT_TP_FRACTION*100:.0f}%)"

    # Indicator values
    price     = indicators.get("current_price")
    ema20     = indicators.get("ema20")
    adx14     = indicators.get("adx14")
    rsi14     = indicators.get("rsi14")
    ema_bull  = bool(indicators.get("ema_stacked_bull"))
    ema_bear  = bool(indicators.get("ema_stacked_bear"))
    ema_stack = "BULL" if ema_bull else ("BEAR" if ema_bear else "NEUTRAL")

    ind_parts = []
    if ema20  is not None: ind_parts.append(f"EMA20={ema20:,.1f}")
    if adx14  is not None: ind_parts.append(f"ADX14={adx14:.1f}")
    if rsi14  is not None: ind_parts.append(f"RSI14={rsi14:.1f}")
    ind_parts.append(f"EMA_stack={ema_stack}")
    indicator_line = "  ".join(ind_parts)

    # Strategy rationale
    strategy_code = candidate.strategy.split()[0] if candidate.strategy else "?"
    if is_debit:
        rationale = f"{strategy_code} is a momentum strategy → debit spread selected."
    else:
        rationale = f"{strategy_code} is a mean-reversion strategy → credit spread selected."

    dc_label   = "Net debit" if is_debit else "Net credit"
    price_str  = f"₹{candidate.underlying_price:,.2f}" if candidate.underlying_price else "N/A"

    lines = [
        "SPREAD ENTRY CANDIDATE",
        f"Underlying: {candidate.symbol}  Current price: {price_str}",
        f"Strategy: {candidate.strategy}  Direction: {candidate.direction}",
        f"Spread type: {spread_label}",
        f"Expiry: {candidate.expiry}",
        "",
        f"Long leg:  {long_leg.tsym:<28} strike={long_leg.strike:>7}  fill_est=₹{long_leg.fill_price:.2f}",
        f"Short leg: {short_leg.tsym:<28} strike={short_leg.strike:>7}  fill_est=₹{short_leg.fill_price:.2f}",
        "",
        f"{dc_label}:  ₹{candidate.net_debit_or_credit:.2f}/lot  Lot size: {candidate.lot_size}",
        f"Max profit: ₹{candidate.max_profit:,.2f}  Max loss: ₹{candidate.max_loss:,.2f}",
        f"Breakeven:  {candidate.breakeven:,.2f}  R:R = {candidate.risk_reward:.2f}",
        "",
        "Risk thresholds (Python-enforced):",
        f"  Max-loss exit:  {SPREAD_MAX_LOSS_EXIT_FRACTION*100:.0f}% = ₹{max_loss_exit_amt:,.2f} loss",
        f"  {tp_label}: = ₹{tp_amt:,.2f} profit",
        "",
        f"Indicators: {indicator_line}",
        "",
        f"Spread type rationale: {rationale}",
        "Gemini role: APPROVE or REJECT this spread candidate only.",
        "DO NOT modify strikes, expiry, quantity, or spread type.",
        "Python owns all execution. Respond with APPROVE or REJECT and a one-line reason.",
    ]
    return "\n".join(lines)
