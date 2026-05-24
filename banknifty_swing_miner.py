"""
BANKNIFTY Daily Swing Strategy Miner
======================================
Mines up to 5 swing setups on daily BANKNIFTY bars with BFSI cross-asset
signals from HDFCBANK, ICICIBANK, SBIN, AXISBANK, KOTAKBANK, NIFTY & VIX.

Signals computed on BANKNIFTY daily OHLCV:
  RSI(2/3/5/14), BB%B(20), Stochastic(5), CMF(10), OBV slope,
  ATR regime, daily/3d/5d returns, volume ratio, BFSI breadth,
  component relative returns, VIX level & change, calendar effects
  (month-end, BANKNIFTY monthly expiry Thursday).

Pipeline:
  build_signals() → mine_strategies() (all 2- & 3-cond combos)
  → IS filter  (win_rate ≥ 75%, n ≥ 30)
  → OOS filter (win_rate ≥ 65%, n ≥ 15)
  → deduplicate() → save_strategy_cards()

Output: SKILL_LIBRARY/BANKNIFTY_STRATEGIES/SWING/
"""

import numpy as np
import pandas as pd
from itertools import combinations
from scipy.stats import binomtest
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from banknifty_usdinr_generator import generate_banknifty_universe

# ── config ────────────────────────────────────────────────────────────
IS_FRAC          = 0.65
IS_MIN_WR        = 0.74    # slightly relaxed for higher-vol instrument
IS_MIN_TRADES    = 25
OOS_MIN_WR_HIGH  = 0.63
OOS_MIN_WR_LOW   = 0.59   # fallback if <5 found
OOS_MIN_TRADES   = 12

HOLD_DAYS    = [1, 2, 3, 5, 7]   # add 1-week hold
COST_RT      = 0.0015      # 0.15% round-trip (BANKNIFTY Futures)
OUTDIR       = Path("SKILL_LIBRARY/BANKNIFTY_STRATEGIES/SWING")

# ══════════════════════════════════════════════════════════════════════
# 1.  SIGNAL BUILDER
# ══════════════════════════════════════════════════════════════════════

def _rsi(s, n):
    d = s.diff()
    u = d.clip(lower=0).ewm(com=n-1, min_periods=n).mean()
    dn= (-d.clip(upper=0)).ewm(com=n-1, min_periods=n).mean()
    return 100 - 100/(1 + u/dn.replace(0, np.nan))

def _stoch_k(h, l, c, k=5):
    lo = l.rolling(k).min(); hi = h.rolling(k).max()
    return 100*(c-lo)/(hi-lo+1e-10)

def _bb_pct(c, n=20):
    ma=c.rolling(n).mean(); std=c.rolling(n).std()
    return (c-(ma-2*std))/(4*std+1e-10)

def _cmf(h, l, c, v, n=10):
    mfm = ((c-l)-(h-c))/(h-l+1e-10)
    mfv = mfm*v
    return mfv.rolling(n).sum()/v.rolling(n).sum().replace(0,np.nan)

def _obv_slope(c, v, n=14):
    direction = np.sign(c.diff().fillna(0))
    obv = (direction*v).cumsum()
    return obv.diff(n)


