"""
agent_loop.py — Core ReAct agent loop for BlitzTrader.

Uses the Anthropic Python SDK to run Claude in a tool-calling loop.
Claude reasons, calls tools, observes results, and acts.
This is a proper autonomous agent — not a scripted pipeline.
"""
import json
import logging
import time
from typing import Optional

import anthropic

logger = logging.getLogger("BlitzTrader.AgentLoop")


class AgentLoop:
    """
    The core agentic loop. Claude is the brain.

    Each iteration:
      1. Assemble context (done by caller)
      2. Send to Claude with tool definitions
      3. Claude reasons and calls tools (0 to N tool calls)
      4. Execute tool calls, return results to Claude
      5. Repeat until Claude produces a final text response
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        tool_registry,
        system_prompt: str,
        max_tool_rounds: int = 10,
        max_tokens: int = 4096,
    ):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._registry = tool_registry
        self._system_prompt = system_prompt
        self._max_tool_rounds = max_tool_rounds
        self._max_tokens = max_tokens

        # Conversation history — persists across iterations within a session
        self._messages: list[dict] = []

        # Token tracking
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def run_iteration(self, user_message: str) -> str:
        """
        Run one complete agent iteration.
        Claude receives the context, reasons, calls tools, and returns.

        :param user_message: The context packet for this iteration
        :returns: Claude's final text response for this iteration
        """
        # Add the user message to conversation
        self._messages.append({
            "role": "user",
            "content": user_message,
        })

        tools = self._registry.get_tool_definitions()
        final_text = ""

        for round_num in range(self._max_tool_rounds):
            response = self._call_with_retry(tools)
            if response is None:
                raise RuntimeError("Claude API call failed after all retries")

            # Track token usage
            self._total_input_tokens += response.usage.input_tokens
            self._total_output_tokens += response.usage.output_tokens

            logger.info(
                f"Round {round_num + 1}: "
                f"stop_reason={response.stop_reason}, "
                f"content_blocks={len(response.content)}, "
                f"tokens={response.usage.input_tokens}in/{response.usage.output_tokens}out"
            )

            # Process response content blocks
            tool_calls = []
            text_parts = []

            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append(block)

            # If there are text parts, accumulate them
            if text_parts:
                final_text = "\n".join(text_parts)

            # If no tool calls, Claude is done reasoning
            if not tool_calls:
                # Add assistant response to history
                self._messages.append({
                    "role": "assistant",
                    "content": response.content,
                })
                break

            # Execute tool calls
            # First, add the assistant message with tool_use blocks
            self._messages.append({
                "role": "assistant",
                "content": response.content,
            })

            # Then add tool results
            tool_results = []
            for tool_call in tool_calls:
                result = self._registry.execute(
                    tool_name=tool_call.name,
                    tool_input=tool_call.input,
                )

                # Convert result to string for Claude
                result_str = json.dumps(result, default=str, ensure_ascii=False)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": result_str,
                })

                logger.info(f"  Tool {tool_call.name}: {result_str[:200]}...")

            # Add tool results as user message
            self._messages.append({
                "role": "user",
                "content": tool_results,
            })
        else:
            logger.warning(
                f"Agent hit max tool rounds ({self._max_tool_rounds}), "
                "forcing stop"
            )

        # Manage context window — trim if too long
        self._trim_history()

        return final_text

    def _call_with_retry(self, tools, max_retries: int = 4) -> object:
        """
        Call the Anthropic API with exponential backoff on rate limits.
        Waits up to 4 minutes total (60s → 120s → ... ) before giving up.
        """
        delay = 65  # slightly over 1 minute — clears the token-per-minute window
        for attempt in range(max_retries):
            try:
                return self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system_prompt,
                    messages=self._messages,
                    tools=tools,
                )
            except anthropic.RateLimitError as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Rate limit hit (attempt {attempt + 1}/{max_retries}). "
                        f"Waiting {delay}s before retry..."
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 300)  # cap at 5 minutes
                else:
                    logger.error(f"Rate limit persists after {max_retries} retries: {e}")
                    raise
            except anthropic.APIError as e:
                logger.error(f"Anthropic API error: {e}")
                raise
            except Exception as e:
                logger.exception("Unexpected error calling Claude")
                raise
        return None

    def inject_message(self, role: str, content: str):
        """
        Inject a message into the conversation history.
        Used for Telegram commands that arrive between iterations.
        """
        self._messages.append({"role": role, "content": content})

    def get_token_usage(self) -> dict:
        """Get cumulative token usage for the session."""
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_input_tokens + self._total_output_tokens,
            "conversation_length": len(self._messages),
        }

    def reset(self):
        """Reset conversation history for a new session."""
        self._messages = []
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def _trim_history(self):
        """
        Trim conversation history if it gets too long.
        Ensure we never sever a tool_use from its tool_result.
        """
        if len(self._messages) <= 30:
            return

        # The startup sequence involves roughly 5 messages to fully execute 
        # get_strategy_docs and the first Telegram ping and return their results.
        # We must keep up to an even index (e.g. index 4) which is a "user" message (tool_result)
        # so that the head ends cleanly.
        head_end = 5
        while head_end < len(self._messages) and self._messages[head_end - 1]["role"] != "assistant":
            head_end += 1
            
        head = self._messages[:head_end]

        # Ensure the tail starts with an "assistant" message so it perfectly alternates
        # with the "user" summary message we are injecting
        tail_start = len(self._messages) - 20
        while tail_start < len(self._messages) and self._messages[tail_start]["role"] != "assistant":
            tail_start += 1
            
        tail = self._messages[tail_start:]

        trimmed_count = len(self._messages) - len(head) - len(tail)

        # Insert a summary marker as a user message
        summary = {
            "role": "user",
            "content": (
                f"[Context trimmed: {trimmed_count} messages removed to fit context window. "
                f"Refer to your earlier decisions via get_todays_trades() if needed.]"
            ),
        }

        self._messages = head + [summary] + tail
        logger.info(f"Trimmed conversation: removed {trimmed_count} messages safely.")
