"""
tools/gemma_observer.py — Local Gemma observer for BlitzTrader.

GemmaObserver evaluates candidate signals asynchronously via a local Ollama
endpoint.  It is NEVER in the decision path:
  - It does not block order placement.
  - Its output cannot approve or reject a trade.
  - Its opinion is recorded for journaling and included in Telegram notifications.

Provider: local Ollama (HTTP API only — no cloud AI SDK dependency)
Default: DISABLED (VM has insufficient RAM/disk for local model)

Invariants (NEVER violate):
  - GemmaObserver result is NOT consulted by GeminiGatekeeper or Python order logic
  - If Gemma is disabled, unavailable, or errors, trading continues normally
  - Gemma has no tool access
  - Uses only stdlib urllib — no third-party AI SDK imports

Request JSON (sent to Ollama /api/generate):
  {"model": MODEL, "prompt": "...", "stream": false, "format": "json"}

Required response schema from the model:
  {
    "opinion": "GOOD" | "BAD" | "UNCLEAR",
    "confidence": <float 0.0-1.0>,
    "reason": "<short reason, max 100 chars>",
    "concerns": ["<concern1>", ...],
    "hallucination_guardrail_ack": true
  }

Internal opinion dict (used by callbacks, Telegram, journal — stable API):
  {
    "alignment": "STRONG"/"MODERATE"/"WEAK"/"CONFLICTED"/"UNAVAILABLE",
    "confidence": float,
    "key_observation": str,
    "concern": str,
    "gemma_error": str | None
  }
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
import urllib.error
import socket
from typing import Optional, Callable

logger = logging.getLogger("BlitzTrader.GemmaObserver")

_VALID_OPINIONS = {"GOOD", "BAD", "UNCLEAR"}
_VALID_ALIGNMENTS = {"STRONG", "MODERATE", "WEAK", "CONFLICTED", "UNAVAILABLE"}

# Map model opinion to internal alignment label
_OPINION_TO_ALIGNMENT = {
    "GOOD": "STRONG",
    "BAD": "WEAK",
    "UNCLEAR": "CONFLICTED",
}

_OBSERVER_SYSTEM_PROMPT = """\
You are Gemma, an observer AI evaluating trading signals for journaling ONLY.
Your output is NEVER used to approve or reject trades. Be concise and honest.

Respond with ONLY valid JSON — no prose, no markdown, no code fences.

Required schema:
{
  "opinion": "GOOD" | "BAD" | "UNCLEAR",
  "confidence": <float 0.0-1.0>,
  "reason": "<one key insight, max 100 chars>",
  "concerns": ["<concern1>", ...],
  "hallucination_guardrail_ack": true
}

opinion:
  GOOD    — evidence supports the signal direction
  BAD     — meaningful conflicting evidence
  UNCLEAR — insufficient or ambiguous evidence

hallucination_guardrail_ack MUST be exactly true (boolean).
Do NOT invent data not provided. Do NOT produce "approved" or "decision" fields.
"""


class GemmaObserver:
    """
    Non-blocking observer that evaluates candidate signals using a local Gemma
    model via Ollama HTTP API.

    When GEMMA_OBSERVER_ENABLED=false (default), submit() immediately calls
    the callback with an UNAVAILABLE opinion — no HTTP calls are made.

    Usage:
        observer = GemmaObserver(enabled=False, ...)
        observer.submit(signal, context)   # returns immediately
        # callback is called with the opinion dict
    """

    def __init__(
        self,
        enabled: bool = False,
        url: str = "http://localhost:11434",
        model: str = "gemma3:1b",
        timeout_seconds: int = 3,
        callback: Optional[Callable[[dict, dict], None]] = None,
    ):
        """
        Args:
            enabled:         Whether to actually call Ollama. Default False.
            url:             Base URL for Ollama API.
            model:           Model name for Ollama (e.g. "gemma3:1b").
            timeout_seconds: Per-call HTTP timeout.
            callback:        Optional fn(signal, opinion) called when result arrives.
                             Called from a background daemon thread.
        """
        self._enabled = enabled
        self._url = url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._callback = callback

    # ────────────────────────────────────────────────────────────────
    #   Public API
    # ────────────────────────────────────────────────────────────────

    def submit(self, signal: dict, context: str) -> None:
        """
        Submit a signal for async observer evaluation.  Returns immediately.

        When disabled, the callback is invoked synchronously with an UNAVAILABLE
        opinion so callers can always count on the callback being fired.
        """
        if not self._enabled:
            opinion = self._unavailable_opinion("Observer disabled (GEMMA_OBSERVER_ENABLED=false)")
            if self._callback:
                try:
                    self._callback(signal, opinion)
                except Exception:
                    logger.exception("GemmaObserver disabled-callback raised")
            return

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
        """Synchronous Ollama HTTP call (run inside daemon thread)."""
        prompt = self._build_prompt(signal, context)
        payload = json.dumps({
            "model": self._model,
            "prompt": f"{_OBSERVER_SYSTEM_PROMPT}\n\n{prompt}",
            "stream": False,
            "format": "json",
        }).encode("utf-8")

        endpoint = f"{self._url}/api/generate"
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, ConnectionRefusedError, OSError) as exc:
            return self._error_opinion(f"Ollama connection error: {exc}")
        except socket.timeout as exc:
            return self._error_opinion(f"Ollama timed out after {self._timeout}s")
        except Exception as exc:
            return self._error_opinion(f"Ollama HTTP error: {type(exc).__name__}: {exc}")

        # Ollama wraps response in {"response": "...", ...}
        try:
            outer = json.loads(raw)
            model_text = outer.get("response", "")
        except json.JSONDecodeError as exc:
            return self._error_opinion(f"Ollama outer JSON parse error: {exc}")

        return self._parse_opinion(model_text)

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
        """Parse model JSON response into internal opinion dict."""
        if not raw:
            return self._error_opinion("Empty response from model")

        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(
                line for line in raw.splitlines() if not line.startswith("```")
            ).strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._error_opinion(f"JSON parse error: {exc} — raw={raw[:100]!r}")

        if not isinstance(data, dict):
            return self._error_opinion(f"Expected JSON object, got {type(data).__name__}")

        # Validate opinion
        opinion_raw = str(data.get("opinion", "")).upper()
        if opinion_raw not in _VALID_OPINIONS:
            return self._error_opinion(f"Invalid opinion: {opinion_raw!r} (expected GOOD/BAD/UNCLEAR)")

        # Validate confidence
        conf = data.get("confidence")
        if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
            return self._error_opinion(f"Invalid confidence: {conf!r} (must be float 0.0-1.0)")

        # Validate hallucination ack
        ack = data.get("hallucination_guardrail_ack")
        if ack is not True:
            return self._error_opinion(
                f"hallucination_guardrail_ack must be true, got {ack!r}"
            )

        # Validate concerns is a list
        concerns = data.get("concerns", [])
        if not isinstance(concerns, list):
            return self._error_opinion(f"concerns must be a list, got {type(concerns).__name__}")

        alignment = _OPINION_TO_ALIGNMENT[opinion_raw]
        return {
            "alignment": alignment,
            "confidence": float(conf),
            "key_observation": str(data.get("reason", ""))[:200],
            "concern": ", ".join(str(c) for c in concerns[:3]),
            "gemma_error": None,
        }

    @staticmethod
    def _unavailable_opinion(reason: str) -> dict:
        return {
            "alignment": "UNAVAILABLE",
            "confidence": 0.0,
            "key_observation": "Observer disabled",
            "concern": reason[:200],
            "gemma_error": reason,
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
