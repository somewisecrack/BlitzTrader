"""
NIFTY 50 — Short-Horizon Leading Indicator Analysis (n = 1, 2, 3, 4, 5 days)
==============================================================================
Targets: direction of close price n trading days ahead
Features: 60+ indicators tuned for short-term (1-5 day) prediction
Method:
  - Spearman IC per feature per n
  - Walk-forward GBM classifier (TimeSeriesSplit)
  - SHAP-style permutation importance on best model
  - Composite leading indicator per n
  - Full performance report
"""

import warnings
warnings.filterwarnings("ignore")

import os, json, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import Patch

from scipy import stats
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, log_loss
from sklearn.preprocessing import RobustScaler
from sklearn.inspection import permutation_importance

sys.path.insert(0, os.path.dirname(__file__))
from nifty_data_generator import generate_nifty_ohlcv

# ─────────────────────────────────────────────────────────────
# 0.  HELPERS
# ─────────────────────────────────────────────────────────────

def _ema(s, n):   return s.ewm(span=n, adjust=False).mean()
def _sma(s, n):   return s.rolling(n).mean()
def _std(s, n):   return s.rolling(n).std()

def _rsi(close, n=14):
    d = close.diff()
    gain = d.clip(lower=0).rolling(n).mean()
    loss = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))

def _atr(h, l, c, n=14):
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(1)
    return tr.rolling(n).mean()

def _stoch(h, l, c, k=14, d=3):
    lo = l.rolling(k).min(); hi = h.rolling(k).max()
    pk = 100*(c-lo)/(hi-lo+1e-9)
    return pk, pk.rolling(d).mean()

def _cci(h, l, c, n=20):
    tp = (h+l+c)/3
    return (tp - _sma(tp,n)) / (0.015 * tp.rolling(n).apply(
        lambda x: np.mean(np.abs(x - x.mean())), raw=True) + 1e-9)

def _williams_r(h, l, c, n=14):
    return -100*(h.rolling(n).max()-c)/(h.rolling(n).max()-l.rolling(n).min()+1e-9)

def _obv(c, v):
    return (np.sign(c.diff().fillna(0))*v).cumsum()

def _mfi(h, l, c, v, n=14):
    tp = (h+l+c)/3
    mf = tp*v
    pos = mf.where(tp > tp.shift(), 0).rolling(n).sum()
    neg = mf.where(tp < tp.shift(), 0).rolling(n).sum()
    return 100 - 100/(1 + pos/neg.replace(0, np.nan))

def _cmf(h, l, c, v, n=20):
    clv = ((c-l)-(h-c))/(h-l+1e-9)
    return (clv*v).rolling(n).sum()/v.rolling(n).sum()

def _zscore(s, n):
    return (s - s.rolling(n).mean()) / (s.rolling(n).std() + 1e-9)

def _hv(c, n):
    return c.pct_change().rolling(n).std() * np.sqrt(252)

def _parkinson_vol(h, l, n=5):
    """Parkinson high-low volatility estimator (annualised)."""
    return (np.log(h/l)**2 / (4*np.log(2))).rolling(n).mean().apply(np.sqrt) * np.sqrt(252)

def _trendline_slope(s, n):
    def slope(x):
        t = np.arange(len(x))
        return np.polyfit(t, x, 1)[0] / x[-1] if x[-1] != 0 else 0
    return s.rolling(n).apply(slope, raw=True)

def _vwap_deviation(c, h, l, v, n=10):
    tp = (h+l+c)/3
    vwap = (tp*v).rolling(n).sum() / v.rolling(n).sum()
    return (c - vwap) / vwap

