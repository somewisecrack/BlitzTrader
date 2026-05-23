"""
NIFTY 50 — High-Accuracy Next-Day Direction Predictor
=======================================================
Binary classification:
  n=1: will tomorrow's close > today's close?
  n=2: will close in 2 days > today's close?

Approach:
  - Features specifically chosen for 1-2 day predictive power
  - Stacking ensemble: GBM + RF + Logistic + ExtraTrees → meta-LR
  - Probability calibration (Platt scaling / Isotonic)
  - Confidence-threshold filtering: report both full and high-confidence accuracy
  - Strict walk-forward OOS (no data leakage)
"""

import warnings
warnings.filterwarnings("ignore")

import os, json, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from scipy import stats
from sklearn.ensemble import (GradientBoostingClassifier, RandomForestClassifier,
                               ExtraTreesClassifier, StackingClassifier)
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV, CalibrationDisplay
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score
from sklearn.preprocessing import RobustScaler
from sklearn.pipeline import Pipeline
from sklearn.feature_selection import SelectKBest, f_classif

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nifty_data_generator import generate_nifty_ohlcv

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "nifty_direction_output")
os.makedirs(OUT_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def _sma(s, n):  return s.rolling(n).mean()
def _rsi(c, n):
    d = c.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))
def _atr(h, l, c, n):
    tr = pd.concat([(h-l),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(1)
    return tr.rolling(n).mean()
def _stoch_k(h, l, c, k):
    return 100*(c - l.rolling(k).min()) / (h.rolling(k).max()-l.rolling(k).min()+1e-9)
def _cci(h, l, c, n):
    tp = (h+l+c)/3
    return (tp-_sma(tp,n))/(0.015*tp.rolling(n).apply(
        lambda x: np.mean(np.abs(x-x.mean())),raw=True)+1e-9)
def _williams_r(h, l, c, n):
    return -100*(h.rolling(n).max()-c)/(h.rolling(n).max()-l.rolling(n).min()+1e-9)
def _zscore(s, n):
    return (s - s.rolling(n).mean()) / (s.rolling(n).std() + 1e-9)
def _obv(c, v):
    return (np.sign(c.diff().fillna(0))*v.astype(float)).cumsum()
def _cmf(h, l, c, v, n):
    clv = ((c-l)-(h-c))/(h-l+1e-9)
    return (clv*v).rolling(n).sum()/v.rolling(n).sum()


# ─────────────────────────────────────────────────────────────
# FEATURE ENGINEERING  (focused on 1-2 day prediction)
# ─────────────────────────────────────────────────────────────

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Every feature here is specifically chosen for its relevance
    to 1-2 day ahead direction prediction.
    """
    c, h, l, v, o = df["Close"], df["High"], df["Low"], df["Volume"], df["Open"]
    F = pd.DataFrame(index=df.index)

    # ── 1. INTRADAY POSITION & CANDLE STRUCTURE ───────────────
    # Where did close end within today's range? High close → near-term fade
    F["close_pos_hl"]   = (c - l) / (h - l + 1e-9)           # 1=top, 0=bottom
    F["body_pct"]       = (c - o) / (h - l + 1e-9)           # positive=bullish candle
    F["upper_wick"]     = (h - pd.concat([c,o],axis=1).max(axis=1)) / (h-l+1e-9)
    F["lower_wick"]     = (pd.concat([c,o],axis=1).min(axis=1) - l) / (h-l+1e-9)
    F["range_expansion"]= (h-l) / _atr(h,l,c,10)             # today's range vs avg range
    F["is_bullish"]     = (c > o).astype(int)

    # ── 2. OVERNIGHT GAP ──────────────────────────────────────
    F["gap_pct"]        = (o - c.shift(1)) / c.shift(1)       # gap up = next-day fade?
    F["gap_direction"]  = np.sign(F["gap_pct"])

    # ── 3. ULTRA-SHORT RSI (most powerful for 1-2d) ───────────
    F["rsi_2"]          = _rsi(c, 2)
    F["rsi_3"]          = _rsi(c, 3)
    F["rsi_5"]          = _rsi(c, 5)
    F["rsi_7"]          = _rsi(c, 7)
    F["rsi_14"]         = _rsi(c, 14)
    # RSI(2) z-score vs recent self (captures relative extreme)
    F["rsi2_z20"]       = _zscore(F["rsi_2"], 20)
    # RSI divergence: price went up but RSI didn't (bearish)
    F["rsi2_div"]       = F["rsi_2"] - F["rsi_2"].shift(3)    # RSI momentum

    # ── 4. SHORT-TERM RETURNS (momentum vs mean-reversion) ────
    F["ret_1d"]         = c.pct_change(1)
    F["ret_2d"]         = c.pct_change(2)
    F["ret_3d"]         = c.pct_change(3)
    F["ret_5d"]         = c.pct_change(5)
    F["ret_10d"]        = c.pct_change(10)
    # Z-score of recent returns (how extreme is the current move?)
    F["ret1_z5"]        = _zscore(F["ret_1d"], 5)
    F["ret1_z20"]       = _zscore(F["ret_1d"], 20)
    F["ret3_z10"]       = _zscore(F["ret_3d"], 10)
    # Consecutive streak
    F["consec_up"]      = (c.diff() > 0).rolling(3).sum()     # up days in last 3
    F["consec_down"]    = (c.diff() < 0).rolling(3).sum()
    F["ret_accel"]      = F["ret_1d"] - F["ret_1d"].shift(1)  # is momentum increasing?

    # ── 5. MEAN-REVERSION OSCILLATORS ─────────────────────────
    F["cci_5"]          = _cci(h, l, c, 5)
    F["cci_10"]         = _cci(h, l, c, 10)
    F["cci_20"]         = _cci(h, l, c, 20)
    F["willr_5"]        = _williams_r(h, l, c, 5)
    F["willr_10"]       = _williams_r(h, l, c, 10)
    F["stoch5_k"]       = _stoch_k(h, l, c, 5)
    F["stoch14_k"]      = _stoch_k(h, l, c, 14)

    # ── 6. EMA POSITION / DISTANCE ────────────────────────────
    for n in [3, 5, 8, 10, 13, 21]:
        F[f"c_vs_ema{n}"] = c / _ema(c, n) - 1
    F["ema3_ema8"]      = _ema(c,3)/_ema(c,8) - 1
    F["ema5_ema13"]     = _ema(c,5)/_ema(c,13) - 1

    # ── 7. BOLLINGER BANDS ────────────────────────────────────
    for n in [10, 20]:
        ma  = _sma(c, n);  sd = c.rolling(n).std()
        F[f"bb_pct_{n}"]   = (c-(ma-2*sd))/(4*sd+1e-9)       # 0=lower, 1=upper
        F[f"bb_width_{n}"]  = 4*sd/ma
    # Bollinger squeeze: narrow bands often precede breakout
    F["bb_squeeze"]     = c.rolling(5).std() / c.rolling(20).std()

    # ── 8. VOLUME SIGNALS ─────────────────────────────────────
    vf = v.astype(float)
    F["vol_ratio_5"]    = vf / _sma(vf, 5)
    F["vol_ratio_10"]   = vf / _sma(vf, 10)
    F["vol_ratio_20"]   = vf / _sma(vf, 20)
    F["up_vol_pct"]     = (vf * (c > o).astype(float)).rolling(5).sum() / \
                          (vf.rolling(5).sum() + 1e-9)
    F["cmf_5"]          = _cmf(h, l, c, v, 5)
    F["cmf_10"]         = _cmf(h, l, c, v, 10)
    F["obv_z20"]        = _zscore(_obv(c,v), 20)
    # Force index: price change × volume (buying/selling pressure)
    fi = c.diff() * vf
    F["force_idx_z"]    = _zscore(fi, 20)
    F["force_idx_ema3"] = _zscore(fi.ewm(span=3,adjust=False).mean(), 50)

    # ── 9. VOLATILITY STATE ───────────────────────────────────
    F["atr5_pct"]       = _atr(h,l,c,5) / c
    F["hv5"]            = c.pct_change().rolling(5).std() * np.sqrt(252)
    F["hv10"]           = c.pct_change().rolling(10).std() * np.sqrt(252)
    # Relative volatility (are we in high or low vol regime?)
    F["vol_regime"]     = F["hv5"] / (F["hv10"] + 1e-9)      # >1 = vol expanding

    # ── 10. VWAP DEVIATION ────────────────────────────────────
    tp = (h+l+c)/3
    for n in [5, 10]:
        vwap = (tp*vf).rolling(n).sum() / vf.rolling(n).sum()
        F[f"vwap_dev_{n}"] = (c - vwap) / vwap

    # ── 11. CONTEXT: PRICE POSITION ───────────────────────────
    F["pct_5d_high"]    = c / h.rolling(5).max() - 1          # how far from 5d high
    F["pct_10d_high"]   = c / h.rolling(10).max() - 1
    F["pct_5d_low"]     = c / l.rolling(5).min() - 1          # how far above 5d low
    F["pct_52w_high"]   = c / h.rolling(252).max() - 1

    # ── 12. CALENDAR EFFECTS ──────────────────────────────────
    F["day_of_week"]    = pd.to_datetime(df.index).dayofweek  # 0=Mon, 4=Fri
    F["is_monday"]      = (F["day_of_week"] == 0).astype(int)
    F["is_friday"]      = (F["day_of_week"] == 4).astype(int)
    F["month"]          = pd.to_datetime(df.index).month
    # Near month-end (often option expiry effects on NIFTY)
    F["days_to_month_end"] = pd.to_datetime(df.index).to_series().apply(
        lambda d: (d + pd.offsets.MonthEnd(0) - d).days)

    # ── 13. INTERACTION FEATURES ──────────────────────────────
    # High RSI + near upper BB → stronger mean-reversion signal
    F["rsi2_x_bbpct"]   = F["rsi_2"] * F["bb_pct_20"] / 100
    F["ret1_x_vol"]     = F["ret_1d"] * F["vol_ratio_5"]
    F["cci5_x_force"]   = _zscore(F["cci_5"], 50) * F["force_idx_z"]
    # Close position × volume: closed at top on high volume → reversal
    F["clpos_x_vol"]    = F["close_pos_hl"] * F["vol_ratio_5"]

    F.replace([np.inf, -np.inf], np.nan, inplace=True)
    print(f"    {F.shape[1]} features built")
    return F


# ─────────────────────────────────────────────────────────────
# MODEL BUILDING
# ─────────────────────────────────────────────────────────────

def make_base_models():
    """Return list of (name, model) tuples for the stacking ensemble."""
    gbm = GradientBoostingClassifier(
        n_estimators=300, max_depth=3, learning_rate=0.05,
        subsample=0.8, min_samples_leaf=15, random_state=42
    )
    rf  = RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=15,
        max_features="sqrt", random_state=42, n_jobs=-1
    )
    et  = ExtraTreesClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=15,
        max_features="sqrt", random_state=42, n_jobs=-1
    )
    lr  = LogisticRegression(C=0.1, max_iter=1000, solver="lbfgs",
                             random_state=42)
    return [("gbm", gbm), ("rf", rf), ("et", et), ("lr", lr)]


def make_stacking_model(base_models):
    meta = LogisticRegression(C=1.0, max_iter=500, random_state=42)
    stack = StackingClassifier(
        estimators=base_models,
        final_estimator=meta,
        cv=5,
        stack_method="predict_proba",
        n_jobs=-1,
        passthrough=False,
    )
    return stack


# ─────────────────────────────────────────────────────────────
# WALK-FORWARD EVALUATION
# ─────────────────────────────────────────────────────────────

def walk_forward(feat: pd.DataFrame, target: pd.Series,
                 n_horizon: int,
                 train_pct: float = 0.60,
                 n_folds: int = 8,
                 top_k_feats: int = 25) -> dict:
    """
    Strict walk-forward: train on expanding window, test on next block.
    Returns predictions, probabilities, and per-fold metrics.
    """
    print(f"\n  [n={n_horizon}d] Walk-forward evaluation …")

    # Align and clean
    df_all = pd.concat([feat, target], axis=1).dropna(subset=[target.name])
    df_all = df_all.ffill().dropna()
    feat_cols = [c for c in df_all.columns if c != target.name]
    X_all = df_all[feat_cols].values
    y_all = df_all[target.name].values
    idx   = df_all.index
    N     = len(df_all)

    init_train = int(N * train_pct)
    fold_size  = (N - init_train) // n_folds

    all_preds   = np.full(N, np.nan)
    all_probs   = np.full(N, np.nan)
    fold_accs   = []
    fold_aucs   = []
    feature_importances = np.zeros(len(feat_cols))

    for fold in range(n_folds):
        tr_end  = init_train + fold * fold_size
        te_start= tr_end
        te_end  = min(te_start + fold_size, N - n_horizon)
        if te_start >= te_end:
            break

        X_tr, y_tr = X_all[:tr_end], y_all[:tr_end]
        X_te, y_te = X_all[te_start:te_end], y_all[te_start:te_end]

        if len(np.unique(y_tr)) < 2:
            continue

        # Scale
        scaler = RobustScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        # Feature selection (ANOVA F-test on training set)
        selector = SelectKBest(f_classif, k=min(top_k_feats, X_tr_s.shape[1]))
        X_tr_sel = selector.fit_transform(X_tr_s, y_tr)
        X_te_sel = selector.transform(X_te_s)

        # Base models with calibration
        base_models = make_base_models()
        cal_models  = []
        for name, mdl in base_models:
            cal = CalibratedClassifierCV(mdl, cv=3, method="isotonic")
            cal.fit(X_tr_sel, y_tr)
            cal_models.append((name, cal))

        # Stacking meta-model
        meta_X_tr = np.column_stack([m.predict_proba(X_tr_sel)[:,1]
                                     for _, m in cal_models])
        meta_X_te = np.column_stack([m.predict_proba(X_te_sel)[:,1]
                                     for _, m in cal_models])
        meta_lr   = LogisticRegression(C=1.0, max_iter=500, random_state=42)
        meta_lr.fit(meta_X_tr, y_tr)

        probs = meta_lr.predict_proba(meta_X_te)[:,1]
        preds = (probs >= 0.5).astype(int)

        all_probs[te_start:te_end] = probs
        all_preds[te_start:te_end] = preds

        acc = accuracy_score(y_te, preds) * 100
        try:
            auc = roc_auc_score(y_te, probs)
        except Exception:
            auc = 0.5
        fold_accs.append(acc)
        fold_aucs.append(auc)

        # Aggregate feature importances from GBM
        gbm_cal = cal_models[0][1]
        if hasattr(gbm_cal.calibrated_classifiers_[0].estimator, "feature_importances_"):
            imp = gbm_cal.calibrated_classifiers_[0].estimator.feature_importances_
            selected_mask = selector.get_support()
            feature_importances[selected_mask] += imp

        print(f"    Fold {fold+1}/{n_folds}: "
              f"train={tr_end}d, test={fold_size}d  "
              f"acc={acc:.1f}%  AUC={auc:.3f}")

    # Overall OOS stats
    valid_mask = ~np.isnan(all_probs)
    oos_acc  = accuracy_score(y_all[valid_mask], all_preds[valid_mask]) * 100
    oos_auc  = roc_auc_score(y_all[valid_mask], all_probs[valid_mask])

    # High-confidence accuracy (only when |prob - 0.5| >= threshold)
    conf_results = {}
    for conf_thresh in [0.55, 0.60, 0.65, 0.70]:
        high_conf = valid_mask & (np.abs(all_probs - 0.5) >= (conf_thresh - 0.5))
        if high_conf.sum() > 20:
            acc_hc = accuracy_score(y_all[high_conf], all_preds[high_conf]) * 100
            conf_results[conf_thresh] = {
                "accuracy"   : round(acc_hc, 2),
                "n_signals"  : int(high_conf.sum()),
                "coverage_pct": round(high_conf.sum() / valid_mask.sum() * 100, 1),
            }

    # Normalize feature importances
    if feature_importances.sum() > 0:
        feature_importances /= feature_importances.sum()
    feat_imp_df = pd.DataFrame({
        "feature"   : feat_cols,
        "importance": feature_importances
    }).sort_values("importance", ascending=False)

    return {
        "n_horizon"     : n_horizon,
        "oos_accuracy"  : round(oos_acc, 2),
        "oos_auc"       : round(oos_auc, 4),
        "fold_accs"     : [round(a,2) for a in fold_accs],
        "fold_aucs"     : [round(a,4) for a in fold_aucs],
        "mean_fold_acc" : round(float(np.mean(fold_accs)),2),
        "std_fold_acc"  : round(float(np.std(fold_accs)),2),
        "high_conf"     : conf_results,
        "feat_imp"      : feat_imp_df,
        "probs"         : all_probs,
        "preds"         : all_preds,
        "y_true"        : y_all,
        "index"         : idx,
    }


# ─────────────────────────────────────────────────────────────
# STRATEGY BACKTEST
# ─────────────────────────────────────────────────────────────

def backtest_strategy(df: pd.DataFrame, result: dict,
                      conf_thresh: float = 0.60) -> dict:
    """
    Simulate: go long when prob > conf_thresh, short when prob < (1-conf_thresh).
    Hold for n days.  Measure compounded equity curve.
    """
    n        = result["n_horizon"]
    idx      = result["index"]
    probs    = pd.Series(result["probs"], index=idx)
    fwd_ret  = df["Close"].pct_change(n).shift(-n).reindex(idx)

    long_mask  = probs >  conf_thresh
    short_mask = probs < (1 - conf_thresh)
    neutral    = ~(long_mask | short_mask)

    sig = pd.Series(0.0, index=idx)
    sig[long_mask]  =  1.0
    sig[short_mask] = -1.0

    strat_ret = (sig * fwd_ret).dropna()
    bh_ret    = fwd_ret.dropna()
    ann       = 252 / n

    active = strat_ret[sig[strat_ret.index] != 0]
    n_long  = int(long_mask.sum())
    n_short = int(short_mask.sum())

    # Equity curves
    equity_strat = (1 + strat_ret).cumprod()
    equity_bh    = (1 + bh_ret).cumprod()

    def sharpe(r):
        return r.mean() / (r.std() + 1e-9) * np.sqrt(ann)

    def max_dd(r):
        eq = (1 + r).cumprod()
        return ((eq - eq.cummax()) / eq.cummax()).min() * 100

    return {
        "conf_threshold"  : conf_thresh,
        "n_long"          : n_long,
        "n_short"         : n_short,
        "n_neutral"       : int(neutral.sum()),
        "coverage_pct"    : round((n_long + n_short) / max(len(probs.dropna()), 1) * 100, 1),
        "win_rate_pct"    : round((active > 0).mean() * 100, 2) if len(active) else 0,
        "avg_ret_pct"     : round(active.mean() * 100, 3) if len(active) else 0,
        "strategy_sharpe" : round(sharpe(strat_ret), 3),
        "bh_sharpe"       : round(sharpe(bh_ret), 3),
        "max_dd_pct"      : round(max_dd(strat_ret), 2),
        "equity_strat"    : equity_strat,
        "equity_bh"       : equity_bh,
    }


# ─────────────────────────────────────────────────────────────
# CHARTS
# ─────────────────────────────────────────────────────────────

def make_charts(df, results, bt_results):
    print("\n  Generating charts …")
    C1, C2 = "#1565C0", "#C62828"

    for n in [1, 2]:
        res = results[n]
        bt  = bt_results[n]
        probs  = pd.Series(res["probs"], index=res["index"])
        y_true = res["y_true"]
        valid  = ~np.isnan(res["probs"])
        idx_v  = res["index"][valid]

        # ── Accuracy × confidence ─────────────────────────────
        thresholds = np.arange(0.50, 0.76, 0.01)
        acc_by_thresh  = []
        cov_by_thresh  = []
        for t in thresholds:
            mask = np.abs(res["probs"] - 0.5) >= (t - 0.5)
            mask &= valid
            if mask.sum() < 10:
                acc_by_thresh.append(np.nan)
                cov_by_thresh.append(0)
            else:
                acc_by_thresh.append(accuracy_score(y_true[mask], res["preds"][mask]) * 100)
                cov_by_thresh.append(mask.sum() / valid.sum() * 100)

        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.suptitle(f"NIFTY Direction Predictor — n={n} day(s) ahead", fontsize=14)

        # ── Panel 1: Accuracy vs confidence threshold ─────────
        ax = axes[0, 0]
        ax2 = ax.twinx()
        ax.plot(thresholds, acc_by_thresh, color=C1, lw=2, marker="o", ms=4)
        ax2.bar(thresholds, cov_by_thresh, width=0.008, alpha=0.3, color="grey")
        ax.axhline(50, ls="--", c="grey", lw=1)
        ax.axhline(55, ls=":", c="green", lw=1, label="55% threshold")
        ax.axhline(60, ls=":", c="orange", lw=1, label="60% threshold")
        ax.set_xlabel("Confidence Threshold (≥ x → take signal)")
        ax.set_ylabel("Accuracy (%)", color=C1)
        ax2.set_ylabel("Coverage (% of days)", color="grey")
        ax.set_title(f"Accuracy vs Confidence Threshold  (n={n}d)")
        ax.legend(fontsize=8)
        ax.set_ylim(45, 80)

        # ── Panel 2: Fold accuracy ─────────────────────────────
        ax = axes[0, 1]
        folds = range(1, len(res["fold_accs"]) + 1)
        colors_f = [C1 if a >= 50 else C2 for a in res["fold_accs"]]
        bars = ax.bar(folds, res["fold_accs"], color=colors_f, alpha=0.85)
        ax.axhline(50, ls="--", c="grey", lw=1)
        for bar, a in zip(bars, res["fold_accs"]):
            ax.text(bar.get_x()+bar.get_width()/2, a+0.3, f"{a:.1f}%",
                    ha="center", fontsize=8)
        ax.set_title(f"Per-Fold OOS Accuracy  (n={n}d)\n"
                     f"Overall: {res['oos_accuracy']:.1f}%  AUC: {res['oos_auc']:.3f}")
        ax.set_xlabel("Fold"); ax.set_ylabel("Accuracy (%)")
        ax.set_ylim(40, max(res["fold_accs"])+5)

        # ── Panel 3: Probability distribution ─────────────────
        ax = axes[1, 0]
        probs_v = res["probs"][valid]
        y_v     = y_true[valid]
        ax.hist(probs_v[y_v==1], bins=30, alpha=0.6, color=C1, label="Actual UP")
        ax.hist(probs_v[y_v==0], bins=30, alpha=0.6, color=C2, label="Actual DOWN")
        ax.axvline(0.5, c="k", lw=1, ls="--")
        ax.set_xlabel("Predicted Probability (UP)")
        ax.set_ylabel("Count")
        ax.set_title(f"Probability Distribution  (n={n}d)")
        ax.legend()

        # ── Panel 4: Equity curve ─────────────────────────────
        ax = axes[1, 1]
        eq_s = bt["equity_strat"].dropna()
        eq_b = bt["equity_bh"].dropna()
        ax.plot(eq_s.index, eq_s.values, color=C1, lw=1.5,
                label=f"Strategy (conf>{bt['conf_threshold']:.0%})")
        ax.plot(eq_b.index, eq_b.values, color="grey", lw=1, ls="--",
                label="Buy & Hold")
        ax.set_title(f"Equity Curve  (n={n}d, coverage={bt['coverage_pct']:.0f}%)")
        ax.set_ylabel("Equity (₹1 start)")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

        plt.tight_layout()
        p = os.path.join(OUT_DIR, f"D{n}_analysis.png")
        plt.savefig(p, dpi=130)
        plt.close()
        print(f"    Saved {p}")

    # ── Feature importance comparison (n=1 vs n=2) ────────────
    fig, axes = plt.subplots(1, 2, figsize=(16, 8))
    for ax, n, color in zip(axes, [1, 2], [C1, C2]):
        fi = results[n]["feat_imp"].head(20)
        ax.barh(fi["feature"][::-1], fi["importance"][::-1],
                color=color, alpha=0.85)
        ax.set_title(f"Feature Importance — n={n} day(s) ahead", fontsize=11)
        ax.set_xlabel("Relative Importance (aggregated GBM)")
        ax.tick_params(axis="y", labelsize=8)
    plt.suptitle("What drives next-day and 2-day NIFTY direction?", fontsize=13)
    plt.tight_layout()
    p = os.path.join(OUT_DIR, "D_feature_importance.png")
    plt.savefig(p, dpi=130)
    plt.close()
    print(f"    Saved {p}")

    # ── Side-by-side summary ───────────────────────────────────
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    metrics_n1 = {"OOS Acc": results[1]["oos_accuracy"],
                  "AUC×100": results[1]["oos_auc"]*100,
                  "WinRate": bt_results[1]["win_rate_pct"],
                  "Sharpe×10": bt_results[1]["strategy_sharpe"]*10}
    metrics_n2 = {"OOS Acc": results[2]["oos_accuracy"],
                  "AUC×100": results[2]["oos_auc"]*100,
                  "WinRate": bt_results[2]["win_rate_pct"],
                  "Sharpe×10": bt_results[2]["strategy_sharpe"]*10}

    x = np.arange(len(metrics_n1))
    w = 0.35
    axes[0].bar(x - w/2, list(metrics_n1.values()), w, label="n=1d", color=C1, alpha=0.85)
    axes[0].bar(x + w/2, list(metrics_n2.values()), w, label="n=2d", color=C2, alpha=0.85)
    axes[0].set_xticks(x); axes[0].set_xticklabels(list(metrics_n1.keys()), fontsize=9)
    axes[0].axhline(50, ls="--", c="grey", lw=1)
    axes[0].set_title("Key Metrics Comparison")
    axes[0].legend()

    # High-confidence accuracy curve for both n
    for ax, n, color in zip(axes[1:], [1, 2], [C1, C2]):
        thresholds = np.arange(0.50, 0.76, 0.01)
        acc_vals, cov_vals = [], []
        for t in thresholds:
            mask = np.abs(results[n]["probs"] - 0.5) >= (t - 0.5)
            mask &= ~np.isnan(results[n]["probs"])
            if mask.sum() < 5:
                acc_vals.append(np.nan); cov_vals.append(0)
            else:
                acc_vals.append(accuracy_score(results[n]["y_true"][mask],
                                               results[n]["preds"][mask]) * 100)
                cov_vals.append(mask.sum() / (~np.isnan(results[n]["probs"])).sum() * 100)
        ax2 = ax.twinx()
        ax.plot(thresholds, acc_vals, color=color, lw=2)
        ax2.fill_between(thresholds, cov_vals, alpha=0.15, color="grey")
        ax.axhline(50, ls="--", c="grey", lw=0.8)
        ax.axhline(60, ls=":", c="green", lw=1)
        ax.set_ylim(44, 80)
        ax.set_xlabel("Confidence threshold")
        ax.set_ylabel("Accuracy (%)", color=color)
        ax2.set_ylabel("Coverage %", color="grey")
        ax.set_title(f"n={n}d: Accuracy vs Confidence")
    plt.tight_layout()
    p = os.path.join(OUT_DIR, "D_summary.png")
    plt.savefig(p, dpi=130)
    plt.close()
    print(f"    Saved {p}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  NIFTY 50 — HIGH-ACCURACY DIRECTION PREDICTOR (n=1d, n=2d)")
    print("=" * 65)

    # ── Data ────────────────────────────────────────────────
    print("\n[1/5] Loading data …")
    df   = generate_nifty_ohlcv(seed=42)
    print(f"    {len(df)} days  {df.index[0].date()} → {df.index[-1].date()}")

    # ── Features ─────────────────────────────────────────────
    print("\n[2/5] Engineering features …")
    feat = build_features(df)

    # ── Targets ──────────────────────────────────────────────
    up_1d = (df["Close"].shift(-1) > df["Close"]).astype(int).rename("up_1d")
    up_2d = (df["Close"].shift(-2) > df["Close"]).astype(int).rename("up_2d")

    # ── Walk-forward evaluation ───────────────────────────────
    print("\n[3/5] Running walk-forward evaluation …")
    results = {}
    for tgt, n in [(up_1d, 1), (up_2d, 2)]:
        results[n] = walk_forward(feat, tgt, n_horizon=n)

    # ── Backtest with confidence filter ──────────────────────
    print("\n[4/5] Backtesting strategy (confidence ≥ 60%) …")
    bt_results = {}
    for n in [1, 2]:
        bt_results[n] = backtest_strategy(df, results[n], conf_thresh=0.60)

    # ── Charts ───────────────────────────────────────────────
    print("\n[5/5] Generating charts …")
    make_charts(df, results, bt_results)

    # ── Save JSON ────────────────────────────────────────────
    report = {}
    for n in [1, 2]:
        res = results[n]
        bt  = bt_results[n]
        hc  = res["high_conf"]
        report[f"n{n}d"] = {
            "oos_accuracy_pct"  : res["oos_accuracy"],
            "oos_auc"           : res["oos_auc"],
            "fold_accuracies"   : res["fold_accs"],
            "mean_fold_acc"     : res["mean_fold_acc"],
            "std_fold_acc"      : res["std_fold_acc"],
            "high_confidence"   : hc,
            "top15_features"    : res["feat_imp"].head(15)[["feature","importance"]]\
                                      .assign(importance=lambda d: d["importance"].round(5))\
                                      .to_dict("records"),
            "backtest_60pct"    : {k:v for k,v in bt.items()
                                   if not isinstance(v, pd.Series)},
        }
    with open(os.path.join(OUT_DIR, "direction_report.json"), "w") as f:
        json.dump(report, f, indent=2)

    # ── Console report ────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  FINAL RESULTS")
    print("=" * 65)

    for n in [1, 2]:
        res = results[n]
        bt  = bt_results[n]
        print(f"\n  ━━━ n = {n} DAY{'S' if n>1 else ' '} AHEAD ━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"  Overall OOS Accuracy : {res['oos_accuracy']:.1f}%  "
              f"(AUC = {res['oos_auc']:.3f})")
        print(f"  Per-fold accuracies  : {res['fold_accs']}")
        print(f"  Mean ± Std           : {res['mean_fold_acc']:.1f}% ± {res['std_fold_acc']:.1f}%")
        print()
        print("  HIGH-CONFIDENCE ACCURACY (only trade when model is sure):")
        for thr, hc in res["high_conf"].items():
            print(f"    Confidence ≥ {thr:.0%}  →  "
                  f"Accuracy = {hc['accuracy']:.1f}%  "
                  f"({hc['n_signals']} signals, "
                  f"{hc['coverage_pct']:.1f}% of days)")
        print()
        print(f"  BACKTEST (confidence ≥ 60%):")
        print(f"    Signals: {bt['n_long']} long, {bt['n_short']} short "
              f"({bt['coverage_pct']:.0f}% of days)")
        print(f"    Win rate    : {bt['win_rate_pct']:.1f}%")
        print(f"    Avg return  : {bt['avg_ret_pct']:+.2f}% per trade")
        print(f"    Sharpe      : {bt['strategy_sharpe']:.2f}  "
              f"(BH = {bt['bh_sharpe']:.2f})")
        print(f"    Max drawdown: {bt['max_dd_pct']:.1f}%")
        print()
        print(f"  TOP 10 FEATURES:")
        for _, row in res["feat_imp"].head(10).iterrows():
            print(f"    {row['feature']:<28}  {row['importance']:.4f}")

    print("\n" + "=" * 65)
    print(f"  Outputs → {OUT_DIR}/")
    print("=" * 65)
    return report


if __name__ == "__main__":
    main()
