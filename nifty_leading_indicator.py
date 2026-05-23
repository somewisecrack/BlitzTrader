"""
NIFTY 50 Leading Indicator Analysis
====================================
- Fetches 10 years of ^NSEI OHLCV data from Yahoo Finance
- Engineers 35+ technical indicators as candidate leading features
- Tests prediction horizons n = 1 to 30 days (forward close price)
- Uses Information Coefficient (IC) and walk-forward Random Forest
  to find the optimal n and the most predictive features
- Builds a composite leading indicator and reports all results
"""

import warnings
warnings.filterwarnings("ignore")

import os
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from scipy import stats
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score
from sklearn.pipeline import Pipeline

# ─────────────────────────────────────────────────────────────
# 1.  DATA FETCHING
# ─────────────────────────────────────────────────────────────

def fetch_nifty_data(period_years: int = 10) -> pd.DataFrame:
    """
    Load NIFTY 50 daily OHLCV.
    Tries Yahoo Finance first; falls back to locally-generated synthetic data
    calibrated to actual NIFTY 50 monthly closing levels (2015-2025).
    """
    print(f"[1/6] Fetching NIFTY 50 (^NSEI) – last {period_years} years …")

    # Try Yahoo Finance
    try:
        import yfinance as yf
        ticker = yf.Ticker("^NSEI")
        df = ticker.history(period=f"{period_years}y", auto_adjust=True, timeout=15)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.dropna(inplace=True)
        if len(df) > 100:
            print(f"    [Yahoo Finance] {len(df)} trading days  "
                  f"|  {df.index[0].date()} → {df.index[-1].date()}")
            return df
    except Exception:
        pass

    # Fallback: historically-calibrated synthetic NIFTY data
    print("    [Yahoo Finance unavailable – using historically-calibrated "
          "synthetic NIFTY data anchored to actual monthly index levels]")
    from nifty_data_generator import generate_nifty_ohlcv
    df = generate_nifty_ohlcv(seed=42)
    cutoff = pd.Timestamp.today() - pd.DateOffset(years=period_years)
    df = df[df.index >= cutoff]
    print(f"    {len(df)} trading days  |  {df.index[0].date()} → {df.index[-1].date()}")
    print(f"    CAGR: {((df['Close'].iloc[-1]/df['Close'].iloc[0])**(252/len(df))-1)*100:.1f}%  "
          f"|  Ann.Vol: {df['Close'].pct_change().std()*np.sqrt(252)*100:.1f}%")
    return df


# ─────────────────────────────────────────────────────────────
# 2.  FEATURE ENGINEERING  (35+ indicators)
# ─────────────────────────────────────────────────────────────

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()

def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(n).mean()
    loss  = (-delta.clip(upper=0)).rolling(n).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def _atr(high, low, close, n: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0))
    return (direction * volume).cumsum()

def _mfi(high, low, close, volume, n: int = 14) -> pd.Series:
    tp  = (high + low + close) / 3
    rmf = tp * volume
    pos = rmf.where(tp > tp.shift(), 0).rolling(n).sum()
    neg = rmf.where(tp < tp.shift(), 0).rolling(n).sum()
    return 100 - 100 / (1 + pos / neg.replace(0, np.nan))

def _cci(high, low, close, n: int = 20) -> pd.Series:
    tp  = (high + low + close) / 3
    ma  = tp.rolling(n).mean()
    md  = tp.rolling(n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - ma) / (0.015 * md.replace(0, np.nan))

def _stoch(high, low, close, k: int = 14, d: int = 3) -> tuple:
    lo_k = low.rolling(k).min()
    hi_k = high.rolling(k).max()
    pct_k = 100 * (close - lo_k) / (hi_k - lo_k).replace(0, np.nan)
    pct_d = pct_k.rolling(d).mean()
    return pct_k, pct_d

def _williams_r(high, low, close, n: int = 14) -> pd.Series:
    hh = high.rolling(n).max()
    ll = low.rolling(n).min()
    return -100 * (hh - close) / (hh - ll).replace(0, np.nan)

def _bb_pct(close: pd.Series, n: int = 20, k: float = 2.0) -> tuple:
    ma  = close.rolling(n).mean()
    std = close.rolling(n).std()
    bb_pct  = (close - (ma - k*std)) / (2*k*std + 1e-9)
    bb_width = 2 * k * std / ma
    return bb_pct, bb_width