# ─────────────────────────────────────────────────────────────
# 1.  FEATURE ENGINEERING  (60+ features, short-term focus)
# ─────────────────────────────────────────────────────────────

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    o = df["Open"]
    feat = pd.DataFrame(index=df.index)

    # ── A. Candle / intraday microstructure ───────────────────
    feat["body_pct"]       = (c - o) / (h - l + 1e-9)       # body direction & size
    feat["upper_wick"]     = (h - pd.concat([c, o], axis=1).max(axis=1)) / (h-l+1e-9)
    feat["lower_wick"]     = (pd.concat([c, o], axis=1).min(axis=1) - l) / (h-l+1e-9)
    feat["close_pct_hl"]   = (c - l) / (h - l + 1e-9)       # close position in range
    feat["range_pct"]      = (h - l) / c                     # intraday range vs price
    feat["gap_pct"]        = (o - c.shift(1)) / c.shift(1)   # overnight gap
    feat["gap_fill_pct"]   = (c - o) / (o - c.shift(1) + 1e-9)  # how much gap filled

    # Candle patterns (binary)
    feat["is_bullish_candle"] = (c > o).astype(int)
    feat["is_doji"]        = ((feat["body_pct"].abs() < 0.1) & (feat["range_pct"] > 0.005)).astype(int)
    feat["is_inside_bar"]  = ((h < h.shift(1)) & (l > l.shift(1))).astype(int)
    feat["is_outside_bar"] = ((h > h.shift(1)) & (l < l.shift(1))).astype(int)
    feat["engulf_bull"]    = ((c > o) & (o < c.shift(1)) & (c > o.shift(1))).astype(int)
    feat["engulf_bear"]    = ((c < o) & (o > c.shift(1)) & (c < o.shift(1))).astype(int)

    # ── B. Ultra-short RSI  (key short-term indicators) ───────
    feat["rsi_2"]   = _rsi(c, 2)
    feat["rsi_3"]   = _rsi(c, 3)
    feat["rsi_5"]   = _rsi(c, 5)
    feat["rsi_7"]   = _rsi(c, 7)
    feat["rsi_14"]  = _rsi(c, 14)
    feat["rsi_2_z"] = _zscore(feat["rsi_2"], 20)   # relative to recent self

    # ── C. Short-term momentum / returns ──────────────────────
    for lag in [1, 2, 3, 5, 10]:
        feat[f"ret_{lag}d"] = c.pct_change(lag)

    # Consecutive up/down days
    feat["consec_up"]   = (c.diff() > 0).rolling(5).sum()
    feat["consec_down"] = (c.diff() < 0).rolling(5).sum()
    feat["streak"]      = (c.diff() > 0).astype(int) - (c.diff() < 0).astype(int)
    feat["streak_cum"]  = feat["streak"].rolling(3).sum()   # +3 = 3 up days in a row

    # ── D. Short-window moving average distances ───────────────
    for w in [3, 5, 8, 10, 13, 20]:
        feat[f"price_vs_ema{w}"] = c / _ema(c, w) - 1

    feat["ema3_vs_ema8"]   = _ema(c,3)  / _ema(c,8)  - 1
    feat["ema5_vs_ema13"]  = _ema(c,5)  / _ema(c,13) - 1
    feat["ema8_vs_ema21"]  = _ema(c,8)  / _ema(c,21) - 1

    # ── E. Stochastic (short windows) ─────────────────────────
    pk5, pd5   = _stoch(h, l, c, 5, 3)
    pk14, pd14 = _stoch(h, l, c, 14, 3)
    feat["stoch5_k"]    = pk5
    feat["stoch5_d"]    = pd5
    feat["stoch5_kd"]   = pk5 - pd5
    feat["stoch14_k"]   = pk14
    feat["stoch14_kd"]  = pk14 - pd14

    # ── F. Williams %R (short windows) ────────────────────────
    feat["willr_5"]  = _williams_r(h, l, c, 5)
    feat["willr_10"] = _williams_r(h, l, c, 10)
    feat["willr_14"] = _williams_r(h, l, c, 14)

    # ── G. CCI (short windows) ────────────────────────────────
    feat["cci_5"]  = _cci(h, l, c, 5)
    feat["cci_10"] = _cci(h, l, c, 10)
    feat["cci_20"] = _cci(h, l, c, 20)

    # ── H. Bollinger Bands ────────────────────────────────────
    for w in [10, 20]:
        ma   = _sma(c, w)
        bstd = _std(c, w)
        feat[f"bb_pct_{w}"]   = (c - (ma - 2*bstd)) / (4*bstd + 1e-9)
        feat[f"bb_width_{w}"]  = 4*bstd / ma
        feat[f"bb_squeeze_{w}"]= (_std(c, w) / _sma(_std(c, w), 50)).fillna(1)

    # ── I. Short-term volatility ──────────────────────────────
    feat["atr5_pct"]      = _atr(h, l, c, 5)  / c
    feat["atr14_pct"]     = _atr(h, l, c, 14) / c
    feat["hv5"]           = _hv(c, 5)
    feat["hv10"]          = _hv(c, 10)
    feat["parkinson5"]    = _parkinson_vol(h, l, 5)
    feat["vol_contraction"]= feat["atr5_pct"] / (feat["atr14_pct"] + 1e-9)  # vol squeeze

    # ── J. Volume signals ─────────────────────────────────────
    v_ma5  = _sma(v.astype(float), 5)
    v_ma10 = _sma(v.astype(float), 10)
    v_ma20 = _sma(v.astype(float), 20)

    feat["vol_ratio_5"]   = v / v_ma5
    feat["vol_ratio_20"]  = v / v_ma20
    feat["vol_surge"]     = (v / v_ma5 > 2.0).astype(int)
    feat["up_vol_ratio"]  = (v * (c > o).astype(float)).rolling(5).sum() / \
                            (v.rolling(5).sum() + 1e-9)   # fraction of vol on up days
    feat["obv_slope_3"]   = _trendline_slope(_obv(c, v).astype(float), 3)
    feat["obv_slope_5"]   = _trendline_slope(_obv(c, v).astype(float), 5)
    feat["mfi_5"]         = _mfi(h, l, c, v, 5)
    feat["mfi_14"]        = _mfi(h, l, c, v, 14)
    feat["cmf_10"]        = _cmf(h, l, c, v, 10)
    feat["vwap_dev_5"]    = _vwap_deviation(c, h, l, v, 5)
    feat["vwap_dev_10"]   = _vwap_deviation(c, h, l, v, 10)

    # Force Index
    fi1 = c.diff() * v
    feat["force_idx_1_z"] = _zscore(fi1, 20)
    feat["force_idx_3_z"] = _zscore(_ema(fi1, 3), 50)

    # ── K. Mean-reversion z-scores ────────────────────────────
    feat["ret1_z5"]  = _zscore(c.pct_change(1), 5)
    feat["ret3_z10"] = _zscore(c.pct_change(3), 10)
    feat["ret5_z20"] = _zscore(c.pct_change(5), 20)

    # ── L. Price position context ─────────────────────────────
    feat["pct_52w_high"] = (c / h.rolling(252).max()) - 1
    feat["pct_10d_high"] = (c / h.rolling(10).max())  - 1
    feat["pct_5d_high"]  = (c / h.rolling(5).max())   - 1
    feat["pct_5d_low"]   = (c / l.rolling(5).min())   - 1

    # ── M. MACD short-window ──────────────────────────────────
    macd = _ema(c, 5) - _ema(c, 13)
    sig  = _ema(macd, 5)
    feat["macd_fast_hist"] = (macd - sig) / c * 100
    feat["macd_fast_cross"]= (macd > sig).astype(int)

    # ── N. Short autocorrelation (mean-reversion vs momentum) ─
    feat["acf1_ret_5"] = c.pct_change().rolling(5).apply(
        lambda x: pd.Series(x).autocorr(1), raw=False)
    feat["ret1_vs_ret2"] = c.pct_change(1) / (c.pct_change(2).abs() + 1e-9)

    feat.replace([np.inf, -np.inf], np.nan, inplace=True)
    print(f"    {feat.shape[1]} features built.")
    return feat


