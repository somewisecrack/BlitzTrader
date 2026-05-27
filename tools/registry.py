"""
tools/registry.py — Tool registry for BlitzTrader.

Maps tool names to Python functions and provides
Anthropic-compatible JSON schema for each tool.

This is what Claude sees as its available tools.

OPTIONS-SPREAD ENFORCEMENT
--------------------------
get_tool_definitions()        — returned to the live LLM agent (options spreads only)
get_legacy_tool_definitions() — NOT passed to the agent; available for manual/informational use only

LIVE_TOOLS   — tool names exposed to the live agent
LEGACY_TOOLS — tool names available only for manual use (never sent to the LLM)

NOTE: place_virtual_order, close_position, close_all_positions are in LEGACY_TOOLS only.
      Python owns all spread entry and exit execution — Gemini is the gatekeeper only.
"""
import logging
from tools.position_serial import (
    build_status_message,
    save_position_index,
    exit_position_by_serial as _exit_by_serial_impl,
)

logger = logging.getLogger("BlitzTrader.ToolRegistry")

# Tool names visible to the live LLM agent during trading
LIVE_TOOLS = [
    "get_spot_price",
    "get_quote",
    "get_candles",
    "get_indicators",
    "get_strategy_signals",
    "get_vix",
    "get_market_depth",
    "get_open_positions",
    "get_virtual_balance",
    "get_todays_trades",
    "get_daily_pnl",
    "cancel_order",
    "get_past_journals",
    "update_memory",
    "set_session_goals",
    "get_session_goals",
    "get_strategy_docs",
    "send_telegram",
    "log_decision",
    "exit_spread_by_serial",
    "get_status_with_serials",
    "get_spread_status",
]

# Tool names available only for manual/informational use — NEVER sent to the live LLM agent
LEGACY_TOOLS = [
    "get_option_chain",
    # Futures execution tools — removed from live path; kept for backtest/manual use
    "place_virtual_order",
    "close_position",
    "close_all_positions",
    "exit_position_by_serial",
]


