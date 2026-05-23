"""
Multi-Asset NIFTY Data Generator
==================================
Generates correlated synthetic OHLCV / price series for:
  ^NSEI      - NIFTY 50 (anchor from actual monthly levels)
  ^GSPC      - S&P 500  (closes AFTER NIFTY → true leading indicator for next day)
  ^VIX       - CBOE VIX (fear gauge, inverse to equities)
  INR=X      - USD/INR  (weak rupee → FII outflows → NIFTY down)
  ^NSEBANK   - Bank Nifty (high-beta NIFTY cousin)
  RELIANCE.NS- Largest NIFTY weight (~10%)
  HDFCBANK.NS- Largest private bank (~12% weight)
  SBIN.NS    - Largest PSU bank
  CL=F       - Crude oil futures (India is oil importer)
  GC=F       - Gold futures (safe-haven, often inverse)
  ^N225      - Nikkei 225 (Asian session before NIFTY)
  ^HSI       - Hang Seng (Asian session)

Correlation structure calibrated to known empirical relationships.
"""

import numpy as np
import pandas as pd
from scipy.linalg import cholesky

# ─────────────────────────────────────────────────────────────
# MONTHLY NIFTY ANCHORS  (from nifty_data_generator.py)
# ─────────────────────────────────────────────────────────────
from nifty_data_generator import MONTHLY_ANCHORS, VOL_OVERRIDES, generate_nifty_ohlcv

# ─────────────────────────────────────────────────────────────
# ASSET PARAMETERS
# ─────────────────────────────────────────────────────────────

# (ticker, start_price, annual_return, annual_vol, beta_to_nifty)
ASSET_PARAMS = {
    "^GSPC"       : (2050,   0.11, 0.17,  0.60),   # S&P 500
    "^VIX"        : (16.0,  -0.02, 0.60, -0.70),   # VIX (mean-reverting)
    "INR=X"       : (63.0,   0.03, 0.06, -0.40),   # USDINR (weak INR = NIFTY down)
    "^NSEBANK"    : (17500,  0.12, 0.22,  0.92),   # Bank Nifty
    "RELIANCE.NS" : (900,    0.15, 0.24,  0.70),   # Reliance
    "HDFCBANK.NS" : (1000,   0.10, 0.20,  0.78),   # HDFC Bank
    "SBIN.NS"     : (220,    0.12, 0.30,  0.75),   # SBI
    "CL=F"        : (55.0,  -0.01, 0.35, -0.18),   # Crude Oil
    "GC=F"        : (1200,   0.08, 0.14, -0.12),   # Gold
    "^N225"       : (18000,  0.07, 0.18,  0.55),   # Nikkei
    "^HSI"        : (23000,  0.02, 0.22,  0.45),   # Hang Seng
}

# Correlation matrix (NIFTY + 11 assets = 12×12)
# Order: NIFTY, GSPC, VIX, INR=X, NSEBANK, REL, HDFC, SBIN, CL, GC, N225, HSI
TICKERS = ["^NSEI","^GSPC","^VIX","INR=X","^NSEBANK",
           "RELIANCE.NS","HDFCBANK.NS","SBIN.NS","CL=F","GC=F","^N225","^HSI"]

# Correlation matrix (approximate empirical values)
CORR = np.array([
#  NSEI  GSPC   VIX  INR=X BNKN   REL   HDFC   SBI   CL    GC   N225   HSI
  [1.00, 0.62, -0.68, -0.42, 0.92, 0.72,  0.78,  0.70, -0.18, -0.12, 0.52, 0.45],  # NIFTY
  [0.62, 1.00, -0.78, -0.28, 0.60, 0.55,  0.58,  0.52, -0.12, -0.08, 0.62, 0.50],  # GSPC
  [-0.68,-0.78, 1.00,  0.22,-0.65,-0.58, -0.62, -0.58,  0.10,  0.15,-0.60,-0.52],  # VIX
  [-0.42,-0.28, 0.22,  1.00,-0.40,-0.32, -0.38, -0.30,  0.08,  0.12,-0.25,-0.20],  # INR=X
  [0.92, 0.60, -0.65, -0.40, 1.00, 0.65,  0.80,  0.72, -0.15, -0.10, 0.48, 0.42],  # BankNifty
  [0.72, 0.55, -0.58, -0.32, 0.65, 1.00,  0.60,  0.55, -0.25, -0.10, 0.45, 0.40],  # Reliance
  [0.78, 0.58, -0.62, -0.38, 0.80, 0.60,  1.00,  0.68, -0.12, -0.08, 0.48, 0.42],  # HDFC
  [0.70, 0.52, -0.58, -0.30, 0.72, 0.55,  0.68,  1.00, -0.10, -0.05, 0.42, 0.38],  # SBI
  [-0.18,-0.12, 0.10,  0.08,-0.15,-0.25, -0.12, -0.10,  1.00,  0.18,-0.12,-0.10],  # Crude
  [-0.12,-0.08, 0.15,  0.12,-0.10,-0.10, -0.08, -0.05,  0.18,  1.00,-0.08,-0.05],  # Gold
  [0.52, 0.62, -0.60, -0.25, 0.48, 0.45,  0.48,  0.42, -0.12, -0.08, 1.00, 0.65],  # Nikkei
  [0.45, 0.50, -0.52, -0.20, 0.42, 0.40,  0.42,  0.38, -0.10, -0.05, 0.65, 1.00],  # HSI
], dtype=float)


