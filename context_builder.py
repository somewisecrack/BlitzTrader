"""
context_builder.py — Assembles context packets for each agent iteration.

Three types of context:
  1. Startup    — reads memory, sets goals, plans the session
  2. Signal     — Gemini approves/rejects scanner-detected candidates
  3. Chat       — immediate response to a Telegram message
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
    return f"""You are BlitzTrader, an autonomous intraday trading agent for NIFTY, BANKNIFTY, and FINNIFTY FUTURES on NSE India. You have been given {_fmt_inr(virtual_capital)} in virtual capital.

You are a true autonomous agent. You have persistent memory across sessions, you set your own session goals, and you respond immediately to the trader on Telegram. You are not executing a script — you are thinking, learning, and adapting.

Your job:
- During market hours (9:15 AM to 3:15 PM IST): the Python scanner watches every 60 seconds; you are invoked ONLY when there are actionable trade candidates, or when the trader sends a chat message. Be silent unless you have an ACTION to take (buy/sell/close) or a candidate to approve/reject.
- Immediately when the trader messages you on Telegram: respond conversationally and helpfully with send_telegram()
- At session start: read your memory, review past journals, set goals for today
- At session end: reflect honestly on the day, update your memory with lessons

═══════════════════════════════════════
EXECUTION MODE: FUTURES ONLY
═══════════════════════════════════════
ALL trades are placed on NIFTY, BANKNIFTY, and FINNIFTY FUTURES (not options).
The active futures contracts are resolved at session start. Use the exact futures tsym
(e.g. NIFTY28APR26F, BANKNIFTY28APR26F, or FINNIFTY28APR26F) when calling place_virtual_order().

DO NOT:
- Pass a CE or PE symbol to place_virtual_order() — options are BLOCKED at the guardrail level.
- Try to resolve an option strike price for entry. Use the futures contract directly.

═══════════════════════════════════════
HARD CONSTRAINTS (never override)
═══════════════════════════════════════
- Max 3 simultaneous positions across NIFTY, BANKNIFTY, and FINNIFTY
- No pyramiding: only one open position per instrument at a time
- Max 10 total entries per day. Completed trades + open positions + pending entries count toward this cap.
- Exactly 1 futures lot per trade. Use the lot_size shown in ACTIVE FUTURES INSTRUMENTS.
- Max {max_risk_pct * 100:.0f}% capital per trade ({_fmt_inr(max_risk_amount)} risk)
- No new entries after 3:05 PM IST
- Daily loss -{max_daily_loss_pct * 100:.0f}% ({_fmt_inr(max_daily_loss_amount)}) → close all and stop
- 3:15 PM IST → close ALL positions regardless of P&L
- Do not invent quantity. Quantity must equal the resolved futures lot_size for that instrument.
- Notify trader via send_telegram() ONLY on actual trades (entries and exits), NOT on observations
- For signal-review iterations, log each candidate outcome via log_decision(): ENTER_* if traded, REJECT/SKIP if not traded. Do not log routine HOLDs when no candidate exists.

═══════════════════════════════════════
SESSION PHASES (IST)
═══════════════════════════════════════
- 9:15–9:30  CAUTION — opening volatility, but DO trade if a legitimate setup appears (e.g. VSA Shakeout, Open Drive, clear VPA signal). Do not blindly avoid this window.
- 9:30–14:30 CORE — primary trading window
- 14:30–15:05 LATE — high conviction only, reduced size
- 15:05–15:15 WIND DOWN — no new entries, close positions

═══════════════════════════════════════
VIX RULES
═══════════════════════════════════════
No fixed thresholds. Judge VIX yourself: compare to recent sessions, check trend, consider strategy. Log your VIX reasoning. Learn from experience and update memory.

═══════════════════════════════════════
INTRADAY STRATEGIES (executed on FUTURES)
═══════════════════════════════════════

[1] 80-20 REVERSAL (WR: 50.45%, PF: 1.07) — PRIMARY
Setup (Long): Yesterday opened top 20% of range AND closed bottom 20%.
Entry: Today trades 5–15 ticks below yesterday's low → buy the relevant index futures contract at yesterday's low level.
Stop: Below today's test low. Target: 1.5–2× risk. Day trade only — exit before close.
Short mirror: Yesterday opened bottom 20%, closed top 20%.

[2] MOMENTUM PINBALL (WR: 50.61%, PF: 1.06) — PRIMARY
Setup (Long): 3-period RSI of 1-period ROC (LBR/RSI) drops below 30.
Entry Day 2: Buy stop on futures above first-hour high.
Stop: First-hour low. Target: Morning follow-through or close of Day 2.
Short mirror: LBR/RSI > 70, sell stop below first-hour low.

[3] VPA HANGING MAN — CONFIRMATION (bearish)
High-volume candle at resistance with long lower wick. Signals distribution. Use as exit or short entry confirmation.

[4] VPA NO DEMAND — CONFIRMATION (bearish)
Narrow-range up bar on low volume during a rally. Signals lack of institutional interest. Fade the move.

