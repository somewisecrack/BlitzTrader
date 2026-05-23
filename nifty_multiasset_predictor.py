"""
NIFTY 50 Multi-Asset Direction Predictor
==========================================
Predicts: will NIFTY close HIGHER or LOWER than today?
  Target 1: tomorrow's close (n=1)
  Target 2: day-after-tomorrow's close (n=2)

Cross-asset features used:
  ^GSPC      S&P 500        — closes AFTER NIFTY → true leading indicator
  ^VIX       CBOE VIX       — fear gauge (inverse)
  INR=X      USD/INR        — currency (weak rupee → FII outflows → NIFTY down)
  ^NSEBANK   Bank Nifty     — high-beta NIFTY twin
  RELIANCE   Largest stock  — ~10% NIFTY weight
  HDFCBANK   Largest bank   — ~12% NIFTY weight
  SBIN       PSU bank       — rate-sensitive
  CL=F       Crude oil      — India is importer (oil up → inflationary)
  GC=F       Gold           — risk-off asset
  ^N225      Nikkei 225     — Asian session (closes ~5h before NIFTY)
  ^HSI       Hang Seng      — Asian session

Model: XGBoost + LightGBM + RandomForest + LogisticRegression → stacked meta-model
       with probability calibration, regime-adaptive weighting, and confidence filter.
"""

import warnings
warnings.filterwarnings("ignore")

import os, sys, json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from scipy import stats
from scipy.linalg import cholesky

import xgboost as xgb
import lightgbm as lgb
from sklearn.ensemble import RandomForestClassifier, VotingClassifier
from sklearn.linear_model import LogisticRegression, RidgeClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.preprocessing import RobustScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, roc_auc_score, brier_score_loss
from sklearn.feature_selection import mutual_info_classif

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_asset_generator import generate_multi_asset

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nifty_multi_output")
os.makedirs(OUT, exist_ok=True)

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
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(1)
    return tr.ewm(span=n, adjust=False).mean()
def _stoch_k(h, l, c, k):
    return 100*(c - l.rolling(k).min()) / (h.rolling(k).max() - l.rolling(k).min() + 1e-9)
def _cci(h, l, c, n):
    tp = (h+l+c)/3
    return (tp - _sma(tp, n)) / (
        0.015 * tp.rolling(n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True) + 1e-9)
def _zscore(s, n):
    return (s - s.rolling(n).mean()) / (s.rolling(n).std() + 1e-9)
def _obv(c, v):
    return (np.sign(c.diff().fillna(0)) * v.astype(float)).cumsum()
def _hurst(ts, max_lag=20):
    """Rolling Hurst exponent (>0.5=trending, <0.5=mean-reverting)."""
    lags = range(2, max_lag)
    tau  = [ts.diff(lag).std() for lag in lags]
    if 0 in tau or np.nan in tau:
        return 0.5
    reg = np.polyfit(np.log(lags), np.log(tau), 1)
    return reg[0]
def _rolling_hurst(c, window=40, max_lag=10):
    return c.rolling(window).apply(
        lambda x: _hurst(pd.Series(x), max_lag), raw=False)
def _cmf(h, l, c, v, n):
    clv = ((c-l)-(h-c))/(h-l+1e-9)
    return (clv*v).rolling(n).sum()/v.rolling(n).sum()


# ─────────────────────────────────────────────────────────────
# FEATURE ENGINEERING
# ─────────────────────────────────────────────────────────────