def _nearest_pd(A):
    """Force matrix to be positive definite via eigenvalue clipping."""
    eigvals, eigvecs = np.linalg.eigh(A)
    eigvals = np.clip(eigvals, 1e-6, None)
    return eigvecs @ np.diag(eigvals) @ eigvecs.T


def generate_multi_asset(seed: int = 42) -> dict:
    """
    Generate daily price series for all assets correlated with NIFTY.
    Returns dict {ticker: DataFrame with OHLCV or Close only}.
    """
    rng    = np.random.default_rng(seed)
    dates  = pd.bdate_range("2015-01-01", "2025-05-23")

    # ── Generate NIFTY first (anchored) ───────────────────────
    nifty_df = generate_nifty_ohlcv(seed=seed)
    nifty_df = nifty_df.reindex(dates).ffill().dropna()
    nifty_ret = nifty_df["Close"].pct_change().fillna(0).values
    N = len(dates)

    # ── Cholesky factor for correlated returns ─────────────────
    C = _nearest_pd(CORR)
    L = cholesky(C, lower=True)

    # ── Generate correlated return innovations ─────────────────
    raw_z = rng.standard_normal((N, len(TICKERS)))   # iid standard normals
    corr_z = (L @ raw_z.T).T                          # correlated

    all_returns = {}
    # NIFTY returns are index 0 — replace with actual generated returns
    all_returns["^NSEI"] = nifty_ret

    for i, ticker in enumerate(TICKERS[1:], start=1):
        s0, mu_ann, sig_ann, _ = ASSET_PARAMS[ticker]
        mu_day  = mu_ann  / 252
        sig_day = sig_ann / np.sqrt(252)

        # Scale innovations to asset volatility
        z = corr_z[:, i] * sig_day + mu_day

        # VIX is mean-reverting (Ornstein-Uhlenbeck) around 18
        if ticker == "^VIX":
            prices = np.zeros(N)
            prices[0] = s0
            kappa = 8.0   # speed of mean reversion
            theta = 18.0  # long-run mean
            for t in range(1, N):
                prices[t] = max(
                    prices[t-1] + kappa/252*(theta-prices[t-1]) + sig_day*prices[t-1]*corr_z[t,i],
                    5.0
                )
            all_returns[ticker] = np.diff(np.log(prices), prepend=np.log(prices[0]))
        else:
            all_returns[ticker] = z

    # ── Build price series from returns ───────────────────────
    asset_close = {}
    asset_close["^NSEI"] = nifty_df["Close"].reindex(dates).ffill()

    for ticker in TICKERS[1:]:
        s0   = ASSET_PARAMS[ticker][0]
        rets = all_returns[ticker]
        if ticker == "^VIX":
            # Already computed prices above; rebuild
            s0_val = s0
            kappa, theta = 8.0, 18.0
            sig_day = ASSET_PARAMS[ticker][2] / np.sqrt(252)
            prices = np.zeros(N)
            prices[0] = s0_val
            i_idx = TICKERS.index(ticker)
            for t in range(1, N):
                prices[t] = max(
                    prices[t-1] + kappa/252*(theta - prices[t-1])
                    + sig_day * prices[t-1] * corr_z[t, i_idx],
                    5.0
                )
            asset_close[ticker] = pd.Series(prices, index=dates)
        else:
            prices = s0 * np.exp(np.cumsum(rets))
            asset_close[ticker] = pd.Series(prices, index=dates)

    # ── Combine into single DataFrame ─────────────────────────
    df = pd.DataFrame(asset_close, index=dates)
    df = df.dropna()
    return df


if __name__ == "__main__":
    df = generate_multi_asset()
    print(df.shape)
    print(df.tail(3).round(2))
    for col in df.columns:
        ret = df[col].pct_change().dropna()
        print(f"{col:20s}  start={df[col].iloc[0]:.1f}  "
              f"end={df[col].iloc[-1]:.1f}  "
              f"ann_ret={((df[col].iloc[-1]/df[col].iloc[0])**(252/len(df))-1)*100:.1f}%  "
              f"ann_vol={ret.std()*np.sqrt(252)*100:.1f}%")
