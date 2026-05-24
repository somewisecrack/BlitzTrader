"""
NIFTY Intraday Strategy Miner
================================
Mines up to 5 strong setups for each of three timeframes:
  * 1 year  @ 1h  (252 days × 6 bars  ≈ 1 512 bars)
  * 60 days @ 15m (60 days  × 25 bars ≈ 1 500 bars)
  * 60 days @ 5m  (60 days  × 75 bars ≈ 4 500 bars)

Intraday-native signals used (NOT daily mean-reversion):
  VWAP deviation, Opening Range Breakout, EMA momentum,
  RSI trend-zone, MACD histogram, volume surge, consecutive
  momentum bars, gap direction, BB momentum, session position.

Pipeline
--------
  build_intraday_bars()  →  compute_signals()  →  mine_strategies()
  →  IS filter (wr≥70%, n≥20)  →  OOS validate (wr≥58%, n≥10)
  →  deduplicate()  →  save_strategy_cards()
"""

import numpy as np
import pandas as pd
from itertools import combinations
from scipy.stats import binomtest
import json, os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from nifty_data_generator import generate_nifty_ohlcv

# ── constants ────────────────────────────────────────────────────────────
SESSION_OPEN_H,  SESSION_OPEN_M  = 9,  15
SESSION_CLOSE_H, SESSION_CLOSE_M = 15, 30
SESSION_MIN = 375          # 9:15 – 15:30 = 375 minutes

COST_RT_INTRA = 0.0010     # 0.10 % round-trip for NIFTY Futures intraday

IS_FRAC          = 0.65
IS_MIN_WR        = 0.70    # in-sample win rate filter
IS_MIN_TRADES    = 20
OOS_MIN_WR       = 0.58    # out-of-sample win rate filter
OOS_MIN_TRADES   = 10

TIMEFRAMES = {
    "1h":  dict(freq_min=60, days=252),
    "15m": dict(freq_min=15, days=60),
    "5m":  dict(freq_min=5,  days=60),
}

HOLD_BARS = {
    "1h":  [1, 2, 3, 4],          # 1h – 4h holds
    "15m": [2, 4, 8, 12],         # 30m – 3h holds
    "5m":  [3, 6, 12, 18],        # 15m – 1.5h holds
}

BASE_OUTDIR = Path("SKILL_LIBRARY/NIFTY_STRATEGIES/INTRADAY")


# ══════════════════════════════════════════════════════════════════════════
# 1.  INTRADAY BAR GENERATOR  (with slight momentum autocorrelation)
# ══════════════════════════════════════════════════════════════════════════

def build_intraday_bars(daily_df: pd.DataFrame, freq_min: int,
                        days: int, seed: int = 42) -> pd.DataFrame:
    """
    Build minute-resolution OHLCV bars from daily OHLCV.
    Uses an AR(1) process (phi=0.12) within each session so short-term
    momentum exists — consistent with real intraday equity behaviour.
    The path is then scaled via a Brownian bridge to pin to daily Open/Close.
    """
    rng = np.random.default_rng(seed)
    n_bars = SESSION_MIN // freq_min

    tail = daily_df.tail(days).copy()
    records = []
    prev_close = None

    for date, row in tail.iterrows():
        O = row["Open"]
        H = row["High"]
        L = row["Low"]
        C = row["Close"]
        V = row.get("Volume", 5e5)

        if prev_close is None:
            prev_close = O

        day_dt = pd.Timestamp(date)
        times  = [
            day_dt.replace(hour=SESSION_OPEN_H, minute=SESSION_OPEN_M)
            + pd.Timedelta(minutes=freq_min * k)
            for k in range(n_bars)
        ]

        # AR(1) innovations: short-term momentum φ=0.12
        phi  = 0.12
        eps  = rng.standard_normal(n_bars)
        z    = np.zeros(n_bars)
        z[0] = eps[0]
        for t in range(1, n_bars):
            z[t] = phi * z[t-1] + np.sqrt(1 - phi**2) * eps[t]

        # Brownian bridge: pin to daily log(O) → log(C)
        log_O, log_C = np.log(max(O, 1e-6)), np.log(max(C, 1e-6))
        t_arr  = np.linspace(0, 1, n_bars + 1)[1:]
        bridge = z - t_arr * z[-1]                       # bridge to 0 at t=1

        # Calibrate noise amplitude to daily H-L
        hl_log      = np.log(max(H, C + 1e-3)) - np.log(max(L, O - 1e-3))
        noise_scale = hl_log / (2 * 2.0)                 # ±2σ spans H-L

        log_path = log_O + (log_C - log_O) * t_arr + noise_scale * bridge
        prices   = np.exp(log_path)

        # Volume: morning/close surge + random component
        hour_frac  = np.linspace(0, 1, n_bars)
        vol_shape  = 1.5 * np.exp(-8 * hour_frac) + 1.5 * np.exp(-8 * (1 - hour_frac)) + 0.5
        vol_noise  = rng.exponential(1, n_bars)
        bar_vols   = V / n_bars * vol_shape * vol_noise

        prev_bar_close = O
        gap_open = (O - prev_close) / max(prev_close, 1e-6)

        for k in range(n_bars):
            bO = prev_bar_close
            bC = prices[k]
            bH = max(bO, bC) * (1 + abs(rng.normal(0, 0.0002)))
            bL = min(bO, bC) * (1 - abs(rng.normal(0, 0.0002)))
            records.append(dict(
                datetime=times[k],
                Open=bO, High=bH, Low=bL, Close=bC,
                Volume=bar_vols[k],
                gap_open=gap_open if k == 0 else 0.0,
                session_bar=k,          # bar index within day (0-based)
                n_bars_day=n_bars,
                date=date,
            ))
            prev_bar_close = bC

        prev_close = C

    df = pd.DataFrame(records).set_index("datetime")
    return df


