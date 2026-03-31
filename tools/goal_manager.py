"""
tools/goal_manager.py — Session goal tracking for BlitzTrader.

Claude sets its own goals at session start after reading memory and
strategy docs. Goals are displayed at the top of every iteration
context so Claude reasons against its declared intentions, not just
raw market state.

Goals are in-memory only — they reset each session. Lessons derived
from goal success/failure should be saved via update_memory().
"""
import logging

logger = logging.getLogger("BlitzTrader.GoalManager")


class GoalManager:
    """
    Manages Claude's self-directed session goals.
    Set once at startup, referenced every iteration.
    """

    def __init__(self):
        self._goals: list[str] = []

    # ──────────────────────────────────────────────────────────
    #   TOOLS (callable by Claude)
    # ──────────────────────────────────────────────────────────

    def set_session_goals(self, goals=None, session_goals=None, **kwargs) -> dict:
        """
        Set your goals for this trading session.
        Call this during startup after reading memory and strategy docs.
        These goals will appear at the top of every market analysis
        iteration to keep your reasoning grounded.

        :param goals: List of 2-5 specific, actionable session goals
        :returns: {status, goals_set}
        """
        # Accept both 'goals' and 'session_goals' (LLM sometimes uses wrong name)
        raw = goals or session_goals or kwargs.get("goal") or kwargs.get("session_goal")
        if not raw:
            return {"error": "Provide a list of at least one goal."}

        # Accept string input — split into list
        if isinstance(raw, str):
            # Split on numbered patterns like "1." or "- " or newlines
            import re
            parts = re.split(r'\d+\.\s*|\n[-•*]\s*|\n', raw)
            raw = [p.strip() for p in parts if p.strip()]

        if not isinstance(raw, list):
            raw = [str(raw)]

        self._goals = [str(g).strip() for g in raw if str(g).strip()]

        logger.info(f"Session goals set: {self._goals}")
        return {
            "status": "goals set",
            "goals_set": self._goals,
            "count": len(self._goals),
        }

    def get_session_goals(self) -> dict:
        """
        Read your current session goals.

        :returns: {goals, count}
        """
        return {
            "goals": self._goals,
            "count": len(self._goals),
            "formatted": self._format_goals(),
        }

    # ──────────────────────────────────────────────────────────
    #   INTERNAL
    # ──────────────────────────────────────────────────────────

    def _format_goals(self) -> str:
        """Return goals as a formatted string for context injection."""
        if not self._goals:
            return "(No session goals set yet)"
        return "\n".join(f"  {i+1}. {g}" for i, g in enumerate(self._goals))

    def has_goals(self) -> bool:
        return bool(self._goals)
