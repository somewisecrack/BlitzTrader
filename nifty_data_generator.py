"""
Synthetic NIFTY 50 Data Generator
===================================
Generates historically-calibrated NIFTY 50 OHLCV data (Jan 2015 – May 2025)
using monthly anchor prices drawn from actual index levels, then fills in
daily bars via Geometric Brownian Motion with regime-switching volatility,
fat-tailed innovations, and volume correlated with volatility.

Monthly anchors are manually set from training-data knowledge of actual
NIFTY 50 close levels.
"""

import numpy as np
import pandas as pd
from scipy.stats import t as t_dist

# ─────────────────────────────────────────────────────────────
# Monthly anchor prices (approximate actual NIFTY 50 index close)
# Source: knowledge of actual NIFTY 50 history
# ─────────────────────────────────────────────────────────────
MONTHLY_ANCHORS = {
    "2015-01": 8561,
    "2015-02": 8902,
    "2015-03": 8492,
    "2015-04": 8181,
    "2015-05": 8434,
    "2015-06": 8369,
    "2015-07": 8533,
    "2015-08": 7971,
    "2015-09": 7948,
    "2015-10": 8065,
    "2015-11": 7935,
    "2015-12": 7946,
    "2016-01": 7563,
    "2016-02": 6987,
    "2016-03": 7738,
    "2016-04": 7850,
    "2016-05": 8161,
    "2016-06": 8288,
    "2016-07": 8638,
    "2016-08": 8786,
    "2016-09": 8611,
    "2016-10": 8625,
    "2016-11": 8224,
    "2016-12": 8185,
    "2017-01": 8563,
    "2017-02": 8879,
    "2017-03": 9173,
    "2017-04": 9304,
    "2017-05": 9621,
    "2017-06": 9521,
    "2017-07": 10020,
    "2017-08": 9918,
    "2017-09": 9788,
    "2017-10": 10335,
    "2017-11": 10226,
    "2017-12": 10531,
    "2018-01": 11028,
    "2018-02": 10492,
    "2018-03": 10114,
    "2018-04": 10739,
    "2018-05": 10736,
    "2018-06": 10714,
    "2018-07": 11356,
    "2018-08": 11680,
    "2018-09": 11008,
    "2018-10": 10386,
    "2018-11": 10877,
    "2018-12": 10863,
    "2019-01": 10831,
    "2019-02": 10793,
    "2019-03": 11624,
    "2019-04": 11748,
    "2019-05": 11922,
    "2019-06": 11788,
    "2019-07": 11118,
    "2019-08": 10948,
    "2019-09": 11475,
    "2019-10": 11878,
    "2019-11": 12056,
    "2019-12": 12168,
    "2020-01": 11963,
    "2020-02": 11202,
    "2020-03": 8598,    # COVID crash
    "2020-04": 9860,
    "2020-05": 9580,
    "2020-06": 10302,
    "2020-07": 11073,
    "2020-08": 11387,
    "2020-09": 11248,
    "2020-10": 11642,
    "2020-11": 12968,
    "2020-12": 13982,
    "2021-01": 13635,
    "2021-02": 14529,
    "2021-03": 14691,
    "2021-04": 14631,
    "2021-05": 15582,
    "2021-06": 15722,
    "2021-07": 15763,
    "2021-08": 16705,
    "2021-09": 17618,
    "2021-10": 18115,
    "2021-11": 16983,
    "2021-12": 17354,
    "2022-01": 17340,
    "2022-02": 16793,
    "2022-03": 17465,
    "2022-04": 17103,
    "2022-05": 16584,
    "2022-06": 15780,
    "2022-07": 16929,
    "2022-08": 17759,
    "2022-09": 16888,
    "2022-10": 18012,
    "2022-11": 18758,
    "2022-12": 18105,
    "2023-01": 17604,
    "2023-02": 17303,
    "2023-03": 17360,
    "2023-04": 18065,
    "2023-05": 18535,
    "2023-06": 18935,
    "2023-07": 19753,
    "2023-08": 19674,
    "2023-09": 19638,
    "2023-10": 19000,
    "2023-11": 19795,
    "2023-12": 21731,
    "2024-01": 21725,
    "2024-02": 22405,
    "2024-03": 22326,
    "2024-04": 22420,
    "2024-05": 23350,
    "2024-06": 23686,
    "2024-07": 24951,
    "2024-08": 25235,
    "2024-09": 26178,
    "2024-10": 23911,
    "2024-11": 23644,
    "2024-12": 23645,
    "2025-01": 23163,
    "2025-02": 22124,
    "2025-03": 23519,
    "2025-04": 24039,
    "2025-05": 24750,
}

# Volatility regimes (monthly, annualised daily vol)
VOL_OVERRIDES = {
    "2020-02": 0.35,
    "2020-03": 0.65,
    "2020-04": 0.45,
    "2020-05": 0.30,
    "2018-10": 0.28,
    "2022-05": 0.26,
    "2022-06": 0.26,
}