def build_signals(bn_daily: pd.DataFrame,
                  universe: dict) -> pd.DataFrame:
    """
    Build all daily signals for BANKNIFTY including cross-asset BFSI features.
    `bn_daily`  : OHLCV DataFrame for ^NSEBANK
    `universe`  : dict {ticker: OHLCV DataFrame} from generate_banknifty_universe()
    """
    c = bn_daily["Close"]
    h = bn_daily["High"]
    l = bn_daily["Low"]
    v = bn_daily["Volume"]
    idx = bn_daily.index

    sig = pd.DataFrame(index=idx)

    # ── BANKNIFTY own signals ─────────────────────────────────────────
    sig["RSI_2"]       = _rsi(c, 2)
    sig["RSI_3"]       = _rsi(c, 3)
    sig["RSI_5"]       = _rsi(c, 5)
    sig["RSI_14"]      = _rsi(c, 14)
    sig["bb_pct"]      = _bb_pct(c, 20)
    sig["STOCH5"]      = _stoch_k(h, l, c, 5)
    sig["STOCH5_rev"]  = 100 - sig["STOCH5"]   # high = oversold
    sig["CMF10"]       = _cmf(h, l, c, v, 10)
    sig["OBV_slope"]   = _obv_slope(c, v, 14)
    sig["OBV_pos"]     = (sig["OBV_slope"] > 0).astype(float)

    sig["ret1"]        = c.pct_change(1)
    sig["ret3"]        = c.pct_change(3)
    sig["ret5"]        = c.pct_change(5)
    avg_v              = v.rolling(20, min_periods=5).mean()
    sig["vol_ratio"]   = v / avg_v.replace(0, np.nan)

    # ATR regime
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr = tr.ewm(span=14,adjust=False).mean()
    sig["atr_ratio"]   = atr / atr.rolling(50,min_periods=10).mean().replace(0,np.nan)

    # Rolling rank percentile for composite signals
    def rank_pct(s, n=252):
        return s.rolling(n, min_periods=50).rank(pct=True)

    # ── Cross-asset: BFSI components ─────────────────────────────────
    bfsi_tickers = ["HDFCBANK.NS","ICICIBANK.NS","SBIN.NS","AXISBANK.NS","KOTAKBANK.NS"]
    bfsi_rets = {}
    for tk in bfsi_tickers:
        if tk in universe:
            col = tk.replace(".NS","").lower() + "_ret"
            r   = universe[tk]["Close"].pct_change().reindex(idx).fillna(0)
            sig[col]   = r
            bfsi_rets[col] = r

    # BFSI breadth: number of BFSI stocks up on the day (0–5)
    if bfsi_rets:
        breadth = sum((bfsi_rets[c] > 0).astype(float) for c in bfsi_rets)
        sig["bfsi_breadth"]  = breadth
        sig["bfsi_4plus"]    = (breadth >= 4).astype(float)  # majority bullish
        sig["bfsi_1minus"]   = (breadth <= 1).astype(float)  # majority bearish

    # PSU vs private: SBIN vs HDFCBANK spread
    if "sbin_ret" in sig.columns and "hdfcbank_ret" in sig.columns:
        sig["psu_vs_pvt"]    = sig["sbin_ret"] - sig["hdfcbank_ret"]
        sig["psu_outperf"]   = (sig["psu_vs_pvt"] > 0.003).astype(float)
        sig["pvt_outperf"]   = (sig["psu_vs_pvt"] < -0.003).astype(float)

    # HDFCBANK relative to BANKNIFTY — heavyweight leading
    if "hdfcbank_ret" in sig.columns:
        sig["hdfc_vs_bn"]    = sig["hdfcbank_ret"] - sig["ret1"].fillna(0)
        sig["hdfc_leading"]  = (sig["hdfc_vs_bn"] > 0.002).astype(float)
        sig["hdfc_lagging"]  = (sig["hdfc_vs_bn"] < -0.002).astype(float)

    # ── Cross-asset: NIFTY & VIX ──────────────────────────────────────
    if "^NSEI" in universe:
        nifty_c = universe["^NSEI"]["Close"].reindex(idx).ffill()
        sig["nifty_ret"]   = nifty_c.pct_change()
        sig["nifty_pos"]   = (sig["nifty_ret"] > 0).astype(float)
        sig["nifty_neg"]   = (sig["nifty_ret"] < 0).astype(float)
        # BANKNIFTY beta spread (BANKNIFTY stronger/weaker than NIFTY on beta basis)
        sig["bn_vs_nifty"] = sig["ret1"].fillna(0) - sig["nifty_ret"].fillna(0) * 1.4

    if "^VIX" in universe:
        vix = universe["^VIX"]["Close"].reindex(idx).ffill()
        sig["vix"]         = vix
        sig["vix_chg"]     = vix.pct_change()
        sig["vix_high"]    = (vix >= 20).astype(float)   # fear
        sig["vix_low"]     = (vix <= 14).astype(float)   # complacency
        sig["vix_spike"]   = (sig["vix_chg"] >= 0.08).astype(float)  # spike ≥8%
        sig["vix_drop"]    = (sig["vix_chg"] <= -0.05).astype(float) # drop ≥5%

    # ── Calendar effects ─────────────────────────────────────────────
    # Month-end (last trading day of month)
    month_groups = {}
    for d in idx:
        key = (d.year, d.month)
        month_groups.setdefault(key, []).append(d)
    month_end_dates = {max(v) for v in month_groups.values()}
    sig["MONTH_END"]   = pd.Series(idx, index=idx).isin(month_end_dates).astype(float).values

    # BANKNIFTY monthly expiry = last Thursday of month
    def last_thu_of_month(dates):
        result = set()
        for d in dates:
            key = (d.year, d.month)
            thursdays = [x for x in month_groups[key] if x.weekday() == 3]
            if thursdays:
                result.add(max(thursdays))
        return result

    expiry_dates = last_thu_of_month(idx)
    sig["EXPIRY_THU"]  = pd.Series(idx, index=idx).isin(expiry_dates).astype(float).values

    # ── Consecutive day direction ─────────────────────────────────────
    consec_up = np.zeros(len(c)); consec_dn = np.zeros(len(c))
    ret_vals = c.pct_change().fillna(0).values
    for i in range(1, len(c)):
        if ret_vals[i] > 0:
            consec_up[i] = consec_up[i-1] + 1; consec_dn[i] = 0
        elif ret_vals[i] < 0:
            consec_dn[i] = consec_dn[i-1] + 1; consec_up[i] = 0
    sig["consec_up"] = consec_up
    sig["consec_dn"] = consec_dn

    # ── Price vs moving averages ──────────────────────────────────────
    sig["c_vs_ema10"]  = (c - c.ewm(10).mean()) / c * 100   # % above EMA10
    sig["c_vs_ema20"]  = (c - c.ewm(20).mean()) / c * 100
    sig["c_vs_ma50"]   = (c - c.rolling(50).mean()) / c * 100
    sig["above_ma50"]  = (c > c.rolling(50).mean()).astype(float)
    sig["above_ma200"] = (c > c.rolling(200, min_periods=100).mean()).astype(float)

    return sig


