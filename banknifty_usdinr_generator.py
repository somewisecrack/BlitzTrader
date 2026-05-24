"""
Correlated synthetic OHLCV generator for BANKNIFTY and USDINR universes.

BANKNIFTY universe (8 series):
  ^NSEBANK    Bank Nifty index (anchor)
  HDFCBANK.NS largest private bank  ~28% weight
  ICICIBANK.NS second largest       ~22% weight
  SBIN.NS      largest PSU bank     ~10% weight
  AXISBANK.NS  Axis Bank            ~10% weight
  KOTAKBANK.NS Kotak Mahindra        ~8% weight
  ^NSEI        NIFTY 50 (parent)
  ^VIX         Fear gauge

USDINR universe (7 series):
  INR=X        USD/INR spot (anchor)
  DX=F         US Dollar Index proxy
  CL=F         Crude oil (India imports ~85% crude)
  GC=F         Gold futures
  US10Y        US 10-year yield proxy
  ^GSPC        S&P 500 (risk-on/off)
  ^HSI         Hang Seng (Asian risk proxy)

Intraday bars at 1h / 15m / 5m are synthesised from the daily series
using a Brownian bridge with AR(1) momentum (φ=0.12).
"""

import numpy as np
import pandas as pd
from scipy.linalg import cholesky
from nifty_data_generator import generate_nifty_ohlcv, MONTHLY_ANCHORS

# ── session constants ────────────────────────────────────────────────
SESSION_OPEN_H,  SESSION_OPEN_M  = 9,  15
SESSION_CLOSE_H, SESSION_CLOSE_M = 15, 30
SESSION_MIN = 375

# ─────────────────────────────────────────────────────────────────────
#  BANKNIFTY UNIVERSE
# ─────────────────────────────────────────────────────────────────────

BN_TICKERS = ["^NSEBANK","HDFCBANK.NS","ICICIBANK.NS","SBIN.NS",
               "AXISBANK.NS","KOTAKBANK.NS","^NSEI","^VIX"]

# (start_price, annual_return, annual_vol)
BN_PARAMS = {
    "^NSEBANK"   : (17500, 0.12, 0.28),
    "HDFCBANK.NS": (1000,  0.10, 0.22),
    "ICICIBANK.NS": (250,  0.14, 0.26),
    "SBIN.NS"    : (220,   0.12, 0.30),
    "AXISBANK.NS": (480,   0.11, 0.28),
    "KOTAKBANK.NS": (1600, 0.09, 0.20),
    "^NSEI"      : (8561,  0.10, 0.18),
    "^VIX"       : (16.0, -0.02, 0.60),
}

# Correlation matrix: NSEBANK, HDFC, ICICI, SBIN, AXIS, KOTAK, NSEI, VIX
BN_CORR = np.array([
#  BNKN  HDFC  ICICI  SBIN  AXIS  KOTAK NSEI   VIX
  [1.00, 0.85, 0.82,  0.75, 0.80, 0.78,  0.92, -0.65],  # BankNifty
  [0.85, 1.00, 0.75,  0.65, 0.72, 0.68,  0.78, -0.58],  # HDFC
  [0.82, 0.75, 1.00,  0.68, 0.74, 0.62,  0.75, -0.55],  # ICICI
  [0.75, 0.65, 0.68,  1.00, 0.70, 0.58,  0.70, -0.52],  # SBIN
  [0.80, 0.72, 0.74,  0.70, 1.00, 0.65,  0.72, -0.54],  # AXIS
  [0.78, 0.68, 0.62,  0.58, 0.65, 1.00,  0.72, -0.50],  # KOTAK
  [0.92, 0.78, 0.75,  0.70, 0.72, 0.72,  1.00, -0.68],  # NSEI
  [-0.65,-0.58,-0.55,-0.52,-0.54,-0.50, -0.68,  1.00],  # VIX
], dtype=float)

# ─────────────────────────────────────────────────────────────────────
#  USDINR UNIVERSE
# ─────────────────────────────────────────────────────────────────────

