"""
NIFTY Composite Leading Indicator (NCLI)
=========================================
A single scored indicator (–100 to +100) built from cross-asset data:
  S&P 500, VIX, USD/INR, Bank Nifty, RELIANCE, HDFCBANK, SBIN,
  Crude Oil, Gold, Nikkei, Hang Seng + NIFTY own price/volume action.

Formula:
  NCLI = 100 × Σ( IC_i × rank_pct_252(sub_signal_i) )
         normalised to [−100, +100] via rolling z-score

Each sub-signal is independently rank-normalised (0=min, 100=max over
252 days) and direction-adjusted (positive contribution always = bullish).
Weights are Spearman IC values measured out-of-sample.

Interpretation:
  NCLI >  40  → Strong Buy  (high confidence UP)
  NCLI  20..40 → Mild Buy
  NCLI −20..20 → Neutral
  NCLI −20..−40 → Mild Sell
  NCLI < −40  → Strong Sell (high confidence DOWN)
"""

import warnings
warnings.filterwarnings("ignore")

import os, sys, json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import Patch
from scipy import stats
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_asset_generator import generate_multi_asset
from nifty_data_generator import generate_nifty_ohlcv

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ncli_output")
os.makedirs(OUT, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def _ema(s, n): return s.ewm(span=n, adjust=False).mean()
def _sma(s, n): return s.rolling(n).mean()
def _rsi(c, n):
    d = c.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))
def _cci(h, l, c, n):
    tp = (h+l+c)/3
    return (tp - _sma(tp,n)) / (0.015 * tp.rolling(n).apply(
        lambda x: np.mean(np.abs(x-x.mean())), raw=True) + 1e-9)
def _stoch_k(h, l, c, k):
    return 100*(c-l.rolling(k).min())/(h.rolling(k).max()-l.rolling(k).min()+1e-9)
def _cmf(h, l, c, v, n):
    clv = ((c-l)-(h-c))/(h-l+1e-9)
    return (clv*v).rolling(n).sum()/v.rolling(n).sum()
def _obv(c, v):
    return (np.sign(c.diff().fillna(0))*v.astype(float)).cumsum()

def rank_pct(s, window=252):
    """Rolling percentile rank in [0, 1] over past `window` bars."""
    return s.rolling(window, min_periods=50).apply(
        lambda x: (x[:-1] < x[-1]).sum() / (len(x)-1) if len(x)>1 else 0.5,
        raw=True
    )


# ─────────────────────────────────────────────────────────────
# STEP 1 — BUILD RAW SUB-SIGNALS
# Each sub_signal is a raw time series with a meaningful value.
# Direction convention is set so HIGH value = BULLISH.
# ─────────────────────────────────────────────────────────────

