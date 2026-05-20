"""
futures_hypothesis.py — Hypothesis and backtest result data models for the BlitzTrader wiki system.

Scope: futures only (NIFTY, BANKNIFTY).
LLM/Gemini is postmarket research only — never a live gatekeeper.
All live trading decisions remain 100% Python-driven.
"""

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Union

logger = logging.getLogger("BlitzTrader.FuturesHypothesis")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FUTURES_SYMBOLS = {"NIFTY", "BANKNIFTY"}

VALID_STATUSES = {"proposed", "backtested", "promoted", "rejected"}

VALID_DIRECTIONS = {"BUY", "SELL"}

try:
    from tools.futures_strategy_engine import STRATEGY_DIRECTIONS as _STRATEGY_DIRECTIONS
except ImportError:
    _STRATEGY_DIRECTIONS: dict = {}

SUPPORTED_FILTER_FIELDS = {
    "rsi14_lt",
    "rsi14_gt",
    "adx14_lt",
    "adx14_gt",
    "ema_stacked_bull",
    "ema_stacked_bear",
    "price_below_vwap",
    "price_above_vwap",
}

# Fields that indicate pairs/equity content — auto-reject if present
PAIRS_FIELDS = {"cointegration", "z_score", "spread", "hedge_ratio", "coint_pvalue"}

# Phrases that indicate LLM live-gatekeeper misuse — auto-reject if found in text
PROHIBITED_PHRASES = {
    "gemini gatekeeper",
    "live approval",
    "live rejection",
    "live gatekeeper",
}

# IST offset
_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> str:
    """Return current time as ISO8601 string in IST."""
    return datetime.now(_IST).isoformat()


# ---------------------------------------------------------------------------
# YAML / JSON helpers
# ---------------------------------------------------------------------------

def _try_import_yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError:
        return None


def _load_file(path: Path) -> dict:
    """Load a YAML or JSON file and return a dict."""
    text = path.read_text(encoding="utf-8")
    yaml = _try_import_yaml()
    if yaml is not None:
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at top level, got {type(data).__name__}")
    return data


