"""
agent_loop.py — Core ReAct agent loop for BlitzTrader.

Uses the Groq Python SDK to run Mixtral in a tool-calling loop.
Mixtral reasons, calls tools, observes results, and acts.
This is a proper autonomous agent — not a scripted pipeline.
"""
import json
import logging
import time
from typing import Optional

from groq import Groq

logger = logging.getLogger("BlitzTrader.AgentLoop")


class AgentLoop:
    """
    The core agentic loop. Mixtral (via Groq) is the brain.

    Each iteration:
      1. Assemble context (done by caller)
      2. Send to Mixtral with tool definitions
      3. Mixtral reasons and calls tools (0 to N tool calls)
      4. Execute tool calls, return results to Mixtral
      5. Repeat until Mixtral produces a final text response
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
        self._client = Groq(api_key=api_key)
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
            if response.usage:
                self._total_input_tokens += response.usage.prompt_tokens
                self._total_output_tokens += response.usage.completion_tokens

            content_msg = response.choices[0].message
            content_len = len(content_msg.content) if content_msg.content else 0
            logger.info(
                f"Round {round_num + 1}: "
                f"finish_reason={response.choices[0].finish_reason}, "
                f"content_blocks={content_len}, "
                f"tokens={response.usage.prompt_tokens if response.usage else 0}in/{response.usage.completion_tokens if response.usage else 0}out"
            )

            # Process response content blocks (Groq format)
            tool_calls = []
            text_parts = []
            message_content = response.choices[0].message

            if message_content.tool_calls:
                tool_calls = message_content.tool_calls

            if message_content.content:
                text_parts.append(message_content.content)

            # If there are text parts, accumulate them
            if text_parts:
                final_text = "\n".join(text_parts)

            # If no tool calls, Mixtral is done reasoning
            if not tool_calls:
                # Add assistant response to history
                self._messages.append({
                    "role": "assistant",
                    "content": message_content.content or "",
                })
                break

            # Execute tool calls
            # First, add the assistant message
            self._messages.append({
                "role": "assistant",
                "content": message_content.content or "",
                "tool_calls": tool_calls if tool_calls else None,
            })

            # Then add tool results
            tool_results = []
            for tool_call in tool_calls:
                result = self._registry.execute(
                    tool_name=tool_call.function.name,
                    tool_input=json.loads(tool_call.function.arguments),
                )

                # Convert result to string for Mixtral
                result_str = json.dumps(result, default=str, ensure_ascii=False)

                tool_results.append({
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "content": result_str,
                })

                logger.info(f"  Tool {tool_call.function.name}: {result_str[:200]}...")

            # Add tool results as user message
            for tool_result in tool_results:
                self._messages.append(tool_result)
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
        Call the Groq API with exponential backoff on rate limits.
        Waits up to 4 minutes total (65s → 130s → ... ) before giving up.
        """
        delay = 65
        for attempt in range(max_retries):
            try:
                # Convert tool definitions to Groq format
                groq_tools = []
                for tool in tools:
                    groq_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool["description"],
                            "parameters": tool["input_schema"],
                        }
                    })

                # Build messages list with system prompt
                messages = [{"role": "system", "content": self._system_prompt}] + self._messages

                return self._client.chat.completions.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    messages=messages,
                    tools=groq_tools if groq_tools else None,
                    tool_choice="auto" if groq_tools else None,
                )
            except Exception as e:
                if "rate_limit" in str(e).lower() or "429" in str(e):
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Rate limit hit (attempt {attempt + 1}/{max_retries}). "
                            f"Waiting {delay}s before retry..."
                        )
                        time.sleep(delay)
                        delay = min(delay * 2, 300)
                    else:
                        logger.error(f"Rate limit persists after {max_retries} retries: {e}")
                        raise
                else:
                    logger.error(f"Groq API error: {e}")
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