def build_sub_signals(prices: pd.DataFrame, ohlcv: pd.DataFrame) -> pd.DataFrame:
    """
    Returns a DataFrame of named sub-signals, each already
    direction-adjusted so that a HIGHER value is more bullish.
    """
    c = prices["^NSEI"]
    h = ohlcv["High"].reindex(prices.index).ffill()
    l = ohlcv["Low"].reindex(prices.index).ffill()
    v = ohlcv["Volume"].reindex(prices.index).ffill()
    o = ohlcv["Open"].reindex(prices.index).ffill()

    spx   = prices["^GSPC"]
    vix   = prices["^VIX"]
    usd   = prices["INR=X"]
    bnk   = prices["^NSEBANK"]
    rel   = prices["RELIANCE.NS"]
    hdfc  = prices["HDFCBANK.NS"]
    sbi   = prices["SBIN.NS"]
    crude = prices["CL=F"]
    gold  = prices["GC=F"]
    n225  = prices["^N225"]
    hsi   = prices["^HSI"]
    vf    = v.astype(float)

    S = pd.DataFrame(index=prices.index)

    # ══ CROSS-ASSET SIGNALS ══════════════════════════════════

    # 1. SPX same-day return  (closes AFTER NIFTY → true lead)
    #    SPX up = NIFTY likely up tomorrow  →  bullish when positive
    S["SPX_ret"]       = spx.pct_change(1)

    # 2. SPX momentum (5-day)
    S["SPX_mom5"]      = spx.pct_change(5)

    # 3. VIX level vs 20d MA  (VIX low vs MA = risk-on = bullish)
    #    Negate so HIGHER = bullish
    S["VIX_vs_MA"]     = -( vix / _sma(vix,20) - 1 )

    # 4. VIX 1-day change  (VIX spike = fear = bearish → negate)
    S["VIX_chg"]       = -vix.diff(1)

    # 5. USD/INR 1-day return  (INR weakens = USDINR goes up = bearish → negate)
    S["INR_ret"]       = -usd.pct_change(1)

    # 6. USD/INR vs 10d EMA  (INR above EMA = bullish → negate USDINR)
    S["INR_vs_EMA"]    = -( usd / _ema(usd,10) - 1 )

    # 7. Bank Nifty 1-day return  (bank strength = NIFTY bullish)
    S["BNK_ret"]       = bnk.pct_change(1)

    # 8. Bank Nifty vs 10d EMA  (banks above EMA = bullish)
    S["BNK_vs_EMA"]    = bnk / _ema(bnk,10) - 1

    # 9. RELIANCE 1-day return
    S["REL_ret"]       = rel.pct_change(1)

    # 10. HDFC Bank 1-day return
    S["HDFC_ret"]      = hdfc.pct_change(1)

    # 11. SBI 1-day return
    S["SBI_ret"]       = sbi.pct_change(1)

    # 12. Crude oil return  (crude UP = inflationary = bearish for India → negate)
    S["CRUDE_ret"]     = -crude.pct_change(1)

    # 13. Gold return  (gold UP = risk-off = bearish for equities → negate)
    S["GOLD_ret"]      = -gold.pct_change(1)

    # 14. Nikkei 1-day return  (Asian sentiment)
    S["N225_ret"]      = n225.pct_change(1)

    # 15. Hang Seng 1-day return  (Asian sentiment)
    S["HSI_ret"]       = hsi.pct_change(1)

    # 16. Asian composite  (avg of Nikkei + HSI)
    S["ASIA_comp"]     = (S["N225_ret"] + S["HSI_ret"]) / 2

    # ══ NIFTY OWN SIGNALS ════════════════════════════════════

    # 17. RSI(2)  — oversold = bullish  (negate: high RSI = overbought = bearish)
    S["RSI2_rev"]      = 100 - _rsi(c, 2)   # 0=overbought(sell), 100=oversold(buy)

    # 18. RSI(14)
    S["RSI14_rev"]     = 100 - _rsi(c, 14)

    # 19. CCI(10)  — negative CCI = oversold = bullish (negate)
    S["CCI10_rev"]     = -_cci(h, l, c, 10)

    # 20. Stochastic(5)  — low %K = oversold = bullish (negate)
    stoch5 = 100*(c-l.rolling(5).min())/(h.rolling(5).max()-l.rolling(5).min()+1e-9)
    S["STOCH5_rev"]    = 100 - stoch5

    # 21. Bollinger %B  — near lower band = oversold = bullish (negate)
    ma20 = _sma(c,20); sd20 = c.rolling(20).std()
    bb_pct = (c-(ma20-2*sd20))/(4*sd20+1e-9)
    S["BB_rev"]        = 1 - bb_pct   # 0=upper band(sell), 1=lower band(buy)

    # 22. Close position in H-L range  (near LOW = bullish reversal → negate)
    S["CLOSE_POS_rev"] = 1 - (c-l)/(h-l+1e-9)

    # 23. Candle body direction  (negative body = bearish day = tomorrow may bounce)
    S["BODY_rev"]      = -(c-o)/(h-l+1e-9)

    # 24. 1-day return (negative = today sold off = bounce tomorrow)
    S["RET1_rev"]      = -c.pct_change(1)

    # 25. 3-day return (mean-reversion: big drop → bounce)
    S["RET3_rev"]      = -c.pct_change(3)

    # 26. Price vs EMA-10  (below EMA = oversold = bullish → negate distance)
    S["PvEMA10_rev"]   = -(c / _ema(c,10) - 1)

    # 27. Price vs EMA-20
    S["PvEMA20_rev"]   = -(c / _ema(c,20) - 1)

    # 28. Chaikin MF (positive CMF = institutional buying = bullish)
    S["CMF10"]         = _cmf(h, l, c, v, 10)

    # 29. OBV slope (rising OBV = bullish)
    obv = _obv(c, vf)
    S["OBV_slope"]     = obv.diff(3) / (obv.abs().rolling(20).mean() + 1e-9)

    # 30. Volume ratio  (high volume on OVERSOLD day = smart money buying)
    vol_ratio = vf / _sma(vf, 20)
    S["VOL_x_REV"]     = vol_ratio * S["RET1_rev"]   # large volume + down day = buy

    # ══ CALENDAR SIGNALS ════════════════════════════════════

    dt = pd.to_datetime(prices.index)
    # Month-end effect: institutional buying near month-end (especially for India)
    S["MONTH_END"]     = (dt.day >= 25).astype(float)
    # Near month-start: fresh allocations
    S["MONTH_START"]   = (dt.day <= 5).astype(float)
    # Expiry day (Thursday): NIFTY weekly/monthly options expiry
    S["EXPIRY_THU"]    = (dt.dayofweek == 3).astype(float)

    # ══ COMPOSITE CROSS-ASSET BREADTH ═══════════════════════

    # 33. Breadth: how many assets are bullish today (0-10)
    bulls = pd.DataFrame({
        "spx" : (S["SPX_ret"]  > 0),
        "bnk" : (S["BNK_ret"]  > 0),
        "rel" : (S["REL_ret"]  > 0),
        "hdfc": (S["HDFC_ret"] > 0),
        "n225": (S["N225_ret"] > 0),
        "hsi" : (S["HSI_ret"]  > 0),
        "inr" : (S["INR_ret"]  > 0),  # already sign-adjusted
        "vix" : (S["VIX_chg"]  > 0),  # already sign-adjusted
        "crude":(S["CRUDE_ret"]> 0),
        "gold": (S["GOLD_ret"] > 0),
    }).astype(float)
    S["BREADTH"]       = bulls.sum(axis=1)   # 0=all bearish, 10=all bullish

    S.replace([np.inf, -np.inf], np.nan, inplace=True)
    return S