def build_features(prices: pd.DataFrame, nifty_ohlcv: pd.DataFrame) -> pd.DataFrame:
    """
    Build all features.  prices = multi-asset close DataFrame.
    nifty_ohlcv = NIFTY OHLCV for intraday/volume features.
    """
    print("  [a] NIFTY price-action features …")
    c = prices["^NSEI"]
    h = nifty_ohlcv["High"].reindex(prices.index).ffill()
    l = nifty_ohlcv["Low"].reindex(prices.index).ffill()
    v = nifty_ohlcv["Volume"].reindex(prices.index).ffill()
    o = nifty_ohlcv["Open"].reindex(prices.index).ffill()
    F = pd.DataFrame(index=prices.index)

    # ── A. NIFTY own price features ───────────────────────────
    F["nifty_ret1"]      = c.pct_change(1)
    F["nifty_ret2"]      = c.pct_change(2)
    F["nifty_ret3"]      = c.pct_change(3)
    F["nifty_ret5"]      = c.pct_change(5)
    F["nifty_ret10"]     = c.pct_change(10)
    F["nifty_rsi2"]      = _rsi(c, 2)
    F["nifty_rsi3"]      = _rsi(c, 3)
    F["nifty_rsi5"]      = _rsi(c, 5)
    F["nifty_rsi14"]     = _rsi(c, 14)
    F["nifty_rsi2_z20"]  = _zscore(F["nifty_rsi2"], 20)
    F["nifty_cci5"]      = _cci(h, l, c, 5)
    F["nifty_cci10"]     = _cci(h, l, c, 10)
    F["nifty_cci20"]     = _cci(h, l, c, 20)
    F["nifty_stoch5"]    = _stoch_k(h, l, c, 5)
    F["nifty_stoch14"]   = _stoch_k(h, l, c, 14)
    F["nifty_willr10"]   = -100*(h.rolling(10).max()-c)/(h.rolling(10).max()-l.rolling(10).min()+1e-9)
    # Candle features
    F["nifty_close_pos"] = (c - l) / (h - l + 1e-9)         # close position in H-L
    F["nifty_body"]      = (c - o) / (h - l + 1e-9)
    F["nifty_upper_wick"]= (h - pd.concat([c,o],axis=1).max(axis=1)) / (h-l+1e-9)
    F["nifty_lower_wick"]= (pd.concat([c,o],axis=1).min(axis=1) - l) / (h-l+1e-9)
    F["nifty_gap"]       = (o - c.shift(1)) / c.shift(1)
    F["nifty_range_atr"] = (h-l) / _atr(h,l,c,10)
    # MA distances
    for n in [5, 10, 20, 50]:
        F[f"nifty_vs_ema{n}"] = c / _ema(c, n) - 1
    F["nifty_ema5_13"]   = _ema(c,5) / _ema(c,13) - 1
    F["nifty_ema8_21"]   = _ema(c,8) / _ema(c,21) - 1
    # Bollinger
    ma20 = _sma(c,20); sd20 = c.rolling(20).std()
    F["nifty_bb_pct"]    = (c - (ma20 - 2*sd20)) / (4*sd20 + 1e-9)
    F["nifty_bb_width"]  = 4*sd20 / ma20
    # Volume
    vf = v.astype(float)
    F["nifty_vol_ratio5"] = vf / _sma(vf,5)
    F["nifty_vol_ratio20"]= vf / _sma(vf,20)
    F["nifty_cmf10"]     = _cmf(h, l, c, v, 10)
    F["nifty_obv_z20"]   = _zscore(_obv(c,v), 20)
    F["nifty_force_z"]   = _zscore(c.diff()*vf, 20)
    # Volatility
    F["nifty_hv5"]       = c.pct_change().rolling(5).std()*np.sqrt(252)
    F["nifty_hv10"]      = c.pct_change().rolling(10).std()*np.sqrt(252)
    F["nifty_vol_regime"]= F["nifty_hv5"] / (F["nifty_hv10"] + 1e-9)
    # Price position
    F["nifty_pct5h"]     = c / h.rolling(5).max() - 1
    F["nifty_pct10h"]    = c / h.rolling(10).max() - 1
    F["nifty_pct5l"]     = c / l.rolling(5).min() - 1
    F["nifty_pct52wh"]   = c / h.rolling(252).max() - 1
    # Streaks
    F["nifty_up3"]       = (c.diff() > 0).rolling(3).sum()
    F["nifty_dn3"]       = (c.diff() < 0).rolling(3).sum()
    F["nifty_consec"]    = F["nifty_up3"] - F["nifty_dn3"]
    # Mean-reversion z-scores
    F["nifty_ret1_z5"]   = _zscore(F["nifty_ret1"], 5)
    F["nifty_ret1_z20"]  = _zscore(F["nifty_ret1"], 20)
    F["nifty_ret3_z20"]  = _zscore(F["nifty_ret3"], 20)
    # Hurst (is market mean-reverting or trending?) — slower to compute
    print("  [b] Hurst exponent …")
    F["nifty_hurst"]     = _rolling_hurst(c, window=40, max_lag=8)

    # ── B. S&P 500 features  ★ TRUE LEADING INDICATOR ★ ───────
    # SPX closes ~8h AFTER NIFTY → SPX return on day t is known
    # BEFORE NIFTY opens on day t+1. So shift(-1) gives the signal
    # for TOMORROW (we use lag=0 i.e. same-day SPX because it's known
    # before next NIFTY session opens).
    print("  [c] S&P 500 leading features …")
    spx = prices["^GSPC"]
    F["spx_ret1"]        = spx.pct_change(1)          # today's SPX → predicts tomorrow NIFTY
    F["spx_ret2"]        = spx.pct_change(2)
    F["spx_ret5"]        = spx.pct_change(5)
    F["spx_rsi5"]        = _rsi(spx, 5)
    F["spx_rsi14"]       = _rsi(spx, 14)
    F["spx_vs_ema10"]    = spx / _ema(spx, 10) - 1
    F["spx_vs_ema20"]    = spx / _ema(spx, 20) - 1
    F["spx_ret1_z20"]    = _zscore(F["spx_ret1"], 20)
    F["spx_cci10"]       = _cci(spx, spx*1.01, spx*0.99, 10)  # approx for index
    F["spx_up3"]         = (spx.diff() > 0).rolling(3).sum()
    # SPX direction: strong binary signal
    F["spx_bull_day"]    = (F["spx_ret1"] > 0.005).astype(int)   # SPX up >0.5%
    F["spx_bear_day"]    = (F["spx_ret1"] < -0.005).astype(int)  # SPX dn >0.5%
    F["spx_big_move"]    = F["spx_ret1"].abs() > 0.01             # SPX moved >1%

    # ── C. VIX features ───────────────────────────────────────
    print("  [d] VIX / fear gauge features …")
    vix = prices["^VIX"]
    F["vix_level"]       = vix
    F["vix_chg1"]        = vix.diff(1)
    F["vix_chg5"]        = vix.diff(5)
    F["vix_z20"]         = _zscore(vix, 20)
    F["vix_z252"]        = _zscore(vix, 252)
    F["vix_above20"]     = (vix > 20).astype(int)
    F["vix_above25"]     = (vix > 25).astype(int)
    F["vix_spike"]       = (F["vix_chg1"] > 2).astype(int)         # VIX jumped >2pts
    F["vix_crush"]       = (F["vix_chg1"] < -2).astype(int)        # VIX crushed
    F["vix_vs_ma20"]     = vix / _sma(vix, 20) - 1

    # ── D. USD/INR features ───────────────────────────────────
    print("  [e] USD/INR features …")
    usd = prices["INR=X"]
    F["inr_ret1"]        = usd.pct_change(1)
    F["inr_ret5"]        = usd.pct_change(5)
    F["inr_z20"]         = _zscore(usd.pct_change(), 20)
    F["inr_vs_ema10"]    = usd / _ema(usd, 10) - 1
    F["inr_depreciation"]= (F["inr_ret1"] > 0.002).astype(int)    # rupee weakening
    F["inr_appreciation"]= (F["inr_ret1"] < -0.002).astype(int)   # rupee strengthening

    # ── E. Bank Nifty features ────────────────────────────────
    print("  [f] Bank Nifty features …")
    bnk = prices["^NSEBANK"]
    F["bnk_ret1"]        = bnk.pct_change(1)
    F["bnk_ret5"]        = bnk.pct_change(5)
    F["bnk_rsi5"]        = _rsi(bnk, 5)
    F["bnk_rsi14"]       = _rsi(bnk, 14)
    F["bnk_vs_nifty"]    = bnk.pct_change(1) - c.pct_change(1)    # Bank Nifty outperform
    F["bnk_vs_ema10"]    = bnk / _ema(bnk, 10) - 1
    F["bnk_cci10"]       = _cci(bnk*1.01, bnk*0.99, bnk, 10)

    # ── F. RELIANCE features ──────────────────────────────────
    rel = prices["RELIANCE.NS"]
    F["rel_ret1"]        = rel.pct_change(1)
    F["rel_ret3"]        = rel.pct_change(3)
    F["rel_rsi14"]       = _rsi(rel, 14)
    F["rel_vs_ema20"]    = rel / _ema(rel, 20) - 1
    F["rel_vs_nifty"]    = rel.pct_change(1) - c.pct_change(1)

    # ── G. HDFCBANK features ──────────────────────────────────
    hdfc = prices["HDFCBANK.NS"]
    F["hdfc_ret1"]       = hdfc.pct_change(1)
    F["hdfc_ret3"]       = hdfc.pct_change(3)
    F["hdfc_rsi14"]      = _rsi(hdfc, 14)
    F["hdfc_vs_ema20"]   = hdfc / _ema(hdfc, 20) - 1

    # ── H. SBI features ───────────────────────────────────────
    sbi = prices["SBIN.NS"]
    F["sbi_ret1"]        = sbi.pct_change(1)
    F["sbi_vs_ema10"]    = sbi / _ema(sbi, 10) - 1
    F["sbi_vs_nifty"]    = sbi.pct_change(1) - c.pct_change(1)

    # ── I. Crude oil features ─────────────────────────────────
    crude = prices["CL=F"]
    F["crude_ret1"]      = crude.pct_change(1)
    F["crude_ret5"]      = crude.pct_change(5)
    F["crude_z20"]       = _zscore(crude.pct_change(), 20)
    F["crude_spike"]     = (F["crude_ret1"] > 0.03).astype(int)   # crude up >3%
    F["crude_crash"]     = (F["crude_ret1"] < -0.03).astype(int)  # crude dn >3%

    # ── J. Gold features ──────────────────────────────────────
    gold = prices["GC=F"]
    F["gold_ret1"]       = gold.pct_change(1)
    F["gold_ret5"]       = gold.pct_change(5)
    F["gold_z20"]        = _zscore(gold.pct_change(), 20)
    F["gold_rush"]       = (F["gold_ret1"] > 0.01).astype(int)    # gold up >1% = risk-off

    # ── K. Asian markets (Nikkei, Hang Seng) ─────────────────
    n225 = prices["^N225"]
    hsi  = prices["^HSI"]
    F["n225_ret1"]       = n225.pct_change(1)
    F["n225_ret5"]       = n225.pct_change(5)
    F["n225_vs_ema10"]   = n225 / _ema(n225, 10) - 1
    F["hsi_ret1"]        = hsi.pct_change(1)
    F["hsi_ret5"]        = hsi.pct_change(5)
    # Asian markets composite: avg of Nikkei + HSI
    F["asia_ret1"]       = (F["n225_ret1"] + F["hsi_ret1"]) / 2
    F["asia_bull"]       = ((F["n225_ret1"] > 0) & (F["hsi_ret1"] > 0)).astype(int)
    F["asia_bear"]       = ((F["n225_ret1"] < 0) & (F["hsi_ret1"] < 0)).astype(int)

    # ── L. CROSS-ASSET COMPOSITE SIGNALS ─────────────────────
    print("  [g] Cross-asset composite signals …")
    # How many assets are bullish right now?
    asset_bulls = pd.DataFrame({
        "spx": (F["spx_ret1"] > 0).astype(int),
        "bnk": (F["bnk_ret1"] > 0).astype(int),
        "rel": (F["rel_ret1"] > 0).astype(int),
        "hdfc":(F["hdfc_ret1"] > 0).astype(int),
        "n225":(F["n225_ret1"] > 0).astype(int),
        "hsi": (F["hsi_ret1"] > 0).astype(int),
        "gold":(F["gold_ret1"] < 0).astype(int),   # gold down = risk-on
        "vix": (F["vix_chg1"]  < 0).astype(int),   # VIX down = risk-on
        "inr": (F["inr_ret1"]  < 0).astype(int),   # INR appreciate = bullish
        "crude":(F["crude_ret1"] < 0).astype(int),  # crude down = good for India
    })
    F["breadth_score"]   = asset_bulls.sum(axis=1)  # 0-10, high=risk-on
    F["breadth_z10"]     = _zscore(F["breadth_score"].astype(float), 10)

    # Global risk-on / risk-off signal (stronger composite)
    F["risk_on_signal"]  = (
        F["spx_ret1"] * 0.35
        + F["bnk_ret1"] * 0.20
        - F["inr_ret1"] * 0.20    # negative: USDINR up = bearish
        - F["vix_z20"] * 0.015    # VIX z-score: high VIX = bearish
        + F["asia_ret1"] * 0.15
        - F["crude_ret1"] * 0.10  # crude up = bearish for India
        - F["gold_ret1"] * 0.05   # gold up = risk-off = bearish
    )
    F["risk_on_z10"]     = _zscore(F["risk_on_signal"], 10)

    # SPX × VIX interaction: SPX up + VIX down = strongest bull signal
    F["spx_vix_signal"]  = F["spx_ret1"] * (-F["vix_chg1"])
    F["spx_inr_signal"]  = F["spx_ret1"] * (-F["inr_ret1"])

    # Rolling correlation NIFTY-SPX (regime signal: decorrelation = caution)
    nifty_r = c.pct_change()
    spx_r   = spx.pct_change()
    F["nifty_spx_corr20"]= nifty_r.rolling(20).corr(spx_r)
    F["nifty_spx_corr5"] = nifty_r.rolling(5).corr(spx_r)

    # ── M. CALENDAR EFFECTS ───────────────────────────────────
    dt = pd.to_datetime(prices.index)
    F["day_of_week"]     = dt.dayofweek
    F["is_monday"]       = (dt.dayofweek == 0).astype(int)
    F["is_friday"]       = (dt.dayofweek == 4).astype(int)
    F["is_thursday"]     = (dt.dayofweek == 3).astype(int)  # expiry day
    F["month"]           = dt.month
    F["day_of_month"]    = dt.day
    F["near_month_end"]  = (dt.day >= 25).astype(int)
    F["near_month_start"]= (dt.day <= 5).astype(int)
    # Week of month (1-5)
    F["week_of_month"]   = (dt.day - 1) // 7 + 1
    # Last Thursday of month proxy (NIFTY monthly expiry)
    F["expiry_week"]     = ((dt.dayofweek == 3) & (dt.day >= 25)).astype(int)

    # ── N. INTERACTION FEATURES ───────────────────────────────
    # Most powerful interactions
    F["spx_x_rsi2"]      = F["spx_ret1"] * (100 - F["nifty_rsi2"]) / 100
    F["spx_x_bnk"]       = F["spx_ret1"] * F["bnk_ret1"]
    F["vix_x_rsi"]       = F["vix_z20"] * F["nifty_rsi14"] / 100
    F["inr_x_spx"]       = -F["inr_ret1"] * F["spx_ret1"]          # both bullish = strong
    F["asian_x_spx"]     = F["asia_ret1"] * F["spx_ret1"]
    F["rsi2_x_close_pos"]= F["nifty_rsi2"] * F["nifty_close_pos"] / 100
    F["consec_x_spx"]    = F["nifty_consec"] * F["spx_ret1"]

    F.replace([np.inf, -np.inf], np.nan, inplace=True)
    print(f"  → {F.shape[1]} total features")
    return F


