"""
tools/registry.py — Tool registry for BlitzTrader.

Maps tool names to Python functions and provides
Anthropic-compatible JSON schema for each tool.

This is what Claude sees as its available tools.

FUTURES-ONLY ENFORCEMENT
------------------------
get_tool_definitions()        — returned to the live LLM agent (FUTURES ONLY)
get_legacy_tool_definitions() — NOT passed to the agent; available for manual/informational use only

LIVE_TOOLS   — tool names exposed to the live agent
LEGACY_TOOLS — tool names available only for manual use (never sent to the LLM)
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
    "get_todays_spread_trades",
    "get_daily_pnl",
    "place_virtual_order",
    "cancel_order",
    "close_position",
    "close_all_positions",
    "get_past_journals",
    "update_memory",
    "set_session_goals",
    "get_session_goals",
    "get_strategy_docs",
    "send_telegram",
    "log_decision",
    "exit_position_by_serial",
    "get_status_with_serials",
]

# Tool names available only for manual/informational use — NEVER sent to the live LLM agent
LEGACY_TOOLS = [
    "get_option_chain",
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
        self._pending_entry_log = None

        # Build the tool map — NOTE: get_option_chain is NOT in this map so the
        # live agent cannot call it.  It lives in _legacy_tool_map only.
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

        # Position & Account / Order Execution (omitted for read-only agents)
        if self._order_exec is not None:
            self._tool_map.update({
                "get_open_positions": self._order_exec.get_open_positions,
                "get_virtual_balance": self._order_exec.get_virtual_balance,
                "get_todays_trades": self._order_exec.get_todays_trades,
                "get_todays_spread_trades": self._order_exec.get_todays_spread_trades,
                "get_daily_pnl": self._order_exec.get_daily_pnl,
                "place_virtual_order": self._place_virtual_order_tracked,
                "cancel_order": self._order_exec.cancel_order,
                "close_position": self._order_exec.close_position,
                "close_all_positions": self._order_exec.close_all_positions,
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
            # Serial-numbered position status + exit-by-serial
            "exit_position_by_serial": self._exit_position_by_serial,
            "get_status_with_serials": self._get_status_with_serials,
        })

        # Legacy tool map — available for manual/informational use ONLY.
        # Never merged into _tool_map; the live LLM agent cannot invoke these.
        self._legacy_tool_map = {}
        if self._market_data is not None:
            self._legacy_tool_map["get_option_chain"] = self._market_data.get_option_chain

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

    def _place_virtual_order_tracked(self, **tool_input) -> dict:
        """Track the latest order result so fake ENTER logs can be suppressed."""
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
        Generate a structured status message with serial-numbered open positions
        and persist the index for exit-by-serial.
        """
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

    def _exit_position_by_serial(self, serial: int) -> dict:
        """
        Exit the open position identified by its Telegram serial number.
        Validates index freshness and live state before placing any orders.
        NEVER opens a new position.
        """
        return _exit_by_serial_impl(
            serial=serial,
            state_manager=self._order_exec._state,
            order_execution=self._order_exec,
            shoonya_client=self._shoonya_client,
            telegram_handler=self._telegram,
            active_tokens=self._active_tokens,
            live_feed=self._live_feed,
        )

    def get_tool_definitions(self) -> list[dict]:
        """
        Return Anthropic-compatible tool definitions for the LIVE LLM agent.

        FUTURES-ONLY: get_option_chain is NOT included here.
        It lives in get_legacy_tool_definitions() and is never sent to the agent.
        """
        return [
            # ── Market Data Tools (FUTURES ONLY) ──
            {
                "name": "get_spot_price",
                "description": (
                    "Get current spot/futures-resolved price for NIFTY or BANKNIFTY. "
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
            # NOTE: get_option_chain is deliberately absent here.
            # It is available only via get_legacy_tool_definitions() for manual use.
            {
                "name": "get_quote",
                "description": (
                    "Get LTP, best bid, best ask for a specific futures trading symbol. "
                    "Use this to check the current price of an active futures contract "
                    "(e.g. NIFTY28APR26F, BANKNIFTY28APR26F). "
                    "Do NOT use for option symbols — options are not used in live execution."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Futures trading symbol, e.g. 'NIFTY28APR26F'",
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
                    "EMA20/50/100 (trend, stacking), RSI14 (momentum), ATR14 (SL sizing), "
                    "ADX14 (trend strength), VWAP (intraday bias), avg_volume_20 (VSA), "
                    "Pivot/R1/R2/S1/S2 (VP-24 pivot bounce), CPR TC/BC/width (VP-20 CPR reversal), "
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
                    "Use this every market-analysis iteration so entries are not missed by "
                    "manual LLM inspection. "
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
                    "Get all currently open virtual positions with entry price, "
                    "current price, and unrealized P&L."
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
                "description": (
                    "Get all trades executed today: both legacy futures trades and closed "
                    "option spreads. Check both futures_trades and spread_trades fields. "
                    "If spread_count > 0 there were option spread trades even if futures_count = 0."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_todays_spread_trades",
                "description": (
                    "Get only the closed option-spread trades for today with full details: "
                    "spread type, legs, fill prices, realized P&L, close reason. "
                    "Also reports currently open spreads. Use at EOD to summarize spread activity."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_daily_pnl",
                "description": (
                    "Get current net P&L for the session as amount and percentage. "
                    "Includes breakdown: spread_realized_pnl and futures_realized_pnl."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            # ── Order Execution Tools ──
            {
                "name": "place_virtual_order",
                "description": (
                    "Place a virtual FUTURES order. MARKET fills immediately at best bid/ask midpoint. "
                    "LIMIT fills only if LTP touches price within 5 minutes, else auto-cancels. "
                    "Hard guardrails enforced: max 3 positions, no pyramiding "
                    "(one open position per instrument), exactly 1 futures lot per trade, "
                    "no entry after 15:05 IST, daily loss limit, margin limit. "
                    "FUTURES ONLY: symbol must be the futures tsym (e.g. NIFTY28APR26F). "
                    "Options (CE/PE) are BLOCKED — do not pass CE/PE symbols."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Futures trading symbol (e.g. 'NIFTY28APR26F'). Options are blocked.",
                        },
                        "direction": {
                            "type": "string",
                            "description": "BUY or SELL",
                            "enum": ["BUY", "SELL"],
                        },
                        "quantity": {
                            "type": "integer",
                            "description": "Exactly 1 futures lot only. Use the resolved lot_size shown in ACTIVE FUTURES INSTRUMENTS.",
                        },
                        "order_type": {
                            "type": "string",
                            "description": "MARKET or LIMIT",
                            "enum": ["MARKET", "LIMIT"],
                            "default": "MARKET",
                        },
                        "price": {
                            "type": "number",
                            "description": "Limit price (required for LIMIT, ignored for MARKET)",
                        },
                        "stop_loss": {
                            "type": "number",
                            "description": (
                                "Stop-loss price. Enforced deterministically on every tick — "
                                "position auto-closes if LTP crosses this level. ALWAYS set this."
                            ),
                        },
                        "target": {
                            "type": "number",
                            "description": (
                                "Profit target price. Enforced deterministically on every tick — "
                                "position auto-closes if LTP reaches this level."
                            ),
                        },
                    },
                    "required": ["symbol", "direction", "quantity", "stop_loss"],
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
                            "description": "Order ID returned by place_virtual_order",
                        }
                    },
                    "required": ["order_id"],
                },
            },
            {
                "name": "close_position",
                "description": "Close an open position at market price.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Symbol of the position to close",
                        }
                    },
                    "required": ["symbol"],
                },
            },
            {
                "name": "close_all_positions",
                "description": (
                    "Close ALL open positions at market price. "
                    "Use at 3:15 PM IST (market close) or on /abort command."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
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
                    "after reading memory and strategy docs. These goals will appear at "
                    "the top of every market analysis iteration to keep your reasoning "
                    "grounded in your declared intentions."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "goals": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "List of 2-5 specific, actionable session goals. "
                                "E.g. 'Avoid trading in first 15 minutes', "
                                "'Only enter if VIX < 18', 'Max 2 trades today'."
                            ),
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
                    "and any NSE-specific strategy documents. Call this at session start "
                    "to understand what strategies to apply."
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
                    "session start/end, trade entries/exits, status updates, "
                    "warnings, EOD summary. The system will auto-append verified "
                    "trade/P&L data to performance messages. NEVER include trade "
                    "counts or P&L figures you did not get from a tool response."
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
                    "call this for every scanner candidate you reject/skip and for executed "
                    "entries/exits. Do not log routine HOLDs when no candidate exists. "
                    "Always explain your reasoning clearly. For EOD entries, the system will auto-append "
                    "verified trade data from the state manager — do NOT fabricate trade "
                    "counts or P&L numbers in your reasoning text."
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
            # ── Serial-numbered status + exit-by-serial ──
            {
                "name": "get_status_with_serials",
                "description": (
                    "Generate a structured status message with serial-numbered open futures positions, "
                    "send it via Telegram, and persist the serial index. "
                    "Call this when the trader asks for a status update or position summary. "
                    "Must be called before exit_position_by_serial to refresh the index."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "exit_position_by_serial",
                "description": (
                    "Exit the open futures position identified by its serial number from the last status message. "
                    "SAFETY: validates index freshness (30 min TTL), re-verifies position is still open, "
                    "and NEVER opens a new position. "
                    "Call when user says: 'exit 2', 'close position 3', 'square off #1', 'close serial 2'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "serial": {
                            "type": "integer",
                            "description": (
                                "The serial number of the position to exit, "
                                "as shown in the last status message."
                            ),
                        }
                    },
                    "required": ["serial"],
                },
            },
        ]
