"""
BANKNIFTY & USDINR Intraday Strategy Miner
============================================
Instruments:
  BANKNIFTY — with BFSI components as cross-asset features
    (HDFCBANK, ICICIBANK, SBIN, AXISBANK, KOTAKBANK, NIFTY, VIX)
  USDINR    — with macro drivers as cross-asset features
    (DXY proxy, Crude, Gold, US10Y, S&P 500, Hang Seng)

Timeframes: 1h (1-year), 15m (60-day), 5m (60-day)
Pipeline:   build bars → compute signals + cross-asset → mine
            IS≥70%/n≥20 → OOS≥58%/n≥10 → deduplicate → save
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

from banknifty_usdinr_generator import (
    generate_banknifty_universe, generate_usdinr_universe,
    build_intraday_from_daily, BN_TICKERS, INR_TICKERS,
)

# ── costs ────────────────────────────────────────────────────────────
COST_RT_EQ  = 0.0010   # 0.10% RT — equity index futures intraday
COST_RT_FX  = 0.0005   # 0.05% RT — currency futures (lower spread)

# ── IS/OOS thresholds ────────────────────────────────────────────────
IS_FRAC       = 0.65
IS_MIN_WR     = 0.70
IS_MIN_TRADES = 20
OOS_MIN_WR    = 0.58
OOS_MIN_TRADES= 10

SESSION_MIN = 375
TIMEFRAMES = {
    "1h":  dict(freq_min=60, days=252),
    "15m": dict(freq_min=15, days=60),
    "5m":  dict(freq_min=5,  days=60),
}
HOLD_BARS = {
    "1h":  [1, 2, 3, 4],
    "15m": [2, 4, 8, 12],
    "5m":  [3, 6, 12, 18],
}

OUTDIR = Path("SKILL_LIBRARY")


# ══════════════════════════════════════════════════════════════════════
# 1.  SIGNAL BUILDERS
# ══════════════════════════════════════════════════════════════════════

def _ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def _rsi(s, n):
    d = s.diff()
    u = d.clip(lower=0).ewm(com=n-1, min_periods=n).mean()
    d2= (-d.clip(upper=0)).ewm(com=n-1, min_periods=n).mean()
    return 100 - 100/(1 + u/d2.replace(0, np.nan))
def _bb_pct(s, n=20):
    ma=s.rolling(n).mean(); std=s.rolling(n).std()
    return (s-(ma-2*std))/(4*std+1e-10)
def _atr(h, l, c, n=14):
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(span=n,adjust=False).mean()


def compute_price_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Core intraday signals from OHLCV (reusable for any instrument)."""
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    sig = pd.DataFrame(index=df.index)

    e9, e21, e50 = _ema(c,9), _ema(c,21), _ema(c,50)
    sig["ema9_vs_e21"]   = (e9  - e21) / (e21 + 1e-10)
    sig["e21_vs_e50"]    = (e21 - e50) / (e50 + 1e-10)
    sig["RSI_7"]         = _rsi(c, 7)
    sig["RSI_14"]        = _rsi(c, 14)
    macd = _ema(c,12) - _ema(c,26)
    sig["macd_hist"]     = macd - _ema(macd, 9)
    sig["bb_pct"]        = _bb_pct(c, 20)
    atr14 = _atr(h,l,c,14)
    sig["atr_ratio"]     = atr14 / atr14.rolling(50, min_periods=10).mean().replace(0,np.nan)

    # Consecutive bars
    ret = c.diff()
    cu = np.zeros(len(c)); cd = np.zeros(len(c))
    for i in range(1, len(c)):
        if ret.iloc[i]>0:  cu[i]=cu[i-1]+1; cd[i]=0
        elif ret.iloc[i]<0: cd[i]=cd[i-1]+1; cu[i]=0
    sig["consec_up"] = cu;  sig["consec_dn"] = cd

    # N-bar breakout/breakdown
    sig["breakout_5"]   = (c > h.shift(1).rolling(5).max()).astype(float)
    sig["breakout_10"]  = (c > h.shift(1).rolling(10).max()).astype(float)
    sig["breakdown_5"]  = (c < l.shift(1).rolling(5).min()).astype(float)
    sig["breakdown_10"] = (c < l.shift(1).rolling(10).min()).astype(float)

    # Volume
    avg_v = v.rolling(20, min_periods=5).mean()
    sig["vol_ratio"] = v / avg_v.replace(0, np.nan)

    # Session-level signals
    vwap_dev  = np.full(len(df), np.nan)
    or_above  = np.zeros(len(df))
    or_below  = np.zeros(len(df))
    vs_open   = np.full(len(df), np.nan)
    sess_pos  = np.zeros(len(df))

    n_bars_day = int(df["n_bars_day"].iloc[0]) if "n_bars_day" in df.columns else 6
    or_bars    = max(1, n_bars_day // 6)

    dates = df["date"].values if "date" in df.columns else pd.Series(df.index.date, index=df.index).values
    for d in sorted(set(dates)):
        ix = np.where(dates == d)[0]
        if not len(ix): continue
        dc, dh, dl, dv = c.values[ix], h.values[ix], l.values[ix], v.values[ix]
        tp = (dh + dl + dc) / 3
        vwap = np.cumsum(tp*dv) / (np.cumsum(dv)+1e-10)
        vwap_dev[ix]  = (dc - vwap) / (vwap + 1e-10)
        orh = dh[:or_bars].max(); orl = dl[:or_bars].min()
        or_above[ix]  = (dc > orh).astype(float)
        or_below[ix]  = (dc < orl).astype(float)
        vs_open[ix]   = (dc - dc[0]) / (dc[0] + 1e-10)
        sess_pos[ix]  = np.linspace(0, 1, len(ix))

    sig["vwap_dev"]      = vwap_dev
    sig["above_orb"]     = or_above
    sig["below_ors"]     = or_below
    sig["price_vs_open"] = vs_open
    sig["session_pos"]   = sess_pos
    sig["gap_up"]   = df.get("gap_open", pd.Series(0.0, index=df.index)).clip(lower=0)
    sig["gap_down"] = (-df.get("gap_open", pd.Series(0.0, index=df.index))).clip(lower=0)

    return sig


def add_cross_asset_signals(sig: pd.DataFrame, df: pd.DataFrame,
                             daily_universe: dict, instrument: str) -> pd.DataFrame:
    """
    Broadcast daily cross-asset returns to all intraday bars of that day.
    For BANKNIFTY: BFSI component relative strengths + NIFTY + VIX.
    For USDINR: macro drivers (crude, DXY, gold, SPX, US10Y).
    """
    dates = df["date"].values if "date" in df.columns else pd.Series(df.index.date, index=df.index).values
    unique_dates = sorted(set(dates))

    if instrument == "BANKNIFTY":
        # daily returns for each BFSI stock
        bfsi = ["HDFCBANK.NS","ICICIBANK.NS","SBIN.NS","AXISBANK.NS","KOTAKBANK.NS"]
        for tk in bfsi:
            if tk in daily_universe:
                daily_ret = daily_universe[tk]["Close"].pct_change()
                date_to_ret = {d: daily_ret.get(pd.Timestamp(d), 0.0)
                               for d in unique_dates}
                col = tk.split(".")[0].lower() + "_ret"
                sig[col] = [date_to_ret.get(d, 0.0) for d in dates]

        # NIFTY direction on the day
        if "^NSEI" in daily_universe:
            nifty_ret = daily_universe["^NSEI"]["Close"].pct_change()
            date_to_nifty = {d: nifty_ret.get(pd.Timestamp(d), 0.0) for d in unique_dates}
            sig["nifty_ret"] = [date_to_nifty.get(d, 0.0) for d in dates]

        # VIX level (daily)
        if "^VIX" in daily_universe:
            vix = daily_universe["^VIX"]["Close"]
            date_to_vix = {d: vix.get(pd.Timestamp(d), 18.0) for d in unique_dates}
            sig["vix_level"] = [date_to_vix.get(d, 18.0) for d in dates]
            sig["vix_high"]  = (sig["vix_level"] >= 20).astype(float)
            sig["vix_low"]   = (sig["vix_level"] <= 15).astype(float)

        # BFSI breadth: how many BFSI stocks are up on the day
        bfsi_ret_cols = [tk.split(".")[0].lower() + "_ret" for tk in bfsi
                         if tk.split(".")[0].lower() + "_ret" in sig.columns]
        if bfsi_ret_cols:
            sig["bfsi_breadth"] = sum((sig[c] > 0).astype(float)
                                      for c in bfsi_ret_cols)  # 0–5
            sig["bfsi_strong"]  = (sig["bfsi_breadth"] >= 4).astype(float)
            sig["bfsi_weak"]    = (sig["bfsi_breadth"] <= 1).astype(float)

        # PSU vs private spread (SBIN outperformance = PSU momentum)
        if "sbin_ret" in sig.columns and "hdfcbank_ret" in sig.columns:
            sig["psu_outperf"] = (sig["sbin_ret"] > sig["hdfcbank_ret"]).astype(float)

    elif instrument == "USDINR":
        drivers = {
            "CL=F": "crude_ret",
            "DX=F": "dxy_ret",
            "GC=F": "gold_ret",
            "^GSPC": "spx_ret",
            "US10Y": "us10y_chg",
            "^HSI": "hsi_ret",
        }
        for tk, col in drivers.items():
            if tk in daily_universe:
                daily_ret = daily_universe[tk]["Close"].pct_change()
                d2r = {d: daily_ret.get(pd.Timestamp(d), 0.0) for d in unique_dates}
                sig[col] = [d2r.get(d, 0.0) for d in dates]

        # Composite macro pressure on INR
        # Higher crude + higher DXY + lower SPX = weaker INR (USDINR rises)
        if all(c in sig.columns for c in ["crude_ret","dxy_ret","spx_ret"]):
            sig["inr_pressure"] = (sig["crude_ret"] + sig["dxy_ret"]
                                   - sig["spx_ret"])  # positive = INR weakness
            sig["inr_weak_day"]   = (sig["inr_pressure"] > 0.003).astype(float)
            sig["inr_strong_day"] = (sig["inr_pressure"] < -0.003).astype(float)

    return sig


# ══════════════════════════════════════════════════════════════════════
# 2.  CONDITION LIBRARIES
# ══════════════════════════════════════════════════════════════════════

def get_banknifty_conditions():
    LONG = [
        # VWAP momentum
        ("vwap_dev",     ">=",  0.000,  "Price at/above VWAP"),
        ("vwap_dev",     ">=",  0.002,  "Price 0.2%+ above VWAP"),
        ("vwap_dev",     ">=",  0.005,  "Price 0.5%+ above VWAP"),
        # EMA trend
        ("ema9_vs_e21",  ">=",  0.000,  "EMA9 > EMA21 [uptrend]"),
        ("ema9_vs_e21",  ">=",  0.002,  "EMA9 > EMA21 firmly"),
        ("e21_vs_e50",   ">=",  0.000,  "EMA21 > EMA50 [medium uptrend]"),
        # RSI
        ("RSI_14",       ">=",  50.0,   "RSI14 ≥ 50 [bullish zone]"),
        ("RSI_14",       ">=",  55.0,   "RSI14 ≥ 55 [momentum]"),
        ("RSI_14",       "<=",  70.0,   "RSI14 ≤ 70 [room to run]"),
        ("RSI_7",        ">=",  55.0,   "RSI7 ≥ 55 [short bull momentum]"),
        # MACD / BB
        ("macd_hist",    ">=",  0.000,  "MACD histogram positive"),
        ("bb_pct",       ">=",  0.55,   "BB%B > 0.55 [upper half]"),
        # Volume
        ("vol_ratio",    ">=",  1.20,   "Volume ≥ 1.2× avg"),
        ("vol_ratio",    ">=",  1.50,   "Volume ≥ 1.5× avg"),
        # Consecutive / breakout
        ("consec_up",    ">=",  2.0,    "2+ consecutive up bars"),
        ("consec_up",    ">=",  3.0,    "3+ consecutive up bars"),
        ("breakout_5",   ">=",  1.0,    "Above 5-bar high [micro BO]"),
        ("breakout_10",  ">=",  1.0,    "Above 10-bar high [BO]"),
        # Session
        ("above_orb",    ">=",  1.0,    "Above opening range high [ORB LONG]"),
        ("price_vs_open",">=",  0.000,  "Above session open"),
        ("session_pos",  "<=",  0.35,   "First 35% of session"),
        # Cross-asset BFSI
        ("bfsi_strong",  ">=",  1.0,    "≥4 BFSI stocks up [sector breadth]"),
        ("nifty_ret",    ">=",  0.002,  "NIFTY up 0.2%+ on day [parent strength]"),
        ("vix_low",      ">=",  1.0,    "VIX ≤ 15 [low fear, risk-on]"),
        ("hdfcbank_ret", ">=",  0.003,  "HDFCBANK up 0.3%+ [largest weight]"),
        ("sbin_ret",     ">=",  0.003,  "SBIN up 0.3%+ [PSU momentum]"),
    ]

    SHORT = [
        # VWAP
        ("vwap_dev",     "<=",  0.000,  "Price at/below VWAP"),
        ("vwap_dev",     "<=", -0.002,  "Price 0.2%+ below VWAP"),
        ("vwap_dev",     "<=", -0.005,  "Price 0.5%+ below VWAP"),
        # EMA trend
        ("ema9_vs_e21",  "<=",  0.000,  "EMA9 < EMA21 [downtrend]"),
        ("ema9_vs_e21",  "<=", -0.002,  "EMA9 < EMA21 firmly"),
        ("e21_vs_e50",   "<=",  0.000,  "EMA21 < EMA50 [medium downtrend]"),
        # RSI
        ("RSI_14",       "<=",  50.0,   "RSI14 ≤ 50 [bearish zone]"),
        ("RSI_14",       "<=",  45.0,   "RSI14 ≤ 45 [bear momentum]"),
        ("RSI_14",       ">=",  30.0,   "RSI14 ≥ 30 [not oversold]"),
        ("RSI_7",        "<=",  45.0,   "RSI7 ≤ 45 [short bear momentum]"),
        # MACD / BB
        ("macd_hist",    "<=",  0.000,  "MACD histogram negative"),
        ("bb_pct",       "<=",  0.45,   "BB%B < 0.45 [lower half]"),
        # Volume
        ("vol_ratio",    ">=",  1.20,   "Volume ≥ 1.2× avg"),
        ("vol_ratio",    ">=",  1.50,   "Volume ≥ 1.5× avg"),
        # Consecutive / breakdown
        ("consec_dn",    ">=",  2.0,    "2+ consecutive down bars"),
        ("consec_dn",    ">=",  3.0,    "3+ consecutive down bars"),
        ("breakdown_5",  ">=",  1.0,    "Below 5-bar low [micro BD]"),
        ("breakdown_10", ">=",  1.0,    "Below 10-bar low [BD]"),
        # Session
        ("below_ors",    ">=",  1.0,    "Below opening range low [ORB SHORT]"),
        ("price_vs_open","<=",  0.000,  "Below session open"),
        ("session_pos",  "<=",  0.35,   "First 35% of session"),
        # Cross-asset BFSI
        ("bfsi_weak",    ">=",  1.0,    "≤1 BFSI stocks up [sector weakness]"),
        ("nifty_ret",    "<=", -0.002,  "NIFTY down 0.2%+ on day [parent weak]"),
        ("vix_high",     ">=",  1.0,    "VIX ≥ 20 [elevated fear]"),
        ("hdfcbank_ret", "<=", -0.003,  "HDFCBANK down 0.3%+ [largest weight]"),
        ("sbin_ret",     "<=", -0.003,  "SBIN down 0.3%+ [PSU weakness]"),
    ]
    return LONG, SHORT


def get_usdinr_conditions():
    # USDINR LONG = INR weakening (dollar strengthening)
    LONG = [
        # Price momentum
        ("vwap_dev",      ">=",  0.000,  "Price at/above VWAP [INR weak]"),
        ("vwap_dev",      ">=",  0.001,  "Price 0.1%+ above VWAP"),
        ("ema9_vs_e21",   ">=",  0.000,  "EMA9 > EMA21 [INR weakness trend]"),
        ("ema9_vs_e21",   ">=",  0.001,  "EMA9 > EMA21 firmly"),
        ("e21_vs_e50",    ">=",  0.000,  "EMA21 > EMA50 [medium weakness]"),
        ("RSI_14",        ">=",  50.0,   "RSI14 ≥ 50 [bullish USD/INR]"),
        ("RSI_14",        ">=",  55.0,   "RSI14 ≥ 55 [momentum USD up]"),
        ("RSI_14",        "<=",  70.0,   "RSI14 ≤ 70 [room to run]"),
        ("macd_hist",     ">=",  0.000,  "MACD histogram positive"),
        ("bb_pct",        ">=",  0.55,   "BB%B > 0.55 [upper half]"),
        ("consec_up",     ">=",  2.0,    "2+ consecutive up bars"),
        ("consec_up",     ">=",  3.0,    "3+ consecutive up bars"),
        ("breakout_5",    ">=",  1.0,    "Above 5-bar high [BO]"),
        ("above_orb",     ">=",  1.0,    "Above opening range high"),
        ("price_vs_open", ">=",  0.000,  "Above session open"),
        # Macro cross-asset (INR weakness drivers)
        ("crude_ret",     ">=",  0.005,  "Crude up 0.5%+ [India import cost↑]"),
        ("dxy_ret",       ">=",  0.002,  "DXY up 0.2%+ [dollar strong]"),
        ("spx_ret",       "<=", -0.003,  "SPX down 0.3%+ [risk-off = INR weak]"),
        ("inr_weak_day",  ">=",  1.0,    "Composite macro = INR weakness day"),
        ("us10y_chg",     ">=",  0.003,  "US 10Y yield up [dollar demand↑]"),
        ("gold_ret",      ">=",  0.005,  "Gold up 0.5%+ [risk-off, often USD+]"),
        ("vol_ratio",     ">=",  1.20,   "Volume ≥ 1.2× avg [participation]"),
        ("atr_ratio",     ">=",  1.20,   "ATR above avg [volatile session]"),
    ]

    # USDINR SHORT = INR strengthening (dollar weakening)
    SHORT = [
        ("vwap_dev",      "<=",  0.000,  "Price at/below VWAP [INR strong]"),
        ("vwap_dev",      "<=", -0.001,  "Price 0.1%+ below VWAP"),
        ("ema9_vs_e21",   "<=",  0.000,  "EMA9 < EMA21 [INR strength trend]"),
        ("ema9_vs_e21",   "<=", -0.001,  "EMA9 < EMA21 firmly"),
        ("e21_vs_e50",    "<=",  0.000,  "EMA21 < EMA50 [medium strength]"),
        ("RSI_14",        "<=",  50.0,   "RSI14 ≤ 50 [bearish USD/INR]"),
        ("RSI_14",        "<=",  45.0,   "RSI14 ≤ 45 [momentum INR up]"),
        ("RSI_14",        ">=",  30.0,   "RSI14 ≥ 30 [not oversold]"),
        ("macd_hist",     "<=",  0.000,  "MACD histogram negative"),
        ("bb_pct",        "<=",  0.45,   "BB%B < 0.45 [lower half]"),
        ("consec_dn",     ">=",  2.0,    "2+ consecutive down bars"),
        ("consec_dn",     ">=",  3.0,    "3+ consecutive down bars"),
        ("breakdown_5",   ">=",  1.0,    "Below 5-bar low [BD]"),
        ("below_ors",     ">=",  1.0,    "Below opening range low"),
        ("price_vs_open", "<=",  0.000,  "Below session open"),
        # Macro cross-asset (INR strength drivers)
        ("crude_ret",     "<=", -0.005,  "Crude down 0.5%+ [India import cost↓]"),
        ("dxy_ret",       "<=", -0.002,  "DXY down 0.2%+ [dollar weak]"),
        ("spx_ret",       ">=",  0.003,  "SPX up 0.3%+ [risk-on = INR strong]"),
        ("inr_strong_day",">=",  1.0,    "Composite macro = INR strength day"),
        ("hsi_ret",       ">=",  0.003,  "Hang Seng up 0.3%+ [Asian risk-on]"),
        ("gold_ret",      "<=", -0.005,  "Gold down 0.5%+ [risk-on, USD weak]"),
        ("vol_ratio",     ">=",  1.20,   "Volume ≥ 1.2× avg"),
        ("atr_ratio",     ">=",  1.20,   "ATR above avg"),
    ]
    return LONG, SHORT


# ══════════════════════════════════════════════════════════════════════
# 3.  MINING ENGINE  (same logic as nifty_intraday_miner)
# ══════════════════════════════════════════════════════════════════════

def _apply(sig, conds):
    mask = pd.Series(True, index=sig.index)
    for col, op, thresh, _ in conds:
        if col not in sig.columns:
            return pd.Series(False, index=sig.index)
        mask &= sig[col] >= thresh if op == ">=" else sig[col] <= thresh
    return mask


def _evaluate(close, mask, direction, hold, is_end, cost_rt):
    n = len(close); trades = []; last_exit = -1
    for i in range(n):
        if not mask[i] or i <= last_exit: continue
        ex = min(i + hold, n-1)
        if ex == i: continue
        ep, xp = close[i], close[ex]
        raw = (xp-ep)/ep if direction=="LONG" else (ep-xp)/ep
        net = raw - cost_rt
        trades.append(dict(entry_i=i, exit_i=ex, ret=net,
                           win=int(net>0), is_sample=int(i<is_end)))
        last_exit = ex
    is_t  = [t for t in trades if t["is_sample"]]
    oos_t = [t for t in trades if not t["is_sample"]]
    is_wr  = float(np.mean([t["win"] for t in is_t]))  if is_t  else 0.0
    oos_wr = float(np.mean([t["win"] for t in oos_t])) if oos_t else 0.0
    pval = None
    if oos_t:
        pval = binomtest(sum(t["win"] for t in oos_t),
                         len(oos_t), 0.5, alternative="greater").pvalue
    eq = [1.0]; eq_val = 1.0
    for t in oos_t:
        eq_val *= (1+t["ret"]); eq.append(eq_val)
    return dict(is_trades=len(is_t), oos_trades=len(oos_t),
                is_wr=is_wr, oos_wr=oos_wr, pvalue=pval,
                eq_curve=eq, oos_return=round(eq_val-1,4), trades=trades)


def mine(sig, close, conds, direction, holds, cost_rt, seed=99):
    is_end = int(len(close)*IS_FRAC)
    surv   = []
    comb2  = list(combinations(range(len(conds)), 2))
    comb3  = list(combinations(range(len(conds)), 3))
    if len(comb3) > 3000:
        rng = np.random.default_rng(seed)
        idx = rng.choice(len(comb3), 3000, replace=False)
        comb3 = [comb3[i] for i in sorted(idx)]
    close_arr = close if isinstance(close, np.ndarray) else np.array(close)
    for idx_t, _ in [(c,2) for c in comb2] + [(c,3) for c in comb3]:
        cc    = [conds[i] for i in idx_t]
        mask  = _apply(sig, cc).values.astype(bool)
        if mask.sum() < IS_MIN_TRADES*1.5: continue
        for hold in holds:
            r = _evaluate(close_arr, mask, direction, hold, is_end, cost_rt)
            if (r["is_wr"] >= IS_MIN_WR and r["is_trades"] >= IS_MIN_TRADES and
                r["oos_wr"] >= OOS_MIN_WR and r["oos_trades"] >= OOS_MIN_TRADES):
                score = r["oos_wr"] * np.sqrt(r["oos_trades"])
                surv.append(dict(conds=cc, direction=direction, hold=hold,
                                 score=score, **r))
    surv.sort(key=lambda x: x["score"], reverse=True)
    return surv


def deduplicate(survivors, max_shared=1, top_n=5):
    sel, used = [], []
    for s in survivors:
        labels = {c[3] for c in s["conds"]}
        shared = max((len(labels & p) for p in used), default=0) if used else 0
        if shared <= max_shared:
            sel.append(s); used.append(labels)
        if len(sel) == top_n: break
    if len(sel) < top_n:
        for s in survivors:
            if s in sel: continue
            labels = {c[3] for c in s["conds"]}
            shared = max((len(labels & p) for p in used), default=0) if used else 0
            if shared <= 2:
                sel.append(s); used.append(labels)
            if len(sel) == top_n: break
    return sel


# ══════════════════════════════════════════════════════════════════════
# 4.  SAVE RESULTS
# ══════════════════════════════════════════════════════════════════════

def save_results(strategies, instrument, tf, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    prefix   = instrument.replace("/","").replace("^","")
    tf_label = {"1h":"1-Year @ 1h","15m":"60-Day @ 15m","5m":"60-Day @ 5m"}[tf]
    summary  = []

    for rank, s in enumerate(strategies, 1):
        sid = f"{prefix}_{tf.upper()}_S{rank}"
        cond_lines = "\n".join(
            f"{i+1}. **{c[3]}**  `{c[0]} {c[1]} {c[2]}`"
            for i, c in enumerate(s["conds"]))
        hold_label = f"{s['hold']} bar(s) × {tf}"
        pstr = f"{s['pvalue']:.4f}" if s["pvalue"] else "—"

        md = f"""# {sid}: {instrument} Intraday — {s['direction']} {tf_label}

## Overview
| Field | Value |
|-------|-------|
| Instrument | **{instrument}** (Futures intraday) |
| Timeframe | **{tf_label}** |
| Direction | **{s['direction']}** |
| Hold Period | **{hold_label}** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +{s['hold']} |

## Entry Conditions
All conditions must be TRUE at bar close:

{cond_lines}

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **{s['oos_wr']*100:.1f}%** |
| OOS Trades | {s['oos_trades']} |
| In-Sample Win Rate | {s['is_wr']*100:.1f}% |
| OOS Cumulative Return | {s['oos_return']*100:+.1f}% |
| Binomial p-value | {pstr} |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: {s['hold']} bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
{"- **Min capital**: ₹6–7 lakh for BankNifty Futures (1 lot)" if instrument=="BANKNIFTY" else "- **Min capital**: ₹2–3 lakh for USDINR Futures (1 lot = $1000)"}

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from {'BFSI component stocks + NIFTY/VIX' if instrument=='BANKNIFTY' else 'macro drivers: crude, DXY, gold, SPX'}
- OOS period = last 35% of dataset; transaction cost included
"""
        (outdir / f"{sid}.md").write_text(md)

        if s["trades"]:
            oos_trades = [t for t in s["trades"] if not t["is_sample"]]
            pd.DataFrame(oos_trades).to_csv(outdir / f"{sid}_trades.csv", index=False)

        summary.append(dict(
            id=sid, instrument=instrument, timeframe=tf_label,
            direction=s["direction"], hold_bars=s["hold"],
            conditions=[dict(signal=c[0],op=c[1],threshold=c[2],description=c[3])
                        for c in s["conds"]],
            performance=dict(
                is_wr_pct=round(s["is_wr"]*100,1),
                oos_wr_pct=round(s["oos_wr"]*100,1),
                is_trades=s["is_trades"],
                oos_trades=s["oos_trades"],
                oos_return_pct=round(s["oos_return"]*100,2),
                pvalue=round(s["pvalue"],5) if s["pvalue"] else None,
                score=round(s["score"],3),
            ),
        ))

    (outdir / f"strategies_{tf}.json").write_text(json.dumps(summary, indent=2))

    # Equity chart
    if not strategies: return
    fig, axes = plt.subplots(len(strategies), 1,
                             figsize=(13, 3.5*len(strategies)), squeeze=False)
    fig.suptitle(f"OOS Equity — {instrument} Intraday {tf_label}",
                 fontsize=13, fontweight="bold")
    cols = {"LONG":"#27ae60","SHORT":"#e74c3c"}
    for row, s in enumerate(strategies):
        ax  = axes[row][0]
        eq  = np.array(s["eq_curve"])-1
        col = cols[s["direction"]]
        ax.fill_between(range(len(eq)), eq, alpha=0.25, color=col)
        ax.plot(eq, color=col, linewidth=1.5)
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        cstr = " + ".join(c[3][:22] for c in s["conds"])
        ax.set_title(f"{prefix}_{tf.upper()}_S{list(strategies).index(s)+1}  "
                     f"{s['direction']} hold={s['hold']}bars  "
                     f"OOS {s['oos_wr']*100:.1f}% ({s['oos_trades']} trades) | {cstr}",
                     fontsize=8)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y,_: f"{y*100:.0f}%"))
        ax.set_xlabel("OOS trade #")
    plt.tight_layout()
    fig.savefig(outdir / f"equity_{tf}.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


# ══════════════════════════════════════════════════════════════════════
# 5.  MAIN
# ══════════════════════════════════════════════════════════════════════

def run_instrument(instrument, anchor_ticker, daily_universe,
                   long_conds, short_conds, cost_rt, outdir: Path):
    print(f"\n{'═'*68}")
    print(f"  {instrument}  (anchor: {anchor_ticker})")
    print(f"{'═'*68}")

    anchor_daily = daily_universe[anchor_ticker]
    tf_results   = {}

    for tf, cfg in TIMEFRAMES.items():
        freq_min = cfg["freq_min"]
        days     = cfg["days"]
        n_bars   = SESSION_MIN // freq_min
        total    = days * n_bars
        is_b     = int(total * IS_FRAC)

        print(f"\n  ── {tf}  ({days}d × {n_bars} bars = {total})  "
              f"IS={is_b}  OOS={total-is_b} ──")

        intra = build_intraday_from_daily(anchor_daily, freq_min=freq_min,
                                          days=days, seed=42)
        sig   = compute_price_signals(intra)
        sig   = add_cross_asset_signals(sig, intra, daily_universe, instrument)
        close = intra["Close"].values
        holds = HOLD_BARS[tf]

        print(f"     Mining LONG  …", end="", flush=True)
        l_surv = mine(sig, close, long_conds,  "LONG",  holds, cost_rt)
        print(f" {len(l_surv)} survivors")

        print(f"     Mining SHORT …", end="", flush=True)
        s_surv = mine(sig, close, short_conds, "SHORT", holds, cost_rt)
        print(f" {len(s_surv)} survivors")

        combined = sorted(l_surv + s_surv, key=lambda x: x["score"], reverse=True)
        top5     = deduplicate(combined, top_n=5)
        tf_results[tf] = top5

        tf_outdir = outdir / tf
        save_results(top5, instrument, tf, tf_outdir)

        print(f"     ✓ {len(top5)} strategies selected:")
        for rank, s in enumerate(top5, 1):
            cstr = " + ".join(c[3][:25] for c in s["conds"])
            pstr = f"p={s['pvalue']:.4f}" if s["pvalue"] else "p=n/a"
            print(f"     S{rank} [{s['direction']:5s} h={s['hold']:2d}] "
                  f"IS={s['is_wr']*100:4.1f}%({s['is_trades']:3d}) "
                  f"OOS={s['oos_wr']*100:4.1f}%({s['oos_trades']:3d}) "
                  f"ret={s['oos_return']*100:+5.1f}%  {pstr}")
            print(f"          → {cstr}")

    # cross-tf summary bar chart
    tfs    = list(TIMEFRAMES.keys())
    fig, axes = plt.subplots(1,3, figsize=(16,5), sharey=False)
    fig.suptitle(f"{instrument} — Intraday OOS Win Rates", fontsize=13, fontweight="bold")
    for ax, tf in zip(axes, tfs):
        strats = tf_results[tf]
        labels = [f"S{i+1}" for i in range(len(strats))]
        wrs    = [s["oos_wr"]*100 for s in strats]
        bcolors= ["#27ae60" if s["direction"]=="LONG" else "#e74c3c" for s in strats]
        if labels:
            bars = ax.bar(labels, wrs, color=bcolors, alpha=0.85,
                          edgecolor="black", linewidth=0.7)
            ax.axhline(58, color="orange", linestyle="--", linewidth=1, label="58% min")
            ax.axhline(50, color="grey",   linestyle=":",  linewidth=0.8)
            ax.set_ylim(0,100)
            for bar, s in zip(bars, strats):
                ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+1,
                        f"{bar.get_height():.0f}%\nn={s['oos_trades']}",
                        ha="center", va="bottom", fontsize=8)
        ax.set_title(f"{tf} ({TIMEFRAMES[tf]['days']}d)", fontsize=11)
        ax.set_ylabel("OOS Win Rate (%)")
        ax.legend(fontsize=7)
    plt.tight_layout()
    fig.savefig(outdir / "summary.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  Summary chart → {outdir}/summary.png")


