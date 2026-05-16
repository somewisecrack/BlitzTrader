"""
tools/futures_strategy_engine.py
Pure-Python futures strategy scanner for backtesting.
Extracted from tools/market_data.py — same logic, no broker dependency.
Input: list of candle dicts (with keys: open, high, low, close, volume, time)
Output: list of signal dicts (same shape as live get_strategy_signals())
"""

# ---------------------------------------------------------------------------
# Supported / unsupported strategy registry
# ---------------------------------------------------------------------------

SUPPORTED_STRATEGIES = {
    "VP-01 Counter Bull Trap",
    "VP-02 Counter Bear Trap",
    "VP-05 3EMA Trend",
    "VP-07 Wicks Pullback",
    "VP-14 Morning Star",
    "VP-15 Evening Star",
    "VP-18 M-Pattern Double Top",
    "VP-19 W-Pattern Double Bottom",
    "VP-21 Extreme Candle Reversal",
}

# Which directions each supported strategy can actually emit.
# Used by hypothesis validation to reject proposals that can never produce trades.
STRATEGY_DIRECTIONS: dict[str, frozenset[str]] = {
    "VP-01 Counter Bull Trap":      frozenset({"SELL"}),
    "VP-02 Counter Bear Trap":      frozenset({"BUY"}),
    "VP-05 3EMA Trend":             frozenset({"BUY", "SELL"}),
    "VP-07 Wicks Pullback":         frozenset({"BUY", "SELL"}),
    "VP-14 Morning Star":           frozenset({"BUY"}),
    "VP-15 Evening Star":           frozenset({"SELL"}),
    "VP-18 M-Pattern Double Top":   frozenset({"SELL"}),
    "VP-19 W-Pattern Double Bottom": frozenset({"BUY"}),
    "VP-21 Extreme Candle Reversal": frozenset({"BUY", "SELL"}),
}

# Strategies that require real-time context not available from OHLCV alone.
UNSUPPORTED_STRATEGIES = {
    "VP-10 First Candle Open",       # needs real-time 09:15 IST detection
    "VP-20 CPR Reversal",            # needs prev-day CPR
    "VP-24 Pivot Bounce",            # needs prev-day pivot
    "VP-24 Pivot Rejection",         # needs prev-day pivot
    "Momentum Pinball",              # needs LBR/RSI from daily data
    "80-20 Reversal",                # needs prev-day OHLC
    "ADX Gapper",                    # needs prev-day OHLC
}


# ---------------------------------------------------------------------------
# Internal helpers (pure-Python, no external dependencies)
# ---------------------------------------------------------------------------

def _ema_series(values: list[float], period: int) -> list[float | None]:
    """Compute EMA over a list of floats.  Returns None at positions before
    the first full period."""
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


def _rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    """Compute RSI over a list of close prices using Wilder smoothing.
    Returns None at positions before enough data is available."""
    n = len(values)
    result: list[float | None] = [None] * n
    if n < period + 1:
        return result
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = values[i] - values[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    # Seed with simple average of first period
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100.0 - 100.0 / (1.0 + rs)
    return result


def _adx_series(candles: list[dict], period: int = 14) -> list[float | None]:
    """Compute ADX(period) over a list of candle dicts using Wilder smoothing.
    Returns a list of the same length as *candles*, with None where data is
    insufficient.  Standard Wilder DX method."""
    n = len(candles)
    result: list[float | None] = [None] * n
    if n < period * 2 + 1:
        return result

    # Compute raw DM+, DM-, TR for each bar (index 0 has no previous bar)
    dm_plus: list[float] = []
    dm_minus: list[float] = []
    trs: list[float] = []
    for i in range(1, n):
        h, l, c_prev = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        up   = h - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - l
        dm_plus.append(up   if up > down and up > 0   else 0.0)
        dm_minus.append(down if down > up and down > 0 else 0.0)
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))

    # Wilder smoothing helper
    def _wilder(vals: list[float], p: int) -> list[float]:
        s = sum(vals[:p])
        out = [s]
        for v in vals[p:]:
            s = s - s / p + v
            out.append(s)
        return out

    atr_w  = _wilder(trs,      period)
    dip_w  = _wilder(dm_plus,  period)
    dim_w  = _wilder(dm_minus, period)

    # DX series
    dxs: list[float] = []
    for a, p, m in zip(atr_w, dip_w, dim_w):
        if a == 0:
            dxs.append(0.0)
            continue
        di_plus  = 100.0 * p / a
        di_minus = 100.0 * m / a
        denom = di_plus + di_minus
        dxs.append(100.0 * abs(di_plus - di_minus) / denom if denom else 0.0)

    # ADX = Wilder-smoothed DX; needs at least `period` DX values
    if len(dxs) < period:
        return result

    adx_vals = _wilder(dxs, period)
    # adx_vals[0] corresponds to candle index `period + period` (2*period-th bar, 0-indexed)
    adx_start_candle_idx = 2 * period  # first candle index that has a valid ADX
    for k, adx_val in enumerate(adx_vals):
        candle_idx = adx_start_candle_idx + k
        if candle_idx < n:
            result[candle_idx] = adx_val

    return result


