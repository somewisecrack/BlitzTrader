"""
tools/gemini_gatekeeper.py — Gemini live entry gatekeeper for BlitzTrader.

GeminiGatekeeper is called AFTER Python hard guardrails pass, BEFORE an order
is placed.  It has a strict 5-second timeout and must return structured JSON.

Invariants (NEVER violate):
- Any timeout, error, rate-limit, invalid JSON, or missing required field → REJECT
- Gemini may APPROVE or REJECT; it may never modify position size or SL/target
- This class has no tool access; it sends one prompt, parses one response

Response schema (all fields required):
  {
    "decision":                          "APPROVE" | "REJECT",
    "confidence":                        float 0.0–1.0,
    "reason":                            str   (≤ 120 chars),
    "risk_notes":                        str | list[str],
    "conditions_checked":                [str, ...],
    "must_not_override_python_guardrails": true   ← REQUIRED boolean literal true
  }

Forbidden fields (auto-reject if present):
  quantity, lot_size, stop_loss, target, capital, leverage,
  instrument, order_type, position_size, size
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

logger = logging.getLogger("BlitzTrader.GeminiGatekeeper")

_REQUIRED_FIELDS = {
    "decision", "confidence", "reason", "risk_notes",
    "conditions_checked", "must_not_override_python_guardrails",
}
_VALID_DECISIONS = {"APPROVE", "REJECT"}

# Fields that would indicate Gemini is trying to override Python execution logic
_FORBIDDEN_FIELDS = {
    "quantity", "lot_size", "stop_loss", "target", "capital", "leverage",
    "instrument", "order_type", "position_size", "size",
}

_GATEKEEPER_SYSTEM_PROMPT = """\
You are the Gemini Entry Gatekeeper for BlitzTrader, an Indian index option-spread trading system.

Python has already validated the strategy signal, hard guardrails, spread construction,
liquidity checks, risk limits, sizing, and execution constraints.

Your ONLY job is to act as a veto-only entry gatekeeper for the specific option-spread
candidate. Do NOT rediscover the strategy. Do NOT demand perfect confirmation.
Approve broadly valid Python-passed candidates unless there is a clear, material reason
to veto the entry right now.

You must respond with ONLY valid JSON — no prose, no markdown, no code fences.

Required JSON schema (ALL fields are required):
{
  "decision": "APPROVE" or "REJECT",
  "confidence": <float 0.0-1.0>,
  "reason": "<concise reason, max 120 chars>",
  "risk_notes": "<key risk or list of risks>",
  "conditions_checked": ["<condition 1>", "<condition 2>", ...],
  "must_not_override_python_guardrails": true
}

HARD CONSTRAINTS — you CANNOT modify any of these (Python owns them absolutely):
- Position size / lot_size / quantity
- Stop-loss or target price
- Capital allocation or leverage
- Instrument selection or order type
You approve or reject ONLY the entry quality. Python owns everything else.
You MUST include "must_not_override_python_guardrails": true in your JSON response.

Rules:
- Default stance: APPROVE if the spread direction and provided indicators are broadly aligned
- REJECT only for clear material contradictions, stale/missing data, illiquidity, very poor R:R,
  or obvious chop/unsafe context