# ─────────────────────────────────────────────────────────────
# STEP 2 — COMPUTE IC WEIGHTS (walk-forward, no leakage)
# ─────────────────────────────────────────────────────────────

def compute_ic_weights(sub_signals: pd.DataFrame,
                       nifty_close: pd.Series,
                       train_end_pct: float = 0.65) -> dict:
    """
    Compute Spearman IC for every sub-signal vs 1d and 2d forward return,
    using only the TRAINING portion of data (first 65%).
    Returns {n: {signal_name: ic_value}}.
    """
    split = int(len(sub_signals) * train_end_pct)
    S_tr  = sub_signals.iloc[:split]

    fwd_1d = nifty_close.pct_change(1).shift(-1)
    fwd_2d = nifty_close.pct_change(2).shift(-2)

    results = {}
    for n, fwd in [(1, fwd_1d), (2, fwd_2d)]:
        fwd_tr = fwd.reindex(S_tr.index)
        ics = {}
        for col in S_tr.columns:
            tmp = pd.concat([S_tr[col], fwd_tr], axis=1).dropna()
            if len(tmp) < 100:
                continue
            ic, pval = spearmanr(tmp.iloc[:,0], tmp.iloc[:,1])
            ics[col] = ic
        results[n] = ics
    return results


# ─────────────────────────────────────────────────────────────
# STEP 3 — BUILD THE NCLI
# ─────────────────────────────────────────────────────────────

def build_ncli(sub_signals: pd.DataFrame,
               ic_weights: dict,
               n_horizon: int,
               rank_window: int = 252,
               top_k: int = 15) -> pd.Series:
    """
    NCLI = 100 × rolling_zscore( Σ IC_i × rank_pct(sub_signal_i) )

    Select top_k signals by |IC|, weight by IC value.
    Positive NCLI → model says UP.  Negative → model says DOWN.
    """
    ics  = ic_weights[n_horizon]
    # Sort by |IC|, take top_k
    top  = sorted(ics.items(), key=lambda x: abs(x[1]), reverse=True)[:top_k]
    names, weights = zip(*top)

    # Rank-normalise each signal over rolling window → [0, 1]
    ranked = pd.DataFrame({
        name: rank_pct(sub_signals[name], rank_window)
        for name in names
        if name in sub_signals.columns
    }, index=sub_signals.index)

    # Weighted sum  (IC already encodes direction: positive IC = higher value = bullish)
    w_arr = np.array([ics[n] for n in ranked.columns])
    raw   = ranked.mul(w_arr, axis=1).sum(axis=1)

    # Rolling z-score → standardised units
    mu  = raw.rolling(rank_window, min_periods=50).mean()
    sig = raw.rolling(rank_window, min_periods=50).std()
    ncli = 100 * (raw - mu) / (sig + 1e-9)
    ncli = ncli.clip(-100, 100)
    ncli.name = f"NCLI_{n_horizon}d"
    return ncli, list(ranked.columns), w_arr


# ─────────────────────────────────────────────────────────────
# STEP 4 — EVALUATE ACCURACY (walk-forward OOS)
# ─────────────────────────────────────────────────────────────

