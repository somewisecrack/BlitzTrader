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
from scipy.stats import t as t_dist
from nifty_data_generator import generate_nifty_ohlcv, MONTHLY_ANCHORS

# ─────────────────────────────────────────────────────────────────────
#  BANKNIFTY MONTHLY ANCHORS (approximate actual close levels)
# ─────────────────────────────────────────────────────────────────────
BN_MONTHLY_ANCHORS = {
    "2015-01": 18000, "2015-02": 19500, "2015-03": 19700, "2015-04": 18700,
    "2015-05": 19100, "2015-06": 18400, "2015-07": 19600, "2015-08": 17500,
    "2015-09": 16300, "2015-10": 17200, "2015-11": 16600, "2015-12": 17100,
    "2016-01": 16300, "2016-02": 14200, "2016-03": 16300, "2016-04": 16400,
    "2016-05": 17700, "2016-06": 18500, "2016-07": 19200, "2016-08": 20600,
    "2016-09": 20200, "2016-10": 20400, "2016-11": 18800, "2016-12": 18300,
    "2017-01": 19300, "2017-02": 21400, "2017-03": 21900, "2017-04": 22500,
    "2017-05": 23500, "2017-06": 22900, "2017-07": 24500, "2017-08": 23700,
    "2017-09": 24400, "2017-10": 25700, "2017-11": 25200, "2017-12": 25500,
    "2018-01": 27400, "2018-02": 24800, "2018-03": 23900, "2018-04": 25400,
    "2018-05": 25900, "2018-06": 26400, "2018-07": 27900, "2018-08": 28600,
    "2018-09": 26500, "2018-10": 24600, "2018-11": 26600, "2018-12": 26900,
    "2019-01": 26700, "2019-02": 26500, "2019-03": 29500, "2019-04": 29800,
    "2019-05": 30300, "2019-06": 29700, "2019-07": 28100, "2019-08": 26800,
    "2019-09": 28000, "2019-10": 29200, "2019-11": 30800, "2019-12": 32200,
    "2020-01": 31400, "2020-02": 27400, "2020-03": 18000,  # COVID crash
    "2020-04": 22000, "2020-05": 21800, "2020-06": 23500,
    "2020-07": 25500, "2020-08": 27300, "2020-09": 24700,
    "2020-10": 26100, "2020-11": 28500, "2020-12": 31500,
    "2021-01": 31700, "2021-02": 35900, "2021-03": 36200, "2021-04": 35500,
    "2021-05": 34800, "2021-06": 35400, "2021-07": 36200, "2021-08": 37700,
    "2021-09": 40700, "2021-10": 41500, "2021-11": 37800, "2021-12": 37500,
    "2022-01": 38000, "2022-02": 36500, "2022-03": 37000, "2022-04": 36500,
    "2022-05": 34800, "2022-06": 32600, "2022-07": 36200, "2022-08": 38400,
    "2022-09": 36600, "2022-10": 41400, "2022-11": 42700, "2022-12": 43200,
    "2023-01": 41500, "2023-02": 40500, "2023-03": 40700, "2023-04": 43400,
    "2023-05": 44800, "2023-06": 44300, "2023-07": 45600, "2023-08": 44000,
    "2023-09": 44600, "2023-10": 43700, "2023-11": 43800, "2023-12": 48800,
    "2024-01": 48500, "2024-02": 46500, "2024-03": 48100, "2024-04": 48200,
    "2024-05": 49700, "2024-06": 51500, "2024-07": 53400, "2024-08": 51300,
    "2024-09": 54500, "2024-10": 51400, "2024-11": 52000, "2024-12": 53700,
    "2025-01": 50700, "2025-02": 48500, "2025-03": 51500, "2025-04": 52500,
    "2025-05": 55000,
}

# Extra volatile months for BANKNIFTY (BN is higher-beta, more volatile)
BN_VOL_OVERRIDES = {
    "2020-02": 0.40, "2020-03": 0.80, "2020-04": 0.55, "2020-05": 0.38,
    "2018-10": 0.32, "2022-05": 0.30, "2022-06": 0.30, "2016-02": 0.30,
    "2021-11": 0.28,
}