- If context is mixed but not clearly hostile, APPROVE with lower confidence
- Do not require every indicator to agree; one imperfect indicator is not enough to reject
- Do not reject only because RSI is overbought/oversold unless it directly invalidates the setup
- Do not reject a bearish spread because indicators are bearish
- Do not reject a bullish spread because indicators are bullish
- BEAR_CALL credit spread: bearish or neutral-to-bearish context supports it
- BULL_PUT credit spread: bullish or neutral-to-bullish context supports it
- BEAR_PUT debit spread: bearish directional follow-through supports it
- BULL_CALL debit spread: bullish directional follow-through supports it
- Do NOT invent market data not provided to you
- Do NOT include fields like stop_loss, target, quantity, lot_size, or capital in your response
- Respond with JSON only. Any other format causes automatic rejection.
"""


class GeminiGatekeeper:
    """
    Calls Gemini with a 5-second timeout to approve or reject one candidate signal.

    Args:
        api_key: Gemini API key
        model:   Model to use (default gemini-2.5-flash-lite)
        timeout_seconds: Hard timeout (default 5)
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash-lite",
        timeout_seconds: int = 5,
    ):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._client = None  # lazy init — avoids import cost at startup

    def _get_client(self):
        if self._client is None:
            from google import genai  # noqa: PLC0415
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    # ────────────────────────────────────────────────────────────────
    #   Public API
    # ────────────────────────────────────────────────────────────────

    def evaluate(
        self,
        signal: dict,
        context: str,
    ) -> dict:
        """
        Ask Gemini whether to approve this signal.

        Returns a dict:
          {
            "decision":  "APPROVE" | "REJECT",
            "approved":  bool,
            "confidence": float,
            "reason":    str,
            "risk_notes": str,
            "conditions_checked": list[str],
            "gatekeeper_error": str | None   # set on timeout/parse failure
          }

        Any failure path returns decision="REJECT", approved=False.
        """
        t0 = time.monotonic()
        prompt = self._build_prompt(signal, context)

        try:
            raw_json = self._call_with_timeout(prompt)
        except TimeoutError as exc:
            elapsed = time.monotonic() - t0
            logger.warning(
                "Gatekeeper TIMEOUT after %.1fs for %s %s — auto-REJECT",
                elapsed, signal.get("symbol"), signal.get("strategy"),
            )
            return self._reject_result(f"Gatekeeper timed out after {elapsed:.1f}s")
        except Exception as exc:
            logger.error(
                "Gatekeeper API error for %s %s: %s — auto-REJECT",
                signal.get("symbol"), signal.get("strategy"), exc,
            )
            return self._reject_result(f"Gatekeeper API error: {type(exc).__name__}: {exc}")

        elapsed = time.monotonic() - t0
        parsed = self._parse_response(raw_json)
        if parsed.get("parse_error"):
            logger.warning(
                "Gatekeeper JSON parse error for %s %s: %s — auto-REJECT",
                signal.get("symbol"), signal.get("strategy"), parsed["parse_error"],
            )
            return self._reject_result(parsed["parse_error"])

        decision = str(parsed.get("decision", "")).upper()
        if decision not in _VALID_DECISIONS:
            return self._reject_result(f"Invalid decision field: {decision!r}")

        approved = (decision == "APPROVE")
        confidence = float(parsed.get("confidence", 0.0))
        logger.info(
            "Gatekeeper %s (%.0f%%) for %s %s %s in %.1fs — %s",
            decision, confidence * 100,
            signal.get("symbol"), signal.get("strategy"), signal.get("direction"),
            elapsed, parsed.get("reason", ""),
        )
        return {
            "decision": decision,
            "approved": approved,
            "confidence": confidence,
            "reason": parsed.get("reason", ""),
            "risk_notes": parsed.get("risk_notes", ""),
            "conditions_checked": parsed.get("conditions_checked", []),
            "gatekeeper_error": None,
            "elapsed_seconds": round(elapsed, 2),
        }

    # ────────────────────────────────────────────────────────────────
    #   Internal helpers
    # ────────────────────────────────────────────────────────────────

    def _build_prompt(self, signal: dict, context: str) -> str:
        symbol = signal.get("symbol", "?")
        strategy = signal.get("strategy", "?")
        direction = signal.get("direction", "?")
        interval = signal.get("interval", "?")
        stop_loss = signal.get("stop_loss")
        target = signal.get("target")
        entry_ref = signal.get("entry_reference")

        lines = [
            f"Candidate signal requiring gate approval:",
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
            "Market context (Python-verified indicators):",
            context or "(no context provided)",
            "",
            "Respond with JSON only.",
        ]
        return "\n".join(lines)

    def _call_with_timeout(self, prompt: str) -> str:
        """Call Gemini in a daemon thread; raise TimeoutError if > self._timeout seconds."""
        from google.genai import types  # noqa: PLC0415

        result_q: queue.Queue = queue.Queue(maxsize=1)

        def worker():
            try:
                client = self._get_client()
                resp = client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=_GATEKEEPER_SYSTEM_PROMPT,
                        max_output_tokens=300,
                        temperature=0.1,
                        response_mime_type="application/json",
                    ),
                )
                result_q.put(("ok", resp.text or ""))
            except Exception as exc:
                result_q.put(("err", exc))

        t = threading.Thread(target=worker, name="BlitzTrader-GatekeeperCall", daemon=True)
        t.start()

        try:
            status, payload = result_q.get(timeout=self._timeout)
        except queue.Empty as exc:
            raise TimeoutError(
                f"Gemini gatekeeper timed out after {self._timeout}s"
            ) from exc

        if status == "err":
            raise payload
        return payload

    def _parse_response(self, raw) -> dict:
        """Parse gatekeeper JSON; returns {parse_error: str} on failure."""
        if raw is None:
            return {"parse_error": "Gemini returned None (empty response)"}
        raw = str(raw).strip()
        # Strip code fences if Gemini ignores response_mime_type
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {"parse_error": f"JSON decode error: {exc} — raw={raw[:200]!r}"}

        if not isinstance(data, dict):
            return {"parse_error": f"Expected JSON object, got {type(data).__name__}"}

        missing = _REQUIRED_FIELDS - data.keys()
        if missing:
            return {"parse_error": f"Missing required fields: {missing}"}

        # must_not_override_python_guardrails must be exactly True
        ack = data.get("must_not_override_python_guardrails")
        if ack is not True:
            return {"parse_error": f"must_not_override_python_guardrails must be true, got {ack!r}"}

        # confidence must be numeric and in [0.0, 1.0]
        conf = data.get("confidence")
        if not isinstance(conf, (int, float)):
            return {"parse_error": f"confidence must be a number, got {type(conf).__name__}: {conf!r}"}
        if not (0.0 <= float(conf) <= 1.0):
            return {"parse_error": f"confidence {conf} outside valid range [0.0, 1.0]"}

        # conditions_checked must be a list
        if not isinstance(data.get("conditions_checked"), list):
            return {"parse_error": f"conditions_checked must be a list, got {type(data.get('conditions_checked')).__name__}"}

        # risk_notes must be str or list
        rn = data.get("risk_notes")
        if not isinstance(rn, (str, list)):
            return {"parse_error": f"risk_notes must be str or list, got {type(rn).__name__}"}

        # Forbidden fields — Gemini attempting to set execution parameters
        forbidden_found = _FORBIDDEN_FIELDS & data.keys()
        if forbidden_found:
            return {"parse_error": f"Response contains forbidden order-instruction fields: {sorted(forbidden_found)}"}

        return data

    @staticmethod
    def _reject_result(error_msg: str) -> dict:
        return {
            "decision": "REJECT",
            "approved": False,
            "confidence": 0.0,
            "reason": "Auto-rejected: gatekeeper error",
            "risk_notes": error_msg[:200],
            "conditions_checked": [],
            "gatekeeper_error": error_msg,
            "elapsed_seconds": None,
        }