# ─────────────────────────────────────────────────────────────
# 2.  TARGETS
# ─────────────────────────────────────────────────────────────

def build_targets(df: pd.DataFrame, horizons=(1,2,3,4,5)) -> pd.DataFrame:
    c = df["Close"]
    tgt = {}
    for n in horizons:
        fwd = c.shift(-n) / c - 1
        tgt[f"fwd_{n}d"]   = fwd
        tgt[f"dir_{n}d"]   = (fwd > 0).astype(int)
    return pd.DataFrame(tgt, index=df.index)


# ─────────────────────────────────────────────────────────────
# 3.  IC ANALYSIS
# ─────────────────────────────────────────────────────────────

def compute_ic(feat: pd.DataFrame, targets: pd.DataFrame,
               horizons=(1,2,3,4,5)) -> pd.DataFrame:
    """Spearman IC: feature vs n-day forward return (per-feature NaN handling)."""
    print("[3/6] Computing Spearman IC …")
    results = {}
    for n in horizons:
        col  = f"fwd_{n}d"
        tgt_s = targets[col].dropna()
        ics   = {}
        for f in feat.columns:
            # Align feature and target, then drop any remaining NaN
            tmp = pd.concat([feat[f], tgt_s], axis=1).dropna()
            if len(tmp) < 100:
                continue
            ic, _ = stats.spearmanr(tmp.iloc[:, 0], tmp.iloc[:, 1])
            ics[f] = ic
        results[n] = ics
    return pd.DataFrame(results)  # rows=features, cols=n horizons