def generate_banknifty_ohlcv(seed: int = 42) -> pd.DataFrame:
    """
    Generate daily BANKNIFTY OHLCV using the same Brownian-bridge technique
    as generate_nifty_ohlcv(), pinned to actual BankNifty monthly close levels.
    Returns DataFrame with Open, High, Low, Close, Volume.
    """
    rng  = np.random.default_rng(seed + 5)
    dates= pd.bdate_range("2015-01-01", "2025-05-23")
    anchor_series = pd.Series(
        {pd.Timestamp(k + "-01"): float(v) for k, v in BN_MONTHLY_ANCHORS.items()}
    )
    sorted_anchors = anchor_series.sort_index()
    anchor_months  = sorted_anchors.index.to_list()
    records = []

    BASE_VOL_ANN = 0.26   # BankNifty ~26% annual vol (vs NIFTY 17%)

    for i in range(len(anchor_months) - 1):
        m_start = anchor_months[i]
        m_end   = anchor_months[i + 1]
        seg_dates = dates[(dates >= m_start) & (dates < m_end)]
        if len(seg_dates) == 0:
            continue

        S0, S_T = float(sorted_anchors.iloc[i]), float(sorted_anchors.iloc[i+1])
        T        = len(seg_dates)
        month_key= m_start.strftime("%Y-%m")
        sigma_ann= BN_VOL_OVERRIDES.get(month_key, BASE_VOL_ANN)
        sigma_day= sigma_ann / np.sqrt(252)

        log_target = np.log(S_T / S0)
        bridge = np.zeros(T)
        for step in range(1, T):
            remaining     = T - step
            bb_drift      = (log_target - bridge[step-1]) / remaining
            eps           = t_dist.rvs(df=5, random_state=rng.integers(1_000_000_000)) * sigma_day
            bridge[step]  = bridge[step-1] + bb_drift + eps

        closes    = S0 * np.exp(bridge)
        closes[-1]= S_T

        for j, (dt, c) in enumerate(zip(seg_dates, closes)):
            atr_mult  = rng.exponential(0.9) + 0.2
            day_range = c * sigma_day * atr_mult * np.sqrt(2) * 1.5
            day_range = max(day_range, c * 0.002)
            if records:
                prev_c  = records[-1]["Close"]
                gap_pct = rng.normal(0, sigma_day * 0.4)
                o = prev_c * (1 + gap_pct)
            else:
                o = c * (1 + rng.normal(0, sigma_day * 0.3))
            mid = (o + c) / 2
            h   = max(mid + day_range/2, o, c)
            l   = min(mid - day_range/2, o, c)
            base_vol_idx = (bridge[j] - bridge[j-1]) if j > 0 else 0
            vol_scalar   = abs(base_vol_idx) / (sigma_day + 1e-9)
            volume       = max(int(rng.lognormal(np.log(8e7), 0.4) * (0.8 + 0.8*vol_scalar)), 1_000_000)
            records.append({"Date": dt, "Open": round(o,1), "High": round(h,1),
                            "Low": round(l,1), "Close": round(c,1), "Volume": volume})

    df = pd.DataFrame(records).set_index("Date")
    df.index = pd.to_datetime(df.index)
    return df.loc["2015-01-01":"2025-05-23"]

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
    nifty_df = generate_nifty_ohlcv(seed=seed)
    daily    = _gen_daily_series(BN_TICKERS, BN_PARAMS, BN_CORR, seed=seed+10)
    # Override ^NSEI and ^NSEBANK with properly monthly-anchored series
    nifty_reindexed = nifty_df.reindex(daily["^NSEI"].index).ffill().dropna()
    daily["^NSEI"]   = nifty_reindexed
    bn_anchored      = generate_banknifty_ohlcv(seed=seed)
    daily["^NSEBANK"]= bn_anchored.reindex(daily["^NSEBANK"].index).ffill().dropna()
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