def _hv(close: pd.Series, n: int = 20) -> pd.Series:
    """Historical (realised) volatility – annualised."""
    return close.pct_change().rolling(n).std() * np.sqrt(252)

def _vwap_dev(close, high, low, volume, n: int = 20) -> pd.Series:
    """Rolling VWAP deviation – how far price is from VWAP."""
    tp   = (high + low + close) / 3
    vwap = (tp * volume).rolling(n).sum() / volume.rolling(n).sum()
    return (close - vwap) / vwap

def _force_idx(close: pd.Series, volume: pd.Series, n: int = 13) -> pd.Series:
    return _ema(close.diff() * volume, n)

def _trix(close: pd.Series, n: int = 18) -> pd.Series:
    """TRIX = 1-period % change of triple-smoothed EMA."""
    e1 = _ema(close, n)
    e2 = _ema(e1, n)
    e3 = _ema(e2, n)
    return e3.pct_change() * 100

def _dpo(close: pd.Series, n: int = 20) -> pd.Series:
    """Detrended Price Oscillator."""
    shift = n // 2 + 1
    return close - _sma(close, n).shift(shift)

def _adx(high, low, close, n: int = 14) -> pd.Series:
    """Average Directional Index strength (0-100)."""
    tr = _atr(high, low, close, 1)
    up   = high.diff().clip(lower=0)
    down = (-low.diff()).clip(lower=0)
    up   = up.where(up > down, 0)
    down = down.where(down > up, 0)
    atr_n = tr.rolling(n).sum()
    di_p = 100 * up.rolling(n).sum() / atr_n.replace(0, np.nan)
    di_m = 100 * down.rolling(n).sum() / atr_n.replace(0, np.nan)
    dx   = 100 * (di_p - di_m).abs() / (di_p + di_m + 1e-9)
    return dx.rolling(n).mean()

def _cmf(high, low, close, volume, n: int = 20) -> pd.Series:
    """Chaikin Money Flow."""
    clv = ((close - low) - (high - close)) / (high - low + 1e-9)
    return (clv * volume).rolling(n).sum() / volume.rolling(n).sum()

def _trendline_slope(close: pd.Series, n: int = 20) -> pd.Series:
    """Rolling linear-regression slope (normalised by price level)."""
    def slope(x):
        t = np.arange(len(x))
        b = np.polyfit(t, x, 1)[0]
        return b / x[-1]
    return close.rolling(n).apply(slope, raw=True)