INR_TICKERS = ["INR=X","DX=F","CL=F","GC=F","US10Y","^GSPC","^HSI"]

INR_PARAMS = {
    "INR=X" : (63.0,  0.030, 0.060),   # USDINR depreciates ~3%/yr
    "DX=F"  : (95.0,  0.010, 0.080),   # DXY
    "CL=F"  : (55.0, -0.010, 0.350),   # Crude Oil
    "GC=F"  : (1200,  0.080, 0.140),   # Gold
    "US10Y" : (2.50,  0.050, 0.200),   # US 10Y yield proxy
    "^GSPC" : (2050,  0.110, 0.170),   # S&P 500
    "^HSI"  : (23000, 0.020, 0.220),   # Hang Seng
}

# Correlation: INR=X, DXY, Crude, Gold, US10Y, SPX, HSI
INR_CORR = np.array([
#  INR    DXY    CL     GC    US10Y  SPX    HSI
  [1.00,  0.72,  0.35,  0.22,  0.30, -0.32, -0.25],  # USDINR (weak INR = high)
  [0.72,  1.00,  0.10,  0.05,  0.45, -0.20, -0.18],  # DXY
  [0.35,  0.10,  1.00,  0.18,  0.05, -0.12, -0.10],  # Crude
  [0.22,  0.05,  0.18,  1.00, -0.15, -0.08, -0.05],  # Gold
  [0.30,  0.45,  0.05, -0.15,  1.00, -0.25, -0.20],  # US10Y
  [-0.32,-0.20, -0.12, -0.08, -0.25,  1.00,  0.50],  # SPX
  [-0.25,-0.18, -0.10, -0.05, -0.20,  0.50,  1.00],  # HSI
], dtype=float)


def _nearest_pd(A):
    ev, evec = np.linalg.eigh(A)
    ev = np.clip(ev, 1e-6, None)
    return evec @ np.diag(ev) @ evec.T


def _gen_daily_series(tickers, params, corr, seed, n_days=2695,
                      start="2015-01-01") -> pd.DataFrame:
    """Generate correlated daily close series via Cholesky."""
    rng   = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    N     = len(dates)
    n     = len(tickers)

    C = _nearest_pd(corr)
    L = cholesky(C, lower=True)
    raw_z  = rng.standard_normal((N, n))
    corr_z = (L @ raw_z.T).T

    prices = {}
    for i, tk in enumerate(tickers):
        s0, mu_ann, sig_ann = params[tk]
        mu  = mu_ann  / 252
        sig = sig_ann / np.sqrt(252)
        if tk == "^VIX":
            p = np.zeros(N); p[0] = s0
            kappa, theta = 8.0, 18.0
            for t in range(1, N):
                p[t] = max(p[t-1] + kappa/252*(theta-p[t-1])
                           + sig*p[t-1]*corr_z[t, i], 5.0)
            prices[tk] = p
        elif tk == "US10Y":
            # OU around 2.5%
            p = np.zeros(N); p[0] = s0
            kappa, theta = 1.0, 2.5
            for t in range(1, N):
                p[t] = max(p[t-1] + kappa/252*(theta-p[t-1])
                           + sig*corr_z[t, i], 0.5)
            prices[tk] = p
        else:
            z     = corr_z[:, i] * sig + mu
            prices[tk] = s0 * np.exp(np.cumsum(z))

    df = pd.DataFrame(prices, index=dates)
    # Synthetic OHLCV: derive H/L/O from Close
    result = {}
    rng2 = np.random.default_rng(seed + 1)
    for tk in tickers:
        c = df[tk].values
        daily_vol = params[tk][2] / np.sqrt(252)
        o = np.concatenate([[c[0]], c[:-1] * (1 + rng2.normal(0, daily_vol*0.3, N-1))])
        h = np.maximum(o, c) * (1 + abs(rng2.normal(0, daily_vol*0.5, N)))
        l = np.minimum(o, c) * (1 - abs(rng2.normal(0, daily_vol*0.5, N)))
        v = abs(rng2.normal(5e5, 2e5, N))
        sub = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v},
                           index=dates)
        result[tk] = sub
    return result   # dict of {ticker: OHLCV DataFrame}