def _candle_stats(c: dict) -> tuple[float, float, float, float]:
    """Returns (body, range, upper_wick, lower_wick)."""
    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"]
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    return body, rng, upper, lower


def _target_for(c: dict, direction: str, stop_loss: float) -> float:
    """2R target from entry = c["close"]."""
    if direction == "BUY":
        return c["close"] + 2 * abs(c["close"] - stop_loss)
    return c["close"] - 2 * abs(stop_loss - c["close"])


def _swing_highs(candles: list[dict]) -> list[tuple[int, dict]]:
    """Return (index, candle) pairs where the candle is a local swing high."""
    highs = []
    for idx in range(1, len(candles) - 1):
        if (candles[idx]["high"] > candles[idx - 1]["high"]
                and candles[idx]["high"] > candles[idx + 1]["high"]):
            highs.append((idx, candles[idx]))
    return highs


def _swing_lows(candles: list[dict]) -> list[tuple[int, dict]]:
    """Return (index, candle) pairs where the candle is a local swing low."""
    lows = []
    for idx in range(1, len(candles) - 1):
        if (candles[idx]["low"] < candles[idx - 1]["low"]
                and candles[idx]["low"] < candles[idx + 1]["low"]):
            lows.append((idx, candles[idx]))
    return lows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_candles(symbol: str, interval: str, candles: list[dict]) -> list[dict]:
    """
    Scan a list of candle dicts for strategy signals.

    Args:
        symbol:   Instrument name (e.g. "NIFTY", "BANKNIFTY", "FINNIFTY").
        interval: Timeframe string matching yfinance conventions (e.g. "5m",
                  "3m", "15m").  The scanner normalises this to "3", "5", "15"
                  internally for consistency with live signal output.
        candles:  List of OHLCV dicts with keys:
                      time (Unix epoch int), open, high, low, close, volume.

    Returns:
        List of signal dicts with keys:
            symbol, interval, strategy, direction, entry_reference,
            stop_loss, target, requires_volume_confirmation, reason,
            candle_index.
        ``candle_index`` is the index into *candles* where the signal fired —
        used by the backtester to walk forward from that point.

    Signal freshness: the candle that TRIGGERS a signal must belong to the
    same calendar date (IST) as the last candle in the list.  Historical
    candles from prior sessions are still used for indicator warm-up but
    cannot emit signals.
    """
    import datetime as _dt
    try:
        import pytz as _pytz
        _IST = _pytz.timezone("Asia/Kolkata")
    except ImportError:
        _IST = None

    if len(candles) < 25:
        return []

    # Determine the "current session date" from the last candle.
    def _ist_date(ts: float) -> _dt.date:
        if _IST is not None:
            return _dt.datetime.fromtimestamp(ts, _IST).date()
        return _dt.datetime.utcfromtimestamp(ts + 19800).date()  # +5:30 offset

    session_date = _ist_date(candles[-1]["time"])

    # Normalise interval label (e.g. "5m" → "5", "15m" → "15")
    norm_interval = interval.replace("m", "").replace("min", "").strip()

    closes = [c["close"] for c in candles]
    ranges = [c["high"] - c["low"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]

    e20_all  = _ema_series(closes, 20)
    e50_all  = _ema_series(closes, 50)
    e100_all = _ema_series(closes, 100)
    rsi_all  = _rsi_series(closes, 14)
    adx_all  = _adx_series(candles, 14)

    signals: list[dict] = []
    # Track (strategy, direction, candle_index) to avoid duplicate signals
    # (mirrors live _emitted_signals set but keyed on index instead of time).
    emitted: set[tuple] = set()

    def _add(i: int, c: dict, strategy: str, direction: str, reason: str,
             stop_loss: float | None, target: float | None,
             requires_volume: bool = False) -> None:
        # ── Session freshness gate ────────────────────────────────────────────
        # The trigger candle must belong to the current session date (IST).
        # Historical candles are used for indicator lookbacks but CANNOT emit.
        if _ist_date(c["time"]) != session_date:
            return
        # ─────────────────────────────────────────────────────────────────────
        key = (strategy, direction, i)
        if key in emitted:
            return
        emitted.add(key)
        signals.append({
            "symbol":                      symbol,
            "interval":                    norm_interval,
            "strategy":                    strategy,
            "direction":                   direction,
            "entry_reference":             round(c["close"], 2),
            "stop_loss":                   round(stop_loss, 2) if stop_loss is not None else None,
            "target":                      round(target, 2) if target is not None else None,
            "requires_volume_confirmation": requires_volume,
            "reason":                      reason,
            "candle_index":                i,
        })

    for i in range(1, len(candles)):
        c = candles[i]
        body, rng, upper, lower = _candle_stats(c)
        if rng <= 0:
            continue

        bull = c["close"] > c["open"]
        bear = c["close"] < c["open"]
        e20  = e20_all[i]
        e50  = e50_all[i]
        e100 = e100_all[i]
        rsi14 = rsi_all[i]
        adx14 = adx_all[i]

        # ── VP-05: 3-EMA trend pullback ──────────────────────────────────────
        if e20 and e50 and e100:
            bull_stack = c["close"] > e20 > e50 > e100
            bear_stack = c["close"] < e20 < e50 < e100
            touched_ema20_long = (c["low"] <= e20 <= max(c["open"], c["close"])
                                  and c["close"] > e20)
            touched_ema50_long = (c["low"] <= e50 <= max(c["open"], c["close"])
                                  and c["close"] > e50)
            touched_ema20_short = (min(c["open"], c["close"]) <= e20 <= c["high"]
                                   and c["close"] < e20)
            touched_ema50_short = (min(c["open"], c["close"]) <= e50 <= c["high"]
                                   and c["close"] < e50)

            if bull_stack and lower > body and (touched_ema20_long or touched_ema50_long):
                sl = e50 if touched_ema20_long else e100
                _add(i, c, "VP-05 3EMA Trend", "BUY",
                     "EMA stack bullish and pin bar rejected EMA20/EMA50",
                     sl, c["close"] + 2 * abs(c["close"] - sl))

            # VP-05 SELL: additionally requires RSI14 <= 40 (fail-closed if unavailable)
            if bear_stack and upper > body and (touched_ema20_short or touched_ema50_short):
                if rsi14 is None or rsi14 > 40:
                    pass  # fail closed — do not emit
                else:
                    sl = e50 if touched_ema20_short else e100
                    _add(i, c, "VP-05 3EMA Trend", "SELL",
                         "EMA stack bearish and pin bar rejected EMA20/EMA50",
                         sl, c["close"] - 2 * abs(sl - c["close"]))

        # ── VP-07: wicks pullback in EMA20 direction ─────────────────────────
        # Both BUY and SELL require ADX14 <= 30; fail closed if unavailable.
        if i >= 10 and e20:
            if adx14 is None or adx14 > 30:
                pass  # ADX gate: fail closed for both directions
            else:
                masters = candles[i - 10:i]
                if bull and c["close"] > e20:
                    for m in masters:
                        m_body, _, _, m_lower = _candle_stats(m)
                        if (m["close"] > m["open"] and m_body > 0
                                and m_lower > 2 * m_body
                                and c["close"] > m["close"]):
                            _add(i, c, "VP-07 Wicks Pullback", "BUY",
                                 "Bullish follow-through above lower-wick master candle and EMA20; confirm volume before trading",
                                 m["low"], c["close"] + 2 * (c["close"] - m["low"]),
                                 True)
                            break
                if bear and c["close"] < e20:
                    for m in masters:
                        m_body, _, m_upper, _ = _candle_stats(m)
                        if (m["close"] < m["open"] and m_body > 0
                                and m_upper > 2 * m_body
                                and c["close"] < m["close"]):
                            _add(i, c, "VP-07 Wicks Pullback", "SELL",
                                 "Bearish follow-through below upper-wick master candle and EMA20; confirm volume before trading",
                                 m["high"], c["close"] - 2 * (m["high"] - c["close"]),
                                 True)
                            break

        # ── VP-01/02: counter trap ───────────────────────────────────────────
        if i >= 10 and e20:
            recent = candles[i - 10:i]
            green_bodies = [
                (abs(rc["close"] - rc["open"]), rc)
                for rc in recent if rc["close"] > rc["open"]
            ]
            red_bodies = [
                (abs(rc["close"] - rc["open"]), rc)
                for rc in recent if rc["close"] < rc["open"]
            ]

            # VP-01 Counter Bull Trap (SELL): requires ADX14 <= 30; fail closed if unavailable
            if green_bodies and c["close"] < e20 and bear:
                if adx14 is not None and adx14 <= 30:
                    _, trap = max(green_bodies, key=lambda item: item[0])
                    if c["close"] < trap["close"]:
                        sl = c["high"]
                        _add(i, c, "VP-01 Counter Bull Trap", "SELL",
                             "Price below EMA20; bearish candle closed below largest recent green candle close",
                             sl, _target_for(c, "SELL", sl))

            # VP-02 Counter Bear Trap (BUY) — NIFTY 3m only (no ADX filter change)
            if (symbol.upper() == "NIFTY" and norm_interval == "3"
                    and red_bodies and c["close"] > e20 and bull):
                _, trap = max(red_bodies, key=lambda item: item[0])
                if c["close"] > trap["close"]:
                    sl = c["low"]
                    _add(i, c, "VP-02 Counter Bear Trap", "BUY",
                         "NIFTY 3m only; price above EMA20 and green candle reclaimed largest recent red candle close",
                         sl, _target_for(c, "BUY", sl))

        # ── VP-14 Morning Star / VP-15 Evening Star ──────────────────────────
        # VP-14 Morning Star BUY: disabled from live executable signals.
        # The pattern is still computed and stored for backtesting/research but
        # is NOT emitted here (scan_candles is the live scanner path).
        if i >= 2 and e20:
            c1, c2, c3 = candles[i - 2], candles[i - 1], c
            c1_body, _, _, _ = _candle_stats(c1)
            c2_body, c2_rng, _, _ = _candle_stats(c2)
            c3_body, _, _, _ = _candle_stats(c3)
            c2_small = c2_rng > 0 and c2_body < 0.3 * c2_rng

            # VP-14: intentionally NOT emitted — shadow/non-executable in live path.
            # Pattern detection kept here as a no-op so backtest logic can be
            # re-enabled by removing the `pass` guard.
            if False and (c1["close"] < c1["open"] and c2_small and bull  # noqa: SIM210
                    and c3_body > 0.5 * c1_body and c["close"] > e20):
                sl = min(c1["low"], c2["low"])
                _add(i, c, "VP-14 Morning Star", "BUY",
                     "3-candle Morning Star; use as support confluence, not standalone",
                     sl, _target_for(c, "BUY", sl))

            if (c1["close"] > c1["open"] and c2_small and bear
                    and c3_body > 0.5 * c1_body and c["close"] < e20):
                sl = max(c1["high"], c2["high"])
                _add(i, c, "VP-15 Evening Star", "SELL",
                     "3-candle Evening Star closed below EMA20",
                     sl, _target_for(c, "SELL", sl))

        # ── VP-18 M-Pattern Double Top / VP-19 W-Pattern Double Bottom ───────
        # VP-18 SELL requires ADX14 <= 30; VP-19 BUY requires ADX14 <= 30.
        # Fail closed if ADX is unavailable.
        if i >= 20:
            window = candles[i - 20:i + 1]
            prior = window[:-1]
            highs_found = _swing_highs(prior)
            lows_found = _swing_lows(prior)

            if adx14 is not None and adx14 <= 30:
                for (idx1, h1), (idx2, h2) in zip(highs_found, highs_found[1:]):
                    if abs(h1["high"] - h2["high"]) / max(h1["high"], h2["high"]) <= 0.005:
                        neckline = min(w["low"] for w in prior[idx1:idx2 + 1])
                        if bear and c["close"] < neckline:
                            sl = max(h1["high"], h2["high"])
                            _add(i, c, "VP-18 M-Pattern Double Top", "SELL",
                                 "Two swing highs within 0.5% followed by bearish neckline break",
                                 sl, _target_for(c, "SELL", sl))
                            break

                for (idx1, l1), (idx2, l2) in zip(lows_found, lows_found[1:]):
                    if abs(l1["low"] - l2["low"]) / min(l1["low"], l2["low"]) <= 0.005:
                        neckline = max(w["high"] for w in prior[idx1:idx2 + 1])
                        if bull and c["close"] > neckline:
                            sl = min(l1["low"], l2["low"])
                            _add(i, c, "VP-19 W-Pattern Double Bottom", "BUY",
                                 "Two swing lows within 0.5% followed by bullish neckline break",
                                 sl, _target_for(c, "BUY", sl))
                            break

        # ── VP-21 Extreme Candle Reversal (15m only) ─────────────────────────
        if i >= 21 and norm_interval == "15":
            prev = candles[i - 1]
            prev_range = prev["high"] - prev["low"]
            avg20 = sum(ranges[i - 21:i - 1]) / 20 if i >= 21 else 0

            if (avg20 and prev_range > 2.5 * avg20
                    and prev["close"] < prev["open"]
                    and bull and c["close"] > prev["close"]):
                _add(i, c, "VP-21 Extreme Candle Reversal", "BUY",
                     "Previous 15m bearish candle range > 2.5x average; current bullish candle reclaimed its close",
                     c["low"], _target_for(c, "BUY", c["low"]))

            if (avg20 and prev_range > 2.5 * avg20
                    and prev["close"] > prev["open"]
                    and bear and c["close"] < prev["close"]):
                _add(i, c, "VP-21 Extreme Candle Reversal", "SELL",
                     "Previous 15m bullish candle range > 2.5x average; current bearish candle lost its close",
                     c["high"], _target_for(c, "SELL", c["high"]))

    return signals