def _zscore(s: pd.Series, n: int = 20) -> pd.Series:
    mu, sigma = s.rolling(n).mean(), s.rolling(n).std()
    return (s - mu) / sigma.replace(0, np.nan)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return a DataFrame of 35+ leading-indicator features."""
    print("[2/6] Engineering features …")
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]

    feat = pd.DataFrame(index=df.index)

    # ── Price returns (past windows) ──────────────────────────
    for w in [1, 3, 5, 10, 20, 60]:
        feat[f"ret_{w}d"] = c.pct_change(w)

    # ── Moving-average distances ──────────────────────────────
    for w in [10, 20, 50, 200]:
        feat[f"price_vs_sma{w}"] = (c / _sma(c, w)) - 1

    # ── EMA crossover ─────────────────────────────────────────
    feat["ema9_vs_ema21"]   = (_ema(c, 9)  / _ema(c, 21))  - 1
    feat["ema21_vs_ema55"]  = (_ema(c, 21) / _ema(c, 55))  - 1
    feat["ema50_vs_ema200"] = (_ema(c, 50) / _ema(c, 200)) - 1

    # ── MACD ──────────────────────────────────────────────────
    macd_line   = _ema(c, 12) - _ema(c, 26)
    macd_signal = _ema(macd_line, 9)
    feat["macd_hist"]       = (macd_line - macd_signal) / c * 100
    feat["macd_vs_signal"]  = (macd_line / macd_signal) - 1

    # ── RSI ───────────────────────────────────────────────────
    feat["rsi_7"]  = _rsi(c, 7)
    feat["rsi_14"] = _rsi(c, 14)
    feat["rsi_21"] = _rsi(c, 21)
    feat["rsi_divergence"] = feat["rsi_14"] - feat["rsi_14"].shift(5)

    # ── Stochastic ────────────────────────────────────────────
    pct_k, pct_d = _stoch(h, l, c)
    feat["stoch_k"]         = pct_k
    feat["stoch_d"]         = pct_d
    feat["stoch_kd_cross"]  = pct_k - pct_d

    # ── Williams %R ───────────────────────────────────────────
    feat["williams_r"] = _williams_r(h, l, c)

    # ── CCI ───────────────────────────────────────────────────
    feat["cci_20"] = _cci(h, l, c, 20)

    # ── Bollinger Bands ───────────────────────────────────────
    bb_pct, bb_width = _bb_pct(c)
    feat["bb_pct"]   = bb_pct
    feat["bb_width"] = bb_width

    # ── ATR (volatility) ──────────────────────────────────────
    feat["atr_pct"]  = _atr(h, l, c, 14) / c
    feat["hv_20"]    = _hv(c, 20)

    # ── Volume indicators ─────────────────────────────────────
    vol_sma20 = _sma(v.astype(float), 20)
    feat["vol_ratio"]    = v / vol_sma20
    feat["obv_slope"]    = _trendline_slope(_obv(c, v).astype(float), 10)
    feat["mfi_14"]       = _mfi(h, l, c, v, 14)
    feat["cmf_20"]       = _cmf(h, l, c, v, 20)
    feat["force_idx_13"] = _zscore(_force_idx(c, v, 13), 252)

    # ── Trend & momentum ──────────────────────────────────────
    feat["adx_14"]        = _adx(h, l, c, 14)
    feat["trix_18"]       = _trix(c, 18)
    feat["dpo_20"]        = _dpo(c, 20) / c * 100
    feat["trendslope_20"] = _trendline_slope(c, 20)
    feat["vwap_dev_20"]   = _vwap_dev(c, h, l, v, 20)

    # ── Statistical / mean-reversion features ─────────────────
    feat["zscore_ret_5_20"]  = _zscore(feat["ret_5d"],  20)
    feat["zscore_ret_10_60"] = _zscore(feat["ret_10d"], 60)
    feat["ret_acf1"]         = c.pct_change().rolling(20).apply(
        lambda x: pd.Series(x).autocorr(1) if len(x) > 1 else np.nan, raw=True
    )

    # ── Higher-timeframe context ──────────────────────────────
    # 52-week position
    feat["pct_52w_high"] = (c / h.rolling(252).max()) - 1
    feat["pct_52w_low"]  = (c / l.rolling(252).min()) - 1

    feat.replace([np.inf, -np.inf], np.nan, inplace=True)
    print(f"    {feat.shape[1]} features built.")
    return feat


# ─────────────────────────────────────────────────────────────
# 3.  TARGET VARIABLES (forward n-day returns)
# ─────────────────────────────────────────────────────────────

def build_targets(df: pd.DataFrame, max_n: int = 30) -> pd.DataFrame:
    c = df["Close"]
    targets = {}
    for n in range(1, max_n + 1):
        fwd_ret = c.shift(-n) / c - 1
        targets[f"fwd_{n}d"]  = fwd_ret
        targets[f"dir_{n}d"]  = (fwd_ret > 0).astype(int)
    return pd.DataFrame(targets, index=df.index)


# ─────────────────────────────────────────────────────────────
# 4.  INFORMATION COEFFICIENT ANALYSIS
# ─────────────────────────────────────────────────────────────

def compute_ic_matrix(feat: pd.DataFrame, targets: pd.DataFrame,
                      min_obs: int = 200) -> pd.DataFrame:
    """
    Spearman IC between every feature and every n-day forward return.
    Returns a (n_features x max_n) DataFrame.
    """
    print("[3/6] Computing Information Coefficients (Spearman IC) …")
    n_days = [int(c.split("_")[1].replace("d",""))
              for c in targets.columns if c.startswith("fwd_")]
    results = {}
    combined = pd.concat([feat, targets], axis=1).dropna(subset=list(feat.columns))
    for n in n_days:
        tgt_col = f"fwd_{n}d"
        valid   = combined.dropna(subset=[tgt_col])
        if len(valid) < min_obs:
            continue
        ics = {}
        for f in feat.columns:
            tmp = valid[[f, tgt_col]].dropna()
            if len(tmp) < min_obs:
                continue
            ic, pval = stats.spearmanr(tmp[f], tmp[tgt_col])
            ics[f] = ic
        results[n] = ics
    ic_df = pd.DataFrame(results)   # rows=features, cols=n
    ic_df.columns = ic_df.columns.astype(int)
    return ic_df


def ic_summary(ic_df: pd.DataFrame) -> pd.DataFrame:
    """
    Per-n: mean |IC|, IC_IR (IC / std), % significant features.
    """
    summary = pd.DataFrame({
        "mean_abs_IC":  ic_df.abs().mean(axis=0),
        "max_abs_IC":   ic_df.abs().max(axis=0),
        "IC_IR":        (ic_df.mean(axis=0) / ic_df.std(axis=0)).abs(),
        "pct_pos_IC":   (ic_df > 0).mean(axis=0) * 100,
    }).sort_index()
    summary["score"] = (
        0.5 * summary["mean_abs_IC"]
        + 0.3 * summary["IC_IR"]
        + 0.2 * (summary["max_abs_IC"])
    )
    return summary


# ─────────────────────────────────────────────────────────────
# 5.  WALK-FORWARD RANDOM FOREST EVALUATION
# ─────────────────────────────────────────────────────────────

def walk_forward_accuracy(feat: pd.DataFrame, targets: pd.DataFrame,
                          top_features: list, n_days_list: list,
                          train_pct: float = 0.65,
                          step_pct: float = 0.05) -> dict:
    """
    Walk-forward out-of-sample directional accuracy for each n in n_days_list.
    Uses top_features.  Returns {n: accuracy}.
    """
    print("[4/6] Walk-forward out-of-sample accuracy (Random Forest) …")
    combined = pd.concat([feat[top_features], targets], axis=1).dropna(
        subset=top_features
    )
    N = len(combined)
    results = {}

    for n in n_days_list:
        dir_col = f"dir_{n}d"
        valid   = combined.dropna(subset=[dir_col])
        nn      = len(valid)
        if nn < 300:
            continue

        train_size = int(nn * train_pct)
        step_size  = max(1, int(nn * step_pct))

        all_preds, all_true = [], []
        cursor = train_size

        while cursor < nn - n:
            train = valid.iloc[:cursor]
            test  = valid.iloc[cursor : cursor + step_size]

            X_tr, y_tr = train[top_features].values, train[dir_col].values
            X_te, y_te = test[top_features].values,  test[dir_col].values

            if len(np.unique(y_tr)) < 2:
                cursor += step_size
                continue

            clf = RandomForestClassifier(
                n_estimators=100, max_depth=5,
                min_samples_leaf=15, random_state=42, n_jobs=-1
            )
            clf.fit(X_tr, y_tr)
            preds = clf.predict(X_te)
            all_preds.extend(preds)
            all_true.extend(y_te)
            cursor += step_size

        if not all_preds:
            continue
        acc = accuracy_score(all_true, all_preds) * 100
        results[n] = acc
        print(f"    n={n:>2}d  OOS accuracy = {acc:.1f}%  "
              f"(on {len(all_true)} samples)")

    return results


# ─────────────────────────────────────────────────────────────
# 6.  COMPOSITE LEADING INDICATOR  (for best n)
# ─────────────────────────────────────────────────────────────

def build_composite_indicator(feat: pd.DataFrame,
                               ic_df: pd.DataFrame,
                               best_n: int,
                               top_k: int = 10) -> pd.Series:
    """
    Weighted-sum composite of top_k features for the best_n horizon.
    Weights = sign(IC) so all sub-indicators push in the same direction.
    Then z-score normalise to [-∞, +∞] scale.
    Positive → bullish (close likely up in best_n days)
    Negative → bearish
    """
    ic_col   = ic_df[best_n].dropna()
    top_feats = ic_col.abs().nlargest(top_k).index.tolist()
    weights   = ic_col[top_feats]           # signed IC as weight

    # Rank-normalise each feature (uniform marginals)
    ranked = feat[top_feats].rank(pct=True) - 0.5  # centred [-.5, +.5]

    composite = ranked.mul(weights.values, axis=1).sum(axis=1)
    composite = (composite - composite.rolling(252).mean()) / \
                (composite.rolling(252).std() + 1e-9)
    composite.name = f"NIFTY_LI_n{best_n}"
    return composite, top_feats, weights


# ─────────────────────────────────────────────────────────────
# 7.  CHARTS
# ─────────────────────────────────────────────────────────────

def make_charts(df, feat, ic_df, ic_sum, wf_acc, composite,
                best_n, top_feats, ic_weights, output_dir):
    """Save all analytical charts."""
    os.makedirs(output_dir, exist_ok=True)

    # ── Chart 1: IC heatmap (features × n) ───────────────────
    fig, ax = plt.subplots(figsize=(16, 10))
    im = ax.imshow(ic_df.values, aspect="auto", cmap="RdYlGn",
                   vmin=-0.15, vmax=0.15)
    ax.set_xticks(range(len(ic_df.columns)))
    ax.set_xticklabels(ic_df.columns, fontsize=8)
    ax.set_yticks(range(len(ic_df.index)))
    ax.set_yticklabels(ic_df.index, fontsize=7)
    ax.set_xlabel("Forward Horizon n (days)")
    ax.set_title("Information Coefficient Heatmap\n"
                 "(Spearman IC between features and n-day forward return)")
    plt.colorbar(im, ax=ax, label="IC")
    plt.tight_layout()
    p = os.path.join(output_dir, "1_ic_heatmap.png")
    plt.savefig(p, dpi=120)
    plt.close()
    print(f"    Saved {p}")

    # ── Chart 2: IC summary by n ───────────────────────────────
    fig, axes = plt.subplots(2, 2, figsize=(14, 8))
    metrics = ["mean_abs_IC", "max_abs_IC", "IC_IR", "score"]
    titles  = ["Mean |IC| by horizon",
               "Max |IC| by horizon",
               "IC Information Ratio",
               "Composite Score"]
    for ax, m, t in zip(axes.flat, metrics, titles):
        ax.bar(ic_sum.index, ic_sum[m], color="#2196F3", alpha=0.8)
        if wf_acc and m == "score":
            ax2 = ax.twinx()
            ns  = sorted(wf_acc.keys())
            ax2.plot(ns, [wf_acc[k] for k in ns], "r-o", ms=4, label="OOS Acc %")
            ax2.set_ylabel("OOS Accuracy %", color="red")
            ax2.axhline(50, ls="--", c="grey", lw=1)
        ax.set_title(t)
        ax.set_xlabel("n (days ahead)")
        ax.axvline(best_n, color="red", lw=1.5, ls="--", label=f"best n={best_n}")
        ax.legend(fontsize=7)
    plt.suptitle("IC Analysis by Prediction Horizon")
    plt.tight_layout()
    p = os.path.join(output_dir, "2_ic_summary_by_n.png")
    plt.savefig(p, dpi=120)
    plt.close()
    print(f"    Saved {p}")

    # ── Chart 3: Feature importance for best n ────────────────
    feat_ic = ic_df[best_n].abs().sort_values(ascending=False).head(20)
    colors  = ["#4CAF50" if ic_df[best_n][f] > 0 else "#F44336"
               for f in feat_ic.index]
    fig, ax = plt.subplots(figsize=(10, 7))
    bars = ax.barh(feat_ic.index[::-1], feat_ic.values[::-1], color=colors[::-1])
    ax.set_title(f"Top-20 Features by |IC|  (n = {best_n} days)")
    ax.set_xlabel("|IC| (Spearman correlation with forward return)")
    ax.axvline(0.03, ls="--", c="grey", lw=1, label="|IC|=0.03")
    ax.legend(fontsize=8)
    # Add sign annotation
    from matplotlib.patches import Patch
    legend_elements = [Patch(fc="#4CAF50", label="Positive IC (bullish)"),
                       Patch(fc="#F44336", label="Negative IC (bearish)")]
    ax.legend(handles=legend_elements, fontsize=8)
    plt.tight_layout()
    p = os.path.join(output_dir, "3_feature_importance.png")
    plt.savefig(p, dpi=120)
    plt.close()
    print(f"    Saved {p}")

    # ── Chart 4: Composite indicator vs NIFTY price ───────────
    recent = 500   # last 500 trading days
    price  = df["Close"].iloc[-recent:]
    comp   = composite.iloc[-recent:]

    fig = plt.figure(figsize=(16, 9))
    gs  = gridspec.GridSpec(3, 1, height_ratios=[2.5, 1, 1])

    ax1 = fig.add_subplot(gs[0])
    ax1.plot(price.index, price.values, color="#1565C0", lw=1.2, label="NIFTY Close")
    ax1.set_title(f"NIFTY 50 Composite Leading Indicator  (n={best_n}d horizon)")
    ax1.set_ylabel("NIFTY Close")
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.3)

    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax2.axhline(0, color="grey", lw=0.8)
    ax2.axhline(1, color="#4CAF50", ls="--", lw=0.8)
    ax2.axhline(-1, color="#F44336", ls="--", lw=0.8)
    ax2.fill_between(comp.index, comp.values, 0,
                     where=(comp.values >= 0), alpha=0.4, color="#4CAF50")
    ax2.fill_between(comp.index, comp.values, 0,
                     where=(comp.values < 0),  alpha=0.4, color="#F44336")
    ax2.plot(comp.index, comp.values, color="#333", lw=0.6)
    ax2.set_ylabel("Composite LI (σ)")
    ax2.set_ylim(-3, 3)
    ax2.grid(alpha=0.3)

    # Forward return at best_n
    fwd_ret = df["Close"].pct_change(best_n).shift(-best_n).iloc[-recent:] * 100
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    ax3.bar(fwd_ret.index, fwd_ret.values,
            color=["#4CAF50" if r > 0 else "#F44336" for r in fwd_ret.fillna(0)],
            alpha=0.6, width=1)
    ax3.set_ylabel(f"{best_n}d fwd ret %")
    ax3.set_xlabel("Date")
    ax3.grid(alpha=0.3)

    plt.tight_layout()
    p = os.path.join(output_dir, "4_composite_indicator.png")
    plt.savefig(p, dpi=120)
    plt.close()
    print(f"    Saved {p}")

    # ── Chart 5: OOS accuracy by n ────────────────────────────
    if wf_acc:
        ns  = sorted(wf_acc.keys())
        acc = [wf_acc[k] for k in ns]
        fig, ax = plt.subplots(figsize=(12, 5))
        bars = ax.bar(ns, acc, color=[
            "#4CAF50" if a >= 53 else "#FF9800" if a >= 51 else "#F44336"
            for a in acc
        ], alpha=0.85)
        ax.axhline(50, ls="--", c="grey", lw=1.2, label="Random (50%)")
        ax.axhline(53, ls=":", c="green", lw=1, label="Good (53%)")
        ax.axvline(best_n, color="red", lw=2, ls="--", label=f"Best n={best_n}")
        for bar, a in zip(bars, acc):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.15,
                    f"{a:.1f}", ha="center", va="bottom", fontsize=7.5)
        ax.set_title("Walk-Forward Out-of-Sample Directional Accuracy by n")
        ax.set_xlabel("Prediction Horizon n (days)")
        ax.set_ylabel("OOS Directional Accuracy (%)")
        ax.legend()
        ax.set_ylim(45, max(acc) + 3)
        plt.tight_layout()
        p = os.path.join(output_dir, "5_oos_accuracy.png")
        plt.savefig(p, dpi=120)
        plt.close()
        print(f"    Saved {p}")


# ─────────────────────────────────────────────────────────────
# 8.  COMPOSITE INDICATOR BACKTEST  (simple long/short strategy)
# ─────────────────────────────────────────────────────────────

def backtest_indicator(df: pd.DataFrame, composite: pd.Series,
                        best_n: int, threshold: float = 0.5) -> dict:
    """
    Long NIFTY when composite > threshold, short when < -threshold.
    Hold for best_n days, no overlap (next entry after current hold expires).
    Returns performance stats.
    """
    ret_n = df["Close"].pct_change(best_n).shift(-best_n)

    signal = pd.Series(0, index=composite.index)
    signal[composite > threshold]  =  1
    signal[composite < -threshold] = -1

    # Strategy return: signal * n-day return (non-overlapping approximation)
    strat_ret = (signal * ret_n).dropna()
    bh_ret    = ret_n.dropna()

    def sharpe(r, ann=252/best_n):
        return (r.mean() / r.std()) * np.sqrt(ann) if r.std() > 0 else 0

    # Win rate
    active = strat_ret[signal[strat_ret.index] != 0]

    stats_dict = {
        "n_days"            : best_n,
        "threshold"         : threshold,
        "total_signals"     : int((signal != 0).sum()),
        "long_signals"      : int((signal > 0).sum()),
        "short_signals"     : int((signal < 0).sum()),
        "strategy_total_ret": round(strat_ret.sum() * 100, 2),
        "bh_total_ret"      : round(bh_ret.sum() * 100, 2),
        "strategy_sharpe"   : round(sharpe(strat_ret), 3),
        "bh_sharpe"         : round(sharpe(bh_ret), 3),
        "win_rate_pct"      : round((active > 0).mean() * 100, 2) if len(active) else 0,
        "avg_signal_ret_pct": round(active.mean() * 100, 3) if len(active) else 0,
    }
    return stats_dict


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    OUT_DIR = os.path.join(os.path.dirname(__file__), "nifty_li_output")
    MAX_N   = 30
    TOP_K   = 10   # features in composite indicator

    # ─── Step 1: data ────────────────────────────────────────
    df = fetch_nifty_data(10)

    # ─── Step 2: features ────────────────────────────────────
    feat = build_features(df)
    tgt  = build_targets(df, MAX_N)

    # ─── Step 3: IC analysis ──────────────────────────────────
    ic_df  = compute_ic_matrix(feat, tgt)
    ic_sum = ic_summary(ic_df)

    # ─── Step 4: walk-forward accuracy ───────────────────────
    # Use top-10 features by mean |IC| across all n (stable feature set)
    top_universal = ic_df.abs().mean(axis=1).nlargest(TOP_K).index.tolist()
    wf_acc = walk_forward_accuracy(
        feat, tgt, top_universal,
        n_days_list=list(range(1, MAX_N + 1))
    )

    # ─── Step 5: determine best n ─────────────────────────────
    print("[5/6] Determining optimal prediction horizon …")
    # Combine OOS accuracy + IC score (normalised)
    ic_scores  = ic_sum["score"].to_dict()
    oos_scores = {n: (acc - 50) for n, acc in wf_acc.items()}  # above random

    combined_score = {}
    for n in range(1, MAX_N + 1):
        ic_s  = ic_scores.get(n, 0)
        oos_s = oos_scores.get(n, 0)
        combined_score[n] = 0.5 * ic_s + 0.005 * oos_s   # IC dominates

    best_n = max(combined_score, key=combined_score.get)
    best_acc = wf_acc.get(best_n, None)
    print(f"    → Best n = {best_n} days  "
          f"(OOS acc = {best_acc:.1f}%,  "
          f"IC score = {ic_scores.get(best_n, 0):.4f})")

    # ─── Step 6: composite indicator ──────────────────────────
    composite, top_feats, ic_weights = build_composite_indicator(
        feat, ic_df, best_n, TOP_K
    )

    # ─── Step 7: backtest ─────────────────────────────────────
    bt = backtest_indicator(df, composite, best_n, threshold=0.5)

    # ─── Step 8: charts ───────────────────────────────────────
    print("[6/6] Generating charts …")
    make_charts(df, feat, ic_df, ic_sum, wf_acc,
                composite, best_n, top_feats, ic_weights, OUT_DIR)

    # ─── Save CSV tables ──────────────────────────────────────
    ic_sum.to_csv(os.path.join(OUT_DIR, "ic_summary_by_n.csv"))
    ic_df.to_csv(os.path.join(OUT_DIR,  "ic_all_features_by_n.csv"))

    # Composite indicator for last 252 days
    comp_export = pd.DataFrame({
        "date"           : composite.index[-252:],
        "composite_li"   : composite.values[-252:],
        "nifty_close"    : df["Close"].reindex(composite.index[-252:]).values,
    })
    comp_export.to_csv(os.path.join(OUT_DIR, "composite_indicator_recent.csv"),
                       index=False)

    # ─── Save JSON results ────────────────────────────────────
    # Full accuracy table
    all_n_results = []
    for n in range(1, MAX_N + 1):
        all_n_results.append({
            "n"             : n,
            "ic_score"      : round(float(ic_scores.get(n, 0)), 5),
            "mean_abs_IC"   : round(float(ic_sum.loc[n, "mean_abs_IC"]) if n in ic_sum.index else 0, 5),
            "IC_IR"         : round(float(ic_sum.loc[n, "IC_IR"]) if n in ic_sum.index else 0, 5),
            "oos_accuracy"  : round(float(wf_acc.get(n, 0)), 2),
            "combined_score": round(float(combined_score.get(n, 0)), 5),
        })

    final_report = {
        "best_n_days"        : int(best_n),
        "best_oos_accuracy"  : round(float(best_acc or 0), 2),
        "best_ic_score"      : round(float(ic_scores.get(best_n, 0)), 5),
        "data_period"        : {
            "start": str(df.index[0].date()),
            "end"  : str(df.index[-1].date()),
            "days" : len(df),
        },
        "top_10_features"    : [
            {
                "rank"   : i + 1,
                "feature": f,
                "IC"     : round(float(ic_df[best_n][f]), 5),
            }
            for i, f in enumerate(top_feats)
        ],
        "composite_formula"  : (
            f"NIFTY_LI_n{best_n} = z-score(Σ IC_i × rank_pct(feature_i), "
            f"rolling 252d)   where features = {top_feats}"
        ),
        "backtest"           : bt,
        "all_n_results"      : all_n_results,
    }

    json_path = os.path.join(OUT_DIR, "final_report.json")
    with open(json_path, "w") as f:
        json.dump(final_report, f, indent=2)

    # ─── Print summary to terminal ────────────────────────────
    print("\n" + "="*65)
    print("  NIFTY 50 LEADING INDICATOR — FINAL RESULTS")
    print("="*65)
    print(f"  Data : {df.index[0].date()} → {df.index[-1].date()}  "
          f"({len(df)} days)")
    print(f"  Features engineered : {feat.shape[1]}")
    print(f"  Horizons tested     : n = 1 … {MAX_N} days")
    print()
    print(f"  ★  OPTIMAL HORIZON  :  n = {best_n} DAYS")
    print(f"     OOS Directional Accuracy : {best_acc:.1f}%  (random = 50%)")
    print(f"     Information Coefficient  : {ic_scores.get(best_n, 0):.4f}")
    print()
    print("  TOP FEATURES (ranked by |IC|):")
    for i, (f, w) in enumerate(zip(top_feats, ic_weights)):
        direction = "bullish" if w > 0 else "bearish"
        print(f"    {i+1:>2}. {f:<28}  IC={w:.4f}  ({direction})")
    print()
    print(f"  COMPOSITE INDICATOR BACKTEST  (n={best_n}d, threshold=±0.5σ):")
    print(f"     Total signals   : {bt['total_signals']} "
          f"(long={bt['long_signals']}, short={bt['short_signals']})")
    print(f"     Win rate        : {bt['win_rate_pct']:.1f}%")
    print(f"     Avg signal ret  : {bt['avg_signal_ret_pct']:.2f}%")
    print(f"     Strategy Sharpe : {bt['strategy_sharpe']:.2f}")
    print(f"     Buy-hold Sharpe : {bt['bh_sharpe']:.2f}")
    print()

    print("  N-by-N ACCURACY TABLE:")
    print(f"  {'n':>3}  {'OOS Acc%':>9}  {'|IC|':>7}  {'IC_IR':>7}  {'Score':>7}")
    print("  " + "-"*42)
    for row in all_n_results:
        marker = "★" if row["n"] == best_n else " "
        print(f"  {row['n']:>3}{marker} {row['oos_accuracy']:>8.1f}%"
              f"  {row['mean_abs_IC']:>7.4f}  {row['IC_IR']:>7.4f}"
              f"  {row['combined_score']:>7.4f}")
    print()
    print(f"  Charts & CSVs → {OUT_DIR}/")
    print("="*65)

    return final_report


if __name__ == "__main__":
    main()
