"""
futures_filter_loader.py — Load active promoted filters and apply them to live signals.

Scope: futures only (NIFTY, BANKNIFTY, FINNIFTY).
Filters are Python-driven post-backtest guardrails.
LLM/Gemini never touches live trade decisions.
"""

import json
import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger("BlitzTrader.FuturesFilterLoader")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FUTURES_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}

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
    """Load a JSON or YAML file and return a dict.

    JSON is tried first (no dependency required).  YAML is attempted as a
    fallback only when PyYAML is installed.
    """
    text = path.read_text(encoding="utf-8")
    # JSON first (no dependency required)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # YAML fallback (only if PyYAML is installed)
    yaml = _try_import_yaml()
    if yaml is not None:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    raise ValueError(f"Cannot parse {path} as JSON or YAML")


# ---------------------------------------------------------------------------
# Filter validation (lightweight — for loader use only)
# ---------------------------------------------------------------------------

def _is_valid_filter(flt: dict) -> bool:
    """Quick structural check before accepting a promoted filter."""
    if not isinstance(flt, dict):
        return False
    if flt.get("scope") != "futures":
        return False
    symbol = flt.get("symbol", "")
    if not isinstance(symbol, str) or symbol.upper() not in FUTURES_SYMBOLS:
        return False
    if not flt.get("id"):
        return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_active_filters(wiki_dir: Union[str, Path]) -> list[dict]:
    """Load all active promoted filters from wiki_dir/promoted_filters/.

    Args:
        wiki_dir: Root wiki directory path.

    Returns:
        List of valid active filter dicts. Returns [] if the directory does not
        exist or contains no valid active filters. Never raises.
    """
    wiki_dir = Path(wiki_dir)
    filters_dir = wiki_dir / "promoted_filters"

    if not filters_dir.exists():
        logger.debug("promoted_filters directory does not exist at %s — returning empty list", filters_dir)
        return []

    active_filters: list[dict] = []

    for fpath in sorted(filters_dir.iterdir()):
        if fpath.name.startswith("."):
            # skip .gitkeep and hidden files
            continue
        if fpath.suffix.lower() not in (".yaml", ".yml", ".json"):
            logger.debug("Skipping non-YAML/JSON file: %s", fpath)
            continue

        try:
            data = _load_file(fpath)
        except Exception as exc:
            logger.warning("Failed to parse promoted filter file %s: %s", fpath, exc)
            continue

        if data.get("status") != "active":
            logger.debug("Skipping non-active filter in %s (status=%r)", fpath.name, data.get("status"))
            continue

        symbol = data.get("symbol", "")
        if not isinstance(symbol, str) or symbol.upper() not in FUTURES_SYMBOLS:
            logger.warning(
                "Skipping filter %s — symbol %r is not a valid futures symbol (%s)",
                fpath.name,
                symbol,
                sorted(FUTURES_SYMBOLS),
            )
            continue

        if not _is_valid_filter(data):
            logger.warning("Skipping invalid filter structure in %s", fpath.name)
            continue

        # Normalise symbol to uppercase for consistent matching later
        data["symbol"] = symbol.upper()
        active_filters.append(data)
        logger.debug("Loaded active filter %s from %s", data.get("id"), fpath.name)

    logger.info("Loaded %d active promoted futures filter(s) from %s", len(active_filters), filters_dir)
    return active_filters


def apply_promoted_filters(
    signal: dict,
    indicators: dict,
    filters: list[dict],
) -> tuple[bool, str]:
    """Apply promoted filters to a live signal.

    Args:
        signal: Signal dict with at least: symbol, strategy, direction.
        indicators: Indicator dict with keys such as rsi14, adx14,
                    ema_stacked_bull, ema_stacked_bear, price_above_vwap.
                    Missing or None values mean "data not available — skip that condition".
        filters: List of active filter dicts (as returned by load_active_filters).

    Returns:
        (True, "") if the signal is allowed by all filters.
        (False, reason_string) if any filter blocks the signal.
    """
    signal_symbol = (signal.get("symbol") or "").upper()
    signal_strategy = signal.get("strategy") or ""
    signal_direction = (signal.get("direction") or "").upper()

    for flt in filters:
        filter_id = flt.get("id", "<unknown>")
        filter_symbol = (flt.get("symbol") or "").upper()

        # -- symbol match --
        if filter_symbol != signal_symbol:
            continue

        # -- strategy match (optional) --
        filter_strategy = flt.get("strategy")
        if filter_strategy and filter_strategy != signal_strategy:
            continue

        # -- direction match (optional) --
        filter_direction = (flt.get("direction") or "").upper()
        if filter_direction and filter_direction != signal_direction:
            continue

        # -- evaluate block_when conditions --
        block_when = (flt.get("filter") or {}).get("block_when")
        if not block_when:
            continue

        blocked, block_reason = _evaluate_block_when(block_when, indicators)
        if blocked:
            reason = f"Rejected by promoted futures filter {filter_id}: {block_reason}"
            logger.info("Signal for %s/%s blocked — %s", signal_symbol, signal_strategy, reason)
            return False, reason

    return True, ""