def generate_nifty_ohlcv(seed: int = 42) -> pd.DataFrame:
    """
    Generate daily NIFTY 50 OHLCV calibrated to monthly anchor prices.
    Returns a DataFrame with columns: Open, High, Low, Close, Volume
    and a DatetimeIndex of trading days.
    """
    rng = np.random.default_rng(seed)

    # ── Build business-day calendar ────────────────────────────
    dates = pd.bdate_range("2015-01-01", "2025-05-23")

    anchor_series = pd.Series(
        {pd.Timestamp(k + "-01"): float(v) for k, v in MONTHLY_ANCHORS.items()}
    )

    # ── Interpolate monthly anchors to daily closes ────────────
    # We build close prices by bridging each month via GBM pinned to endpoints
    records = []
    sorted_anchors = anchor_series.sort_index()
    anchor_months  = sorted_anchors.index.to_list()

    for i in range(len(anchor_months) - 1):
        m_start = anchor_months[i]
        m_end   = anchor_months[i + 1]

        # Business days in this segment (excluding m_end which belongs to next)
        seg_dates = dates[(dates >= m_start) & (dates < m_end)]
        if len(seg_dates) == 0:
            continue

        S0   = float(sorted_anchors.iloc[i])
        S_T  = float(sorted_anchors.iloc[i + 1])
        T    = len(seg_dates)

        # Drift that pins close to S_T at end
        month_key = m_start.strftime("%Y-%m")
        sigma_ann  = VOL_OVERRIDES.get(month_key, 0.17)
        sigma_day  = sigma_ann / np.sqrt(252)

        # Use Brownian bridge: X_t = log(S_t/S0)
        # dX = drift*dt + sigma*dW   with X_T = log(S_T/S0)
        log_target = np.log(S_T / S0)
        bridge = np.zeros(T)
        for step in range(1, T):
            # Brownian bridge drift = (target - current_pos) / remaining_steps
            remaining  = T - step
            bb_drift   = (log_target - bridge[step - 1]) / remaining
            # Fat-tailed innovations (Student t, df=5)
            eps = t_dist.rvs(df=5, random_state=rng.integers(1e9)) * sigma_day
            bridge[step] = bridge[step - 1] + bb_drift + eps

        closes = S0 * np.exp(bridge)
        closes[-1] = S_T   # exact pin

        for j, (dt, c) in enumerate(zip(seg_dates, closes)):
            # Intraday range via ATR model
            base_vol  = sigma_day
            atr_mult  = rng.exponential(0.9) + 0.2   # ~U[0.2, 2]
            day_range = c * base_vol * atr_mult * np.sqrt(2) * 1.5
            day_range = max(day_range, c * 0.002)    # min 0.2%

            # Open: yesterday's close ± small gap
            if records:
                prev_c  = records[-1]["Close"]
                gap_pct = rng.normal(0, sigma_day * 0.4)
                o = prev_c * (1 + gap_pct)
            else:
                o = c * (1 + rng.normal(0, sigma_day * 0.3))

            # Place High/Low symmetrically around midpoint of O and C
            mid = (o + c) / 2
            h   = mid + day_range / 2
            l   = mid - day_range / 2
            # Ensure H >= max(O,C) and L <= min(O,C)
            h = max(h, o, c)
            l = min(l, o, c)

            # Volume: correlated with volatility
            base_vol_idx = (bridge[j] - bridge[j-1]) if j > 0 else 0
            vol_scalar   = abs(base_vol_idx) / (sigma_day + 1e-9)
            # Base volume ~300M–500M units, spikes on big moves
            base_volume  = rng.lognormal(np.log(3.5e8), 0.4)
            volume       = base_volume * (0.8 + 0.8 * vol_scalar)
            volume       = max(int(volume), 10_000_000)

            records.append({
                "Date"  : dt,
                "Open"  : round(o, 2),
                "High"  : round(h, 2),
                "Low"   : round(l, 2),
                "Close" : round(c, 2),
                "Volume": int(volume),
            })

    df = pd.DataFrame(records).set_index("Date")
    df.index = pd.to_datetime(df.index)
    # Clip to requested range
    df = df.loc["2015-01-01":"2025-05-23"]
    return df


if __name__ == "__main__":
    df = generate_nifty_ohlcv()
    print(f"Generated {len(df)} days of NIFTY data")
    print(f"Range: {df.index[0].date()} → {df.index[-1].date()}")
    print(f"Start close: {df['Close'].iloc[0]:.0f}")
    print(f"End close  : {df['Close'].iloc[-1]:.0f}")
    print(f"CAGR       : {((df['Close'].iloc[-1]/df['Close'].iloc[0])**(252/len(df))-1)*100:.1f}%")
    print(f"Annualised vol: {df['Close'].pct_change().std()*np.sqrt(252)*100:.1f}%")
    df.to_csv("nifty_synthetic_ohlcv.csv")
    print("Saved nifty_synthetic_ohlcv.csv")