def evaluate_ncli(ncli: pd.Series,
                  nifty_close: pd.Series,
                  n_horizon: int,
                  train_pct: float = 0.65) -> dict:
    """
    Walk-forward accuracy: use NCLI threshold to predict direction.
    Reports accuracy at multiple NCLI threshold levels.
    """
    fwd_dir = (nifty_close.shift(-n_horizon) > nifty_close).astype(int)
    df = pd.concat([ncli, fwd_dir.rename("dir")], axis=1).dropna()

    split    = int(len(df) * train_pct)
    df_test  = df.iloc[split:]

    ncli_oos = df_test[ncli.name]
    dir_oos  = df_test["dir"]

    # Overall accuracy (signal = sign of NCLI)
    pred_oos = (ncli_oos > 0).astype(int)
    oos_acc  = (pred_oos == dir_oos).mean() * 100

    # Accuracy at various threshold levels
    thresh_results = {}
    for thresh in [10, 20, 30, 40, 50, 60, 70]:
        mask = ncli_oos.abs() > thresh
        if mask.sum() < 30:
            continue
        acc = (pred_oos[mask] == dir_oos[mask]).mean() * 100
        thresh_results[thresh] = {
            "accuracy"    : round(acc, 2),
            "n_signals"   : int(mask.sum()),
            "coverage_pct": round(mask.mean() * 100, 1),
            "long_signals": int((ncli_oos[mask] > 0).sum()),
            "short_signals": int((ncli_oos[mask] < 0).sum()),
        }

    # Decile analysis: split OOS into 10 buckets by NCLI score
    # → do extreme deciles predict better?
    decile_acc = {}
    try:
        deciles = pd.qcut(ncli_oos, q=10, labels=False)
        for d in range(10):
            mask = deciles == d
            if mask.sum() >= 10:
                acc = (pred_oos[mask] == dir_oos[mask]).mean() * 100
                decile_acc[d+1] = {
                    "ncli_range": (round(ncli_oos[mask].min(),1),
                                   round(ncli_oos[mask].max(),1)),
                    "accuracy"  : round(acc, 2),
                    "n"         : int(mask.sum()),
                }
    except Exception:
        pass

    return {
        "oos_accuracy"  : round(oos_acc, 2),
        "oos_n"         : len(df_test),
        "threshold_acc" : thresh_results,
        "decile_acc"    : decile_acc,
    }


# ─────────────────────────────────────────────────────────────
# STEP 5 — BACKTEST (equity curve)
# ─────────────────────────────────────────────────────────────

def backtest_ncli(ncli: pd.Series,
                  nifty_close: pd.Series,
                  n_horizon: int,
                  threshold: float = 30.0,
                  train_pct: float = 0.65) -> dict:
    """
    Long NIFTY when NCLI > +threshold, Short when NCLI < -threshold.
    OOS only (after train_pct split).
    """
    fwd_ret = nifty_close.pct_change(n_horizon).shift(-n_horizon)
    df = pd.concat([ncli, fwd_ret.rename("ret")], axis=1).dropna()
    split   = int(len(df) * train_pct)
    df_test = df.iloc[split:]

    sig = pd.Series(0.0, index=df_test.index)
    sig[df_test[ncli.name] >  threshold] =  1.0
    sig[df_test[ncli.name] < -threshold] = -1.0

    strat   = (sig * df_test["ret"])
    bh      = df_test["ret"]
    active  = strat[sig != 0]
    ann     = 252 / n_horizon

    def sharpe(r): return r.mean()/(r.std()+1e-9)*np.sqrt(ann)
    def mdd(r):
        eq=(1+r).cumprod(); return ((eq-eq.cummax())/eq.cummax()).min()*100

    return {
        "threshold"      : threshold,
        "n_long"         : int((sig>0).sum()),
        "n_short"        : int((sig<0).sum()),
        "n_neutral"      : int((sig==0).sum()),
        "coverage_pct"   : round((sig!=0).sum()/len(sig)*100, 1),
        "win_rate_pct"   : round((active>0).mean()*100, 2) if len(active) else 0,
        "avg_ret_pct"    : round(active.mean()*100, 3) if len(active) else 0,
        "strategy_sharpe": round(sharpe(strat), 3),
        "bh_sharpe"      : round(sharpe(bh), 3),
        "max_dd_pct"     : round(mdd(strat), 2),
        "equity_strat"   : (1+strat).cumprod(),
        "equity_bh"      : (1+bh).cumprod(),
    }