def _evaluate_block_when(block_when: dict, indicators: dict) -> tuple[bool, str]:
    """Evaluate block_when conditions against indicators.

    Returns (True, human_readable_reason) if any condition is satisfied (i.e. signal should be blocked).
    Returns (False, "") if no condition blocks.
    """
    reasons: list[str] = []

    for field, threshold in block_when.items():
        if field not in SUPPORTED_FILTER_FIELDS:
            logger.warning("Unknown filter field %r — skipping (will not block)", field)
            continue

        try:
            blocked, msg = _check_condition(field, threshold, indicators)
        except Exception as exc:
            logger.warning("Error evaluating filter condition %r=%r: %s — skipping", field, threshold, exc)
            continue

        if blocked:
            reasons.append(msg)

    if reasons:
        return True, "; ".join(reasons)
    return False, ""


def _check_condition(field: str, threshold, indicators: dict) -> tuple[bool, str]:
    """Evaluate a single block_when condition.

    Returns (True, message) if the signal should be blocked by this condition.
    Returns (False, "") otherwise (including when indicator data is missing/None).
    """
    # -- numeric comparisons --
    if field == "rsi14_lt":
        val = indicators.get("rsi14")
        if val is None:
            return False, ""
        if val < threshold:
            return True, f"rsi14 ({val:.2f}) < {threshold}"
        return False, ""

    if field == "rsi14_gt":
        val = indicators.get("rsi14")
        if val is None:
            return False, ""
        if val > threshold:
            return True, f"rsi14 ({val:.2f}) > {threshold}"
        return False, ""

    if field == "adx14_lt":
        val = indicators.get("adx14")
        if val is None:
            return False, ""
        if val < threshold:
            return True, f"adx14 ({val:.2f}) < {threshold}"
        return False, ""

    if field == "adx14_gt":
        val = indicators.get("adx14")
        if val is None:
            return False, ""
        if val > threshold:
            return True, f"adx14 ({val:.2f}) > {threshold}"
        return False, ""

    # -- boolean EMA stack conditions --
    if field == "ema_stacked_bull":
        val = indicators.get("ema_stacked_bull")
        if val is None:
            return False, ""
        if val == threshold:
            return True, f"ema_stacked_bull is {val} (matches block threshold {threshold})"
        return False, ""

    if field == "ema_stacked_bear":
        val = indicators.get("ema_stacked_bear")
        if val is None:
            return False, ""
        if val == threshold:
            return True, f"ema_stacked_bear is {val} (matches block threshold {threshold})"
        return False, ""

    # -- VWAP conditions --
    if field == "price_below_vwap":
        # Block when price IS below VWAP (i.e. price_above_vwap is False) and threshold is True
        price_above_vwap = indicators.get("price_above_vwap")
        if price_above_vwap is None:
            return False, ""
        if threshold is True and price_above_vwap is False:
            return True, "price is below VWAP (price_above_vwap=False)"
        return False, ""

    if field == "price_above_vwap":
        # Block when price IS above VWAP (i.e. price_above_vwap is True) and threshold is True
        price_above_vwap = indicators.get("price_above_vwap")
        if price_above_vwap is None:
            return False, ""
        if threshold is True and price_above_vwap is True:
            return True, "price is above VWAP (price_above_vwap=True)"
        return False, ""

    # Should not reach here — caller screens for unsupported fields
    logger.warning("Unhandled filter field %r in _check_condition — skipping", field)
    return False, ""
