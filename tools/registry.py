"""
tools/registry.py — Tool registry for BlitzTrader.

Maps tool names to Python functions and provides
Anthropic-compatible JSON schema for each tool.

This is what Claude sees as its available tools.
"""
import logging

logger = logging.getLogger("BlitzTrader.ToolRegistry")


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
    ):
        self._market_data = market_data
        self._order_exec = order_execution
        self._telegram = telegram
        self._journal = journal
        self._strategy = strategy_reader
        self._memory = memory_reader
        self._goals = goal_manager

        # Build the tool map
        self._tool_map = {
            # Market Data
            "get_spot_price": self._market_data.get_spot_price,
            "get_option_chain": self._market_data.get_option_chain,
            "get_quote": self._market_data.get_quote,
            "get_candles": self._market_data.get_candles,
            "get_vix": self._market_data.get_vix,
            "get_market_depth": self._market_data.get_market_depth,
            # Position & Account
            "get_open_positions": self._order_exec.get_open_positions,
            "get_virtual_balance": self._order_exec.get_virtual_balance,
            "get_todays_trades": self._order_exec.get_todays_trades,
            "get_daily_pnl": self._order_exec.get_daily_pnl,
            # Order Execution
            "place_virtual_order": self._order_exec.place_virtual_order,
            "cancel_order": self._order_exec.cancel_order,
            "close_position": self._order_exec.close_position,
            "close_all_positions": self._order_exec.close_all_positions,
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
            "log_decision": self._journal.log_decision,
        }

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
            logger.info(f"Executing tool: {tool_name}({tool_input})")
            result = func(**tool_input)
            logger.info(f"Tool {tool_name} returned: {type(result).__name__}")
            return result
        except TypeError as e:
            logger.error(f"Tool {tool_name} bad args: {e}")
            return {"error": f"Invalid arguments for {tool_name}: {e}"}
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed")
            return {"error": f"Tool {tool_name} failed: {e}"}

    def get_tool_definitions(self) -> list[dict]:
        """
        Return Anthropic-compatible tool definitions for Claude.
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
                "name": "get_option_chain",
                "description": (
                    "Get full option chain with strikes, LTP, bid, ask, OI for a given "
                    "index and expiry. Returns chain of CE and PE options around ATM."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "string",
                            "description": "Index: 'NIFTY' or 'BANKNIFTY'",
                            "enum": ["NIFTY", "BANKNIFTY"],
                        },
                        "expiry": {
                            "type": "string",
                            "description": "Expiry prefix, e.g. '27MAR' or '03APR'",
                        },
                    },
                    "required": ["index", "expiry"],
                },
            },
            {
                "name": "get_quote",
                "description": (
                    "Get LTP, best bid, best ask for a specific trading symbol. "
                    "Use this to check current price of a specific option or future."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Trading symbol, e.g. 'NIFTY27MAR24500CE'",
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
                            "enum": ["1", "5", "15", "30", "60"],
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
                "description": "Get all trades executed today with entry/exit prices and realized P&L.",
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
            # ── Order Execution Tools ──
            {
                "name": "place_virtual_order",
                "description": (
                    "Place a virtual order. MARKET fills immediately at best bid/ask midpoint. "
                    "LIMIT fills only if LTP touches price within 5 minutes, else auto-cancels. "
                    "Hard guardrails enforced: max 2 positions, no entry after 15:05 IST, "
                    "daily loss limit, position size limit."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Trading symbol (e.g., 'NIFTY27MAR24500CE')",
                        },
                        "direction": {
                            "type": "string",
                            "description": "BUY or SELL",
                            "enum": ["BUY", "SELL"],
                        },
                        "quantity": {
                            "type": "integer",
                            "description": "Number of units (use lot size: NIFTY=25, BANKNIFTY=15)",
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
                    },
                    "required": ["symbol", "direction", "quantity"],
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
                    "Call this at EOD after reflecting on today's session. You own this "
                    "file entirely — rewrite, consolidate, and refine as you see fit. "
                    "Include: what worked, what didn't, rules to follow, patterns observed."
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
                    "warnings, EOD summary."
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
                    "Log a trading decision to today's journal. You MUST call this "
                    "after EVERY decision — including holds and skips. Always explain "
                    "your reasoning clearly."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "Decision type: ENTER_LONG, ENTER_SHORT, EXIT, HOLD, SKIP, STOP, ABORT",
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
        ]
