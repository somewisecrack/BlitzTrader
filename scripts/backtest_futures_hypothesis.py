#!/usr/bin/env python3
"""
scripts/backtest_futures_hypothesis.py
----------------------------------------
Backtests a futures hypothesis using yfinance OHLCV data and real strategy logic.

Downloads OHLCV data, runs the actual price-action strategy scanner
(same logic as live get_strategy_signals), applies the hypothesis filter,
computes baseline vs. filtered performance stats, and writes a compact JSON result.

Usage:
    python3 scripts/backtest_futures_hypothesis.py --hypothesis wiki/hypotheses/HYP-20260509-001.yaml
    python3 scripts/backtest_futures_hypothesis.py --hypothesis HYP-20260509-001.yaml --period 120d --interval 5m
"""

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from tools.futures_hypothesis import (
        load_hypothesis,
        validate_hypothesis,
        write_backtest_result,
    )
except ImportError as e:
    print(
        f"ERROR: Could not import tools.futures_hypothesis: {e}\n"
        "Make sure tools/futures_hypothesis.py exists and is importable.",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from tools.futures_strategy_engine import scan_candles, SUPPORTED_STRATEGIES
except ImportError as e:
    print(
        f"ERROR: Could not import tools.futures_strategy_engine: {e}\n"
        "Make sure tools/futures_strategy_engine.py exists and is importable.",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from tools.futures_filter_loader import _check_condition
except ImportError as e:
    print(
        f"ERROR: Could not import tools.futures_filter_loader: {e}",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FUTURES_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}

TICKER_MAP = {
    "NIFTY":     "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY":  "NIFTY_FIN_SERVICE.NS",
}

MAX_OUTPUT_BYTES = 1_000_000
MAX_HOLD_CANDLES = 20


# ---------------------------------------------------------------------------
# Data conversion
# ---------------------------------------------------------------------------

def df_to_candles(df) -> list[dict]:
    """Convert a yfinance DataFrame to a list of candle dicts."""
    candles = []
    for ts, row in df.iterrows():
        candles.append({
            "time":   int(ts.timestamp()),
            "open":   float(row["Open"]),
            "high":   float(row["High"]),
            "low":    float(row["Low"]),
            "close":  float(row["Close"]),
            "volume": float(row.get("Volume", 0)),
        })
    return candles


# ---------------------------------------------------------------------------
# Indicator helpers (pure-Python — no pandas required)
# ---------------------------------------------------------------------------

def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Simple RSI from a list of closes (last value)."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def _ema_list(values: list[float], period: int) -> list[float | None]:
    """EMA series — returns None at positions before the first full period."""
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    result[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        result[i] = e
    return result


def _compute_adx(candles: list[dict], period: int = 14) -> float | None:
    """Simple ADX from a candle list (last value).
    Uses Wilder's smoothing (EMA with alpha = 1/period)."""
    if len(candles) < period + 1:
        return None

    trs, dm_plus_list, dm_minus_list = [], [], []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        ph = candles[i - 1]["high"]
        pl = candles[i - 1]["low"]
        pc = candles[i - 1]["close"]

        tr = max(h - l, abs(h - pc), abs(l - pc))
        dm_p = max(h - ph, 0) if (h - ph) > (pl - l) else 0
        dm_m = max(pl - l, 0) if (pl - l) > (h - ph) else 0
        trs.append(tr)
        dm_plus_list.append(dm_p)
        dm_minus_list.append(dm_m)

    if len(trs) < period:
        return None

    alpha = 1.0 / period

    def _wilder_smooth(data):
        res: list[float | None] = [None] * len(data)
        e = sum(data[:period]) / period
        res[period - 1] = e
        for i in range(period, len(data)):
            e = data[i] * alpha + e * (1 - alpha)
            res[i] = e
        return res

    atr_s = _wilder_smooth(trs)
    dmp_s = _wilder_smooth(dm_plus_list)
    dmm_s = _wilder_smooth(dm_minus_list)

    dx_list: list[float] = []
    for i in range(period - 1, len(trs)):
        atr_v = atr_s[i]
        dmp_v = dmp_s[i]
        dmm_v = dmm_s[i]
        if not atr_v:
            continue
        di_p = 100 * dmp_v / atr_v
        di_m = 100 * dmm_v / atr_v
        denom = di_p + di_m
        if denom == 0:
            dx_list.append(0.0)
        else:
            dx_list.append(100 * abs(di_p - di_m) / denom)

    if len(dx_list) < period:
        return None

    adx_val = sum(dx_list[-period:]) / period
    return round(adx_val, 2)


def _compute_ema_stack(closes: list[float]) -> tuple[bool, bool]:
    """Return (ema_stacked_bull, ema_stacked_bear) at the last close."""
    e9_s = _ema_list(closes, 9)
    e21_s = _ema_list(closes, 21)
    e50_s = _ema_list(closes, 50)
    i = len(closes) - 1
    e9, e21, e50 = e9_s[i], e21_s[i], e50_s[i]
    if e9 is None or e21 is None or e50 is None:
        return False, False
    bull = e9 > e21 > e50
    bear = e9 < e21 < e50
    return bull, bear


# ---------------------------------------------------------------------------
# VWAP helpers
# ---------------------------------------------------------------------------

def _needs_vwap(block_when: dict) -> bool:
    """True if any filter condition requires VWAP."""
    return "price_above_vwap" in block_when or "price_below_vwap" in block_when


def _compute_vwap_series(candles: list[dict], interval: str) -> "list[float] | None":
    """Compute a per-candle VWAP series.

    Intraday intervals: cumulative (typical_price × volume) / cumulative_volume,
    resetting at each new calendar day.

    Daily intervals: rolling daily VWAP = cumulative (close × volume) /
    cumulative_volume from the first bar to each bar. Using close price and
    volume only — no H/L required.

    Returns None if no meaningful volume data is present (all zeros),
    which signals the caller to fall back to the daily timeframe.
    """
    if not candles:
        return None

    total_vol = sum(c["volume"] for c in candles)
    if total_vol == 0:
        return None

    _DAILY_INTERVALS = {"1d", "5d", "1wk", "1mo"}
    is_daily = interval in _DAILY_INTERVALS

    result: list[float] = []

    if is_daily:
        cum_close_vol = 0.0
        cum_vol = 0.0
        for c in candles:
            cum_close_vol += c["close"] * c["volume"]
            cum_vol += c["volume"]
            result.append(cum_close_vol / cum_vol if cum_vol > 0 else c["close"])
    else:
        from datetime import datetime, timezone as _tz
        cum_tp_vol = 0.0
        cum_vol = 0.0
        current_date = None

        for c in candles:
            bar_date = datetime.fromtimestamp(c["time"], tz=_tz.utc).date()
            if bar_date != current_date:
                cum_tp_vol = 0.0
                cum_vol = 0.0
                current_date = bar_date

            tp = (c["high"] + c["low"] + c["close"]) / 3.0
            cum_tp_vol += tp * c["volume"]
            cum_vol += c["volume"]
            result.append(cum_tp_vol / cum_vol if cum_vol > 0 else tp)

    return result


# ---------------------------------------------------------------------------
# Filter application
# ---------------------------------------------------------------------------

def signal_passes_filter(
    sig: dict,
    candles: list[dict],
    block_when: dict,
    vwap_series: "list[float] | None" = None,
) -> bool:
    """
    Returns True if the signal is NOT blocked by the hypothesis filter.

    Computes RSI14, ADX14, EMA-stack, and (when vwap_series is provided)
    price_above/below_vwap indicators at the signal's candle index and
    evaluates each block_when condition using the same _check_condition()
    function used by the live filter loader.
    """
    if not block_when:
        return True

    idx = sig["candle_index"]
    closes = [c["close"] for c in candles[:idx + 1]]

    rsi_val = _compute_rsi(closes)
    adx_val = _compute_adx(candles[:idx + 1])
    ema_bull, ema_bear = _compute_ema_stack(closes)

    price_above_vwap = None
    price_below_vwap = None
    if vwap_series is not None and idx < len(vwap_series):
        vwap_val = vwap_series[idx]
        close = candles[idx]["close"]
        price_above_vwap = close > vwap_val
        price_below_vwap = close < vwap_val

    indicators = {
        "rsi14":            rsi_val,
        "adx14":            adx_val,
        "ema_stacked_bull": ema_bull,
        "ema_stacked_bear": ema_bear,
        "price_above_vwap": price_above_vwap,
        "price_below_vwap": price_below_vwap,
    }

    for field, threshold in block_when.items():
        try:
            blocked, _ = _check_condition(field, threshold, indicators)
        except Exception:
            blocked = False
        if blocked:
            return False  # signal removed from filtered set

    return True


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate_trade(sig: dict, candles: list[dict], max_hold: int = MAX_HOLD_CANDLES) -> float:
    """
    Walk forward max_hold candles from signal candle.

    BUY:  stop_loss hit if low  <= stop_loss; target hit if high >= target.
    SELL: stop_loss hit if high >= stop_loss; target hit if low  <= target.
    If both hit in the same candle, stop wins (conservative).
    If neither hits within max_hold, exit at close of last candle vs entry.
    Returns P&L in POINTS (not rupees, not fixed +1/-0.5).
    """
    idx = sig["candle_index"]
    direction = sig["direction"]
    entry = sig["entry_reference"]
    sl = sig["stop_loss"]
    target = sig["target"]

    if sl is None or target is None:
        return 0.0

    n = len(candles)
    for j in range(idx + 1, min(idx + 1 + max_hold, n)):
        c = candles[j]
        if direction == "BUY":
            stop_hit = c["low"] <= sl
            target_hit = c["high"] >= target
            if stop_hit and target_hit:
                return sl - entry       # stop wins — loss (negative)
            if stop_hit:
                return sl - entry       # loss
            if target_hit:
                return target - entry   # profit
        elif direction == "SELL":
            stop_hit = c["high"] >= sl
            target_hit = c["low"] <= target
            if stop_hit and target_hit:
                return entry - sl       # stop wins — loss (negative)
            if stop_hit:
                return entry - sl       # loss
            if target_hit:
                return entry - target   # profit

    # Max hold expired — exit at last candle close
    last_idx = min(idx + max_hold, n - 1)
    last_close = candles[last_idx]["close"]
    if direction == "BUY":
        return last_close - entry
    else:
        return entry - last_close


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def compute_stats(pnl_list: list[float]) -> dict:
    """Compute trading stats from a list of per-trade P&L values (in points)."""
    if not pnl_list:
        return {
            "trades":               0,
            "win_rate":             0,
            "net_pnl_points":       0,
            "profit_factor":        None,
            "max_drawdown_points":  0,
            "avg_trade":            0,
        }

    wins   = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p <= 0]
    net    = sum(pnl_list)
    pf     = (sum(wins) / abs(sum(losses))
              if losses and sum(wins) > 0 else None)

    cumulative: list[float] = []
    running = 0.0
    for p in pnl_list:
        running += p
        cumulative.append(running)

    peak = 0.0
    max_dd = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd

    return {
        "trades":               len(pnl_list),
        "win_rate":             round(len(wins) / len(pnl_list), 4),
        "net_pnl_points":       round(net, 2),
        "profit_factor":        round(pf, 4) if pf is not None else None,
        "max_drawdown_points":  round(max_dd, 2),
        "avg_trade":            round(net / len(pnl_list), 2),
    }


# ---------------------------------------------------------------------------
# Promotion decision
# ---------------------------------------------------------------------------

def evaluate_promotion(
    baseline: dict,
    filtered: dict,
    min_baseline_trades: int,
    min_filtered_trades: int,
) -> dict:
    """Evaluate whether the filter meets promotion thresholds."""
    reasons: list[str] = []

    if baseline["trades"] < min_baseline_trades:
        reasons.append(
            f"Baseline trades {baseline['trades']} < minimum {min_baseline_trades}."
        )

    if filtered["trades"] < min_filtered_trades:
        reasons.append(
            f"Filtered trades {filtered['trades']} < minimum {min_filtered_trades}."
        )

    pf_base = baseline.get("profit_factor")
    pf_filt = filtered.get("profit_factor")
    pf_ok = False
    if pf_base is not None and pf_filt is not None:
        pf_ok = pf_filt >= pf_base * 1.05
        if not pf_ok:
            pct = ((pf_filt / pf_base) - 1) * 100 if pf_base != 0 else 0
            reasons.append(
                f"Profit factor improvement {pct:.1f}% < required 5% "
                f"(baseline={pf_base:.3f}, filtered={pf_filt:.3f})."
            )
    elif pf_base is None and pf_filt is None:
        pf_ok = True   # no losses in either — treat as ok
    elif pf_filt is None and pf_base is not None:
        pf_ok = True   # filtered set has no losses — perfect
    else:
        pf_ok = True

    dd_base = baseline.get("max_drawdown_points", 0)
    dd_filt = filtered.get("max_drawdown_points", 0)
    dd_ok = dd_filt <= dd_base * 1.05
    if not dd_ok:
        reasons.append(
            f"Filtered drawdown {dd_filt:.2f} > 105% of baseline {dd_base:.2f}."
        )

    pnl_ok = (
        filtered["net_pnl_points"] > baseline["net_pnl_points"]
        or filtered["net_pnl_points"] > 0
    )
    if not pnl_ok:
        reasons.append(
            f"Filtered net P&L {filtered['net_pnl_points']:.2f} not better than "
            f"baseline {baseline['net_pnl_points']:.2f} and not positive."
        )

    sufficient_samples = (
        baseline["trades"] >= min_baseline_trades
        and filtered["trades"] >= min_filtered_trades
    )

    promote = (
        len(reasons) == 0
        and pf_ok
        and dd_ok
        and pnl_ok
        and sufficient_samples
    )

    if promote:
        pf_pct = 0.0
        if pf_base and pf_filt:
            pf_pct = ((pf_filt / pf_base) - 1) * 100
        dd_pct = 0.0
        if dd_base > 0:
            dd_pct = (1 - dd_filt / dd_base) * 100
        reason = (
            f"Profit factor improved {pf_pct:.1f}% with acceptable trade count"
            + (f" and {dd_pct:.1f}% drawdown reduction." if dd_pct > 0 else ".")
        )
    else:
        reason = " ".join(reasons) if reasons else "Promotion thresholds not met."

    return {"promote": promote, "reason": reason}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _write_result(result: dict, wiki_dir: Path, hyp_id: str) -> None:
    """Write backtest result JSON to wiki/backtest_results/."""
    output_dir = wiki_dir / "backtest_results"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{hyp_id}.json"

    try:
        write_backtest_result(result, output_path)
    except Exception:
        content = json.dumps(result, indent=2, ensure_ascii=False)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_OUTPUT_BYTES:
            print(
                f"WARNING: Result exceeds 1 MB ({len(encoded)} bytes). Writing compact version.",
                file=sys.stderr,
            )
            result_compact = {k: v for k, v in result.items() if k != "raw_data"}
            content = json.dumps(result_compact, separators=(",", ":"), ensure_ascii=False)
        output_path.write_text(content, encoding="utf-8")

    print(f"[backtest_futures_hypothesis] Written: {output_path}")

    size = output_path.stat().st_size
    if size > MAX_OUTPUT_BYTES:
        print(
            f"WARNING: Output file {output_path} is {size} bytes (> 1 MB).",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# yfinance hard limits: 5m/15m/30m → max 60d; 1h → max 730d; 1d+ → unlimited
_INTERVAL_DEFAULT_PERIOD: dict[str, str] = {
    "1m":  "7d",
    "2m":  "59d",
    "5m":  "59d",
    "15m": "59d",
    "30m": "59d",
    "1h":  "1y",
    "90m": "59d",
    "1d":  "2y",
    "5d":  "5y",
    "1wk": "10y",
    "1mo": "10y",
}


def _default_period_for_interval(interval: str) -> str:
    return _INTERVAL_DEFAULT_PERIOD.get(interval, "59d")


def _check_data_sufficiency(
    ticker: str, interval: str, block_when: dict, yf
) -> tuple[str, str, str]:
    """Pre-flight check: probe 5 days of data to determine whether the
    requested interval has what the filter needs.

    Returns (effective_interval, effective_period, note) where:
      - effective_interval: interval to use for the full download
      - effective_period:   period to use for the full download
      - note:               human-readable explanation of any adjustment made
    """
    # Default: use what was requested
    effective_interval = interval
    effective_period = _default_period_for_interval(interval)
    note = ""

    # Only need to probe when the filter requires volume-dependent indicators
    if not _needs_vwap(block_when):
        return effective_interval, effective_period, note

    # Probe: download a small slice to check volume availability
    try:
        probe = yf.download(
            ticker, period="5d", interval=interval,
            auto_adjust=True, progress=False,
        )
        if hasattr(probe.columns, "levels"):
            probe.columns = [c[0] if isinstance(c, tuple) else c for c in probe.columns]
    except Exception:
        probe = None

    has_volume = (
        probe is not None
        and len(probe) > 0
        and "Volume" in probe.columns
        and int(probe["Volume"].sum()) > 0
    )

    if has_volume:
        note = f"Volume confirmed at {interval} — VWAP will be computed intraday."
    else:
        effective_interval = "1d"
        effective_period = "5y"
        note = (
            f"No volume at {interval} — switching to 1d/5y for rolling daily VWAP "
            f"(fractal equivalence)."
        )

    return effective_interval, effective_period, note


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BlitzTrader futures hypothesis backtester"
    )
    parser.add_argument("--hypothesis", required=True,
                        help="Path to hypothesis YAML/JSON")
    parser.add_argument("--period", default=None,
                        help="yfinance period (default: auto-selected based on --interval)")
    parser.add_argument("--interval", default="5m",
                        help="yfinance interval (default: 5m)")
    parser.add_argument("--wiki-dir", default=None,
                        help="Wiki directory (default: {repo_root}/wiki)")
    parser.add_argument("--min-baseline-trades", type=int, default=20,
                        help="Minimum baseline trades required (default: 20)")
    parser.add_argument("--min-filtered-trades", type=int, default=10,
                        help="Minimum filtered trades required (default: 10)")
    args = parser.parse_args()

    period = args.period if args.period else _default_period_for_interval(args.interval)

    wiki_dir = (
        Path(args.wiki_dir).expanduser().resolve()
        if args.wiki_dir
        else _REPO_ROOT / "wiki"
    )
    hyp_path = Path(args.hypothesis).expanduser().resolve()

    if not hyp_path.exists():
        print(f"ERROR: Hypothesis file not found: {hyp_path}", file=sys.stderr)
        sys.exit(1)

    # ── Load and validate hypothesis ─────────────────────────────────────────
    print(f"[backtest_futures_hypothesis] Loading: {hyp_path}")
    hypothesis = load_hypothesis(hyp_path)

    ok, reason = validate_hypothesis(hypothesis)
    if not ok:
        print(f"ERROR: Hypothesis validation failed: {reason}", file=sys.stderr)
        sys.exit(1)

    hyp_id   = hypothesis.get("id", hyp_path.stem)
    symbol   = hypothesis.get("symbol", "").upper()
    strategy = hypothesis.get("strategy", "")
    block_when = hypothesis.get("filter", {}).get("block_when", {})

    if symbol not in FUTURES_SYMBOLS:
        print(f"ERROR: Symbol '{symbol}' not in {FUTURES_SYMBOLS}", file=sys.stderr)
        sys.exit(1)

    ticker = TICKER_MAP[symbol]
    print(f"  Hypothesis: {hyp_id} | Symbol: {symbol} | Ticker: {ticker}")
    print(f"  Strategy:   {strategy}")
    print(f"  Requested:  {args.interval} / {_default_period_for_interval(args.interval)}")
    print(f"  Filter:     {block_when}")

    # ── Check strategy support ────────────────────────────────────────────────
    if strategy not in SUPPORTED_STRATEGIES:
        reason_str = f"Strategy '{strategy}' is not supported for backtesting"
        print(f"WARNING: {reason_str}")
        result = {
            "hypothesis_id":      hyp_id,
            "status":             "unavailable",
            "symbol":             symbol,
            "ticker":             ticker,
            "strategy":           strategy,
            "period":             period,
            "interval":           args.interval,
            "reason":             reason_str,
            "promotion_decision": {
                "promote": False,
                "reason":  "unsupported strategy",
            },
        }
        _write_result(result, wiki_dir, hyp_id)
        sys.exit(0)

    # ── Data sufficiency check (pre-flight) ───────────────────────────────────
    try:
        import yfinance as yf
    except ImportError:
        print("ERROR: yfinance not installed. Run: pip install yfinance", file=sys.stderr)
        sys.exit(1)

    print(f"[backtest_futures_hypothesis] Checking data sufficiency for {ticker} at {args.interval}...")
    effective_interval, period, sufficiency_note = _check_data_sufficiency(
        ticker, args.interval, block_when, yf
    )
    if sufficiency_note:
        print(f"  {sufficiency_note}")
    if effective_interval != args.interval:
        print(f"  Interval adjusted: {args.interval} → {effective_interval} | Period: {period}")

    # ── Download OHLCV data ───────────────────────────────────────────────────
    print(f"[backtest_futures_hypothesis] Downloading {ticker} ({effective_interval}, {period})...")
    try:
        df = yf.download(
            ticker,
            period=period,
            interval=effective_interval,
            auto_adjust=True,
            progress=False,
        )
    except Exception as exc:
        print(f"ERROR: yfinance download failed for {ticker}: {exc}", file=sys.stderr)
        result = {
            "hypothesis_id":      hyp_id,
            "status":             "unavailable",
            "symbol":             symbol,
            "ticker":             ticker,
            "strategy":           strategy,
            "period":             period,
            "interval":           effective_interval,
            "reason":             str(exc),
            "promotion_decision": {"promote": False, "reason": "data unavailable"},
        }
        _write_result(result, wiki_dir, hyp_id)
        sys.exit(0)

    if df is None or len(df) == 0:
        print(f"WARNING: No data returned for {ticker}. Marking unavailable.")
        result = {
            "hypothesis_id":      hyp_id,
            "status":             "unavailable",
            "symbol":             symbol,
            "ticker":             ticker,
            "strategy":           strategy,
            "period":             period,
            "interval":           effective_interval,
            "reason":             f"No OHLCV data returned for ticker {ticker}",
            "promotion_decision": {"promote": False, "reason": "data unavailable"},
        }
        _write_result(result, wiki_dir, hyp_id)
        sys.exit(0)

    # Flatten MultiIndex columns (yfinance sometimes returns them)
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    print(f"  Downloaded {len(df)} rows.")

    # ── Convert to candle list ────────────────────────────────────────────────
    candles = df_to_candles(df)
    if not candles:
        print("WARNING: candle list is empty after conversion. Marking unavailable.")
        result = {
            "hypothesis_id":      hyp_id,
            "status":             "unavailable",
            "symbol":             symbol,
            "ticker":             ticker,
            "strategy":           strategy,
            "period":             period,
            "interval":           effective_interval,
            "reason":             "Empty candle list after DataFrame conversion",
            "promotion_decision": {"promote": False, "reason": "data unavailable"},
        }
        _write_result(result, wiki_dir, hyp_id)
        sys.exit(0)

    # ── Compute VWAP series if needed ─────────────────────────────────────────
    vwap_series = None
    if _needs_vwap(block_when):
        vwap_series = _compute_vwap_series(candles, effective_interval)
        if vwap_series is not None:
            print(f"  VWAP series computed ({len(vwap_series)} values).")
        else:
            print("WARNING: VWAP series could not be computed even after interval adjustment.")

    # ── Generate baseline signals ─────────────────────────────────────────────
    print(f"[backtest_futures_hypothesis] Scanning candles for {strategy} signals...")
    all_signals = scan_candles(symbol, effective_interval, candles)
    # Keep only signals for the hypothesis strategy
    baseline_signals = [s for s in all_signals if s["strategy"] == strategy]

    # Optionally restrict to the hypothesis direction if specified
    direction_filter = hypothesis.get("direction", "").upper()
    if direction_filter in ("BUY", "SELL"):
        baseline_signals = [s for s in baseline_signals
                            if s["direction"] == direction_filter]
        print(f"  After direction filter ({direction_filter}): {len(baseline_signals)} signals")
    else:
        print(f"  Total strategy signals: {len(baseline_signals)}")

    if not baseline_signals:
        print("WARNING: No signals found for this strategy in the downloaded data.")

    # ── Simulate trades ───────────────────────────────────────────────────────
    print("[backtest_futures_hypothesis] Simulating trades...")
    baseline_pnl: list[float] = []
    filtered_pnl: list[float] = []

    for sig in baseline_signals:
        pnl = simulate_trade(sig, candles)
        baseline_pnl.append(pnl)

        if signal_passes_filter(sig, candles, block_when, vwap_series):
            filtered_pnl.append(pnl)

    baseline_stats = compute_stats(baseline_pnl)
    filtered_stats = compute_stats(filtered_pnl)
    filtered_stats["skipped_trades"] = baseline_stats["trades"] - filtered_stats["trades"]

    print(
        f"  Baseline: {baseline_stats['trades']} trades, "
        f"win_rate={baseline_stats['win_rate']:.2%}, "
        f"net_pnl={baseline_stats['net_pnl_points']:.2f}pts"
    )
    print(
        f"  Filtered: {filtered_stats['trades']} trades, "
        f"win_rate={filtered_stats['win_rate']:.2%}, "
        f"net_pnl={filtered_stats['net_pnl_points']:.2f}pts, "
        f"skipped={filtered_stats['skipped_trades']}"
    )

    # ── Evaluate promotion ────────────────────────────────────────────────────
    promotion_decision = evaluate_promotion(
        baseline_stats,
        filtered_stats,
        args.min_baseline_trades,
        args.min_filtered_trades,
    )

    status = "passed" if promotion_decision["promote"] else "failed"

    result = {
        "hypothesis_id":      hyp_id,
        "status":             status,
        "symbol":             symbol,
        "ticker":             ticker,
        "strategy":           strategy,
        "period":             period,
        "interval":           effective_interval,
        "baseline":           baseline_stats,
        "filtered":           filtered_stats,
        "promotion_decision": promotion_decision,
    }

    _write_result(result, wiki_dir, hyp_id)
    print(
        f"[backtest_futures_hypothesis] Result: {status.upper()} — "
        f"{promotion_decision['reason']}"
    )


if __name__ == "__main__":
    main()