# ─────────────────────────────────────────────────────────────
# CHARTS
# ─────────────────────────────────────────────────────────────

def make_charts(prices, ohlcv, ncli_1, ncli_2,
                eval_1, eval_2, bt_1, bt_2,
                sub_signals, ic_weights, comps_1, comps_2, w1, w2):
    C_bull = "#1B5E20"; C_bear = "#B71C1C"; C_neu = "#78909C"

    # ── Chart 1: NCLI panels on NIFTY price ──────────────────
    recent = 600
    nifty  = prices["^NSEI"].iloc[-recent:]

    fig = plt.figure(figsize=(18, 14))
    gs  = gridspec.GridSpec(4, 1, height_ratios=[2.5, 1.5, 1.5, 1])
    fig.suptitle("NIFTY Composite Leading Indicator (NCLI)\n"
                 "Single indicator built from 12 assets × 33 sub-signals",
                 fontsize=14, fontweight="bold")

    ax0 = fig.add_subplot(gs[0])
    ax0.plot(nifty.index, nifty.values, color="#263238", lw=1.2)
    ax0.set_ylabel("NIFTY 50", fontsize=10)
    ax0.grid(alpha=0.25)
    ax0.set_title("NIFTY 50 Price", fontsize=10)

    for ax, ncli, label, eval_r, bt, cmap_pos, cmap_neg in [
        (fig.add_subplot(gs[1]), ncli_1, "NCLI₁ (n=1d)", eval_1, bt_1, "#1B5E20", "#B71C1C"),
        (fig.add_subplot(gs[2]), ncli_2, "NCLI₂ (n=2d)", eval_2, bt_2, "#1A237E", "#880E4F"),
    ]:
        ncli_r = ncli.iloc[-recent:]
        ax.axhline(0,  color="grey",    lw=0.8)
        ax.axhline(30, color=cmap_pos, lw=0.8, ls="--", alpha=0.7)
        ax.axhline(-30,color=cmap_neg, lw=0.8, ls="--", alpha=0.7)
        ax.fill_between(ncli_r.index, ncli_r.values, 0,
                        where=ncli_r.values >= 0, alpha=0.5, color=cmap_pos)
        ax.fill_between(ncli_r.index, ncli_r.values, 0,
                        where=ncli_r.values < 0,  alpha=0.5, color=cmap_neg)
        ax.plot(ncli_r.index, ncli_r.values, color="#37474F", lw=0.6)
        ax.set_ylabel(label, fontsize=9)
        ax.set_ylim(-100, 100)
        ax.grid(alpha=0.2)
        acc_30 = eval_r["threshold_acc"].get(30, {}).get("accuracy", 0)
        cov_30 = eval_r["threshold_acc"].get(30, {}).get("coverage_pct", 0)
        ax.set_title(f"{label}  |  OOS acc (|NCLI|>30) = {acc_30:.1f}%  "
                     f"coverage={cov_30:.0f}%  Sharpe={bt['strategy_sharpe']:.2f}",
                     fontsize=9)

    # Breadth bar (bottom)
    ax3 = fig.add_subplot(gs[3])
    breadth = sub_signals["BREADTH"].iloc[-recent:]
    colors  = [C_bull if b >= 6 else C_bear if b <= 4 else C_neu for b in breadth]
    ax3.bar(breadth.index, breadth.values, color=colors, alpha=0.7, width=1)
    ax3.axhline(5, color="grey", lw=0.8, ls="--")
    ax3.set_ylabel("Breadth (0-10)", fontsize=8)
    ax3.set_ylim(0, 10)
    ax3.grid(alpha=0.2)

    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "NCLI_main.png"), dpi=130)
    plt.close()

    # ── Chart 2: Accuracy by NCLI level (decile + threshold) ─
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("NCLI Accuracy Analysis — OOS Period", fontsize=13)

    for row, (ncli, eval_r, label, color) in enumerate([
        (ncli_1, eval_1, "NCLI₁ (n=1d)", "#1B5E20"),
        (ncli_2, eval_2, "NCLI₂ (n=2d)", "#1A237E"),
    ]):
        # Threshold accuracy
        ax = axes[row, 0]
        t_data = eval_r["threshold_acc"]
        ts = sorted(t_data.keys())
        accs = [t_data[t]["accuracy"] for t in ts]
        covs = [t_data[t]["coverage_pct"] for t in ts]
        ax2 = ax.twinx()
        ax.plot(ts, accs, color=color, lw=2, marker="o", ms=6)
        ax2.bar(ts, covs, width=4, alpha=0.2, color="grey")
        ax.axhline(50, ls="--", c="grey", lw=1)
        ax.axhline(55, ls=":", c="green", lw=1)
        ax.axhline(60, ls=":", c="orange", lw=1)
        ax.set_xlabel("|NCLI| threshold"); ax.set_ylabel("Accuracy %", color=color)
        ax2.set_ylabel("Coverage %", color="grey")
        ax.set_ylim(44, 85); ax.set_title(f"{label}: Accuracy vs |NCLI| Level")

        # Decile accuracy
        ax = axes[row, 1]
        d_data = eval_r["decile_acc"]
        if d_data:
            ds   = sorted(d_data.keys())
            daccs = [d_data[d]["accuracy"] for d in ds]
            ranges = [f"{d_data[d]['ncli_range'][0]:.0f}\n{d_data[d]['ncli_range'][1]:.0f}"
                      for d in ds]
            bar_colors = [C_bull if a>=55 else C_bear if a<50 else C_neu for a in daccs]
            bars = ax.bar(ds, daccs, color=bar_colors, alpha=0.85)
            ax.axhline(50, ls="--", c="grey", lw=1)
            for bar, a in zip(bars, daccs):
                ax.text(bar.get_x()+bar.get_width()/2, a+0.5,
                        f"{a:.0f}%", ha="center", fontsize=8)
            ax.set_xticks(ds)
            ax.set_xticklabels([f"D{d}" for d in ds], fontsize=8)
            ax.set_xlabel("NCLI Decile (D1=most bearish, D10=most bullish)")
            ax.set_ylabel("Accuracy %")
            ax.set_ylim(35, 80)
            ax.set_title(f"{label}: Accuracy by NCLI Decile")

    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "NCLI_accuracy.png"), dpi=130)
    plt.close()

    # ── Chart 3: Component weights (pie + bar) ────────────────
    fig, axes = plt.subplots(1, 2, figsize=(16, 8))
    for ax, names, ws, label, color in [
        (axes[0], comps_1, w1, "NCLI₁ (n=1d)", "#1B5E20"),
        (axes[1], comps_2, w2, "NCLI₂ (n=2d)", "#1A237E"),
    ]:
        abs_ws = np.abs(ws)
        order  = np.argsort(abs_ws)[::-1]
        sorted_names = [names[i] for i in order]
        sorted_ws    = ws[order]
        sorted_abs   = abs_ws[order]
        bar_colors   = [color if w > 0 else "#C62828" for w in sorted_ws]
        bars = ax.barh(sorted_names[::-1], sorted_ws[::-1],
                       color=bar_colors[::-1], alpha=0.85)
        ax.axvline(0, c="grey", lw=0.8)
        ax.set_title(f"{label} — Component Weights (IC)\n"
                     f"Green=bullish signal, Red=bearish signal",
                     fontsize=10)
        ax.set_xlabel("IC Weight (Spearman correlation with forward return)")
        ax.tick_params(axis="y", labelsize=8)
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(fc=color,   label="Bullish contributor"),
                            Patch(fc="#C62828",label="Bearish contributor")],
                  fontsize=8)
    plt.suptitle("NCLI Sub-Signal Weights\n(Spearman IC with NIFTY forward return)",
                 fontsize=12)
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "NCLI_weights.png"), dpi=130)
    plt.close()

    # ── Chart 4: Equity curves ────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    for ax, bt, ncli, label in [
        (axes[0], bt_1, ncli_1, "NCLI₁ (n=1d)"),
        (axes[1], bt_2, ncli_2, "NCLI₂ (n=2d)"),
    ]:
        eq_s = bt["equity_strat"].dropna()
        eq_b = bt["equity_bh"].dropna()
        ax.plot(eq_s.index, eq_s.values, color="#1B5E20", lw=1.8,
                label=f"NCLI strategy (|NCLI|>{bt['threshold']:.0f})  "
                      f"Sharpe={bt['strategy_sharpe']:.2f}")
        ax.plot(eq_b.index, eq_b.values, color="grey", lw=1.2, ls="--",
                label=f"Buy & Hold  Sharpe={bt['bh_sharpe']:.2f}")
        ax.set_title(f"{label}  |  Win={bt['win_rate_pct']:.0f}%  "
                     f"Avg={bt['avg_ret_pct']:+.2f}%/trade  "
                     f"Coverage={bt['coverage_pct']:.0f}%  "
                     f"MaxDD={bt['max_dd_pct']:.1f}%")
        ax.legend(fontsize=9)
        ax.set_ylabel("Equity (₹1 start)")
        ax.grid(alpha=0.3)
    plt.suptitle("NCLI Strategy Equity Curve (OOS Period)", fontsize=12)
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "NCLI_equity.png"), dpi=130)
    plt.close()

    print(f"  4 charts saved → {OUT}/")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print("=" * 68)
    print("  NIFTY COMPOSITE LEADING INDICATOR (NCLI) — SINGLE INDICATOR")
    print("=" * 68)

    # ── Data ────────────────────────────────────────────────
    print("\n[1/6] Generating multi-asset data …")
    prices = generate_multi_asset(seed=42)
    ohlcv  = generate_nifty_ohlcv(seed=42).reindex(prices.index).ffill()
    print(f"    {len(prices)} days  "
          f"{prices.index[0].date()} → {prices.index[-1].date()}")

    # ── Sub-signals ──────────────────────────────────────────
    print("\n[2/6] Computing 33 sub-signals …")
    S = build_sub_signals(prices, ohlcv)
    print(f"    {S.shape[1]} sub-signals computed")

    # ── IC weights ───────────────────────────────────────────
    print("\n[3/6] Computing Spearman IC weights (training set only) …")
    ic_weights = compute_ic_weights(S, prices["^NSEI"], train_end_pct=0.65)

    print("\n  Top 10 sub-signals by |IC| for n=1d:")
    ics1 = ic_weights[1]
    for name, ic in sorted(ics1.items(), key=lambda x: abs(x[1]), reverse=True)[:10]:
        dir_lbl = "↑bullish" if ic > 0 else "↓bearish"
        print(f"    {name:<20}  IC={ic:+.4f}  ({dir_lbl})")

    print("\n  Top 10 sub-signals by |IC| for n=2d:")
    ics2 = ic_weights[2]
    for name, ic in sorted(ics2.items(), key=lambda x: abs(x[1]), reverse=True)[:10]:
        dir_lbl = "↑bullish" if ic > 0 else "↓bearish"
        print(f"    {name:<20}  IC={ic:+.4f}  ({dir_lbl})")

    # ── Build NCLI ───────────────────────────────────────────
    print("\n[4/6] Building NCLI …")
    ncli_1, comps_1, w1 = build_ncli(S, ic_weights, 1, top_k=15)
    ncli_2, comps_2, w2 = build_ncli(S, ic_weights, 2, top_k=15)
    print(f"    NCLI₁ range: {ncli_1.min():.1f} to {ncli_1.max():.1f}")
    print(f"    NCLI₂ range: {ncli_2.min():.1f} to {ncli_2.max():.1f}")

    # ── Evaluate ─────────────────────────────────────────────
    print("\n[5/6] Evaluating OOS accuracy …")
    eval_1 = evaluate_ncli(ncli_1, prices["^NSEI"], 1)
    eval_2 = evaluate_ncli(ncli_2, prices["^NSEI"], 2)

    # ── Backtest ─────────────────────────────────────────────
    bt_1 = backtest_ncli(ncli_1, prices["^NSEI"], 1, threshold=30)
    bt_2 = backtest_ncli(ncli_2, prices["^NSEI"], 2, threshold=30)

    # ── Charts ───────────────────────────────────────────────
    print("\n[6/6] Generating charts …")
    make_charts(prices, ohlcv, ncli_1, ncli_2,
                eval_1, eval_2, bt_1, bt_2,
                S, ic_weights, comps_1, comps_2, w1, w2)

    # ── Save outputs ─────────────────────────────────────────
    ncli_df = pd.DataFrame({
        "Date"   : prices.index,
        "NCLI_1d": ncli_1.values.round(2),
        "NCLI_2d": ncli_2.values.round(2),
        "NIFTY"  : prices["^NSEI"].values.round(2),
        "Signal_1d": pd.cut(ncli_1, bins=[-100,-40,-20,20,40,100],
                            labels=["STRONG_SELL","SELL","NEUTRAL","BUY","STRONG_BUY"]).astype(str),
    })
    ncli_df.to_csv(os.path.join(OUT, "ncli_values.csv"), index=False)

    report = {
        "indicator_name": "NIFTY Composite Leading Indicator (NCLI)",
        "formula": (
            "NCLI = clip[ 100 × z_252( Σ IC_i × rank_pct_252(sub_signal_i) ), -100, 100 ]"
        ),
        "interpretation": {
            ">40": "STRONG BUY — high confidence UP tomorrow",
            "20 to 40": "BUY",
            "-20 to 20": "NEUTRAL",
            "-40 to -20": "SELL",
            "<-40": "STRONG SELL — high confidence DOWN tomorrow",
        },
        "n1d": {
            "oos_accuracy_pct"  : eval_1["oos_accuracy"],
            "threshold_results" : eval_1["threshold_acc"],
            "decile_results"    : eval_1["decile_acc"],
            "backtest_thresh30" : {k:v for k,v in bt_1.items()
                                   if not isinstance(v, pd.Series)},
            "components": [
                {"rank": i+1, "signal": n, "IC": round(float(ic_weights[1][n]),4),
                 "direction": "bullish" if ic_weights[1][n]>0 else "bearish"}
                for i, n in enumerate(comps_1)
            ],
        },
        "n2d": {
            "oos_accuracy_pct"  : eval_2["oos_accuracy"],
            "threshold_results" : eval_2["threshold_acc"],
            "decile_results"    : eval_2["decile_acc"],
            "backtest_thresh30" : {k:v for k,v in bt_2.items()
                                   if not isinstance(v, pd.Series)},
            "components": [
                {"rank": i+1, "signal": n, "IC": round(float(ic_weights[2][n]),4),
                 "direction": "bullish" if ic_weights[2][n]>0 else "bearish"}
                for i, n in enumerate(comps_2)
            ],
        },
    }
    with open(os.path.join(OUT, "ncli_report.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)

    # ── Console report ────────────────────────────────────────
    print("\n" + "=" * 68)
    print("  NCLI — FINAL REPORT")
    print("=" * 68)

    print("""
  FORMULA:
    NCLI = clip[ 100 × z₂₅₂( Σ IC_i × rank_pct₂₅₂(sub_signal_i) ), -100, 100 ]

    Where:
      sub_signal_i = one of 33 raw signals from 12 assets
      rank_pct₂₅₂  = percentile rank over rolling 252 days (0=min, 1=max)
      IC_i          = Spearman correlation with forward return (train set)
      z₂₅₂          = rolling z-score over 252 days → standardised output

  SCALE:  NCLI > +40 = STRONG BUY
          NCLI +20..+40 = BUY
          NCLI ±20 = NEUTRAL
          NCLI -20..-40 = SELL
          NCLI < -40 = STRONG SELL
    """)

    for n, ncli, ev, bt, comps, ws in [
        (1, ncli_1, eval_1, bt_1, comps_1, w1),
        (2, ncli_2, eval_2, bt_2, comps_2, w2),
    ]:
        print(f"  ━━━ NCLI₍{n}d₎ — predicts {n}-day ahead direction ━━━━━━━━━━━")
        print(f"  Overall OOS accuracy : {ev['oos_accuracy']:.1f}%")
        print()
        print("  ACCURACY BY |NCLI| LEVEL:")
        print(f"  {'|NCLI|>':>10}  {'Accuracy':>9}  {'Signals':>8}  {'Coverage':>9}")
        print("  " + "-"*42)
        for thresh, td in sorted(ev["threshold_acc"].items()):
            print(f"  {'|NCLI|>'+str(thresh):>10}  "
                  f"{td['accuracy']:>8.1f}%  "
                  f"{td['n_signals']:>8}  "
                  f"{td['coverage_pct']:>8.1f}%")
        print()
        print("  BACKTEST (|NCLI| > 30, OOS only):")
        print(f"    {bt['n_long']} long + {bt['n_short']} short  "
              f"({bt['coverage_pct']:.0f}% coverage)")
        print(f"    Win rate     = {bt['win_rate_pct']:.1f}%")
        print(f"    Avg return   = {bt['avg_ret_pct']:+.2f}% / trade")
        print(f"    Sharpe       = {bt['strategy_sharpe']:.2f}  "
              f"(BH = {bt['bh_sharpe']:.2f})")
        print(f"    Max drawdown = {bt['max_dd_pct']:.1f}%")
        print()
        print("  TOP 15 COMPONENTS (by |IC|):")
        print(f"  {'Rank':>5}  {'Sub-Signal':<22}  {'IC':>8}  {'Direction'}")
        print("  " + "-"*52)
        for i, (nm, w) in enumerate(zip(comps, ws)):
            print(f"  {i+1:>5}. {nm:<22}  {w:>+8.4f}  "
                  f"{'↑ bullish' if w>0 else '↓ bearish'}")
        print()

    print(f"  Outputs → {OUT}/")
    print("=" * 68)

    return report


if __name__ == "__main__":
    main()