class ToolRegistry:
    """
    Manages tool definitions and execution routing.
    """

    def __init__(
        self,
        market_data,
        order_execution,
        telegram,
        journal,
        strategy_reader,
        memory_reader,
        goal_manager,
        live_feed=None,
        shoonya_client=None,
        active_tokens=None,
        spread_portfolio=None,
    ):
        self._market_data = market_data
        self._order_exec = order_execution
        self._telegram = telegram
        self._journal = journal
        self._strategy = strategy_reader
        self._memory = memory_reader
        self._goals = goal_manager
        self._live_feed = live_feed
        self._shoonya_client = shoonya_client
        self._active_tokens = active_tokens or {}
        self._spread_portfolio = spread_portfolio
        self._pending_entry_log = None

        # Build the tool map — NOTE: futures execution tools (place_virtual_order,
        # close_position, close_all_positions) are NOT exposed to the live LLM.
        # They live in _legacy_tool_map only.
        # Components that are None (e.g. Q&A agent which has no market_data or
        # order_execution) are simply omitted from the map.
        self._tool_map = {}

        # Market Data (omitted when market_data is None — e.g. Q&A agent)
        if self._market_data is not None:
            self._tool_map.update({
                "get_spot_price": self._market_data.get_spot_price,
                "get_quote": self._market_data.get_quote,
                "get_candles": self._market_data.get_candles,
                "get_indicators": self._market_data.get_indicators,
                "get_strategy_signals": self._market_data.get_strategy_signals,
                "get_vix": self._market_data.get_vix,
                "get_market_depth": self._market_data.get_market_depth,
            })

        # Position & Account (read-only — no order execution exposed to LLM)
        if self._order_exec is not None:
            self._tool_map.update({
                "get_open_positions": self._order_exec.get_open_positions,
                "get_virtual_balance": self._order_exec.get_virtual_balance,
                "get_todays_trades": self._order_exec.get_todays_trades,
                "get_daily_pnl": self._order_exec.get_daily_pnl,
                "cancel_order": self._order_exec.cancel_order,
            })

        # Always-available tools
        self._tool_map.update({
            # Strategy
            "get_strategy_docs": self._strategy.get_strategy_docs,
            # Memory & Goals
            "get_past_journals": self._memory.get_past_journals,
            "update_memory": self._memory.update_memory,
            "set_session_goals": self._goals.set_session_goals,
            "get_session_goals": self._goals.get_session_goals,
            # Communication
            "send_telegram": self._telegram.send_telegram,
            # Journal
            "log_decision": self._log_decision_guarded,
            # Spread status + serial exit
            "exit_spread_by_serial": self._exit_spread_by_serial,
            "get_status_with_serials": self._get_status_with_serials,
            "get_spread_status": self._get_spread_status,
        })

        # Legacy tool map — available for manual/informational use ONLY.
        # Never merged into _tool_map; the live LLM agent cannot invoke these.
        self._legacy_tool_map = {}
        if self._market_data is not None:
            self._legacy_tool_map["get_option_chain"] = self._market_data.get_option_chain
        if self._order_exec is not None:
            self._legacy_tool_map.update({
                "place_virtual_order": self._place_virtual_order_tracked,
                "close_position": self._order_exec.close_position,
                "close_all_positions": self._order_exec.close_all_positions,
                "exit_position_by_serial": self._exit_position_by_serial_legacy,
            })

    def execute(self, tool_name: str, tool_input: dict) -> dict:
        """
        Execute a tool by name with given input.
        Returns the tool's result dict.
        """
        func = self._tool_map.get(tool_name)
        if not func:
            logger.error(f"Unknown tool: {tool_name}")
            return {"error": f"Unknown tool: {tool_name}"}

        try:
            tool_input = tool_input or {}  # Handle None from LLM

            # Try calling with given args first
            logger.info(f"Executing tool: {tool_name}({tool_input})")
            try:
                result = func(**tool_input)
            except TypeError:
                # LLM likely sent extra/wrong kwargs — retry with only valid params
                import inspect
                sig = inspect.signature(func)
                valid_params = set(sig.parameters.keys()) - {"self"}
                filtered = {k: v for k, v in tool_input.items() if k in valid_params}
                # If func accepts **kwargs, pass everything
                if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                    filtered = tool_input
                logger.warning(f"Tool {tool_name}: filtered args {set(tool_input) - set(filtered)}, retrying with {set(filtered)}")
                result = func(**filtered) if filtered else func()

            logger.info(f"Tool {tool_name} returned: {type(result).__name__}")
            return result
        except TypeError as e:
            logger.error(f"Tool {tool_name} bad args: {e}")
            return {"error": f"Invalid arguments for {tool_name}: {e}"}
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed")
            return {"error": f"Tool {tool_name} failed: {e}"}

    # ── Legacy / internal helpers (not exposed to LLM) ──────────────────────

    def _place_virtual_order_tracked(self, **tool_input) -> dict:
        """Track the latest order result so fake ENTER logs can be suppressed.
        NOT exposed to the live LLM — legacy/manual use only."""
        result = self._order_exec.place_virtual_order(**tool_input)
        symbol = str(tool_input.get("symbol") or result.get("symbol") or "").upper()
        direction = str(tool_input.get("direction") or result.get("direction") or "").upper()
        self._pending_entry_log = {
            "symbol": symbol,
            "direction": direction,
            "status": str(result.get("status", "")).upper(),
            "result": result,
        }
        return result

    def _exit_position_by_serial_legacy(self, serial: int) -> dict:
        """Legacy futures serial exit — NOT exposed to the live LLM."""
        return _exit_by_serial_impl(
            serial=serial,
            state_manager=self._order_exec._state,
            order_execution=self._order_exec,
            shoonya_client=self._shoonya_client,
            telegram_handler=self._telegram,
            active_tokens=self._active_tokens,
            live_feed=self._live_feed,
        )

    # ── Live-path tool implementations ──────────────────────────────────────

    def _log_decision_guarded(
        self,
        action: str,
        symbol: str = "",
        reason: str = "",
        strategy_applied: str = "",
        market_context_summary: str = "",
    ) -> dict:
        """
        Prevent misleading ENTER logs unless a matching order actually succeeded.
        """
        action_up = (action or "").upper()
        symbol_up = (symbol or "").upper()

        if action_up in ("ENTER_LONG", "ENTER_SHORT"):
            expected_direction = "BUY" if action_up == "ENTER_LONG" else "SELL"
            pending = self._pending_entry_log or {}
            pending_status = str(pending.get("status", "")).upper()
            pending_symbol = str(pending.get("symbol", "")).upper()
            pending_direction = str(pending.get("direction", "")).upper()

            if (
                pending_status not in ("FILLED", "PENDING")
                or pending_symbol != symbol_up
                or pending_direction != expected_direction
            ):
                failure = pending.get("result", {}) if isinstance(pending.get("result"), dict) else {}
                failure_reason = failure.get("error") or failure.get("message") or "No successful order placement was recorded."
                logger.warning(
                    "Suppressing misleading %s journal entry for %s. "
                    "Latest matching order result was status=%s error=%r",
                    action_up,
                    symbol_up or "(no symbol)",
                    pending_status or "NONE",
                    failure.get("error"),
                )
                self._pending_entry_log = None
                return self._journal.log_decision(
                    action="REJECT",
                    symbol=symbol,
                    strategy_applied=strategy_applied,
                    market_context_summary=market_context_summary,
                    reason=(
                        "Entry was not executed. "
                        f"Latest order result: {failure_reason}"
                    ),
                )

            self._pending_entry_log = None

        return self._journal.log_decision(
            action=action,
            symbol=symbol,
            reason=reason,
            strategy_applied=strategy_applied,
            market_context_summary=market_context_summary,
        )

    def _get_status_with_serials(self) -> dict:
        """
        Generate a structured status message with serial-numbered open spreads
        and persist the index for exit-by-serial.
        """
        if self._spread_portfolio is not None:
            # Spread-aware status — main live path
            lines = self._spread_portfolio.build_status_lines()
            msg = "\n".join(lines) if lines else "No open spreads."
            self._telegram.send_telegram(msg)
            open_spreads = self._spread_portfolio.get_open_spreads()
            # Build a serial index for exit_spread_by_serial
            index_payload = {
                "positions": [
                    {"serial": i + 1, "spread_id": s.spread_id}
                    for i, s in enumerate(open_spreads)
                ],
                "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                "type": "spreads",
            }
            save_position_index(index_payload)
            return {
                "status": "sent",
                "spread_count": len(open_spreads),
                "generated_at": index_payload["generated_at"],
            }
        else:
            # Fallback: legacy futures status
            msg, index_payload = build_status_message(
                state_manager=self._order_exec._state,
                live_feed=self._live_feed,
                shoonya_client=self._shoonya_client,
                active_tokens=self._active_tokens,
            )
            save_position_index(index_payload)
            self._telegram.send_telegram(msg)
            return {
                "status": "sent",
                "position_count": len(index_payload.get("positions", [])),
                "generated_at": index_payload.get("generated_at"),
            }

    def _get_spread_status(self) -> dict:
        """
        Return current open spreads P&L summary as a dict.
        Does NOT send Telegram — returns data only.
        """
        if self._spread_portfolio is None:
            return {"error": "Spread portfolio not initialised"}
        lines = self._spread_portfolio.build_status_lines()
        open_spreads = self._spread_portfolio.get_open_spreads()
        return {
            "open_spreads": len(open_spreads),
            "summary_lines": lines,
        }

    def _exit_spread_by_serial(self, serial: int) -> dict:
        """
        Exit the open spread identified by its Telegram serial number.
        Validates index freshness and live state before placing any orders.
        NEVER opens a new position.
        """
        import json, os
        from pathlib import Path
        from datetime import datetime, timezone, timedelta

        INDEX_PATH = Path("runtime/position_index.json")
        MAX_AGE_MINUTES = 30

        if self._spread_portfolio is None:
            msg = "⚠️ Spread portfolio not initialised — cannot exit."
            self._telegram.send_telegram(msg)
            return {"error": msg}

        # Load the serial index
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as fh:
                index = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            msg = "⚠️ No position index found. Call get_status_with_serials() first."
            self._telegram.send_telegram(msg)
            return {"error": msg}

        # Check index type
        if index.get("type") != "spreads":
            msg = "⚠️ Position index is for legacy futures positions. Use get_status_with_serials() to refresh."
            self._telegram.send_telegram(msg)
            return {"error": msg}

        # Check freshness
        try:
            generated_at = datetime.fromisoformat(index["generated_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - generated_at
            if age > timedelta(minutes=MAX_AGE_MINUTES):
                msg = f"⚠️ Position index is stale ({age.seconds // 60}m old). Call get_status_with_serials() to refresh."
                self._telegram.send_telegram(msg)
                return {"error": msg}
        except Exception:
            pass  # If we can't parse, proceed anyway

        # Find the spread_id for this serial
        positions = index.get("positions", [])
        spread_id = None
        for p in positions:
            if p.get("serial") == serial:
                spread_id = p.get("spread_id")
                break

        if spread_id is None:
            msg = f"⚠️ Serial #{serial} not found. Valid serials: {[p['serial'] for p in positions]}."
            self._telegram.send_telegram(msg)
            return {"error": msg}

        # Find the live spread object
        open_spreads = self._spread_portfolio.get_open_spreads()
        target_spread = None
        for sp in open_spreads:
            if sp.spread_id == spread_id:
                target_spread = sp
                break

        if target_spread is None:
            msg = f"⚠️ Spread {spread_id} (serial #{serial}) is no longer open — may have already been closed."
            self._telegram.send_telegram(msg)
            return {"error": msg}

        # Close it
        result = self._spread_portfolio.close_spread(
            spread=target_spread,
            reason=f"manual Telegram exit serial #{serial}",
        )
        if result.get("ok"):
            msg = (
                f"✅ Spread #{serial} [{spread_id}] closed manually.\n"
                f"{target_spread.symbol} {target_spread.spread_type} expiry {target_spread.expiry}\n"
                f"P&L: ₹{result.get('realized_pnl', 0):+,.2f}"
            )
        else:
            msg = f"⚠️ Failed to close spread #{serial}: {result.get('error', 'unknown error')}"
        self._telegram.send_telegram(msg)
        return result

    def get_tool_definitions(self) -> list[dict]:
        """
        Return Anthropic-compatible tool definitions for the LIVE LLM agent.

        OPTIONS-SPREAD ONLY: place_virtual_order, close_position, close_all_positions
        are NOT included here — they live in get_legacy_tool_definitions() only.
        Python owns all spread execution; Gemini approves/rejects candidates only.
        """
        return [
            # ── Market Data Tools ──
            {
                "name": "get_spot_price",
                "description": (
                    "Get current spot price for NIFTY or BANKNIFTY. "
                    "Returns spot_price, change, change_pct, high, low, open."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "string",
                            "description": "Index name: 'NIFTY' or 'BANKNIFTY'",
                            "enum": ["NIFTY", "BANKNIFTY"],
                        }
                    },
                    "required": ["index"],
                },
            },
            {
                "name": "get_quote",
                "description": (
                    "Get LTP, best bid, best ask for a specific trading symbol. "
                    "Use this to check the current price of an active options contract "
                    "(e.g. NIFTY29MAY2624500CE) or index futures."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Trading symbol, e.g. 'NIFTY29MAY2624500CE'",
                        }
                    },
                    "required": ["symbol"],
                },
            },
            {
                "name": "get_candles",
                "description": (
                    "Get last N OHLCV candles at given interval. Use to assess "
                    "momentum, trend, support/resistance levels."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Trading symbol",
                        },
                        "interval": {
                            "type": "string",
                            "description": "Candle interval in minutes",
                            "enum": ["1", "3", "5", "15", "30", "60"],
                            "default": "5",
                        },
                        "count": {
                            "type": "integer",
                            "description": "Number of candles to return",
                            "default": 20,
                        },
                    },
                    "required": ["symbol"],
                },
            },
            {
                "name": "get_indicators",
                "description": (
                    "Get all technical indicators for a symbol computed from Shoonya REST candles "
                    "(primary source, multi-day history) with live-feed candles as fallback. "
                    "ALWAYS call this before evaluating any strategy. Returns: "
                    "EMA20/50/100 (trend, stacking), RSI14 (momentum), ATR14, "
                    "ADX14 (trend strength), VWAP (intraday bias), avg_volume_20 (VSA), "
                    "ema_stacked_bull/bear flags (VP-05 3-EMA trend filter)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Trading symbol e.g. 'NIFTY' or 'BANKNIFTY'",
                        },
                        "interval": {
                            "type": "string",
                            "description": "Candle interval in minutes to compute indicators on",
                            "enum": ["1", "3", "5", "15", "30", "60"],
                            "default": "5",
                        },
                    },
                    "required": ["symbol"],
                },
            },
            {
                "name": "get_strategy_signals",
                "description": (
                    "Deterministically scan recent NIFTY/BANKNIFTY candles for approved "
                    "price-action, VSA/VPA confirmation, and daily first-hour strategy setups. "
                    "Returns candidate entries with strategy, direction, entry reference, "
                    "stop_loss, target, requires_volume_confirmation, and rule-based reasoning."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "NIFTY, BANKNIFTY, or BOTH",
                            "enum": ["NIFTY", "BANKNIFTY", "BOTH"],
                            "default": "BOTH",
                        },
                        "lookback_bars": {
                            "type": "integer",
                            "description": "How many most-recent bars per timeframe to scan",
                            "default": 5,
                        },
                    },
                },
            },
            {
                "name": "get_vix",
                "description": "Get current India VIX value. Higher VIX = higher implied volatility.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_market_depth",
                "description": (
                    "Get full order book depth (up to 5 bid/ask levels) for a symbol. "
                    "Useful for assessing liquidity."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Trading symbol",
                        }
                    },
                    "required": ["symbol"],
                },
            },
            # ── Position & Account Tools ──
            {
                "name": "get_open_positions",
                "description": (
                    "Get all currently open option spread positions with entry details "
                    "and unrealized P&L."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_virtual_balance",
                "description": "Get current virtual cash, margin used, and available margin.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_todays_trades",
                "description": "Get all option spread trades executed today with entry/exit details and realized P&L.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_daily_pnl",
                "description": "Get current net P&L for the session as amount and percentage.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "cancel_order",
                "description": "Cancel a pending limit order by order_id.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "order_id": {
                            "type": "string",
                            "description": "Order ID returned by the order placement system",
                        }
                    },
                    "required": ["order_id"],
                },
            },
            # ── Memory & Goal Tools ──
            {
                "name": "get_past_journals",
                "description": (
                    "Read your persistent memory and last 7 days of trading journals. "
                    "Call this at session start to learn from past performance, "
                    "remember lessons, and avoid repeating mistakes."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "update_memory",
                "description": (
                    "Overwrite your persistent memory file with new insights and lessons. "
                    "Call this at EOD after reflecting on today's session. "
                    "CRITICAL: You MUST call get_todays_trades() and get_daily_pnl() BEFORE "
                    "calling this tool. The system will auto-append verified trade data. "
                    "NEVER write trade counts, P&L numbers, or win rates that you did not "
                    "get from a tool response. If no trades occurred, write 'No trades today'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": (
                                "Your full updated memory — lessons, behavioural rules, "
                                "patterns, and anything you want to remember next session."
                            ),
                        }
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "set_session_goals",
                "description": (
                    "Set your goals for this trading session. Call this during startup "
                    "after reading memory and strategy docs."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "goals": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of 2-5 specific, actionable session goals.",
                        }
                    },
                    "required": ["goals"],
                },
            },
            {
                "name": "get_session_goals",
                "description": "Read the goals you set at the start of this session.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            # ── Strategy Tool ──
            {
                "name": "get_strategy_docs",
                "description": (
                    "Read all strategy documentation. Returns the master trading library "
                    "and any NSE-specific strategy documents. Call this at session start."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            # ── Communication Tool ──
            {
                "name": "send_telegram",
                "description": (
                    "Send a message to the trader via Telegram. Use for: "
                    "session start/end, spread entries/exits, status updates, "
                    "warnings, EOD summary. NEVER include trade counts or P&L "
                    "figures you did not get from a tool response."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Message text to send (supports Markdown)",
                        }
                    },
                    "required": ["message"],
                },
            },
            # ── Journal Tool ──
            {
                "name": "log_decision",
                "description": (
                    "Log a trading decision to today's journal. In signal-review iterations, "
                    "call this for every scanner candidate you reject/skip. "
                    "Do not log routine HOLDs when no candidate exists."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "Decision type: ENTER_LONG, ENTER_SHORT, EXIT, HOLD, REJECT, SKIP, STOP, ABORT",
                        },
                        "symbol": {
                            "type": "string",
                            "description": "Instrument symbol (empty for HOLD/SKIP)",
                            "default": "",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Your detailed reasoning for this decision",
                        },
                        "strategy_applied": {
                            "type": "string",
                            "description": "Which strategy triggered or was assessed",
                            "default": "",
                        },
                        "market_context_summary": {
                            "type": "string",
                            "description": "Brief current market conditions summary",
                            "default": "",
                        },
                    },
                    "required": ["action", "reason"],
                },
            },
            # ── Spread Status + Serial Exit ──
            {
                "name": "get_status_with_serials",
                "description": (
                    "Generate a structured status message with serial-numbered open option spreads, "
                    "send it via Telegram, and persist the serial index. "
                    "Call this when the trader asks for a status update or position summary. "
                    "Must be called before exit_spread_by_serial to refresh the index."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_spread_status",
                "description": (
                    "Return current open spreads count and P&L summary as structured data. "
                    "Does NOT send Telegram — use get_status_with_serials() for that. "
                    "Use to check spread state during analysis."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "exit_spread_by_serial",
                "description": (
                    "Exit the open option spread identified by its serial number from the last status message. "
                    "SAFETY: validates index freshness (30 min TTL), re-verifies spread is still open, "
                    "closes BOTH legs, and NEVER opens a new position. "
                    "Call when user says: 'exit 2', 'close spread 3', 'square off #1', 'close serial 2'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "serial": {
                            "type": "integer",
                            "description": (
                                "The serial number of the spread to exit, "
                                "as shown in the last status message."
                            ),
                        }
                    },
                    "required": ["serial"],
                },
            },
        ]

    def get_legacy_tool_definitions(self) -> list[dict]:
        """
        Return tool definitions for legacy/manual tools.
        These are NEVER passed to the live LLM agent.
        Available only for manual diagnostics, backtesting, or explicit developer use.
        """
        return [
            {
                "name": "place_virtual_order",
                "description": (
                    "[LEGACY — not exposed to live agent] "
                    "Place a virtual FUTURES order. Used for backtesting only. "
                    "The live path uses SpreadExecutionEngine instead."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "direction": {"type": "string", "enum": ["BUY", "SELL"]},
                        "quantity": {"type": "integer"},
                        "order_type": {"type": "string", "enum": ["MARKET", "LIMIT"], "default": "MARKET"},
                        "price": {"type": "number"},
                        "stop_loss": {"type": "number"},
                        "target": {"type": "number"},
                    },
                    "required": ["symbol", "direction", "quantity", "stop_loss"],
                },
            },
            {
                "name": "close_position",
                "description": "[LEGACY — not exposed to live agent] Close a futures position at market.",
                "input_schema": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                },
            },
            {
                "name": "close_all_positions",
                "description": "[LEGACY — not exposed to live agent] Close ALL open futures positions.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_option_chain",
                "description": "[LEGACY] Get NSE option chain for manual inspection.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "expiry": {"type": "string"},
                    },
                    "required": ["symbol"],
                },
            },
        ]