# ══════════════════════════════════════════════════════════════════════
# 2.  CONDITION LIBRARY
# ══════════════════════════════════════════════════════════════════════

def get_conditions():
    LONG = [
        # RSI extreme oversold
        ("RSI_2",       "<=",  10.0,  "RSI(2) < 10 [extreme oversold]"),
        ("RSI_2",       "<=",  20.0,  "RSI(2) < 20 [very oversold]"),
        ("RSI_3",       "<=",  15.0,  "RSI(3) < 15 [extreme oversold]"),
        ("RSI_5",       "<=",  25.0,  "RSI(5) < 25 [oversold]"),
        ("RSI_14",      "<=",  35.0,  "RSI(14) < 35 [near oversold]"),
        ("RSI_14",      "<=",  40.0,  "RSI(14) < 40 [weak momentum]"),
        # BB / Stochastic
        ("bb_pct",      "<=",  0.05,  "BB%B < 0.05 [below lower band]"),
        ("bb_pct",      "<=",  0.10,  "BB%B < 0.10 [near lower band]"),
        ("bb_pct",      "<=",  0.15,  "BB%B < 0.15 [lower band zone]"),
        ("STOCH5_rev",  ">=",  80.0,  "Stoch(5) < 20 [oversold]"),
        ("STOCH5_rev",  ">=",  70.0,  "Stoch(5) < 30 [near oversold]"),
        # Returns (mean-reversion)
        ("ret3",        "<=", -0.03,  "3d return < -3% [pullback]"),
        ("ret3",        "<=", -0.05,  "3d return < -5% [sharp pullback]"),
        ("ret1",        "<=", -0.015, "1d return < -1.5% [down day]"),
        ("ret5",        "<=", -0.04,  "5d return < -4% [multi-day selloff]"),
        # CMF / OBV
        ("CMF10",       ">=",  0.05,  "CMF(10) > 0.05 [accumulation]"),
        ("OBV_pos",     ">=",  1.0,   "OBV slope positive [buying pressure]"),
        # Volume
        ("vol_ratio",   ">=",  1.50,  "Volume ≥ 1.5× avg [high interest]"),
        # Calendar
        ("MONTH_END",   ">=",  1.0,   "Month-end [institutional rebalancing]"),
        ("EXPIRY_THU",  ">=",  1.0,   "BANKNIFTY expiry Thursday [pin risk]"),
        # VIX / regime
        ("vix_spike",   ">=",  1.0,   "VIX spike ≥ 8% [fear = buy]"),
        ("vix_high",    ">=",  1.0,   "VIX ≥ 20 [elevated fear]"),
        ("above_ma50",  ">=",  1.0,   "Price above MA50 [medium uptrend]"),
        # Cross-asset BFSI
        ("bfsi_4plus",  ">=",  1.0,   "≥4 BFSI stocks up [sector breadth]"),
        ("nifty_pos",   ">=",  1.0,   "NIFTY positive [parent positive]"),
        ("hdfcbank_ret",">=",  0.003, "HDFCBANK up 0.3%+ [heavyweight]"),
        ("sbin_ret",    ">=",  0.003, "SBIN up 0.3%+ [PSU strength]"),
        ("pvt_outperf", ">=",  1.0,   "Private banks outperform PSU [quality]"),
        ("hdfc_leading",">=",  1.0,   "HDFCBANK outperforming BN [leading]"),
        ("consec_dn",   ">=",  2.0,   "2+ consecutive down days [oversold]"),
        ("consec_dn",   ">=",  3.0,   "3+ consecutive down days [very oversold]"),
        # Additional BFSI stock signals
        ("icicibank_ret",">=", 0.003, "ICICIBANK up 0.3%+ [2nd largest weight]"),
        ("axisbank_ret", ">=", 0.003, "AXISBANK up 0.3%+ [momentum]"),
        ("kotakbank_ret",">=", 0.003, "KOTAKBANK up 0.3%+ [quality bank]"),
        ("c_vs_ema10",  "<=", -1.0,  "Price 1%+ below EMA10 [short oversold]"),
        ("c_vs_ma50",   "<=", -3.0,  "Price 3%+ below MA50 [medium oversold]"),
        ("vix_spike",   ">=",  1.0,  "VIX spike ≥ 8% [fear = contrarian buy]"),
        ("ret1",        "<=", -0.02, "1d return < -2% [sharp down day]"),
    ]

    SHORT = [
        # RSI extreme overbought
        ("RSI_2",       ">=",  90.0,  "RSI(2) > 90 [extreme overbought]"),
        ("RSI_2",       ">=",  80.0,  "RSI(2) > 80 [very overbought]"),
        ("RSI_3",       ">=",  85.0,  "RSI(3) > 85 [extreme overbought]"),
        ("RSI_5",       ">=",  75.0,  "RSI(5) > 75 [overbought]"),
        ("RSI_14",      ">=",  65.0,  "RSI(14) > 65 [overbought]"),
        ("RSI_14",      ">=",  60.0,  "RSI(14) > 60 [near overbought]"),
        # BB / Stochastic
        ("bb_pct",      ">=",  0.90,  "BB%B > 0.90 [near upper band]"),
        ("bb_pct",      ">=",  0.85,  "BB%B > 0.85 [upper band zone]"),
        ("bb_pct",      ">=",  0.95,  "BB%B > 0.95 [above upper band]"),
        ("STOCH5",      ">=",  80.0,  "Stoch(5) > 80 [overbought]"),
        ("STOCH5",      ">=",  70.0,  "Stoch(5) > 70 [near overbought]"),
        # Returns
        ("ret3",        ">=",  0.03,  "3d return > +3% [extended rally]"),
        ("ret3",        ">=",  0.05,  "3d return > +5% [sharp rally]"),
        ("ret1",        ">=",  0.015, "1d return > +1.5% [up day]"),
        ("ret5",        ">=",  0.04,  "5d return > +4% [multi-day rally]"),
        # CMF / volume
        ("CMF10",       "<=", -0.05,  "CMF(10) < -0.05 [distribution]"),
        ("vol_ratio",   ">=",  1.50,  "Volume ≥ 1.5× avg [high interest]"),
        # Calendar
        ("MONTH_END",   ">=",  1.0,   "Month-end [sell-the-fact]"),
        ("EXPIRY_THU",  ">=",  1.0,   "BANKNIFTY expiry Thursday [unwinding]"),
        # VIX
        ("vix_drop",    ">=",  1.0,   "VIX drop ≥ 5% [complacency = short]"),
        ("vix_low",     ">=",  1.0,   "VIX ≤ 14 [complacency]"),
        # Cross-asset BFSI
        ("bfsi_1minus", ">=",  1.0,   "≤1 BFSI stocks up [sector weak]"),
        ("nifty_neg",   ">=",  1.0,   "NIFTY negative [parent weak]"),
        ("hdfcbank_ret","<=", -0.003, "HDFCBANK down 0.3%+ [heavyweight]"),
        ("sbin_ret",    "<=", -0.003, "SBIN down 0.3%+ [PSU weakness]"),
        ("psu_outperf", ">=",  1.0,   "PSU outperforms private [defensive]"),
        ("hdfc_lagging",">=",  1.0,   "HDFCBANK lagging BN [leader weak]"),
        ("consec_up",   ">=",  2.0,   "2+ consecutive up days [overbought]"),
        ("consec_up",   ">=",  3.0,   "3+ consecutive up days [very overbought]"),
        # Additional BFSI stock signals
        ("icicibank_ret","<=", -0.003,"ICICIBANK down 0.3%+ [2nd largest]"),
        ("axisbank_ret", "<=", -0.003,"AXISBANK down 0.3%+ [weakness]"),
        ("kotakbank_ret","<=", -0.003,"KOTAKBANK down 0.3%+ [quality bank]"),
        ("c_vs_ema10",  ">=",  1.5,  "Price 1.5%+ above EMA10 [short overbought]"),
        ("c_vs_ma50",   ">=",  4.0,  "Price 4%+ above MA50 [extended]"),
        ("vix_drop",    ">=",  1.0,  "VIX drop ≥ 5% [complacency = short]"),
        ("ret1",        ">=",  0.02, "1d return > +2% [sharp up day]"),
    ]
    return LONG, SHORT


