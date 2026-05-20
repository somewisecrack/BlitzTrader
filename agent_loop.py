"""
agent_loop.py — Core ReAct agent loop for BlitzTrader.

Uses the Google Gemini SDK to run Gemini 2.5 Flash in a tool-calling loop.
The model reasons, calls tools, observes results, and acts.
"""
import json
import logging
import queue
import threading
import time

from google import genai
from google.genai import types

logger = logging.getLogger("BlitzTrader.AgentLoop")


def _build_gemini_tools(tool_definitions: list[dict]) -> list[types.Tool]:
    """Convert our tool definitions to Gemini format."""
    declarations = []
    for tool_def in tool_definitions:
        schema = tool_def.get("input_schema", {})
        properties = {}
        for prop_name, prop_schema in schema.get("properties", {}).items():
            prop_type = prop_schema.get("type", "STRING").upper()
            # Map JSON schema types to Gemini types
            type_map = {
                "STRING": "STRING",
                "INTEGER": "INTEGER",
                "NUMBER": "NUMBER",
                "BOOLEAN": "BOOLEAN",
                "ARRAY": "ARRAY",
                "OBJECT": "OBJECT",
            }
            gemini_type = type_map.get(prop_type, "STRING")

            prop_kwargs = {
                "type": gemini_type,
                "description": prop_schema.get("description", ""),
            }

            # Handle enum
            if "enum" in prop_schema:
                prop_kwargs["enum"] = prop_schema["enum"]

            # Handle array items
            if gemini_type == "ARRAY" and "items" in prop_schema:
                item_type = prop_schema["items"].get("type", "STRING").upper()
                prop_kwargs["items"] = types.Schema(type=item_type)

            properties[prop_name] = types.Schema(**prop_kwargs)

        params = types.Schema(
            type="OBJECT",
            properties=properties,
            required=schema.get("required", []),
        ) if properties else None

        declarations.append(types.FunctionDeclaration(
            name=tool_def["name"],
            description=tool_def["description"],
            parameters=params,
        ))

    return [types.Tool(function_declarations=declarations)]