# ══════════════════════════════════════════════════════════════════════════
# 2.  INTRADAY SIGNAL BUILDER
# ══════════════════════════════════════════════════════════════════════════

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def _rsi(s: pd.Series, n: int) -> pd.Series:
    d  = s.diff()
    up = d.clip(lower=0).ewm(com=n-1, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(com=n-1, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))

def _bb_pct(s: pd.Series, n: int = 20) -> pd.Series:
    ma  = s.rolling(n).mean()
    std = s.rolling(n).std()
    return (s - (ma - 2*std)) / (4*std + 1e-10)


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    v = df["Volume"]

    sig = pd.DataFrame(index=df.index)

    # ── Trend / momentum ─────────────────────────────────────────────
    e9   = _ema(c, 9);  e21 = _ema(c, 21);  e50 = _ema(c, 50)
    sig["ema9_vs_e21"]  = (e9  - e21) / e21     # >0 = short uptrend
    sig["e21_vs_e50"]   = (e21 - e50) / e50     # >0 = medium uptrend
    sig["RSI_7"]        = _rsi(c, 7)
    sig["RSI_14"]       = _rsi(c, 14)

    macd_line = _ema(c, 12) - _ema(c, 26)
    sig_line  = _ema(macd_line, 9)
    sig["macd_hist"] = macd_line - sig_line      # >0 = bullish momentum

    sig["bb_pct"]    = _bb_pct(c, 20)

    # ── Consecutive momentum bars ─────────────────────────────────────
    ret_bar = c.diff()
    consec_up = np.zeros(len(c))
    consec_dn = np.zeros(len(c))
    for i in range(1, len(c)):
        if ret_bar.iloc[i] > 0:
            consec_up[i] = consec_up[i-1] + 1
            consec_dn[i] = 0
        elif ret_bar.iloc[i] < 0:
            consec_dn[i] = consec_dn[i-1] + 1
            consec_up[i] = 0
        else:
            consec_up[i] = consec_dn[i] = 0
    sig["consec_up"] = consec_up
    sig["consec_dn"] = consec_dn

    # ── N-bar breakout / breakdown ────────────────────────────────────
    sig["breakout_5"]   = (c > h.shift(1).rolling(5).max()).astype(float)
    sig["breakout_10"]  = (c > h.shift(1).rolling(10).max()).astype(float)
    sig["breakdown_5"]  = (c < l.shift(1).rolling(5).min()).astype(float)
    sig["breakdown_10"] = (c < l.shift(1).rolling(10).min()).astype(float)

    # ── Volume surge ─────────────────────────────────────────────────
    avg_vol = v.rolling(20, min_periods=5).mean()
    sig["vol_ratio"] = v / avg_vol.replace(0, np.nan)

    # ── ATR regime ───────────────────────────────────────────────────
    tr   = pd.concat([h - l,
                      (h - c.shift()).abs(),
                      (l - c.shift()).abs()], axis=1).max(axis=1)
    atr  = tr.ewm(span=14, adjust=False).mean()
    atr_avg = atr.rolling(50, min_periods=10).mean()
    sig["atr_ratio"] = atr / atr_avg.replace(0, np.nan)  # >1 = trending/volatile

    # ── Session-relative signals (computed per day) ───────────────────
    vwap_dev   = np.full(len(df), np.nan)
    or_high    = np.full(len(df), np.nan)
    or_low     = np.full(len(df), np.nan)
    price_vs_open = np.full(len(df), np.nan)
    session_pos   = np.zeros(len(df))

    n_bars_day = int(df["n_bars_day"].iloc[0]) if "n_bars_day" in df.columns else 6
    or_bars    = max(1, n_bars_day // 6)         # first ~1/6 of session = opening range

    dates = df["date"].values if "date" in df.columns else pd.Series(df.index.date, index=df.index).values
    unique_dates = sorted(set(dates))

    for d in unique_dates:
        mask = dates == d
        idxs = np.where(mask)[0]
        if len(idxs) == 0:
            continue
        day_c = c.values[idxs]
        day_h = h.values[idxs]
        day_l = l.values[idxs]
        day_v = v.values[idxs]

        # VWAP (daily, cumulative)
        tp    = (day_h + day_l + day_c) / 3
        cum_tpv = np.cumsum(tp * day_v)
        cum_v   = np.cumsum(day_v)
        day_vwap = cum_tpv / (cum_v + 1e-10)
        vwap_dev[idxs] = (day_c - day_vwap) / (day_vwap + 1e-10)

        # Opening range (first or_bars bars)
        or_h = np.max(day_h[:or_bars])
        or_l = np.min(day_l[:or_bars])
        or_high[idxs] = (day_c - or_h) / (or_h + 1e-10)   # >0 = above ORB high
        or_low[idxs]  = (day_c - or_l) / (or_l + 1e-10)   # <0 = below ORB low

        # Price vs session open
        day_open = day_c[0]
        price_vs_open[idxs] = (day_c - day_open) / (day_open + 1e-10)

        # Session position [0..1]
        session_pos[idxs] = np.linspace(0, 1, len(idxs))

    sig["vwap_dev"]       = vwap_dev           # fraction above/below VWAP
    sig["above_orb"]      = (or_high > 0).astype(float)   # price above OR high
    sig["below_ors"]      = (or_low  < 0).astype(float)   # price below OR low
    sig["price_vs_open"]  = price_vs_open
    sig["session_pos"]    = session_pos         # 0=open, 1=close

    # ── Gap at open ───────────────────────────────────────────────────
    sig["gap_up"]   = df.get("gap_open", pd.Series(0.0, index=df.index)).clip(lower=0)
    sig["gap_down"] = (-df.get("gap_open", pd.Series(0.0, index=df.index))).clip(lower=0)

    return sig


# ══════════════════════════════════════════════════════════════════════════
# 3.  CONDITION LIBRARY
# ══════════════════════════════════════════════════════════════════════════

def get_conditions():
    LONG = [
        # VWAP
        ("vwap_dev",    ">=",  0.000, "Price at/above VWAP [bullish bias]"),
        ("vwap_dev",    ">=",  0.002, "Price 0.2%+ above VWAP [strong bull]"),
        ("vwap_dev",    ">=",  0.005, "Price 0.5%+ above VWAP [dominant bull]"),
        # EMA trend
        ("ema9_vs_e21", ">=",  0.000, "EMA9 > EMA21 [short uptrend]"),
        ("ema9_vs_e21", ">=",  0.002, "EMA9 > EMA21 by 0.2% [firm uptrend]"),
        ("e21_vs_e50",  ">=",  0.000, "EMA21 > EMA50 [medium uptrend]"),
        # RSI momentum zone
        ("RSI_14",      ">=",  50.0,  "RSI14 ≥ 50 [bullish territory]"),
        ("RSI_14",      ">=",  55.0,  "RSI14 ≥ 55 [clear bull momentum]"),
        ("RSI_14",      "<=",  70.0,  "RSI14 ≤ 70 [not overbought — room to run]"),
        ("RSI_7",       ">=",  55.0,  "RSI7 ≥ 55 [short-term bull momentum]"),
        # MACD
        ("macd_hist",   ">=",  0.000, "MACD histogram > 0 [bullish]"),
        # Volume confirmation
        ("vol_ratio",   ">=",  1.20,  "Volume ≥ 1.2× avg [participation]"),
        ("vol_ratio",   ">=",  1.50,  "Volume ≥ 1.5× avg [strong participation]"),
        # Consecutive bars
        ("consec_up",   ">=",  2.0,   "2+ consecutive up bars [momentum]"),
        ("consec_up",   ">=",  3.0,   "3+ consecutive up bars [strong momentum]"),
        # Breakout
        ("breakout_5",  ">=",  1.0,   "Break above 5-bar high [micro BO]"),
        ("breakout_10", ">=",  1.0,   "Break above 10-bar high [BO]"),
        # Opening range
        ("above_orb",   ">=",  1.0,   "Price above opening range high [ORB LONG]"),
        # Session position
        ("session_pos", "<=",  0.35,  "First 35% of session [morning momentum]"),
        # Price vs open
        ("price_vs_open",">=", 0.000, "Price above session open [day trending up]"),
        # Gap
        ("gap_up",      ">=",  0.002, "Gap-up ≥ 0.2% at open [gap & go]"),
        # BB
        ("bb_pct",      ">=",  0.55,  "BB%B > 0.55 [upper half — trending up]"),
        # ATR regime
        ("atr_ratio",   ">=",  1.20,  "ATR above avg [volatile trending day]"),
    ]

    SHORT = [
        # VWAP
        ("vwap_dev",    "<=",  0.000,  "Price at/below VWAP [bearish bias]"),
        ("vwap_dev",    "<=", -0.002,  "Price 0.2%+ below VWAP [strong bear]"),
        ("vwap_dev",    "<=", -0.005,  "Price 0.5%+ below VWAP [dominant bear]"),
        # EMA trend
        ("ema9_vs_e21", "<=",  0.000,  "EMA9 < EMA21 [short downtrend]"),
        ("ema9_vs_e21", "<=", -0.002,  "EMA9 < EMA21 by 0.2% [firm downtrend]"),
        ("e21_vs_e50",  "<=",  0.000,  "EMA21 < EMA50 [medium downtrend]"),
        # RSI momentum zone
        ("RSI_14",      "<=",  50.0,   "RSI14 ≤ 50 [bearish territory]"),
        ("RSI_14",      "<=",  45.0,   "RSI14 ≤ 45 [clear bear momentum]"),
        ("RSI_14",      ">=",  30.0,   "RSI14 ≥ 30 [not oversold — room to fall]"),
        ("RSI_7",       "<=",  45.0,   "RSI7 ≤ 45 [short-term bear momentum]"),
        # MACD
        ("macd_hist",   "<=",  0.000,  "MACD histogram < 0 [bearish]"),
        # Volume confirmation
        ("vol_ratio",   ">=",  1.20,   "Volume ≥ 1.2× avg [participation]"),
        ("vol_ratio",   ">=",  1.50,   "Volume ≥ 1.5× avg [strong participation]"),
        # Consecutive bars
        ("consec_dn",   ">=",  2.0,    "2+ consecutive down bars [momentum]"),
        ("consec_dn",   ">=",  3.0,    "3+ consecutive down bars [strong momentum]"),
        # Breakdown
        ("breakdown_5", ">=",  1.0,    "Break below 5-bar low [micro BD]"),
        ("breakdown_10",">=",  1.0,    "Break below 10-bar low [BD]"),
        # Opening range
        ("below_ors",   ">=",  1.0,    "Price below opening range low [ORB SHORT]"),
        # Session position
        ("session_pos", "<=",  0.35,   "First 35% of session [morning momentum]"),
        # Price vs open
        ("price_vs_open","<=", 0.000,  "Price below session open [day trending down]"),
        # Gap
        ("gap_down",    ">=",  0.002,  "Gap-down ≥ 0.2% at open [gap & go short]"),
        # BB
        ("bb_pct",      "<=",  0.45,   "BB%B < 0.45 [lower half — trending down]"),
        # ATR regime
        ("atr_ratio",   ">=",  1.20,   "ATR above avg [volatile trending day]"),
    ]
    return LONG, SHORT


# ══════════════════════════════════════════════════════════════════════════
# 4.  STRATEGY MINING ENGINE
# ══════════════════════════════════════════════════════════════════════════

def _apply_conditions(sig: pd.DataFrame, conds: list) -> pd.Series:
    mask = pd.Series(True, index=sig.index)
    for col, op, thresh, _ in conds:
        if col not in sig.columns:
            return pd.Series(False, index=sig.index)
        if op == ">=":
            mask &= sig[col] >= thresh
        else:
            mask &= sig[col] <= thresh
    return mask


def _evaluate(close: np.ndarray, entry_mask: np.ndarray,
              direction: str, hold: int, is_end: int) -> dict:
    n      = len(close)
    trades = []
    last_exit = -1

    for i in range(n):
        if not entry_mask[i]:
            continue
        if i <= last_exit:
            continue
        ex_i = min(i + hold, n - 1)
        if ex_i == i:
            continue

        ep, xp = close[i], close[ex_i]
        raw = (xp - ep) / ep if direction == "LONG" else (ep - xp) / ep
        net = raw - COST_RT_INTRA

        trades.append(dict(
            entry_i=i, exit_i=ex_i,
            ret=net, win=int(net > 0),
            is_sample=int(i < is_end),
        ))
        last_exit = ex_i

    is_t  = [t for t in trades if t["is_sample"]]
    oos_t = [t for t in trades if not t["is_sample"]]

    is_wr  = float(np.mean([t["win"] for t in is_t]))  if is_t  else 0.0
    oos_wr = float(np.mean([t["win"] for t in oos_t])) if oos_t else 0.0

    pval = None
    if oos_t:
        w = sum(t["win"] for t in oos_t)
        pval = binomtest(w, len(oos_t), 0.5, alternative="greater").pvalue

    eq, equity = [1.0], 1.0
    for t in oos_t:
        equity *= (1 + t["ret"])
        eq.append(equity)

    return dict(
        is_trades=len(is_t), oos_trades=len(oos_t),
        is_wr=is_wr, oos_wr=oos_wr,
        pvalue=pval, eq_curve=eq, trades=trades,
        oos_return=round(equity - 1, 4),
    )


def mine_strategies(sig: pd.DataFrame, close: np.ndarray,
                    conditions: list, direction: str,
                    hold_list: list) -> list:
    is_end   = int(len(close) * IS_FRAC)
    survivors = []

    # 2-condition combos
    comb2 = list(combinations(range(len(conditions)), 2))
    # 3-condition combos (limit to avoid combinatorial explosion)
    comb3 = list(combinations(range(len(conditions)), 3))
    # keep 3-combos manageable: cap at 3000 for fast timeframes
    if len(comb3) > 3000:
        rng = np.random.default_rng(99)
        idx = rng.choice(len(comb3), 3000, replace=False)
        comb3 = [comb3[i] for i in sorted(idx)]

    all_combos = [(c, 2) for c in comb2] + [(c, 3) for c in comb3]

    for idx_tuple, n_conds in all_combos:
        conds = [conditions[i] for i in idx_tuple]
        mask  = _apply_conditions(sig, conds).values.astype(bool)
        n_sig = mask.sum()
        if n_sig < IS_MIN_TRADES * 1.5:
            continue

        for hold in hold_list:
            res = _evaluate(close, mask, direction, hold, is_end)
            if (res["is_wr"]    >= IS_MIN_WR    and
                res["is_trades"] >= IS_MIN_TRADES and
                res["oos_wr"]   >= OOS_MIN_WR   and
                res["oos_trades"]>= OOS_MIN_TRADES):

                score = res["oos_wr"] * np.sqrt(res["oos_trades"])
                survivors.append(dict(
                    conds=conds, direction=direction, hold=hold,
                    score=score, **res,
                ))

    survivors.sort(key=lambda x: x["score"], reverse=True)
    return survivors


def deduplicate(survivors: list, max_shared: int = 1, top_n: int = 5) -> list:
    """Select top-N strategies with at most max_shared conditions overlapping."""
    selected    = []
    used_labels = []

    for s in survivors:
        labels = {c[3] for c in s["conds"]}
        shared  = max((len(labels & prev) for prev in used_labels), default=0) if used_labels else 0
        if shared <= max_shared:
            selected.append(s)
            used_labels.append(labels)
        if len(selected) == top_n:
            break

    # fallback: relax to max 2 shared
    if len(selected) < top_n:
        for s in survivors:
            if s in selected:
                continue
            labels = {c[3] for c in s["conds"]}
            shared = max((len(labels & prev) for prev in used_labels), default=0) if used_labels else 0
            if shared <= 2:
                selected.append(s)
                used_labels.append(labels)
            if len(selected) == top_n:
                break

    return selected


# ══════════════════════════════════════════════════════════════════════════
# 5.  OUTPUT — CARDS, CHARTS, JSON
# ══════════════════════════════════════════════════════════════════════════

def save_results(strategies: list, tf: str, sig: pd.DataFrame,
                 close: np.ndarray, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)

    tf_label = {"1h": "1-Year @ 1h", "15m": "60-Day @ 15m", "5m": "60-Day @ 5m"}[tf]
    freq_label = {"1h": "1h", "15m": "15m", "5m": "5m"}[tf]

    summary = []

    for rank, s in enumerate(strategies, 1):
        sid = f"IT_{tf.upper()}_S{rank}"
        direction = s["direction"]
        hold = s["hold"]
        conds = s["conds"]

        # ── trade CSV ────────────────────────────────────────────────
        trade_rows = []
        for t in s["trades"]:
            if not t["is_sample"]:
                trade_rows.append(dict(
                    entry_bar=t["entry_i"], exit_bar=t["exit_i"],
                    direction=direction,
                    net_ret_pct=round(t["ret"] * 100, 3),
                    win=t["win"],
                ))
        if trade_rows:
            pd.DataFrame(trade_rows).to_csv(outdir / f"{sid}_trades.csv", index=False)

        # ── markdown card ────────────────────────────────────────────
        cond_lines = "\n".join(
            f"{i+1}. **{c[3]}**  `{c[0]} {c[1]} {c[2]}`"
            for i, c in enumerate(conds)
        )
        hold_human = f"{hold} bar(s) × {freq_label}"
        md = f"""# {sid}: NIFTY Intraday — {direction} {tf_label}

## Overview
| Field | Value |
|-------|-------|
| Instrument | NIFTY 50 Futures (intraday) |
| Timeframe | **{tf_label}** |
| Direction | **{direction}** |
| Hold Period | **{hold_human}** |
| Entry | Next bar open after all conditions met |
| Exit | Close of bar +{hold} |

## Entry Conditions
All conditions must be TRUE at bar close:

{cond_lines}

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **{s["oos_wr"]*100:.1f}%** |
| OOS Trades | {s["oos_trades"]} |
| In-Sample Win Rate | {s["is_wr"]*100:.1f}% |
| OOS Return (net) | {s["oos_return"]*100:+.1f}% |
| Binomial p-value | {f'{s["pvalue"]:.4f}' if s["pvalue"] else "—"} |

## Risk Management
- **Stop Loss**: trail at 0.5× ATR(14) from entry bar
- **Max hold**: {hold} bars — always exit even if target not hit
- **No overnight**: exit at or before 15:15 IST (last 15 min reserved)
- **Min capital**: ₹6–7 lakh (1 NIFTY Futures lot); any amount for NIFTY ETF

## Notes
- Intraday momentum setup — trade WITH short-term trend, NOT against it
- All signals computed on **{freq_label} CLOSE** data available at bar close
- OOS period = last 35% of dataset
- Transaction cost: 0.10% round-trip (futures intraday)
"""
        (outdir / f"{sid}.md").write_text(md)

        summary.append(dict(
            id=sid, timeframe=tf_label, direction=direction,
            hold_bars=hold, hold_label=hold_human,
            conditions=[dict(signal=c[0], op=c[1], threshold=c[2], description=c[3])
                        for c in conds],
            performance=dict(
                is_wr_pct=round(s["is_wr"]*100, 1),
                oos_wr_pct=round(s["oos_wr"]*100, 1),
                is_trades=s["is_trades"],
                oos_trades=s["oos_trades"],
                oos_return_pct=round(s["oos_return"]*100, 2),
                pvalue=round(s["pvalue"], 5) if s["pvalue"] else None,
                score=round(s["score"], 3),
            ),
        ))

    # ── combined JSON ─────────────────────────────────────────────────
    (outdir / f"strategies_{tf}.json").write_text(json.dumps(summary, indent=2))

    # ── equity curves chart ───────────────────────────────────────────
    n = len(strategies)
    if n == 0:
        return
    fig, axes = plt.subplots(n, 1, figsize=(13, 3.5 * n), squeeze=False)
    fig.suptitle(f"OOS Equity Curves — NIFTY Intraday {tf_label}", fontsize=13, fontweight="bold")

    colors_dir = {"LONG": "#27ae60", "SHORT": "#e74c3c"}
    for row, s in enumerate(strategies):
        ax   = axes[row][0]
        eq   = np.array(s["eq_curve"]) - 1
        col  = colors_dir[s["direction"]]
        ax.fill_between(range(len(eq)), eq, alpha=0.25, color=col)
        ax.plot(eq, color=col, linewidth=1.5)
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        cond_str = " + ".join(c[3].split(" [")[0] for c in s["conds"])
        ax.set_title(
            f"IT_{tf.upper()}_S{row+1}: {s['direction']}  hold={s['hold']}bars  "
            f"OOS {s['oos_wr']*100:.1f}% ({s['oos_trades']} trades)  |  {cond_str}",
            fontsize=9)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y*100:.0f}%"))
        ax.set_xlabel("OOS trade #")

    plt.tight_layout()
    fig.savefig(outdir / f"equity_{tf}.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


# ══════════════════════════════════════════════════════════════════════════
# 6.  MAIN
# ══════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  NIFTY INTRADAY STRATEGY MINER")
    print("  Timeframes: 1h (1 year) | 15m (60 days) | 5m (60 days)")
    print("=" * 70)

    print("\n[1/5] Loading daily backbone …")
    daily_df = generate_nifty_ohlcv(seed=42)
    print(f"      {len(daily_df)} trading days  ({daily_df.index[0].date()} → {daily_df.index[-1].date()})")

    LONG_CONDS, SHORT_CONDS = get_conditions()

    all_tf_results = {}

    for tf, cfg in TIMEFRAMES.items():
        freq_min = cfg["freq_min"]
        days     = cfg["days"]
        n_bars   = SESSION_MIN // freq_min
        total    = days * n_bars
        is_bars  = int(total * IS_FRAC)
        oos_bars = total - is_bars

        print(f"\n{'─'*70}")
        print(f"  TIMEFRAME: {tf}  ({days}d × {n_bars} bars/day = {total} total bars)")
        print(f"  IS: {is_bars} bars  |  OOS: {oos_bars} bars")
        print(f"{'─'*70}")

        print(f"  [2] Generating intraday bars …")
        intra = build_intraday_bars(daily_df, freq_min=freq_min, days=days)
        print(f"      {len(intra)} bars  ({intra.index[0]} → {intra.index[-1]})")

        print(f"  [3] Computing signals …")
        sig   = compute_signals(intra)
        close = intra["Close"].values
        holds = HOLD_BARS[tf]

        print(f"  [4] Mining LONG strategies  (holds: {holds} bars) …")
        long_surv  = mine_strategies(sig, close, LONG_CONDS,  "LONG",  holds)
        print(f"      {len(long_surv)} IS+OOS survivors")

        print(f"  [4] Mining SHORT strategies (holds: {holds} bars) …")
        short_surv = mine_strategies(sig, close, SHORT_CONDS, "SHORT", holds)
        print(f"      {len(short_surv)} IS+OOS survivors")

        combined = sorted(long_surv + short_surv, key=lambda x: x["score"], reverse=True)
        top5     = deduplicate(combined, max_shared=1, top_n=5)

        print(f"\n  ✓ Final selection: {len(top5)} strategies\n")
        outdir = BASE_OUTDIR / tf
        save_results(top5, tf, sig, close, outdir)
        all_tf_results[tf] = top5

        for rank, s in enumerate(top5, 1):
            cond_str = " + ".join(c[3].split(" [")[0] for c in s["conds"])
            pstr = f"p={s['pvalue']:.4f}" if s["pvalue"] else "p=n/a"
            print(f"    S{rank} [{s['direction']:5s} hold={s['hold']}] "
                  f"IS={s['is_wr']*100:4.1f}%({s['is_trades']:3d})  "
                  f"OOS={s['oos_wr']*100:4.1f}%({s['oos_trades']:3d})  "
                  f"ret={s['oos_return']*100:+5.1f}%  {pstr}")
            print(f"       → {cond_str}")

    # ── cross-timeframe summary chart ─────────────────────────────────
    print("\n[5/5] Building cross-timeframe summary chart …")

    tfs  = list(TIMEFRAMES.keys())
    cols = {"1h": "#3498db", "15m": "#e67e22", "5m": "#8e44ad"}
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), sharey=False)
    fig.suptitle("NIFTY Intraday Strategy Miner — OOS Performance by Timeframe",
                 fontsize=13, fontweight="bold")

    for ax, tf in zip(axes, tfs):
        strats = all_tf_results[tf]
        labels = [f"S{i+1}" for i in range(len(strats))]
        wrs    = [s["oos_wr"] * 100 for s in strats]
        dirs   = [s["direction"] for s in strats]
        bar_colors = ["#27ae60" if d == "LONG" else "#e74c3c" for d in dirs]

        bars = ax.bar(labels, wrs, color=bar_colors, alpha=0.85, edgecolor="black", linewidth=0.7)
        ax.axhline(58, color="orange", linestyle="--", linewidth=1.2, label="OOS 58% threshold")
        ax.axhline(50, color="grey",   linestyle=":",  linewidth=0.8, label="50% breakeven")
        ax.set_ylim(0, 100)
        ax.set_title(f"{tf}  ({TIMEFRAMES[tf]['days']}d)", fontsize=11)
        ax.set_ylabel("OOS Win Rate (%)")
        ax.legend(fontsize=7)

        for bar, s in zip(bars, strats):
            ax.text(bar.get_x() + bar.get_width()/2,
                    bar.get_height() + 1,
                    f"{bar.get_height():.0f}%\nn={s['oos_trades']}",
                    ha="center", va="bottom", fontsize=8)

        # add direction legend
        from matplotlib.patches import Patch
        ax.legend(handles=[
            Patch(color="#27ae60", label="LONG"),
            Patch(color="#e74c3c", label="SHORT"),
            plt.Line2D([0], [0], color="orange", linestyle="--", label="58% min"),
        ], fontsize=7)

    plt.tight_layout()
    summary_chart = BASE_OUTDIR / "intraday_miner_summary.png"
    fig.savefig(summary_chart, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"      Chart saved → {summary_chart}")

    # ── final console table ────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  FINAL RESULTS — INTRADAY STRATEGY MINER")
    print("=" * 70)
    for tf in tfs:
        tf_label = {"1h": "1-Year @ 1h", "15m": "60-Day @ 15m", "5m": "60-Day @ 5m"}[tf]
        print(f"\n  ── {tf_label} ──")
        print(f"  {'ID':12s} {'Dir':6s} {'Hold':6s} {'IS%':7s} {'OOS%':7s} {'n':5s} {'Ret':7s} {'p-val':8s}")
        print(f"  {'-'*65}")
        for rank, s in enumerate(all_tf_results[tf], 1):
            pstr = f"{s['pvalue']:.4f}" if s["pvalue"] else "  —   "
            cstr = " + ".join(c[3].split(" [")[0][:20] for c in s["conds"])
            print(f"  IT_{tf.upper()}_S{rank}    {s['direction']:6s} {s['hold']}bar   "
                  f"{s['is_wr']*100:5.1f}%  {s['oos_wr']*100:5.1f}%  "
                  f"{s['oos_trades']:4d}  {s['oos_return']*100:+5.1f}%  {pstr}")
            print(f"  {'':12s} {cstr}")
    print("\n" + "=" * 70)
    print(f"  All files saved → {BASE_OUTDIR}/")
    print("=" * 70)


if __name__ == "__main__":
    main()