def generate_banknifty_universe(seed=42):
    """Returns dict {ticker: daily OHLCV DataFrame} for BankNifty universe."""
    # Use existing NIFTY generator for ^NSEI anchor
    nifty_df = generate_nifty_ohlcv(seed=seed)
    daily = _gen_daily_series(BN_TICKERS, BN_PARAMS, BN_CORR, seed=seed+10)
    # Override NSEI with real anchor
    nifty_reindexed = nifty_df.reindex(daily["^NSEI"].index).ffill().dropna()
    daily["^NSEI"] = nifty_reindexed
    return daily


def generate_usdinr_universe(seed=42):
    """Returns dict {ticker: daily OHLCV DataFrame} for USDINR universe."""
    return _gen_daily_series(INR_TICKERS, INR_PARAMS, INR_CORR, seed=seed+20)


def build_intraday_from_daily(daily_df: pd.DataFrame, freq_min: int,
                               days: int, seed: int = 42) -> pd.DataFrame:
    """
    Synthesise intraday OHLCV from last `days` of a daily OHLCV DataFrame.
    AR(1) φ=0.12 for slight intraday momentum.
    """
    rng    = np.random.default_rng(seed)
    n_bars = SESSION_MIN // freq_min
    tail   = daily_df.tail(days).copy()
    records = []
    prev_close = None

    for date, row in tail.iterrows():
        O, H, L, C = row["Open"], row["High"], row["Low"], row["Close"]
        V = row.get("Volume", 5e5)
        if prev_close is None:
            prev_close = O

        day_dt = pd.Timestamp(date)
        times  = [day_dt.replace(hour=SESSION_OPEN_H, minute=SESSION_OPEN_M)
                  + pd.Timedelta(minutes=freq_min * k) for k in range(n_bars)]

        phi = 0.12
        eps = rng.standard_normal(n_bars)
        z   = np.zeros(n_bars); z[0] = eps[0]
        for t in range(1, n_bars):
            z[t] = phi * z[t-1] + np.sqrt(1 - phi**2) * eps[t]

        log_O, log_C = np.log(max(O, 1e-6)), np.log(max(C, 1e-6))
        t_arr  = np.linspace(0, 1, n_bars + 1)[1:]
        bridge = z - t_arr * z[-1]
        hl_log = np.log(max(H, C+1e-3)) - np.log(max(L, O-1e-3))
        noise  = hl_log / 4.0 * bridge

        log_path = log_O + (log_C - log_O)*t_arr + noise
        prices   = np.exp(log_path)

        # Volume: U-shaped (open/close heavy)
        hf = np.linspace(0, 1, n_bars)
        vs = 1.5*np.exp(-8*hf) + 1.5*np.exp(-8*(1-hf)) + 0.5
        vn = rng.exponential(1, n_bars)
        bv = V / n_bars * vs * vn

        gap = (O - prev_close) / max(prev_close, 1e-6)
        pbc = O
        for k in range(n_bars):
            bO = pbc; bC = prices[k]
            bH = max(bO,bC)*(1+abs(rng.normal(0,2e-4)))
            bL = min(bO,bC)*(1-abs(rng.normal(0,2e-4)))
            records.append(dict(datetime=times[k],
                                Open=bO, High=bH, Low=bL, Close=bC, Volume=bv[k],
                                gap_open=gap if k==0 else 0.0,
                                session_bar=k, n_bars_day=n_bars, date=date))
            pbc = bC
        prev_close = C

    return pd.DataFrame(records).set_index("datetime")


if __name__ == "__main__":
    bn = generate_banknifty_universe()
    ir = generate_usdinr_universe()
    print("BankNifty universe:", {k: len(v) for k,v in bn.items()})
    print("USDINR universe:  ", {k: len(v) for k,v in ir.items()})
    print(bn["^NSEBANK"].tail(3).round(1))
    print(ir["INR=X"].tail(3).round(4))