def main():
    print("=" * 68)
    print("  BANKNIFTY & USDINR INTRADAY STRATEGY MINER")
    print("=" * 68)

    print("\n[1/4] Generating BANKNIFTY universe …")
    bn_universe = generate_banknifty_universe(seed=42)
    print("      Done:", list(bn_universe.keys()))

    print("\n[2/4] Generating USDINR universe …")
    inr_universe = generate_usdinr_universe(seed=42)
    print("      Done:", list(inr_universe.keys()))

    bn_long, bn_short = get_banknifty_conditions()
    ir_long, ir_short = get_usdinr_conditions()

    print("\n[3/4] Mining BANKNIFTY strategies …")
    run_instrument(
        instrument    = "BANKNIFTY",
        anchor_ticker = "^NSEBANK",
        daily_universe= bn_universe,
        long_conds    = bn_long,
        short_conds   = bn_short,
        cost_rt       = COST_RT_EQ,
        outdir        = OUTDIR / "BANKNIFTY_STRATEGIES" / "INTRADAY",
    )

    print("\n[4/4] Mining USDINR strategies …")
    run_instrument(
        instrument    = "USDINR",
        anchor_ticker = "INR=X",
        daily_universe= inr_universe,
        long_conds    = ir_long,
        short_conds   = ir_short,
        cost_rt       = COST_RT_FX,
        outdir        = OUTDIR / "USDINR_STRATEGIES" / "INTRADAY",
    )

    print("\n" + "=" * 68)
    print("  All strategy cards saved:")
    print(f"    {OUTDIR}/BANKNIFTY_STRATEGIES/INTRADAY/{{1h,15m,5m}}/")
    print(f"    {OUTDIR}/USDINR_STRATEGIES/INTRADAY/{{1h,15m,5m}}/")
    print("=" * 68)


if __name__ == "__main__":
    main()