# ─────────────────────────────────────────────────────────────
# 4.  WALK-FORWARD  GBM CLASSIFIER
# ─────────────────────────────────────────────────────────────

def walk_forward_eval(feat: pd.DataFrame, targets: pd.DataFrame,
                      top_feats_per_n: dict,
                      horizons=(1,2,3,4,5),
                      n_splits: int = 5) -> dict:
    """
    TimeSeriesSplit walk-forward GBM for each n.
    Returns {n: {"accuracy": float, "per_fold": list}}
    """
    print("[4/6] Walk-forward GBM classifier …")
    combined = pd.concat([feat, targets], axis=1)
    results  = {}

    for n in horizons:
        dir_col   = f"dir_{n}d"
        top_feats = top_feats_per_n[n]
        sub  = combined[top_feats + [dir_col]].dropna(subset=[dir_col])
        # Forward-fill remaining feature NaNs then drop any remaining
        sub  = sub.ffill().dropna()
        X, y = sub[top_feats].values, sub[dir_col].values
        nn   = len(X)

        tscv = TimeSeriesSplit(n_splits=n_splits, gap=n)
        fold_accs   = []
        all_pred, all_true = [], []

        for train_idx, test_idx in tscv.split(X):
            X_tr, y_tr = X[train_idx], y[train_idx]
            X_te, y_te = X[test_idx],  y[test_idx]
            if len(np.unique(y_tr)) < 2:
                continue

            scaler = RobustScaler()
            X_tr_s = scaler.fit_transform(X_tr)
            X_te_s = scaler.transform(X_te)

            clf = GradientBoostingClassifier(
                n_estimators=200, max_depth=3, learning_rate=0.05,
                subsample=0.8, min_samples_leaf=10, random_state=42
            )
            clf.fit(X_tr_s, y_tr)
            preds = clf.predict(X_te_s)
            acc   = accuracy_score(y_te, preds) * 100
            fold_accs.append(acc)
            all_pred.extend(preds)
            all_true.extend(y_te)

        oos_acc = accuracy_score(all_true, all_pred) * 100
        results[n] = {
            "accuracy"  : round(oos_acc, 2),
            "per_fold"  : [round(a, 2) for a in fold_accs],
            "std"       : round(float(np.std(fold_accs)), 2),
            "n_samples" : len(all_true),
        }
        print(f"    n={n}d  OOS accuracy={oos_acc:.1f}%  "
              f"folds={[f'{a:.0f}%' for a in fold_accs]}")

    return results


# ─────────────────────────────────────────────────────────────
# 5.  PERMUTATION IMPORTANCE  (for best features per n)
# ─────────────────────────────────────────────────────────────