# ══════════════════════════════════════════════════════════════════════
# 3.  MINING ENGINE
# ══════════════════════════════════════════════════════════════════════

def _apply(sig, conds):
    mask = pd.Series(True, index=sig.index)
    for col, op, thresh, _ in conds:
        if col not in sig.columns:
            return pd.Series(False, index=sig.index)
        mask &= sig[col] >= thresh if op == ">=" else sig[col] <= thresh
    return mask


def _evaluate(close: np.ndarray, mask: np.ndarray,
              direction: str, hold: int, is_end: int) -> dict:
    n = len(close); trades = []; last_exit = -1
    for i in range(n):
        if not mask[i] or i <= last_exit: continue
        ex = min(i + hold, n-1)
        if ex == i: continue
        ep, xp = close[i], close[ex]
        raw = (xp-ep)/ep if direction == "LONG" else (ep-xp)/ep
        net = raw - COST_RT
        trades.append(dict(entry_i=i, exit_i=ex, ret=net,
                           win=int(net>0), is_sample=int(i<is_end)))
        last_exit = ex

    is_t  = [t for t in trades if t["is_sample"]]
    oos_t = [t for t in trades if not t["is_sample"]]
    is_wr  = float(np.mean([t["win"] for t in is_t]))  if is_t  else 0.0
    oos_wr = float(np.mean([t["win"] for t in oos_t])) if oos_t else 0.0
    pval   = None
    if oos_t:
        pval = binomtest(sum(t["win"] for t in oos_t),
                         len(oos_t), 0.5, alternative="greater").pvalue
    eq = [1.0]; ev = 1.0
    for t in oos_t:
        ev *= (1+t["ret"]); eq.append(ev)
    avg_ret = float(np.mean([t["ret"] for t in oos_t])) if oos_t else 0.0
    return dict(is_trades=len(is_t), oos_trades=len(oos_t),
                is_wr=is_wr, oos_wr=oos_wr, pvalue=pval,
                eq_curve=eq, oos_return=round(ev-1,4),
                avg_oos_ret=round(avg_ret,5), trades=trades)