# ─────────────────────────────────────────────────────────────
# REGIME DETECTOR
# ─────────────────────────────────────────────────────────────

def detect_regime(prices: pd.DataFrame) -> pd.Series:
    """
    Simple 3-regime detector:
      0 = BEAR  (trending down, high vol)
      1 = RANGE (mean-reverting, normal vol)
      2 = BULL  (trending up, normal/low vol)
    """
    c   = prices["^NSEI"]
    ret = c.pct_change()
    ma50  = _sma(c, 50)
    ma200 = _sma(c, 200)
    hv20  = ret.rolling(20).std() * np.sqrt(252)
    hv252 = ret.rolling(252).std() * np.sqrt(252)

    bull = ((c > ma50) & (c > ma200) & (hv20 < hv252 * 1.3))
    bear = ((c < ma50) & (c < ma200) & (hv20 > hv252 * 1.2))

    regime = pd.Series(1, index=c.index, name="regime")  # default=RANGE
    regime[bull] = 2
    regime[bear] = 0
    return regime


# ─────────────────────────────────────────────────────────────
# PURGED WALK-FORWARD EVALUATION
# ─────────────────────────────────────────────────────────────

def purged_walk_forward(feat: pd.DataFrame,
                        target: pd.Series,
                        regime: pd.Series,
                        n_horizon: int,
                        n_folds: int = 8,
                        train_pct: float = 0.60,
                        embargo: int = 5,
                        top_k: int = 40) -> dict:
    """
    Expanding-window walk-forward with:
    - Embargo period (gap between train and test)
    - Regime-specific feature scaling
    - Ensemble: XGB + LGB + RF + LR → stacked meta LR
    - Per-fold probability calibration
    """
    print(f"\n  ── n={n_horizon}d walk-forward ({n_folds} folds, embargo={embargo}d) ──")

    # Align everything
    all_idx = feat.index.intersection(target.index).intersection(regime.index)
    feat    = feat.reindex(all_idx)
    target  = target.reindex(all_idx)
    regime  = regime.reindex(all_idx)

    # Impute: forward-fill then fill remaining with 0
    feat = feat.ffill().fillna(0)

    # Feature selection via mutual information on first 60% of data
    init_n  = int(len(feat) * train_pct)
    X_init  = feat.iloc[:init_n].values
    y_init  = target.iloc[:init_n].values

    mi_scores = mutual_info_classif(X_init, y_init, random_state=42)
    top_idx   = np.argsort(mi_scores)[::-1][:top_k]
    feat_cols = feat.columns[top_idx].tolist()
    print(f"    Top feature by MI: {feat_cols[0]}  ({mi_scores[top_idx[0]]:.4f})")

    X_all = feat[feat_cols].values
    y_all = target.values
    r_all = regime.values
    N     = len(X_all)

    fold_size  = (N - init_n) // n_folds
    all_probs  = np.full(N, np.nan)
    all_preds  = np.full(N, np.nan)
    fold_stats = []
    feat_imps  = np.zeros(len(feat_cols))

    for fold in range(n_folds):
        tr_end   = init_n + fold * fold_size
        te_start = tr_end + embargo
        te_end   = min(te_start + fold_size, N - n_horizon)
        if te_start >= te_end:
            break

        X_tr, y_tr = X_all[:tr_end],        y_all[:tr_end]
        X_te, y_te = X_all[te_start:te_end], y_all[te_start:te_end]
        r_te       = r_all[te_start:te_end]

        if len(np.unique(y_tr)) < 2 or len(X_te) < 5:
            continue

        # --- Scale ---
        scaler = RobustScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        # --- Base models ---
        xgb_clf = xgb.XGBClassifier(
            n_estimators=400, max_depth=4, learning_rate=0.03,
            subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
            reg_alpha=0.1, reg_lambda=1.0,
            use_label_encoder=False, eval_metric="logloss",
            random_state=42, verbosity=0, n_jobs=-1
        )
        lgb_clf = lgb.LGBMClassifier(
            n_estimators=400, max_depth=4, learning_rate=0.03,
            num_leaves=15, subsample=0.8, colsample_bytree=0.7,
            min_child_samples=15, reg_alpha=0.1, reg_lambda=1.0,
            random_state=42, verbosity=-1, n_jobs=-1
        )
        rf_clf  = RandomForestClassifier(
            n_estimators=300, max_depth=6, min_samples_leaf=15,
            max_features="sqrt", random_state=42, n_jobs=-1
        )
        lr_clf  = LogisticRegression(C=0.05, max_iter=500,
                                     solver="lbfgs", random_state=42)

        base_clfs = [
            ("xgb", CalibratedClassifierCV(xgb_clf, cv=3, method="isotonic")),
            ("lgb", CalibratedClassifierCV(lgb_clf, cv=3, method="isotonic")),
            ("rf",  CalibratedClassifierCV(rf_clf,  cv=3, method="isotonic")),
            ("lr",  CalibratedClassifierCV(lr_clf,  cv=3, method="sigmoid")),
        ]

        # Fit base models
        for _, clf in base_clfs:
            clf.fit(X_tr_s, y_tr)

        # --- Weighted average ensemble (no stacking to avoid leakage) ---
        # Weights: XGB=0.35, LGB=0.35, RF=0.20, LR=0.10
        weights = [0.35, 0.35, 0.20, 0.10]
        base_probs = np.column_stack([clf.predict_proba(X_te_s)[:,1]
                                      for _, clf in base_clfs])
        probs = (base_probs * weights).sum(axis=1)
        preds = (probs >= 0.5).astype(int)

        all_probs[te_start:te_end] = probs
        all_preds[te_start:te_end] = preds

        acc = accuracy_score(y_te, preds) * 100
        try:
            auc = roc_auc_score(y_te, probs)
        except:
            auc = 0.5

        # Per-regime accuracy
        regime_acc = {}
        for r_val, r_name in [(0,"BEAR"),(1,"RANGE"),(2,"BULL")]:
            mask = r_te == r_val
            if mask.sum() >= 10:
                regime_acc[r_name] = round(
                    accuracy_score(y_te[mask], preds[mask])*100, 1)

        fold_stats.append({
            "fold": fold+1, "train_size": tr_end,
            "test_size": te_end-te_start,
            "accuracy": round(acc,2), "auc": round(auc,4),
            "regime_acc": regime_acc
        })

        # Feature importance from XGB (uncalibrated)
        try:
            inner_xgb = xgb.XGBClassifier(
                n_estimators=200, max_depth=4, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
                use_label_encoder=False, eval_metric="logloss",
                random_state=42, verbosity=0, n_jobs=-1
            )
            inner_xgb.fit(X_tr_s, y_tr)
            feat_imps += inner_xgb.feature_importances_
        except:
            pass

        print(f"    Fold {fold+1}/{n_folds}  train={tr_end}d  "
              f"acc={acc:.1f}%  AUC={auc:.3f}  "
              f"regime_acc={regime_acc}")

    # ── Overall OOS metrics ────────────────────────────────────
    valid = ~np.isnan(all_probs)
    oos_acc = accuracy_score(y_all[valid], all_preds[valid]) * 100
    oos_auc = roc_auc_score(y_all[valid], all_probs[valid])

    # ── High-confidence accuracy ───────────────────────────────
    hc_results = {}
    for thr in [0.52, 0.55, 0.58, 0.60, 0.63, 0.65, 0.70]:
        mask = valid & (np.abs(all_probs - 0.5) >= (thr - 0.5))
        if mask.sum() >= 50:
            hc_acc = accuracy_score(y_all[mask], all_preds[mask]) * 100
            hc_results[thr] = {
                "accuracy"    : round(hc_acc, 2),
                "n_signals"   : int(mask.sum()),
                "coverage_pct": round(mask.sum() / valid.sum() * 100, 1),
            }

    # ── Regime-wise overall accuracy ──────────────────────────
    regime_overall = {}
    for r_val, r_name in [(0,"BEAR"),(1,"RANGE"),(2,"BULL")]:
        mask = valid & (r_all == r_val)
        if mask.sum() >= 20:
            regime_overall[r_name] = {
                "accuracy": round(accuracy_score(y_all[mask],all_preds[mask])*100,2),
                "n_days"  : int(mask.sum()),
            }

    # Feature importance DataFrame
    feat_imp_df = pd.DataFrame({
        "feature"   : feat_cols,
        "importance": feat_imps / (feat_imps.sum() + 1e-9)
    }).sort_values("importance", ascending=False).reset_index(drop=True)

    fold_accs = [fs["accuracy"] for fs in fold_stats]
    fold_aucs = [fs["auc"] for fs in fold_stats]

    print(f"\n  OVERALL  acc={oos_acc:.1f}%  AUC={oos_auc:.3f}  "
          f"folds={[f['accuracy'] for f in fold_stats]}")

    return {
        "n_horizon"    : n_horizon,
        "oos_acc"      : round(oos_acc, 2),
        "oos_auc"      : round(oos_auc, 4),
        "mean_fold_acc": round(np.mean(fold_accs), 2),
        "std_fold_acc" : round(np.std(fold_accs), 2),
        "fold_stats"   : fold_stats,
        "high_conf"    : hc_results,
        "regime"       : regime_overall,
        "feat_imp"     : feat_imp_df,
        "probs"        : all_probs,
        "preds"        : all_preds,
        "y_true"       : y_all,
        "index"        : feat.index,
        "top_features" : feat_cols,
    }