def permutation_importances(feat: pd.DataFrame, targets: pd.DataFrame,
                            top_feats_per_n: dict,
                            horizons=(1,2,3,4,5)) -> dict:
    """Train GBM on full dataset, then permutation-importance rank features."""
    print("[5/6] Permutation importance ranking …")
    combined = pd.concat([feat, targets], axis=1)
    perm_imp = {}

    for n in horizons:
        dir_col   = f"dir_{n}d"
        top_feats = top_feats_per_n[n]
        sub   = combined[top_feats + [dir_col]].dropna(subset=[dir_col])
        valid = sub.ffill().dropna()

        split = int(len(valid) * 0.65)
        train = valid.iloc[:split]
        test  = valid.iloc[split:]

        X_tr = train[top_feats].values
        y_tr = train[dir_col].values
        X_te = test[top_feats].values
        y_te = test[dir_col].values

        scaler = RobustScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        clf = GradientBoostingClassifier(
            n_estimators=300, max_depth=3, learning_rate=0.05,
            subsample=0.8, min_samples_leaf=10, random_state=42
        )
        clf.fit(X_tr_s, y_tr)
        base_acc = accuracy_score(y_te, clf.predict(X_te_s))

        pi = permutation_importance(
            clf, X_te_s, y_te, n_repeats=20,
            scoring="accuracy", random_state=42
        )
        imp_df = pd.DataFrame({
            "feature"   : top_feats,
            "importance": pi.importances_mean,
            "std"       : pi.importances_std,
        }).sort_values("importance", ascending=False)
        perm_imp[n] = imp_df
        print(f"    n={n}d  base accuracy={base_acc*100:.1f}%  "
              f"top feature: {imp_df.iloc[0]['feature']}")

    return perm_imp


# ─────────────────────────────────────────────────────────────
# 6.  COMPOSITE LEADING INDICATOR  (per n)
# ─────────────────────────────────────────────────────────────

def build_composite(feat: pd.DataFrame, ic_df: pd.DataFrame,
                    perm_imp: dict, n: int, top_k: int = 8) -> pd.Series:
    """
    Blend IC-selected and permutation-important features into a
    z-score normalised composite indicator for horizon n.
    """
    ic_col = ic_df[n].dropna().abs()

    # Weight = IC + permutation importance (normalised)
    perm_df = perm_imp[n].set_index("feature")["importance"].clip(lower=0)
    ic_norm   = (ic_col   / (ic_col.max()   + 1e-9)).rename("ic")
    perm_norm = (perm_df  / (perm_df.max()  + 1e-9)).rename("perm")

    combined_score = ic_norm.add(perm_norm, fill_value=0)
    # Only keep features that exist in feat and ic_df[n]
    all_candidates = [f for f in combined_score.nlargest(top_k * 3).index
                      if f in feat.columns and f in ic_df[n].index]
    top_feats = all_candidates[:top_k]

    signs  = np.sign(ic_df[n][top_feats].values)
    ranked = feat[top_feats].rank(pct=True) - 0.5
    raw    = ranked.mul(signs, axis=1).sum(axis=1)
    norm   = (raw - raw.rolling(252).mean()) / (raw.rolling(252).std() + 1e-9)
    norm.name = f"LI_{n}d"
    return norm, top_feats


# ─────────────────────────────────────────────────────────────
# 7.  SIGNAL BACKTEST  (per n)
# ─────────────────────────────────────────────────────────────

def backtest(df: pd.DataFrame, comp: pd.Series, n: int,
             threshold: float = 0.5) -> dict:
    fwd_ret = df["Close"].pct_change(n).shift(-n)
    sig     = pd.Series(0, index=comp.index)
    sig[comp >  threshold] =  1
    sig[comp < -threshold] = -1

    strat   = (sig * fwd_ret).dropna()
    active  = strat[sig[strat.index] != 0]
    ann     = 252 / n

    def sharpe(r):
        return r.mean() / (r.std() + 1e-9) * np.sqrt(ann)

    return {
        "n"              : n,
        "signals"        : int((sig != 0).sum()),
        "long_signals"   : int((sig > 0).sum()),
        "short_signals"  : int((sig < 0).sum()),
        "win_rate"       : round((active > 0).mean() * 100, 2) if len(active) else 0,
        "avg_ret_pct"    : round(active.mean() * 100, 3)   if len(active) else 0,
        "strategy_sharpe": round(sharpe(strat), 3),
        "bh_sharpe"      : round(sharpe(fwd_ret.dropna()), 3),
    }


# ─────────────────────────────────────────────────────────────
# 8.  CHARTS
# ─────────────────────────────────────────────────────────────

