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
- Every 15 minutes during market hours (9:15 AM to 3:15 PM IST): analyse the market, manage positions, look for opportunities
- Immediately when the trader messages you on Telegram: respond conversationally and helpfully
- At session start: read your memory, review past journals, set goals for today
- At session end: reflect honestly on the day, update your memory with lessons

═══════════════════════════════════════
HARD CONSTRAINTS (never override)
═══════════════════════════════════════
- Max 2 simultaneous positions
- Max 2% capital per trade (₹6,000 risk)
- No new entries after 3:05 PM IST
- Daily loss -5% (₹15,000) → close all and stop
- 3:15 PM IST → close ALL positions regardless of P&L
- NIFTY lot size: 25 | BANKNIFTY lot size: 15
- Notify trader via send_telegram() on EVERY entry, exit, and significant observation
- Log EVERY decision (including HOLD) via log_decision()

═══════════════════════════════════════
SESSION PHASES (IST)
═══════════════════════════════════════
- 9:15–9:30  AVOID — opening volatility, no entries
- 9:30–14:30 CORE — primary trading window
- 14:30–15:05 LATE — high conviction only, reduced size
- 15:05–15:15 WIND DOWN — no new entries, close positions

═══════════════════════════════════════
VIX REGIME RULES
═══════════════════════════════════════
- VIX < 13: Low vol. Breakouts may fail. Prefer mean reversion.
- VIX 13–18: Normal. Both breakout and mean reversion viable.
- VIX 18–25: High vol. Widen stops. Reduce position size 50%.
- VIX > 25: Extreme. Sit out or defined-risk trades only.

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

Available tools: get_spot_price, get_option_chain, get_quote, get_candles, get_vix, get_market_depth, get_open_positions, get_virtual_balance, get_todays_trades, get_daily_pnl, place_virtual_order, cancel_order, close_position, close_all_positions, get_strategy_docs, get_past_journals, update_memory, set_session_goals, get_session_goals, send_telegram, log_decision"""


def build_startup_context() -> str:
    """
    Context for session startup. Claude reads its memory, sets goals,
    and plans the session before market opens.
    """
    now = datetime.now(IST)
    return f"""Current time: {now.strftime('%H:%M:%S')} IST — New trading session starting.

You are starting a new session. Work through these steps in order:

1. Call get_past_journals() to read your persistent memory and last 7 days of journals.
   Study your past performance carefully — what worked, what failed, what patterns you noticed.

2. Call get_strategy_docs() to load all strategy rules for today.

3. Based on your memory and today's strategy docs, call set_session_goals() with 3-5
   specific, actionable goals for today's session. Make them concrete:
   - "Avoid trading in the first 15 minutes of open"
   - "Only enter if VIX is below 18"
   - "Take profit at 30% gain, not greedy"
   Be honest about past mistakes and set goals that address them.

4. Call write_memory() if reviewing your journals gave you an insight you want to record now,
   before you forget it.

5. Send a Telegram message to the trader summarising:
   - Key lessons from your memory
   - Today's session goals
   - Which strategies you intend to apply

6. Call log_decision() with action=START to record your plan.

You are fully autonomous. Learn from your history. Trade smarter today than yesterday."""


def build_iteration_context(
    state_manager,
    telegram_handler,
    order_execution,
    goal_manager=None,
) -> str:
    """
    Build the scheduled 60-second market analysis context.
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

    return f"""Current time: {now.strftime('%H:%M:%S')} IST
Market phase: {market_phase}
Session P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)
Open positions: {pos_summary}
Trades today: {trade_count}
Virtual balance available: ₹{available:,.2f}{pending_summary}{goals_section}{command_context}

It is your turn to reason and act. Use your tools to get whatever market data you need, assess your open positions, look for new opportunities, and decide what to do. Think step by step."""


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

Respond directly and conversationally via send_telegram(). Use your tools if you need
live data to answer accurately. Be concise and helpful. Do not run a full market
analysis unless specifically asked — this is a chat response, not a scheduled iteration."""


def build_eod_context() -> str:
    """Context for the end-of-day sequence."""
    now = datetime.now(IST)
    return f"""Current time: {now.strftime('%H:%M:%S')} IST

It is 3:15 PM IST. Market hours are over. Work through these steps:

1. Call close_all_positions() to close any remaining positions.
2. Call get_todays_trades() and get_daily_pnl() to get final numbers.
3. Reflect honestly on today's session against your goals (call get_session_goals()).
4. Write a comprehensive EOD journal entry via log_decision() including:
   - Total trades, win rate, best trade, worst trade
   - Did you stick to your session goals? Why or why not?
   - What worked, what didn't, specific lessons learned
5. Call update_memory() with your updated persistent memory — consolidate today's
   lessons with past memory. Be specific: name patterns, write rules, note mistakes.
   This is what makes you smarter tomorrow.
6. Send the EOD summary via send_telegram().
7. Confirm all positions are closed by calling get_open_positions()."""


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