# ─────────────────────────────────────────────────────────────
# BACKTEST
# ─────────────────────────────────────────────────────────────

def run_backtest(nifty_close: pd.Series, result: dict,
                 conf_thresh: float = 0.60) -> dict:
    n       = result["n_horizon"]
    idx     = result["index"]
    probs   = pd.Series(result["probs"], index=idx)
    fwd_ret = nifty_close.pct_change(n).shift(-n).reindex(idx)

    long_m  = probs >  conf_thresh
    short_m = probs < (1 - conf_thresh)
    sig     = pd.Series(0.0, index=idx)
    sig[long_m]  =  1.0
    sig[short_m] = -1.0

    strat = (sig * fwd_ret).dropna()
    bh    = fwd_ret.dropna()
    ann   = 252 / n
    active = strat[sig[strat.index] != 0]

    def sharpe(r): return r.mean()/(r.std()+1e-9)*np.sqrt(ann)
    def mdd(r):
        eq=(1+r).cumprod(); return ((eq-eq.cummax())/eq.cummax()).min()*100

    return {
        "conf_thresh"   : conf_thresh,
        "n_long"        : int(long_m.sum()),
        "n_short"       : int(short_m.sum()),
        "coverage_pct"  : round((long_m|short_m).sum()/max(probs.notna().sum(),1)*100,1),
        "win_rate_pct"  : round((active>0).mean()*100,2) if len(active) else 0,
        "avg_ret_pct"   : round(active.mean()*100,3) if len(active) else 0,
        "strategy_sharpe": round(sharpe(strat),3),
        "bh_sharpe"     : round(sharpe(bh),3),
        "max_dd_pct"    : round(mdd(strat),2),
        "strat_equity"  : (1+strat).cumprod(),
        "bh_equity"     : (1+bh).cumprod(),
    }