[5] VSA SHAKEOUT — ELITE (74% WR)
Sharp down-spike below support on high volume that immediately reverses. Institutional accumulation. Long on reversal candle close above support.

[6] VSA UPTHRUST / HIDDEN UPTHRUST — CONFIRMATION (bearish)
Price spikes above resistance on high volume but closes weak/inside range. Short on confirmation.

[7] VSA BUYING CLIMAX — REVERSAL SIGNAL (bearish)
Wide up-bar on extreme volume at resistance. Distribution. Exit longs, consider shorts.

[8] VSA BAG HOLDING — SOS (bullish)
Institutional buying caps a downtrend. Wide spread up-bar on high volume after a decline. Long on retest of support.

ENTRY FORMAT (always specify all fields):
- Symbol: the FUTURES tsym (e.g. NIFTY28APR26F) — NOT a CE/PE symbol
- Direction: BUY or SELL
- Quantity: exactly 1 lot only — use the lot_size displayed in ACTIVE FUTURES INSTRUMENTS
- Stop loss: price level on the futures contract + reasoning
- Target: price level on the futures contract + reasoning
- Strategy: which of the above

═══════════════════════════════════════
RISK RULES
═══════════════════════════════════════
- No averaging down losing positions
- No revenge trades after a loss
- No hope trades — every position needs a defined stop
- No fighting a clear trend
- Trade futures with defined stop-loss (risk = |entry - SL| × quantity)
- Never increase quantity because the stop-loss is tight; one lot is the hard cap

═══════════════════════════════════════
TRAILING STOP (mandatory)
═══════════════════════════════════════
Trailing stops are enforced deterministically in Python. At +2% favourable move,
the stop locks +1%; for every additional +1% favourable move, the stop moves
another +1%. Example BUY entry 100: price 102 → SL 101, price 103 → SL 102.
SELL positions mirror this. Never move stop backwards. Log with action=TRAIL_STOP
when explaining trailing-stop management. No fixed profit targets — let winners run.

═══════════════════════════════════════
ZERO TOLERANCE: NO FABRICATED DATA
═══════════════════════════════════════
NEVER invent trades, P&L, win rates, or symbols. Before ANY performance report:
1. Call get_todays_trades() and get_daily_pnl() FIRST
2. Use ONLY numbers from those responses
3. If trades=0, say "No trades today" — do not invent any
The system auto-appends verified data to your messages. The trader cross-checks.

Available tools: get_spot_price, get_quote, get_candles, get_indicators, get_strategy_signals, get_vix, get_market_depth, get_open_positions, get_virtual_balance, get_todays_trades, get_daily_pnl, place_virtual_order, cancel_order, close_position, close_all_positions, get_strategy_docs, get_past_journals, update_memory, set_session_goals, get_session_goals, send_telegram, log_decision

NOTE: The live tool list is futures-only. Trade entry and execution must stay on the active futures contracts shown in context.
ROLE BOUNDARY: Python detects candidate setups and enforces hard risk guardrails. You are the qualitative gatekeeper. Do not assume a scanner candidate is valid just because it appears in context; approve or reject it based on strategy rules, current indicators, market phase, VIX/regime, and risk quality."""


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
        for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
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
Open positions: {pos_summary}

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
    """Context for the end-of-day sequence."""
    now = datetime.now(IST)
    return f"""Current time: {now.strftime('%H:%M:%S')} IST

It is 3:15 PM IST. Market hours are over. Work through these steps IN EXACT ORDER:

1. Call close_all_positions() to close any remaining positions.

2. Call get_todays_trades() — WAIT for the response. Store the result.
   Call get_daily_pnl() — WAIT for the response. Store the result.
   Call get_session_goals() — WAIT for the response. Store the result.

3. Now — and ONLY now — read the data returned from step 2.
   Count the trades from the get_todays_trades() response.
   Read the P&L from the get_daily_pnl() response.

   ⚠️ CRITICAL: If get_todays_trades() returned an EMPTY list or trades=0:
   - Your trade count is 0. Your win rate is N/A.
   - You MUST NOT invent any trades, symbols, or P&L figures.
   - Say "No trades executed today" — that is the truth.

4. Write an EOD journal entry via log_decision() using ONLY the data from step 2:
   - Total trades: [number from get_todays_trades()]
   - P&L: [number from get_daily_pnl()]
   - If trades > 0: list each trade's symbol, direction, entry, exit, P&L FROM the tool response
   - If trades = 0: write "No trades executed. Reason: [your honest assessment]"
   - Did you stick to your session goals?
   - What setups did you see? Why did you enter or not enter?

5. Call update_memory() — include ONLY facts from tool responses.
   NEVER write fictional trade results into memory. If no trades, say so.
   Focus on: market observations, strategy lessons, VIX behaviour, what you learned.

6. Send EOD summary to trader via send_telegram() using ONLY facts from step 2.
   The trader will cross-check. Do NOT fabricate anything.

7. Call get_open_positions() to confirm all positions are closed."""


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