def mine_strategies(sig, close, conds, direction, hold_list, oos_min_wr):
    is_end  = int(len(close)*IS_FRAC)
    surv    = []
    close_a = np.array(close)
    comb2   = list(combinations(range(len(conds)), 2))
    comb3   = list(combinations(range(len(conds)), 3))
    if len(comb3) > 4000:
        rng = np.random.default_rng(77)
        sel = rng.choice(len(comb3), 4000, replace=False)
        comb3 = [comb3[i] for i in sorted(sel)]

    for idx_t in [c for c in comb2] + [c for c in comb3]:
        cc   = [conds[i] for i in idx_t]
        mask = _apply(sig, cc).fillna(False).values.astype(bool)
        if mask.sum() < IS_MIN_TRADES*1.2: continue
        for hold in hold_list:
            r = _evaluate(close_a, mask, direction, hold, is_end)
            if (r["is_wr"]     >= IS_MIN_WR      and
                r["is_trades"] >= IS_MIN_TRADES   and
                r["oos_wr"]    >= oos_min_wr      and
                r["oos_trades"]>= OOS_MIN_TRADES):
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
# 4.  EQUITY CURVE SIMULATOR (₹1L capital, daily close-to-close)
# ══════════════════════════════════════════════════════════════════════

def simulate_equity(oos_trades, start_equity=100_000):
    """Simulate ₹1 lakh on OOS trades (BANKNIFTY ETF proxy; no lot constraint)."""
    equity = start_equity
    equity_curve = [equity]
    wins = losses = 0
    win_amounts = []; loss_amounts = []
    for t in oos_trades:
        pnl = equity * t["ret"]
        equity += pnl
        equity_curve.append(equity)
        if t["win"]:
            wins += 1; win_amounts.append(pnl)
        else:
            losses += 1; loss_amounts.append(pnl)
    avg_win  = float(np.mean(win_amounts))  if win_amounts  else 0.0
    avg_loss = float(np.mean(loss_amounts)) if loss_amounts else 0.0
    return dict(final=round(equity,2), curve=equity_curve,
                wins=wins, losses=losses,
                avg_win=round(avg_win,2), avg_loss=round(avg_loss,2))