class AgentLoop:
    """
    The core agentic loop. Gemini 2.5 Flash is the brain.

    Each iteration:
      1. Assemble context (done by caller)
      2. Send to Gemini with tool definitions
      3. Gemini reasons and calls tools (0 to N tool calls)
      4. Execute tool calls, return results to Gemini
      5. Repeat until Gemini produces a final text response
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        tool_registry,
        system_prompt: str,
        max_tool_rounds: int = 10,
        max_tokens: int = 8192,
        api_timeout_seconds: int = 45,
    ):
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._registry = tool_registry
        self._system_prompt = system_prompt
        self._max_tool_rounds = max_tool_rounds
        self._max_tokens = max_tokens
        self._api_timeout_seconds = api_timeout_seconds

        # Gemini conversation history
        self._history: list[types.Content] = []

        # Token tracking
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._last_error: dict | None = None

    def run_iteration(
        self,
        user_message: str,
        model: str = None,
        max_tokens: int = None,
        max_tool_rounds: int = None,
    ) -> str:
        """
        Run one complete agent iteration.
        Each iteration starts fresh to keep context clean.
        """
        # Start fresh each iteration
        self._history = []
        self._last_error = None

        tool_defs = self._registry.get_tool_definitions()
        gemini_tools = _build_gemini_tools(tool_defs)
        final_text = ""
        active_model = model or self._model
        active_max_tokens = max_tokens or self._max_tokens
        active_max_tool_rounds = max_tool_rounds or self._max_tool_rounds

        # Add user message
        self._history.append(types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_message)],
        ))

        logger.info(
            "Starting Gemini iteration model=%s max_tokens=%s max_tool_rounds=%s",
            active_model,
            active_max_tokens,
            active_max_tool_rounds,
        )

        for round_num in range(active_max_tool_rounds):
            response = self._call_with_retry(
                gemini_tools,
                model=active_model,
                max_tokens=active_max_tokens,
            )
            if response is None:
                logger.warning("API call returned None — ending iteration gracefully")
                break

            # Track tokens
            if response.usage_metadata:
                self._total_input_tokens += response.usage_metadata.prompt_token_count or 0
                self._total_output_tokens += response.usage_metadata.candidates_token_count or 0

            if not response.candidates:
                logger.warning("API returned empty candidates — ending iteration gracefully")
                break
            candidate = response.candidates[0]
            parts = candidate.content.parts if candidate.content and candidate.content.parts else []

            # Log round info
            input_t = response.usage_metadata.prompt_token_count if response.usage_metadata else 0
            output_t = response.usage_metadata.candidates_token_count if response.usage_metadata else 0
            logger.info(
                f"Round {round_num + 1}: "
                f"parts={len(parts)}, "
                f"tokens={input_t}in/{output_t}out"
            )

            # If empty response, skip this round
            if not parts:
                logger.warning(f"Round {round_num + 1}: empty response from model, skipping")
                break

            # Separate text and function calls
            function_calls = []
            text_parts = []

            for part in parts:
                if part.function_call:
                    function_calls.append(part.function_call)
                elif part.text:
                    text_parts.append(part.text)

            if text_parts:
                final_text = "\n".join(text_parts)

            # If no function calls, model is done
            if not function_calls:
                # Add model response to history
                self._history.append(candidate.content)
                break

            # Add model response to history (includes function calls)
            self._history.append(candidate.content)

            # Execute function calls and build responses
            function_responses = []
            for fc in function_calls:
                tool_input = dict(fc.args) if fc.args else {}
                result = self._registry.execute(
                    tool_name=fc.name,
                    tool_input=tool_input,
                )

                result_str = json.dumps(result, default=str, ensure_ascii=False)
                logger.info(f"  Tool {fc.name}: {result_str[:200]}...")

                function_responses.append(types.Part.from_function_response(
                    name=fc.name,
                    response=result,
                ))

            # Add tool results to history
            self._history.append(types.Content(
                role="user",
                parts=function_responses,
            ))
        else:
            logger.warning(
                f"Agent hit max tool rounds ({active_max_tool_rounds}), forcing stop"
            )

        return final_text

    def _generate_content_with_timeout(
        self,
        *,
        model: str,
        contents,
        config,
    ):
        """
        Run Gemini in a daemon worker so a hung network/model call cannot freeze
        the trading loop, Telegram handling, SL checks, or EOD cleanup.
        """
        result_q: queue.Queue = queue.Queue(maxsize=1)

        def worker():
            try:
                result_q.put((
                    "ok",
                    self._client.models.generate_content(
                        model=model,
                        contents=contents,
                        config=config,
                    ),
                ))
            except Exception as exc:
                result_q.put(("err", exc))

        thread = threading.Thread(
            target=worker,
            name="BlitzTrader-GeminiCall",
            daemon=True,
        )
        thread.start()
        try:
            status, payload = result_q.get(timeout=self._api_timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError(
                f"Gemini API call timed out after {self._api_timeout_seconds}s"
            ) from exc
        if status == "err":
            raise payload
        return payload

    def get_last_error(self) -> dict | None:
        """Return the last API failure metadata for caller-side circuit breakers."""
        return self._last_error

    def _call_with_retry(
        self,
        gemini_tools,
        model: str = None,
        max_tokens: int = None,
        max_retries: int = 3,
    ) -> object:
        """Call Gemini API with retry on transient errors."""
        delay = 5
        active_model = model or self._model
        active_max_tokens = max_tokens or self._max_tokens
        for attempt in range(max_retries):
            try:
                return self._generate_content_with_timeout(
                    model=active_model,
                    contents=self._history,
                    config=types.GenerateContentConfig(
                        system_instruction=self._system_prompt,
                        tools=gemini_tools,
                        max_output_tokens=active_max_tokens,
                        temperature=0.3,
                    ),
                )
            except Exception as e:
                err_str = str(e).lower()
                if isinstance(e, TimeoutError):
                    self._last_error = {
                        "kind": "api_timeout",
                        "model": active_model,
                        "message": str(e),
                    }
                    logger.error("%s — disabling retries for this call", e)
                    return None
                if "429" in str(e) or "resource" in err_str or "quota" in err_str:
                    if "monthly spending cap" in err_str or "spend cap" in err_str:
                        self._last_error = {
                            "kind": "monthly_spending_cap",
                            "model": active_model,
                            "message": str(e),
                        }
                        logger.error(
                            "Gemini monthly spending cap exceeded — disabling retries "
                            "for this call. Manage the project spend cap in AI Studio."
                        )
                        return None
                    # Daily quota exhausted — retrying in 5/10s will never help.
                    # Bail out immediately so the main loop can continue rather
                    # than blocking for 30+ seconds.
                    if "perday" in err_str or "per_day" in err_str or "GenerateRequestsPerDay" in str(e):
                        self._last_error = {
                            "kind": "daily_quota_exhausted",
                            "model": active_model,
                            "message": str(e),
                        }
                        logger.error(
                            f"Gemini DAILY quota exhausted — no retries until midnight UTC. "
                            f"Check quota usage at https://ai.dev/rate-limit and billing at "
                            f"https://console.cloud.google.com/billing. Ensure a paid-tier API "
                            f"key is set in GEMINI_API_KEY (current model: {active_model})."
                        )
                        return None
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Rate limit (attempt {attempt + 1}/{max_retries}). "
                            f"Waiting {delay}s..."
                        )
                        time.sleep(delay)
                        delay = min(delay * 2, 60)
                    else:
                        logger.error(f"Rate limit persists: {e}")
                        return None
                else:
                    err_str_full = str(e)
                    # Detect 503/UNAVAILABLE (ServiceUnavailable, DeadlineExceeded,
                    # grpc UNAVAILABLE, or any message containing "503"/"UNAVAILABLE")
                    is_unavailable = (
                        "503" in err_str_full
                        or "UNAVAILABLE" in err_str_full
                        or "ServiceUnavailable" in type(e).__name__
                        or "DeadlineExceeded" in type(e).__name__
                        or (hasattr(e, "code") and callable(getattr(e, "code", None))
                            and str(getattr(e, "code", lambda: "")()) == "StatusCode.UNAVAILABLE")
                    )
                    if is_unavailable:
                        self._last_error = {
                            "kind": "service_unavailable",
                            "model": active_model,
                            "message": err_str_full,
                        }
                        if attempt < max_retries - 1:
                            logger.warning(
                                "Gemini 503/UNAVAILABLE (attempt %d/%d). "
                                "Waiting %ds before retry...",
                                attempt + 1, max_retries, delay,
                            )
                            time.sleep(delay)
                            delay = min(delay * 2, 30)
                            continue
                        logger.error(
                            "Gemini 503/UNAVAILABLE after %d attempts — returning None",
                            max_retries,
                        )
                        return None
                    logger.error(f"Gemini API error: {e}")
                    self._last_error = {
                        "kind": "api_error",
                        "model": active_model,
                        "message": err_str_full,
                    }
                    if attempt >= max_retries - 1:
                        return None
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
        return None

    def inject_message(self, role: str, content: str):
        """Inject a message into conversation history."""
        self._history.append(types.Content(
            role=role,
            parts=[types.Part.from_text(text=content)],
        ))

    def get_token_usage(self) -> dict:
        """Get cumulative token usage for the session."""
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_input_tokens + self._total_output_tokens,
            "conversation_length": len(self._history),
        }

    def reset(self):
        """Reset conversation history for a new session."""
        self._history = []
        self._total_input_tokens = 0
        self._total_output_tokens = 0