def _write_yaml_or_json(data: dict, path: Path) -> None:
    """Write data preferring YAML, falling back to JSON."""
    yaml = _try_import_yaml()
    if yaml is not None:
        text = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        path.write_text(text, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_json(data: dict, path: Path) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Text scanning helpers
# ---------------------------------------------------------------------------

def _contains_prohibited_phrase(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in PROHIBITED_PHRASES)


def _scan_for_prohibited(data: dict) -> bool:
    """Recursively scan dict values for prohibited phrases."""
    for v in data.values():
        if isinstance(v, str) and _contains_prohibited_phrase(v):
            return True
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and _contains_prohibited_phrase(item):
                    return True
        if isinstance(v, dict):
            if _scan_for_prohibited(v):
                return True
    return False


def _has_pairs_fields(data: dict) -> bool:
    """Recursively check if any dict key is a pairs-related field."""
    for k in data.keys():
        if k in PAIRS_FIELDS:
            return True
    for v in data.values():
        if isinstance(v, dict) and _has_pairs_fields(v):
            return True
    return False


# ---------------------------------------------------------------------------
# Hypothesis
# ---------------------------------------------------------------------------

def load_hypothesis(path: Union[str, Path]) -> dict:
    """Load a hypothesis from a YAML or JSON file.

    Args:
        path: Path to the hypothesis file.

    Returns:
        dict containing the hypothesis data.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if the file cannot be parsed.
    """
    path = Path(path)
    return _load_file(path)


def validate_hypothesis(data: dict) -> tuple[bool, str]:
    """Validate a hypothesis dict.

    Returns:
        (True, "") if valid.
        (False, error_message) if invalid.
    """
    # --- scope ---
    if data.get("scope") != "futures":
        return False, f"scope must be 'futures', got: {data.get('scope')!r}"

    # --- symbol ---
    symbol = data.get("symbol", "")
    if not isinstance(symbol, str):
        return False, "symbol must be a string"
    if ".NS" in symbol or ".ns" in symbol:
        return False, f"symbol must not contain '.NS' suffix (got {symbol!r}) — pairs/equity symbols are out of scope"
    if symbol.upper() not in FUTURES_SYMBOLS:
        return False, f"symbol must be one of {sorted(FUTURES_SYMBOLS)}, got: {symbol!r}"

    # --- strategy ---
    strategy = data.get("strategy", "")
    if not isinstance(strategy, str) or not strategy.strip():
        return False, "strategy must be a non-empty string"

    # --- direction (optional) ---
    direction = data.get("direction")
    if direction is not None:
        if not isinstance(direction, str) or direction.upper() not in VALID_DIRECTIONS:
            return False, f"direction must be BUY or SELL (or absent), got: {direction!r}"
        # Check that the strategy actually emits this direction
        allowed = _STRATEGY_DIRECTIONS.get(strategy)
        if allowed is not None and direction.upper() not in allowed:
            return False, (
                f"strategy {strategy!r} only emits {sorted(allowed)} signals; "
                f"direction {direction!r} can never produce trades"
            )

    # --- filter ---
    flt = data.get("filter")
    if flt is not None:
        if not isinstance(flt, dict):
            return False, "filter must be a mapping"
        block_when = flt.get("block_when")
        if block_when is not None:
            if not isinstance(block_when, dict):
                return False, "filter.block_when must be a mapping"
            unsupported = set(block_when.keys()) - SUPPORTED_FILTER_FIELDS
            if unsupported:
                return False, (
                    f"filter.block_when contains unsupported fields: {sorted(unsupported)}. "
                    f"Supported: {sorted(SUPPORTED_FILTER_FIELDS)}"
                )

    # --- status ---
    status = data.get("status", "proposed")
    if status not in VALID_STATUSES:
        return False, f"status must be one of {sorted(VALID_STATUSES)}, got: {status!r}"

    # --- pairs fields rejection ---
    if _has_pairs_fields(data):
        return False, "Hypothesis contains pairs/equity fields (cointegration, z_score, spread, hedge_ratio, coint_pvalue) — pairs trading is out of scope"

    # --- LLM live gatekeeper phrase rejection ---
    if _scan_for_prohibited(data):
        return False, (
            "Hypothesis text contains prohibited phrase(s) implying LLM live-gatekeeper use "
            "(e.g. 'gemini gatekeeper', 'live approval', 'live rejection', 'live gatekeeper'). "
            "LLM is postmarket research only."
        )

    return True, ""


def write_hypothesis(data: dict, path: Union[str, Path]) -> None:
    """Write a hypothesis dict to a YAML (preferred) or JSON file.

    Args:
        data: Hypothesis dict.
        path: Destination file path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml_or_json(data, path)


# ---------------------------------------------------------------------------
# Backtest result
# ---------------------------------------------------------------------------

_BACKTEST_SECTION_FIELDS = {"trades", "net_pnl_points", "profit_factor", "max_drawdown_points"}


def load_backtest_result(path: Union[str, Path]) -> dict:
    """Load a backtest result from a JSON file.

    Args:
        path: Path to the JSON result file.

    Returns:
        dict containing the backtest result.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if the file cannot be parsed or is not a dict.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at top level in backtest result, got {type(data).__name__}")
    return data


def validate_backtest_result(data: dict) -> tuple[bool, str]:
    """Validate a backtest result dict.

    Returns:
        (True, "") if valid.
        (False, error_message) if invalid.
    """
    # --- hypothesis_id ---
    if not data.get("hypothesis_id"):
        return False, "hypothesis_id must be present and non-empty"

    # --- status ---
    status = data.get("status")
    if status not in ("passed", "failed"):
        return False, f"status must be 'passed' or 'failed', got: {status!r}"

    # --- symbol ---
    symbol = data.get("symbol", "")
    if not isinstance(symbol, str) or symbol.upper() not in FUTURES_SYMBOLS:
        return False, f"symbol must be one of {sorted(FUTURES_SYMBOLS)}, got: {symbol!r}"

    # --- baseline section ---
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        return False, "baseline section must be present and be a mapping"
    missing_baseline = _BACKTEST_SECTION_FIELDS - set(baseline.keys())
    if missing_baseline:
        return False, f"baseline section missing fields: {sorted(missing_baseline)}"

    # --- filtered section ---
    filtered = data.get("filtered")
    if not isinstance(filtered, dict):
        return False, "filtered section must be present and be a mapping"
    missing_filtered = _BACKTEST_SECTION_FIELDS - set(filtered.keys())
    if missing_filtered:
        return False, f"filtered section missing fields: {sorted(missing_filtered)}"

    # --- promotion_decision ---
    promo = data.get("promotion_decision")
    if not isinstance(promo, dict):
        return False, "promotion_decision must be present and be a mapping"
    if "promote" not in promo:
        return False, "promotion_decision must contain 'promote' (bool)"
    if not isinstance(promo["promote"], bool):
        return False, f"promotion_decision.promote must be a bool, got: {type(promo['promote']).__name__}"
    if "reason" not in promo:
        return False, "promotion_decision must contain 'reason'"

    return True, ""


def write_backtest_result(data: dict, path: Union[str, Path]) -> None:
    """Write a backtest result dict to a JSON file.

    Args:
        data: Backtest result dict.
        path: Destination file path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(data, path)


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

def _derive_filter_id(hypothesis_id: str) -> str:
    """Derive a FILT-YYYYMMDD-NNN id from a HYP-YYYYMMDD-NNN id."""
    match = re.search(r"(\d{8}-\d+)", hypothesis_id)
    if match:
        return f"FILT-{match.group(1)}"
    # fallback: use current date
    return f"FILT-{datetime.now(_IST).strftime('%Y%m%d')}-001"


def promote_if_passed(hypothesis: dict, result: dict) -> "dict | None":
    """Return a promoted filter dict if the backtest result recommends promotion.

    Args:
        hypothesis: Hypothesis dict (as returned by load_hypothesis).
        result: Backtest result dict (as returned by load_backtest_result).

    Returns:
        A promoted filter dict if promotion_decision.promote is True, else None.
    """
    promo = result.get("promotion_decision", {})
    if not promo.get("promote", False):
        return None

    filter_id = _derive_filter_id(hypothesis.get("id", "HYP-UNKNOWN"))

    promoted = {
        "id": filter_id,
        "source_hypothesis": hypothesis.get("id"),
        "scope": "futures",
        "status": "active",
        "strategy": hypothesis.get("strategy"),
        "symbol": hypothesis.get("symbol"),
        "direction": hypothesis.get("direction"),
        "filter": hypothesis.get("filter", {}),
        "promoted_at": _now_ist(),
    }
    return promoted