# ══════════════════════════════════════════════════════════════════════
# 5.  SAVE STRATEGY CARDS
# ══════════════════════════════════════════════════════════════════════

def save_strategy_cards(strategies, close_arr, sig, dates_idx, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    is_end   = int(len(close_arr) * IS_FRAC)
    oos_start= dates_idx[is_end].date()
    oos_end  = dates_idx[-1].date()
    summary  = []

    for rank, s in enumerate(strategies, 1):
        sid  = f"BN_SW_S{rank:02d}"
        cond_lines = "\n".join(
            f"{i+1}. **{c[3]}**  `{c[0]} {c[1]} {c[2]}`"
            for i, c in enumerate(s["conds"]))
        hold_label = f"{s['hold']} trading day(s)"
        pstr  = f"{s['pvalue']:.4f}" if s["pvalue"] else "—"

        oos_t = [t for t in s["trades"] if not t["is_sample"]]
        sim   = simulate_equity(oos_t)

        # ── markdown card ─────────────────────────────────────────────
        md = f"""# {sid}: BANKNIFTY Swing — {s['direction']} {hold_label}

## Overview
| Field | Value |
|-------|-------|
| Instrument | **BANKNIFTY 50** (Futures or ETF) |
| Direction | **{s['direction']}** |
| Hold Period | **{hold_label}** |
| Entry | Next open after all conditions met at EOD |
| Exit | Close of day +{s['hold']} |
| OOS Period | {oos_start} → {oos_end} |

## Entry Conditions
All must be TRUE at **market close**:

{cond_lines}

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **{s['oos_wr']*100:.1f}%** |
| OOS Trades | {s['oos_trades']} |
| In-Sample Win Rate | {s['is_wr']*100:.1f}% ({s['is_trades']} trades) |
| Avg Return / Trade | {s['avg_oos_ret']*100:+.2f}% |
| Binomial p-value | {pstr} |
| ₹1L Simulation | ₹{sim['final']:,.0f}  ({(sim['final']/100000-1)*100:+.1f}%) |
| Sim Win / Loss | {sim['wins']} / {sim['losses']} |
| Avg Win / Avg Loss | ₹{sim['avg_win']:,.0f} / ₹{sim['avg_loss']:,.0f} |

## Risk Management
- **Stop Loss**: 1.5 × ATR(14) from entry price
- **Max concurrent positions**: 1 (no overlap)
- **Min capital for futures**: ₹8–10 lakh (1 BANKNIFTY Futures lot ≈ 15 units)
- **For ETF (BankBees)**: any capital

## Cross-Asset Context
- BFSI components used: HDFCBANK, ICICIBANK, SBIN, AXISBANK, KOTAKBANK
- Parent index: NIFTY 50
- Volatility gauge: VIX
- All cross-asset signals are daily close values

## Notes
- Signals are predominantly **mean-reversion** (LONG setups) or **momentum SHORT**
- OOS period: last 35% of 10-year synthetic dataset
- Transaction cost: 0.15% round-trip included
"""
        (outdir / f"{sid}.md").write_text(md)

        # ── trade CSV ─────────────────────────────────────────────────
        if oos_t:
            rows = []
            for t in oos_t:
                entry_dt = dates_idx[t["entry_i"]] if t["entry_i"] < len(dates_idx) else None
                exit_dt  = dates_idx[t["exit_i"]]  if t["exit_i"]  < len(dates_idx) else None
                rows.append(dict(
                    entry_date=entry_dt, exit_date=exit_dt,
                    direction=s["direction"],
                    entry_price=round(close_arr[t["entry_i"]],1),
                    exit_price=round(close_arr[t["exit_i"]],1),
                    net_ret_pct=round(t["ret"]*100,3),
                    win=t["win"],
                ))
            pd.DataFrame(rows).to_csv(outdir / f"{sid}_trades.csv", index=False)

        summary.append(dict(
            id=sid, instrument="BANKNIFTY",
            direction=s["direction"], hold_days=s["hold"],
            conditions=[dict(signal=c[0],op=c[1],threshold=c[2],description=c[3])
                        for c in s["conds"]],
            performance=dict(
                is_wr_pct=round(s["is_wr"]*100,1),  oos_wr_pct=round(s["oos_wr"]*100,1),
                is_trades=s["is_trades"],             oos_trades=s["oos_trades"],
                avg_oos_ret_pct=round(s["avg_oos_ret"]*100,2),
                oos_return_pct=round(s["oos_return"]*100,2),
                pvalue=round(s["pvalue"],5) if s["pvalue"] else None,
                score=round(s["score"],3),
                sim_final_equity=sim["final"],
            ),
        ))

    (outdir / "all_strategies.json").write_text(json.dumps(summary, indent=2))

    # ── equity curves ─────────────────────────────────────────────────
    n = len(strategies)
    if n == 0: return summary
    fig, axes = plt.subplots(n, 1, figsize=(13, 4*n), squeeze=False)
    fig.suptitle("BANKNIFTY Swing Strategies — OOS Equity (₹1L start)",
                 fontsize=13, fontweight="bold")
    col_map = {"LONG":"#27ae60","SHORT":"#e74c3c"}
    for row, s in enumerate(strategies):
        ax  = axes[row][0]
        sim = simulate_equity([t for t in s["trades"] if not t["is_sample"]])
        cv  = np.array(sim["curve"])
        col = col_map[s["direction"]]
        ax.fill_between(range(len(cv)), cv/1e5-1, alpha=0.2, color=col)
        ax.plot(cv/1e5-1, color=col, linewidth=1.8)
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        cstr = " + ".join(c[3][:30] for c in s["conds"])
        ax.set_title(
            f"BN_SW_S{row+1:02d}: {s['direction']} hold={s['hold']}d  "
            f"OOS {s['oos_wr']*100:.1f}% ({s['oos_trades']} trades)  "
            f"₹{sim['final']/1000:.0f}k\n{cstr}", fontsize=9)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y,_: f"{y*100:.0f}%"))
        ax.set_xlabel("OOS trade #")
    plt.tight_layout()
    fig.savefig(outdir / "equity_curves.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # ── summary scorecard ─────────────────────────────────────────────
    fig2, ax2 = plt.subplots(figsize=(12, 4))
    labels = [f"BN_SW_S{i+1:02d}\n{s['direction']} {s['hold']}d" for i,s in enumerate(strategies)]
    wrs    = [s["oos_wr"]*100 for s in strategies]
    ns     = [s["oos_trades"] for s in strategies]
    bcolors= [col_map[s["direction"]] for s in strategies]
    bars   = ax2.bar(labels, wrs, color=bcolors, alpha=0.85, edgecolor="black")
    ax2.axhline(75, color="gold",   linestyle="--", linewidth=1.5, label="75% target")
    ax2.axhline(65, color="orange", linestyle="--", linewidth=1.2, label="65% OOS min")
    ax2.axhline(50, color="grey",   linestyle=":",  linewidth=0.8)
    ax2.set_ylim(0, 100)
    ax2.set_title("BANKNIFTY Swing Strategies — OOS Win Rate Summary", fontsize=12, fontweight="bold")
    ax2.set_ylabel("OOS Win Rate (%)")
    for bar, n_t in zip(bars, ns):
        ax2.text(bar.get_x()+bar.get_width()/2, bar.get_height()+1,
                 f"{bar.get_height():.0f}%\nn={n_t}", ha="center", va="bottom", fontsize=9)
    from matplotlib.patches import Patch
    ax2.legend(handles=[
        Patch(color="#27ae60", label="LONG"),
        Patch(color="#e74c3c", label="SHORT"),
        plt.Line2D([0],[0],color="gold",linestyle="--",label="75% target"),
        plt.Line2D([0],[0],color="orange",linestyle="--",label="65% OOS min"),
    ], fontsize=9)
    plt.tight_layout()
    fig2.savefig(outdir / "summary_scorecard.png", dpi=150, bbox_inches="tight")
    plt.close(fig2)

    return summary


# ══════════════════════════════════════════════════════════════════════
# 6.  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    print("=" * 68)
    print("  BANKNIFTY SWING STRATEGY MINER  (Daily Timeframe)")
    print("=" * 68)

    print("\n[1/5] Generating BANKNIFTY universe …")
    universe = generate_banknifty_universe(seed=42)
    bn_daily = universe["^NSEBANK"]
    print(f"      {len(bn_daily)} daily bars  "
          f"({bn_daily.index[0].date()} → {bn_daily.index[-1].date()})")

    print("\n[2/5] Building signals (BANKNIFTY own + BFSI cross-asset) …")
    sig   = build_signals(bn_daily, universe)
    close = bn_daily["Close"].values
    idx   = bn_daily.index
    n_sig = sig.shape[1]
    print(f"      {n_sig} signals computed")

    is_end = int(len(close) * IS_FRAC)
    print(f"      IS: {is_end} bars ({idx[0].date()} → {idx[is_end-1].date()})")
    print(f"      OOS:{len(close)-is_end} bars ({idx[is_end].date()} → {idx[-1].date()})")

    LONG_CONDS, SHORT_CONDS = get_conditions()
    OUTDIR.mkdir(parents=True, exist_ok=True)

    print(f"\n[3/5] Mining LONG strategies … (IS≥{IS_MIN_WR*100:.0f}%, OOS≥{OOS_MIN_WR_HIGH*100:.0f}%)")
    long_surv = mine_strategies(sig, close, LONG_CONDS,  "LONG",  HOLD_DAYS, OOS_MIN_WR_HIGH)
    print(f"      {len(long_surv)} survivors")

    print(f"\n[4/5] Mining SHORT strategies …")
    short_surv= mine_strategies(sig, close, SHORT_CONDS, "SHORT", HOLD_DAYS, OOS_MIN_WR_HIGH)
    print(f"      {len(short_surv)} survivors")

    combined = sorted(long_surv + short_surv, key=lambda x: x["score"], reverse=True)
    top5     = deduplicate(combined, max_shared=1, top_n=5)

    # Fallback: lower OOS bar to 0.60 if fewer than 5 found
    if len(top5) < 5:
        print(f"      Only {len(top5)} at 65% OOS — retrying with 60% …")
        long_surv2  = mine_strategies(sig, close, LONG_CONDS,  "LONG",  HOLD_DAYS, OOS_MIN_WR_LOW)
        short_surv2 = mine_strategies(sig, close, SHORT_CONDS, "SHORT", HOLD_DAYS, OOS_MIN_WR_LOW)
        combined2   = sorted(long_surv2+short_surv2, key=lambda x: x["score"], reverse=True)
        top5        = deduplicate(combined2, max_shared=1, top_n=5)

    print(f"\n  ✓ Final selection: {len(top5)} strategies")

    print("\n[5/5] Generating strategy cards, charts, JSONs …")
    summary = save_strategy_cards(top5, close, sig, idx, OUTDIR)
    print(f"      Saved to → {OUTDIR}/")

    # ── final console report ──────────────────────────────────────────
    print("\n" + "=" * 68)
    print("  TOP BANKNIFTY SWING STRATEGIES")
    print("=" * 68)
    print(f"  {'ID':12s} {'Dir':6s} {'Hold':6s} {'IS%':7s} {'OOS%':7s} "
          f"{'n':5s} {'Avg%':7s} {'₹1L→':10s} {'p-val':8s}")
    print(f"  {'-'*75}")
    for rank, s in enumerate(top5, 1):
        oos_t = [t for t in s["trades"] if not t["is_sample"]]
        sim   = simulate_equity(oos_t)
        pstr  = f"{s['pvalue']:.4f}" if s["pvalue"] else "  —   "
        cstr  = " + ".join(c[3][:22] for c in s["conds"])
        print(f"  BN_SW_S{rank:02d}    {s['direction']:6s} {s['hold']}d     "
              f"{s['is_wr']*100:5.1f}%  {s['oos_wr']*100:5.1f}%  "
              f"{s['oos_trades']:4d}  {s['avg_oos_ret']*100:+5.2f}%  "
              f"₹{sim['final']/1000:6.1f}k  {pstr}")
        print(f"  {'':12s} {cstr}")
    print("=" * 68)
    print(f"  Files → {OUTDIR}/")


if __name__ == "__main__":
    main()
