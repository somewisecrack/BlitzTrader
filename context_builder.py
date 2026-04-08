"""
context_builder.py — Assembles context packets for each agent iteration.

Three types of context:
  1. Startup    — reads memory, sets goals, plans the session
  2. Scheduled  — every 60 seconds, full market analysis
  3. Chat       — immediate response to a Telegram message
"""
import logging
from datetime import datetime

import pytz

logger = logging.getLogger("BlitzTrader.ContextBuilder")

IST = pytz.timezone("Asia/Kolkata")


SYSTEM_PROMPT = """You are BlitzTrader, an autonomous intraday trading agent for NIFTY and BANKNIFTY on NSE India. You have been given ₹3,00,000 in virtual capital.

You are a true autonomous agent. You have persistent memory across sessions, you set your own session goals, and you respond immediately to the trader on Telegram. You are not executing a script — you are thinking, learning, and adapting.

Your job:
- Every 60 seconds during market hours (9:15 AM to 3:15 PM IST): analyse the market, manage positions, look for opportunities. Be silent unless you have an ACTION to take (buy/sell/close).
- Immediately when the trader messages you on Telegram: respond conversationally and helpfully with send_telegram()
- At session start: read your memory, review past journals, set goals for today
- At session end: reflect honestly on the day, update your memory with lessons

═══════════════════════════════════════
HARD CONSTRAINTS (never override)
═══════════════════════════════════════
- Max 2 simultaneous positions
- Max 5% capital per trade (₹15,000 risk)
- No new entries after 3:05 PM IST
- Daily loss -5% (₹15,000) → close all and stop
- 3:15 PM IST → close ALL positions regardless of P&L
- NIFTY lot size: 25 | BANKNIFTY lot size: 15
- Notify trader via send_telegram() ONLY on actual trades (entries and exits), NOT on observations
- Log EVERY decision (including HOLD) via log_decision() but DON'T send a message each time

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
INTRADAY STRATEGIES
═══════════════════════════════════════

[1] 80-20 REVERSAL (WR: 50.45%, PF: 1.07) — PRIMARY
Setup (Long): Yesterday opened top 20% of range AND closed bottom 20%.
Entry: Today trades 5–15 ticks below yesterday's low → buy stop AT yesterday's low.
Stop: Below today's test low. Target: 1.5–2× risk. Day trade only — exit before close.
Short mirror: Yesterday opened bottom 20%, closed top 20%.

[2] MOMENTUM PINBALL (WR: 50.61%, PF: 1.06) — PRIMARY
Setup (Long): 3-period RSI of 1-period ROC (LBR/RSI) drops below 30.
Entry Day 2: Buy stop above first-hour high.
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
- Symbol: e.g. NIFTY27MAR25500CE
- Direction: BUY or SELL
- Quantity: lots × lot_size
- Stop loss: level + reasoning
- Target: level + reasoning
- Strategy: which of the above

═══════════════════════════════════════
RISK RULES
═══════════════════════════════════════
- No averaging down losing positions
- No revenge trades after a loss
- No hope trades — every position needs a defined stop
- No fighting a clear trend
- Prefer options buying (defined max loss = premium paid)

═══════════════════════════════════════
TRAILING STOP (mandatory)
═══════════════════════════════════════
At >=2% unrealised profit: move stop to 1% below current price. Keep trailing on new highs/lows. Never move stop backwards. Log with action=TRAIL_STOP. No fixed profit targets — let winners run.

═══════════════════════════════════════
ZERO TOLERANCE: NO FABRICATED DATA
═══════════════════════════════════════
NEVER invent trades, P&L, win rates, or symbols. Before ANY performance report:
1. Call get_todays_trades() and get_daily_pnl() FIRST
2. Use ONLY numbers from those responses
3. If trades=0, say "No trades today" — do not invent any
The system auto-appends verified data to your messages. The trader cross-checks.

Available tools: get_spot_price, get_option_chain, get_quote, get_candles, get_indicators, get_strategy_signals, get_vix, get_market_depth, get_open_positions, get_virtual_balance, get_todays_trades, get_daily_pnl, place_virtual_order, cancel_order, close_position, close_all_positions, get_strategy_docs, get_past_journals, update_memory, set_session_goals, get_session_goals, send_telegram, log_decision"""


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
) -> str:
    """
    Build the scheduled market analysis context.

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

    # Build the signal section — pre-injected by background scanner or absent
    if pending_signals:
        import json as _json
        signal_lines = _json.dumps(pending_signals, indent=2, default=str)
        signal_section = f"""
BACKGROUND SCANNER — NEW SIGNALS DETECTED ({len(pending_signals)}):
{signal_lines}

DO NOT call get_strategy_signals() — those results are already above.
For each signal above you MUST either:
  a) Trade it — call get_indicators() to confirm, then place_virtual_order(), or
  b) Reject it — call log_decision() with the exact reason (wrong phase, spread too wide, etc.)
Silently ignoring a signal is not allowed."""
        analysis_sequence = """
MANDATORY ANALYSIS SEQUENCE (signal-triggered iteration):
1. For each signal listed above:
   a. Call get_indicators(symbol, interval) for the signal's symbol and tool interval to confirm
      EMAs, RSI, ADX, ATR align with the strategy rules.
      For daily-first-hour signals, interval is set to "2" and signal_timeframe shows the setup type.
   b. If requires_volume_confirmation=true, call get_candles() and verify avg_volume_20.
   c. If all conditions met: get_option_chain(), size the position, place_virtual_order().
   d. If any condition fails: log_decision() with specific reason — do not skip silently.
2. After handling all signals, check open positions — adjust stops if needed.
3. Only place orders if ALL conditions in the strategy doc are met — not just some."""
    else:
        signal_section = ""
        analysis_sequence = """
MANDATORY ANALYSIS SEQUENCE (scheduled 5-min iteration):
1. Call get_indicators() for BOTH symbols on MULTIPLE timeframes:
   - get_indicators("NIFTY", "2") and get_indicators("BANKNIFTY", "2")   ← primary
   - get_indicators("NIFTY", "5") and get_indicators("BANKNIFTY", "5")   ← confirmation
   - get_indicators("NIFTY", "15") when checking higher-timeframe bias
   This gives you EMA20/50/100, RSI14, daily_lbr_rsi, ATR14, ADX14, VWAP, Pivot/CPR.
2. Use get_candles(symbol, interval, count) for pattern recognition if indicators suggest a setup.
3. Cross-reference with strategy rules:
   - VP-05: ema_stacked_bull/bear + pin bar at EMA20 or EMA50
   - VP-20: narrow CPR day only (cpr_is_narrow=True)
   - VP-24: proximity to pivot/r1/r2/s1/s2 within 0.1%
   - Momentum Pinball: daily_lbr_rsi < 30 or > 70, entry on first-hour breakout
   - ALL strategies: atr14 for SL sizing, adx14 > 20 for trend confirmation
4. If a setup is valid: get_option_chain(), size position, place_virtual_order().
5. Only place orders if ALL conditions in the strategy doc are met."""

    return f"""Current time: {now.strftime('%H:%M:%S')} IST
Market phase: {market_phase}
Session P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)
Open positions: {pos_summary}
Trades today: {trade_count}
Virtual balance available: ₹{available:,.2f}{pending_summary}{goals_section}{command_context}{signal_section}
{analysis_sequence}

IMPORTANT: Be silent in this iteration unless you have an ACTION:
- Send Telegram ONLY if you are entering or exiting a position
- Do NOT send "thinking" or "observing" messages during market scans
- Always log decisions via log_decision() even if you HOLD
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
