"""
tools/gemma_observer.py — Gemma local-model observer for BlitzTrader.

GemmaObserver evaluates every candidate signal asynchronously in a background
thread.  It is NEVER in the decision path:
  - It does not block order placement.
  - Its output cannot approve or reject a trade.
  - Its opinion is recorded for journaling and included in Telegram notifications.

Model: gemma-3-4b-it via google-genai SDK (same SDK as Gemini, lighter model).

Invariants (NEVER violate):
  - GemmaObserver result is NOT consulted by GeminiGatekeeper or Python order logic
  - If Gemma API fails/times out, trading continues normally — error is logged only
  - Gemma has no tool access
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional, Callable

logger = logging.getLogger("BlitzTrader.GemmaObserver")

_OBSERVER_SYSTEM_PROMPT = """\
You are Gemma, an observer AI for BlitzTrader. You evaluate trading signals for educational
and journaling purposes ONLY. Your output is NEVER used to approve or reject trades.

Respond with ONLY valid JSON — no prose, no markdown, no code fences.

Required JSON schema:
{
  "alignment": "STRONG" | "MODERATE" | "WEAK" | "CONFLICTED",
  "confidence": <float 0.0-1.0>,
  "key_observation": "<one insight, max 100 chars>",
  "concern": "<one concern or 'none', max 100 chars>"
}

alignment:
  STRONG     — clear evidence supporting the signal direction
  MODERATE   — partial support, some uncertainty
  WEAK       — little support; signal may be premature
  CONFLICTED — significant conflicting evidence present

Be concise and honest. You are not approving or rejecting — just observing.
"""

_VALID_ALIGNMENTS = {"STRONG", "MODERATE", "WEAK", "CONFLICTED"}


class GemmaObserver:
    """
    Non-blocking observer that evaluates candidate signals using gemma-3-4b-it.

    Usage:
        observer = GemmaObserver(api_key="...", callback=_record_opinion)
        observer.submit(signal, context)   # returns immediately
        # ... callback is called with the opinion dict when Gemma responds
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemma-3-4b-it",
        timeout_seconds: int = 15,
        callback: Optional[Callable[[dict, dict], None]] = None,
    ):
        """
        Args:
            api_key:         Gemini/Google API key (same key, lighter model)
            model:           Model to use (default: gemma-3-4b-it)
            timeout_seconds: Per-call timeout (non-blocking — caller never waits)
            callback:        Optional fn(signal, opinion) called when result arrives.
                             Called from a background daemon thread.
        """
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._callback = callback
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is None:
            from google import genai  # noqa: PLC0415
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    # ────────────────────────────────────────────────────────────────
    #   Public API
    # ────────────────────────────────────────────────────────────────

    def submit(self, signal: dict, context: str) -> None:
        """
        Submit a signal for async observer evaluation.  Returns immediately.

        The callback (if provided) is invoked from a daemon thread when Gemma
        responds.  The opinion dict always contains a 'gemma_error' key (None
        on success) so callers never need to guard against missing fields.
        """
        thread = threading.Thread(
            target=self._evaluate_async,
            args=(signal, context),
            name=f"BlitzTrader-Gemma-{signal.get('symbol', '?')}",
            daemon=True,
        )
        thread.start()

    # ────────────────────────────────────────────────────────────────
    #   Internal
    # ────────────────────────────────────────────────────────────────

    def _evaluate_async(self, signal: dict, context: str) -> None:
        t0 = time.monotonic()
        opinion = self._run(signal, context)
        elapsed = round(time.monotonic() - t0, 2)
        opinion["elapsed_seconds"] = elapsed

        symbol = signal.get("symbol", "?")
        strategy = signal.get("strategy", "?")

        if opinion.get("gemma_error"):
            logger.warning(
                "Gemma observer error for %s %s: %s",
                symbol, strategy, opinion["gemma_error"],
            )
        else:
            logger.info(
                "Gemma observer %s (%.0f%%) for %s %s in %.1fs — %s",
                opinion.get("alignment", "?"),
                float(opinion.get("confidence", 0)) * 100,
                symbol, strategy, elapsed,
                opinion.get("key_observation", ""),
            )

        if self._callback:
            try:
                self._callback(signal, opinion)
            except Exception:
                logger.exception("GemmaObserver callback raised")

    def _run(self, signal: dict, context: str) -> dict:
        """Synchronous Gemma call (run inside daemon thread)."""
        from google.genai import types  # noqa: PLC0415

        prompt = self._build_prompt(signal, context)
        result_q: queue.Queue = queue.Queue(maxsize=1)

        def worker():
            try:
                client = self._get_client()
                resp = client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=_OBSERVER_SYSTEM_PROMPT,
                        max_output_tokens=200,
                        temperature=0.2,
                        response_mime_type="application/json",
                    ),
                )
                result_q.put(("ok", resp.text or ""))
            except Exception as exc:
                result_q.put(("err", exc))

        t = threading.Thread(
            target=worker, name="BlitzTrader-GemmaCall", daemon=True
        )
        t.start()

        try:
            status, payload = result_q.get(timeout=self._timeout)
        except queue.Empty:
            return self._error_opinion(f"Gemma timed out after {self._timeout}s")

        if status == "err":
            return self._error_opinion(f"Gemma API error: {type(payload).__name__}: {payload}")

        return self._parse_opinion(payload)

    def _build_prompt(self, signal: dict, context: str) -> str:
        symbol = signal.get("symbol", "?")
        strategy = signal.get("strategy", "?")
        direction = signal.get("direction", "?")
        interval = signal.get("interval", "?")
        stop_loss = signal.get("stop_loss")
        target = signal.get("target")
        entry_ref = signal.get("entry_reference")

        lines = [
            "Observe this candidate signal (for journaling only — not a trade decision):",
            f"  Symbol:    {symbol}",
            f"  Strategy:  {strategy}",
            f"  Direction: {direction}",
            f"  Interval:  {interval}m",
        ]
        if entry_ref is not None:
            lines.append(f"  Entry ref: ₹{entry_ref:.2f}")
        if stop_loss is not None:
            lines.append(f"  Stop-loss: ₹{stop_loss:.2f}")
        if target is not None:
            lines.append(f"  Target:    ₹{target:.2f}")
        lines += [
            "",
            "Current indicators:",
            context or "(no indicators provided)",
            "",
            "Respond with JSON only.",
        ]
        return "\n".join(lines)

    def _parse_opinion(self, raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(
                line for line in raw.splitlines() if not line.startswith("```")
            ).strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._error_opinion(f"JSON parse error: {exc}")

        if not isinstance(data, dict):
            return self._error_opinion(f"Expected JSON object, got {type(data).__name__}")

        alignment = str(data.get("alignment", "")).upper()
        if alignment not in _VALID_ALIGNMENTS:
            return self._error_opinion(f"Invalid alignment: {alignment!r}")

        return {
            "alignment": alignment,
            "confidence": float(data.get("confidence", 0.0)),
            "key_observation": str(data.get("key_observation", ""))[:200],
            "concern": str(data.get("concern", ""))[:200],
            "gemma_error": None,
        }

    @staticmethod
    def _error_opinion(msg: str) -> dict:
        return {
            "alignment": "WEAK",
            "confidence": 0.0,
            "key_observation": "Observer unavailable",
            "concern": msg[:200],
            "gemma_error": msg,
        }