# ─────────────────────────────────────────────────────────────
# CHARTS
# ─────────────────────────────────────────────────────────────

def make_charts(results, bts, regime):
    C1, C2 = "#1565C0", "#C62828"
    print("\n  Generating charts …")

    for n in [1, 2]:
        res = results[n]; bt = bts[n]
        probs = res["probs"]; valid = ~np.isnan(probs)
        thrs  = np.arange(0.50, 0.76, 0.01)

        acc_curve, cov_curve = [], []
        for t in thrs:
            mask = valid & (np.abs(probs - 0.5) >= (t - 0.5))
            acc_curve.append(accuracy_score(res["y_true"][mask], res["preds"][mask])*100
                             if mask.sum() >= 10 else np.nan)
            cov_curve.append(mask.sum()/valid.sum()*100 if valid.sum() else 0)

        fig, axes = plt.subplots(2, 3, figsize=(18, 10))
        fig.suptitle(f"NIFTY Multi-Asset Direction Predictor — n={n} day(s)",
                     fontsize=13, fontweight="bold")

        # Panel 1: Accuracy vs confidence
        ax = axes[0,0]; ax2 = ax.twinx()
        ax.plot(thrs, acc_curve, C1, lw=2, marker="o", ms=4)
        ax2.bar(thrs, cov_curve, width=0.008, alpha=0.25, color="grey")
        ax.axhline(50, ls="--", c="grey", lw=1, label="50% baseline")
        ax.axhline(55, ls=":", c="green", lw=1); ax.axhline(60, ls=":", c="orange", lw=1)
        ax.set_xlabel("Confidence threshold"); ax.set_ylabel("Accuracy %", color=C1)
        ax2.set_ylabel("Coverage %", color="grey"); ax.set_ylim(44, 80)
        ax.set_title("Accuracy vs Confidence Threshold")
        ax.legend(fontsize=8)

        # Panel 2: Fold accuracies
        ax = axes[0,1]
        faccs = [f["accuracy"] for f in res["fold_stats"]]
        fcolors = [C1 if a>=50 else C2 for a in faccs]
        bars = ax.bar(range(1,len(faccs)+1), faccs, color=fcolors, alpha=0.85)
        ax.axhline(50, ls="--", c="grey", lw=1)
        for bar, a in zip(bars, faccs):
            ax.text(bar.get_x()+bar.get_width()/2, a+0.3, f"{a:.0f}%", ha="center", fontsize=8)
        ax.set_title(f"Per-Fold Accuracy  OOS={res['oos_acc']:.1f}%  AUC={res['oos_auc']:.3f}")
        ax.set_xlabel("Fold"); ax.set_ylabel("Accuracy %"); ax.set_ylim(40, max(faccs)+6)

        # Panel 3: Regime accuracy
        ax = axes[0,2]
        reg_data = res["regime"]
        reg_names = list(reg_data.keys())
        reg_accs  = [reg_data[r]["accuracy"] for r in reg_names]
        reg_ns    = [reg_data[r]["n_days"]   for r in reg_names]
        rcolors   = ["#C62828","#FF9800","#2E7D32"][:len(reg_names)]
        bars = ax.bar(reg_names, reg_accs, color=rcolors[:len(reg_names)], alpha=0.85)
        ax.axhline(50, ls="--", c="grey", lw=1)
        for bar, a, nn in zip(bars, reg_accs, reg_ns):
            ax.text(bar.get_x()+bar.get_width()/2, a+0.3, f"{a:.0f}%\n(n={nn})",
                    ha="center", fontsize=8)
        ax.set_title("Accuracy by Market Regime"); ax.set_ylabel("Accuracy %")
        ax.set_ylim(40, max(reg_accs)+8)

        # Panel 4: Top-20 features
        ax = axes[1,0]
        fi = res["feat_imp"].head(20)
        ax.barh(fi["feature"][::-1], fi["importance"][::-1], color=C1, alpha=0.85)
        ax.set_title("Top 20 Features (XGBoost importance)"); ax.set_xlabel("Importance")
        ax.tick_params(axis="y", labelsize=7)

        # Panel 5: Probability histogram
        ax = axes[1,1]
        pv = probs[valid]; yv = res["y_true"][valid]
        ax.hist(pv[yv==1], bins=30, alpha=0.6, color=C1, label="Actual UP", density=True)
        ax.hist(pv[yv==0], bins=30, alpha=0.6, color=C2, label="Actual DOWN", density=True)
        ax.axvline(0.5, c="k", lw=1, ls="--")
        ax.set_title("Predicted Probability Distribution"); ax.set_xlabel("P(UP)")
        ax.legend(fontsize=8)

        # Panel 6: Equity curve
        ax = axes[1,2]
        eq_s = bt["strat_equity"].dropna()
        eq_b = bt["bh_equity"].dropna()
        ax.plot(eq_s.index, eq_s.values, C1, lw=1.5,
                label=f"Strategy (conf≥{bt['conf_thresh']:.0%}, Sharpe={bt['strategy_sharpe']:.2f})")
        ax.plot(eq_b.index, eq_b.values, "grey", lw=1, ls="--",
                label=f"Buy&Hold (Sharpe={bt['bh_sharpe']:.2f})")
        ax.set_title(f"Equity Curve  (win={bt['win_rate_pct']:.0f}%,  cov={bt['coverage_pct']:.0f}%)")
        ax.legend(fontsize=8); ax.grid(alpha=0.3)

        plt.tight_layout()
        p = os.path.join(OUT, f"MA_n{n}.png")
        plt.savefig(p, dpi=130); plt.close()
        print(f"    Saved {p}")

    # ── Feature importance comparison ─────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(18, 9))
    for ax, n, c in zip(axes, [1,2], [C1,C2]):
        fi = results[n]["feat_imp"].head(25)
        # Color by feature category
        def cat_color(f):
            if "spx" in f: return "#1A237E"
            if "vix" in f: return "#B71C1C"
            if "inr" in f: return "#1B5E20"
            if "bnk" in f: return "#E65100"
            if "nifty" in f: return "#006064"
            return "#4A148C"
        colors = [cat_color(f) for f in fi["feature"][::-1]]
        ax.barh(fi["feature"][::-1], fi["importance"][::-1], color=colors, alpha=0.85)
        ax.set_title(f"n={n}d Top-25 Features", fontsize=11)
        ax.set_xlabel("Importance")
        ax.tick_params(axis="y", labelsize=8)
        # Legend
        from matplotlib.patches import Patch
        legend_els = [
            Patch(fc="#1A237E", label="S&P 500 (SPX)"),
            Patch(fc="#B71C1C", label="VIX"),
            Patch(fc="#1B5E20", label="USD/INR"),
            Patch(fc="#E65100", label="Bank Nifty"),
            Patch(fc="#006064", label="NIFTY own"),
            Patch(fc="#4A148C", label="Other"),
        ]
        ax.legend(handles=legend_els, fontsize=8)
    plt.suptitle("Which assets drive NIFTY direction prediction?", fontsize=13)
    plt.tight_layout()
    p = os.path.join(OUT, "MA_feature_importance.png")
    plt.savefig(p, dpi=130); plt.close()
    print(f"    Saved {p}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print("=" * 68)
    print("  NIFTY MULTI-ASSET DIRECTION PREDICTOR  (n=1d, n=2d)")
    print("=" * 68)

    # Data
    print("\n[1/5] Generating multi-asset data …")
    from nifty_data_generator import generate_nifty_ohlcv
    prices    = generate_multi_asset(seed=42)
    nifty_ohv = generate_nifty_ohlcv(seed=42).reindex(prices.index).ffill()
    print(f"    {len(prices)} days  {prices.index[0].date()} → {prices.index[-1].date()}")
    print(f"    Assets: {list(prices.columns)}")

    # Regime
    print("\n[2/5] Detecting market regimes …")
    regime = detect_regime(prices)
    for r,n in [(0,"BEAR"),(1,"RANGE"),(2,"BULL")]:
        print(f"    {n}: {(regime==r).sum()} days ({(regime==r).mean()*100:.0f}%)")

    # Features
    print("\n[3/5] Engineering cross-asset features …")
    feat = build_features(prices, nifty_ohv)

    # Targets (binary: did NIFTY close higher n days later?)
    nifty_c = prices["^NSEI"]
    up_1d   = (nifty_c.shift(-1) > nifty_c).astype(int).rename("up_1d")
    up_2d   = (nifty_c.shift(-2) > nifty_c).astype(int).rename("up_2d")

    # Walk-forward
    print("\n[4/5] Walk-forward evaluation …")
    results = {}
    for tgt, n in [(up_1d, 1), (up_2d, 2)]:
        results[n] = purged_walk_forward(feat, tgt, regime, n_horizon=n)

    # Backtest
    print("\n[5/5] Backtesting strategy …")
    bts = {}
    for n in [1, 2]:
        bts[n] = run_backtest(nifty_c, results[n], conf_thresh=0.60)

    # Charts
    make_charts(results, bts, regime)

    # ── Final report ──────────────────────────────────────────
    report = {}
    for n in [1, 2]:
        res = results[n]; bt = bts[n]
        report[f"n{n}d"] = {
            "oos_accuracy_pct" : res["oos_acc"],
            "oos_auc"          : res["oos_auc"],
            "mean_fold_acc"    : res["mean_fold_acc"],
            "std_fold_acc"     : res["std_fold_acc"],
            "high_confidence"  : res["high_conf"],
            "regime_accuracy"  : res["regime"],
            "top20_features"   : res["feat_imp"].head(20)[["feature","importance"]]\
                                     .assign(importance=lambda d: d["importance"].round(5))\
                                     .to_dict("records"),
            "backtest_60pct"   : {k:v for k,v in bt.items()
                                  if not isinstance(v, pd.Series)},
        }
    with open(os.path.join(OUT, "multiasset_report.json"), "w") as f:
        json.dump(report, f, indent=2)

    # ── Console summary ───────────────────────────────────────
    print("\n" + "=" * 68)
    print("  FINAL RESULTS — MULTI-ASSET NIFTY DIRECTION PREDICTOR")
    print("=" * 68)

    for n in [1, 2]:
        res = results[n]; bt = bts[n]
        fold_accs = [f["accuracy"] for f in res["fold_stats"]]
        print(f"\n  ━━━ n = {n} DAY{'S' if n>1 else ' '} AHEAD ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"  Overall OOS Accuracy : {res['oos_acc']:.1f}%  (AUC={res['oos_auc']:.3f})")
        print(f"  Fold accuracies      : {fold_accs}")
        print(f"  Mean ± Std           : {res['mean_fold_acc']:.1f}% ± {res['std_fold_acc']:.1f}%")
        print()
        print("  ACCURACY BY REGIME:")
        for rname, rdata in res["regime"].items():
            print(f"    {rname:6s}: {rdata['accuracy']:.1f}%  ({rdata['n_days']} days)")
        print()
        print("  HIGH-CONFIDENCE FILTER:")
        for thr, hc in res["high_conf"].items():
            print(f"    Conf ≥ {thr:.0%}  →  acc={hc['accuracy']:.1f}%  "
                  f"signals={hc['n_signals']}  coverage={hc['coverage_pct']:.1f}%")
        print()
        print(f"  BACKTEST (conf ≥ 60%):")
        print(f"    {bt['n_long']} long + {bt['n_short']} short  "
              f"({bt['coverage_pct']:.0f}% coverage)")
        print(f"    Win rate={bt['win_rate_pct']:.1f}%  "
              f"Avg ret={bt['avg_ret_pct']:+.2f}%  "
              f"Sharpe={bt['strategy_sharpe']:.2f}  "
              f"(BH={bt['bh_sharpe']:.2f})")
        print(f"    Max drawdown={bt['max_dd_pct']:.1f}%")
        print()
        print("  TOP 10 FEATURES:")
        for _, row in res["feat_imp"].head(10).iterrows():
            print(f"    {row['feature']:<30}  {row['importance']:.4f}")

    print("\n" + "=" * 68)
    print(f"  Outputs → {OUT}/")
    print("=" * 68)


if __name__ == "__main__":
    main()