def make_charts(df, feat, ic_df, wf_results, perm_imp,
                composites, backtests, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    HORIZONS = [1, 2, 3, 4, 5]
    COLORS   = ["#1976D2", "#388E3C", "#F57C00", "#7B1FA2", "#C62828"]
    N_COLOR  = dict(zip(HORIZONS, COLORS))

    # ── Chart 1: Per-n OOS accuracy comparison ─────────────────
    fig, ax = plt.subplots(figsize=(10, 5))
    xs = [f"n={n}d" for n in HORIZONS]
    ys = [wf_results[n]["accuracy"] for n in HORIZONS]
    bars = ax.bar(xs, ys, color=[N_COLOR[n] for n in HORIZONS], alpha=0.85, width=0.5)
    ax.axhline(50, ls="--", c="grey", lw=1.2, label="Random baseline (50%)")
    ax.axhline(53, ls=":", c="green", lw=1, label="Good (53%)")
    for bar, y, n in zip(bars, ys, HORIZONS):
        folds_str = " / ".join(f"{f:.0f}" for f in wf_results[n]["per_fold"])
        ax.text(bar.get_x() + bar.get_width()/2, y + 0.2,
                f"{y:.1f}%\n({folds_str})", ha="center", va="bottom", fontsize=8)
    ax.set_title("Walk-Forward OOS Directional Accuracy — per Horizon\n"
                 "(fold accuracies shown in parentheses)")
    ax.set_ylabel("OOS Accuracy (%)")
    ax.set_ylim(45, max(ys) + 6)
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "S1_oos_accuracy_per_n.png"), dpi=130)
    plt.close()

    # ── Chart 2: IC heatmap (top 30 features × 5 horizons) ────
    top30 = ic_df.abs().mean(axis=1).nlargest(30).index
    sub   = ic_df.loc[top30]
    fig, ax = plt.subplots(figsize=(8, 12))
    im = ax.imshow(sub.values, aspect="auto", cmap="RdYlGn", vmin=-0.25, vmax=0.25)
    ax.set_xticks(range(5))
    ax.set_xticklabels([f"n={n}d" for n in HORIZONS])
    ax.set_yticks(range(len(top30)))
    ax.set_yticklabels(top30, fontsize=8)
    plt.colorbar(im, ax=ax, label="Spearman IC")
    ax.set_title("Spearman IC Heatmap — Top 30 Features\n"
                 "Green=bullish predictive power, Red=bearish")
    for i in range(len(top30)):
        for j in range(5):
            val = sub.values[i, j]
            ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                    fontsize=6, color="black" if abs(val) < 0.15 else "white")
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "S2_ic_heatmap.png"), dpi=130)
    plt.close()

    # ── Chart 3: Permutation importance per n (5 subplots) ────
    fig, axes = plt.subplots(1, 5, figsize=(20, 7))
    for ax, n in zip(axes, HORIZONS):
        df_pi = perm_imp[n].head(12)
        colors_bar = [N_COLOR[n]] * len(df_pi)
        ax.barh(df_pi["feature"][::-1], df_pi["importance"][::-1],
                xerr=df_pi["std"][::-1], color=colors_bar[::-1], alpha=0.85)
        ax.set_title(f"n = {n}d", fontsize=11, fontweight="bold", color=N_COLOR[n])
        ax.set_xlabel("Permutation Importance\n(accuracy drop)")
        ax.axvline(0, c="grey", lw=0.8)
        ax.tick_params(axis="y", labelsize=8)
    plt.suptitle("Permutation Feature Importance per Horizon", fontsize=13)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "S3_permutation_importance.png"), dpi=130)
    plt.close()

    # ── Chart 4: Composite indicators overlaid on NIFTY ───────
    recent = 400
    price  = df["Close"].iloc[-recent:]
    fig    = plt.figure(figsize=(16, 11))
    gs     = gridspec.GridSpec(6, 1, height_ratios=[2.5,1,1,1,1,1])

    ax0 = fig.add_subplot(gs[0])
    ax0.plot(price.index, price.values, "#263238", lw=1.2, label="NIFTY Close")
    ax0.set_ylabel("NIFTY Close", fontsize=9)
    ax0.set_title("NIFTY 50 — Short-Horizon Composite Leading Indicators")
    ax0.legend(loc="upper left", fontsize=8)
    ax0.grid(alpha=0.25)

    for idx, n in enumerate(HORIZONS, 1):
        ax = fig.add_subplot(gs[idx], sharex=ax0)
        comp = composites[n].iloc[-recent:]
        ax.axhline(0, c="grey", lw=0.7)
        ax.axhline(1,  c="#4CAF50", ls="--", lw=0.7)
        ax.axhline(-1, c="#F44336", ls="--", lw=0.7)
        ax.fill_between(comp.index, comp.values, 0,
                        where=comp.values >= 0, alpha=0.5, color="#4CAF50")
        ax.fill_between(comp.index, comp.values, 0,
                        where=comp.values < 0,  alpha=0.5, color="#F44336")
        ax.plot(comp.index, comp.values, N_COLOR[n], lw=0.5)
        acc = wf_results[n]["accuracy"]
        ax.set_ylabel(f"LI {n}d\n({acc:.1f}%)", fontsize=7.5)
        ax.set_ylim(-3.5, 3.5)
        ax.grid(alpha=0.2)

    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "S4_composite_indicators.png"), dpi=130)
    plt.close()

    # ── Chart 5: Backtest summary ─────────────────────────────
    metrics = ["win_rate", "avg_ret_pct", "strategy_sharpe", "bh_sharpe"]
    titles  = ["Win Rate (%)", "Avg Signal Return (%)",
               "Strategy Sharpe", "Buy-Hold Sharpe"]
    fig, axes = plt.subplots(1, 4, figsize=(16, 5))
    for ax, m, t in zip(axes, metrics, titles):
        vals = [backtests[n][m] for n in HORIZONS]
        bars = ax.bar([f"n={n}d" for n in HORIZONS], vals,
                      color=[N_COLOR[n] for n in HORIZONS], alpha=0.85)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.2,
                    f"{v:.2f}", ha="center", fontsize=8)
        ax.set_title(t)
        ax.set_ylim(0, max(vals) * 1.3 + 0.1)
    plt.suptitle("Composite Indicator Backtest Performance per Horizon", fontsize=12)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "S5_backtest_summary.png"), dpi=130)
    plt.close()

    print(f"    5 charts saved → {out_dir}/")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    HORIZONS  = [1, 2, 3, 4, 5]
    TOP_K_IC  = 20   # candidates from IC for GBM
    TOP_K_COMP= 8    # features in final composite

    OUT_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "nifty_short_li_output")

    # ─ 1. Data ──────────────────────────────────────────────
    print("[1/6] Loading NIFTY 50 data …")
    df = generate_nifty_ohlcv(seed=42)
    print(f"    {len(df)} days  {df.index[0].date()} → {df.index[-1].date()}")

    # ─ 2. Features ──────────────────────────────────────────
    print("[2/6] Engineering features …")
    feat = build_features(df)
    tgt  = build_targets(df, HORIZONS)

    # ─ 3. IC analysis ────────────────────────────────────────
    ic_df = compute_ic(feat, tgt, HORIZONS)

    # Top-K features per n (for GBM / composite)
    top_feats_per_n = {}
    for n in HORIZONS:
        candidates = ic_df[n].dropna().abs().nlargest(TOP_K_IC).index.tolist()
        # Keep only features that are present in feat and have enough non-NaN rows
        valid_cols = [f for f in candidates
                      if f in feat.columns and feat[f].notna().sum() > 200]
        top_feats_per_n[n] = valid_cols if valid_cols else feat.columns[:TOP_K_IC].tolist()
        print(f"    n={n}d  {len(top_feats_per_n[n])} candidate features")

    # ─ 4. Walk-forward accuracy ───────────────────────────────
    wf_results = walk_forward_eval(feat, tgt, top_feats_per_n, HORIZONS)

    # ─ 5. Permutation importance ─────────────────────────────
    perm_imp_results = permutation_importances(feat, tgt, top_feats_per_n, HORIZONS)

    # ─ 6. Composite indicator per n ──────────────────────────
    composites = {}
    composite_meta = {}
    for n in HORIZONS:
        comp, top_feats = build_composite(feat, ic_df, perm_imp_results, n, TOP_K_COMP)
        composites[n] = comp
        composite_meta[n] = {
            "features": top_feats,
            "ic_values": {f: round(float(ic_df[n].get(f, 0)), 4) for f in top_feats},
        }

    # ─ 7. Backtest ───────────────────────────────────────────
    backtests = {n: backtest(df, composites[n], n) for n in HORIZONS}

    # ─ 8. Charts ─────────────────────────────────────────────
    print("[6/6] Generating charts …")
    make_charts(df, feat, ic_df, wf_results, perm_imp_results,
                composites, backtests, OUT_DIR)

    # ─ Save outputs ───────────────────────────────────────────
    # IC table
    ic_df.to_csv(os.path.join(OUT_DIR, "ic_table.csv"))

    # Final JSON report
    final = {
        "data"          : {"start": str(df.index[0].date()),
                           "end"  : str(df.index[-1].date()),
                           "days" : len(df)},
        "horizons"      : {},
    }
    for n in HORIZONS:
        # Top 10 features by permutation importance
        pi_top = perm_imp_results[n].head(10)
        top10_list = []
        for _, row in pi_top.iterrows():
            top10_list.append({
                "rank"         : int(_ + 1),
                "feature"      : row["feature"],
                "perm_imp"     : round(float(row["importance"]), 5),
                "IC"           : round(float(ic_df[n].get(row["feature"], 0)), 4),
            })
        final["horizons"][str(n)] = {
            "oos_accuracy_pct" : wf_results[n]["accuracy"],
            "fold_accuracies"  : wf_results[n]["per_fold"],
            "accuracy_std"     : wf_results[n]["std"],
            "composite_features": composite_meta[n]["features"],
            "top10_by_perm_imp" : top10_list,
            "backtest"          : backtests[n],
        }

    with open(os.path.join(OUT_DIR, "short_horizon_report.json"), "w") as f:
        json.dump(final, f, indent=2)

    # ─ Print report ───────────────────────────────────────────
    print("\n" + "="*70)
    print("  NIFTY 50 — SHORT-HORIZON LEADING INDICATOR REPORT")
    print("="*70)
    print(f"  Data  : {df.index[0].date()} → {df.index[-1].date()}  ({len(df)} days)")
    print(f"  Features engineered : {feat.shape[1]}")
    print()

    for n in HORIZONS:
        bt  = backtests[n]
        wf  = wf_results[n]
        pi  = perm_imp_results[n]
        cm  = composite_meta[n]
        print(f"  ─── n = {n} DAY{'S' if n>1 else ''} ───────────────────────────────────────")
        print(f"  OOS Directional Accuracy : {wf['accuracy']:.1f}%  "
              f"(±{wf['std']:.1f}%)  random=50%")
        print(f"  Backtest  win rate={bt['win_rate']:.1f}%  "
              f"avg_ret={bt['avg_ret_pct']:.2f}%  "
              f"Sharpe={bt['strategy_sharpe']:.2f}  "
              f"(BH Sharpe={bt['bh_sharpe']:.2f})")
        print(f"  Top features (perm importance):")
        for _, row in pi.head(8).iterrows():
            ic_v = ic_df[n].get(row["feature"], 0)
            dirn = "↑bull" if ic_v > 0 else "↓bear"
            print(f"    {'·':1s} {row['feature']:<26}  "
                  f"imp={row['importance']:+.4f}  IC={ic_v:+.3f} ({dirn})")
        print(f"  Composite formula :")
        print(f"    LI_{n}d = z₂₅₂[ Σ sign(IC_i) × rank_pct(feature_i) ]")
        print(f"    features = {cm['features']}")
        print()

    print("  SUMMARY TABLE")
    print(f"  {'n':>4}  {'OOS Acc':>8}  {'WinRate':>8}  "
          f"{'AvgRet':>8}  {'Sharpe':>8}  {'BH Sharpe':>10}")
    print("  " + "-"*55)
    for n in HORIZONS:
        bt = backtests[n]
        wf = wf_results[n]
        print(f"  {n:>4}d  {wf['accuracy']:>7.1f}%  {bt['win_rate']:>7.1f}%  "
              f"  {bt['avg_ret_pct']:>6.2f}%  {bt['strategy_sharpe']:>8.2f}  "
              f"{bt['bh_sharpe']:>10.2f}")
    print()
    print(f"  Outputs → {OUT_DIR}/")
    print("="*70)


if __name__ == "__main__":
    main()
