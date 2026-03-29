"""
Backtest of PDF strategies on Nifty (^NSEI) and BankNifty (^NSEBANK)
Timeframes : 1m, 2m, 5m, 15m
Author     : auto-generated
"""

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import json

# ─────────────────────────────────────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fetch(symbol, interval, days):
    """yfinance caps intraday history: 1m→7d, 2m/5m→60d, 15m→60d"""
    end = datetime.now()
    start = end - timedelta(days=days)
    df = yf.download(symbol, start=start, end=end,
                     interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
    else:
        df.index = df.index.tz_convert("Asia/Kolkata")
    # Keep only market hours 09:15–15:30 IST
    df = df.between_time("09:15", "15:30")
    # Flatten multi-index columns if any
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open","High","Low","Close","Volume"]].dropna()
    return df

def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()

def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l,
                    (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

# ─────────────────────────────────────────────────────────────────────────────
# GENERIC BACK-TESTER
# ─────────────────────────────────────────────────────────────────────────────

def backtest(df, signals_fn, rr=2.0, sl_atr_mult=1.5, label=""):
    """
    signals_fn(df) → Series of 1 (long), -1 (short), 0 (flat)
    Each signal is taken on the *next bar open*.
    Stop = sl_atr_mult × ATR(14) from entry bar.
    Target = rr × stop distance.
    Position closed at target, stop, or end-of-day.
    """
    df = df.copy()
    df["atr"] = atr(df)
    df["sig"] = signals_fn(df)

    trades = []
    i = 0
    n = len(df)
    while i < n - 1:
        sig = df["sig"].iloc[i]
        if sig == 0:
            i += 1
            continue

        entry_price = df["Open"].iloc[i + 1]
        entry_time  = df.index[i + 1]
        sl_dist     = df["atr"].iloc[i] * sl_atr_mult
        if pd.isna(sl_dist) or sl_dist <= 0:
            i += 1
            continue

        sl     = entry_price - sig * sl_dist
        target = entry_price + sig * sl_dist * rr
        entry_date = entry_time.date()

        result = None
        exit_price = None
        exit_time  = None

        for j in range(i + 1, n):
            bar = df.iloc[j]
            bar_date = df.index[j].date()

            # Force close at end of day
            if bar_date != entry_date:
                exit_price = df["Close"].iloc[j - 1]
                exit_time  = df.index[j - 1]
                result = "EOD"
                i = j
                break

            if sig == 1:           # LONG
                if bar["Low"] <= sl:
                    exit_price = sl;     exit_time = df.index[j]; result = "SL"; i = j + 1; break
                if bar["High"] >= target:
                    exit_price = target; exit_time = df.index[j]; result = "TP"; i = j + 1; break
            else:                  # SHORT
                if bar["High"] >= sl:
                    exit_price = sl;     exit_time = df.index[j]; result = "SL"; i = j + 1; break
                if bar["Low"] <= target:
                    exit_price = target; exit_time = df.index[j]; result = "TP"; i = j + 1; break
        else:
            exit_price = df["Close"].iloc[-1]
            exit_time  = df.index[-1]
            result = "EOD"
            i = n

        if exit_price is None:
            i += 1
            continue

        pnl = sig * (exit_price - entry_price)
        trades.append(dict(entry_time=entry_time, exit_time=exit_time,
                           direction="LONG" if sig == 1 else "SHORT",
                           entry=round(entry_price, 2), exit=round(exit_price, 2),
                           sl=round(sl, 2), target=round(target, 2),
                           pnl=round(pnl, 2), result=result, strategy=label))

    return pd.DataFrame(trades)

def summarise(trades_df, label=""):
    if trades_df.empty:
        return dict(strategy=label, trades=0, win_rate="N/A", net_pnl=0,
                    avg_win=0, avg_loss=0, profit_factor="N/A", max_dd=0)
    wins  = trades_df[trades_df["result"] == "TP"]
    loses = trades_df[trades_df["result"] == "SL"]
    n = len(trades_df)
    wr = round(len(wins) / n * 100, 1)
    net = round(trades_df["pnl"].sum(), 2)
    avg_w = round(wins["pnl"].mean(), 2) if not wins.empty else 0
    avg_l = round(loses["pnl"].mean(), 2) if not loses.empty else 0
    pf = round(wins["pnl"].sum() / abs(loses["pnl"].sum()), 2) \
         if not loses.empty and loses["pnl"].sum() != 0 else "∞"
    # max drawdown on cumulative pnl
    cum = trades_df["pnl"].cumsum()
    running_max = cum.cummax()
    dd = (running_max - cum).max()
    return dict(strategy=label, trades=n, win_rate=f"{wr}%",
                net_pnl=net, avg_win=avg_w, avg_loss=avg_l,
                profit_factor=pf, max_dd=round(dd, 2))

# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY SIGNAL FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

# ---- PDF-1 : 1. Open Drive (OD) ─ gap-up/down first candle momentum ---------
def sig_open_drive(df):
    prev_close = df["Close"].shift(1)
    gap_up   = (df["Open"] > prev_close * 1.002) & (df["Close"] > df["Open"])
    gap_down = (df["Open"] < prev_close * 0.998) & (df["Close"] < df["Open"])
    sig = pd.Series(0, index=df.index)
    sig[gap_up]   = 1
    sig[gap_down] = -1
    return sig

# ---- PDF-2 : 2. Morning Star (gap-down open, bullish reversal off PDL) ------
def sig_morning_star(df):
    pdl = df["Low"].shift(1)
    bullish = df["Close"] > df["Open"]
    bounced_pdl = (df["Low"] <= pdl) & (df["Close"] > pdl)
    sig = pd.Series(0, index=df.index)
    sig[bounced_pdl & bullish] = 1
    return sig

# ---- PDF-3 : 3. Evening Star (gap-up open, bearish reversal off PDH) --------
def sig_evening_star(df):
    pdh = df["High"].shift(1)
    bearish = df["Close"] < df["Open"]
    rejected_pdh = (df["High"] >= pdh) & (df["Close"] < pdh)
    sig = pd.Series(0, index=df.index)
    sig[rejected_pdh & bearish] = -1
    return sig

# ---- PDF-4 : 4. CPR Breakout ─ close above 20-bar highest high / below low --
def sig_cpr_breakout(df, n=20):
    hh = df["High"].rolling(n).max().shift(1)
    ll = df["Low"].rolling(n).min().shift(1)
    sig = pd.Series(0, index=df.index)
    sig[df["Close"] > hh] = 1
    sig[df["Close"] < ll] = -1
    return sig

# ---- PDF-5 : 5. Supply Zone Breakout ─ breakout above prior swing high ------
def sig_supply_zone_breakout(df, lookback=10):
    swing_high = df["High"].rolling(lookback).max().shift(1)
    swing_low  = df["Low"].rolling(lookback).min().shift(1)
    sig = pd.Series(0, index=df.index)
    sig[df["Close"] > swing_high] = 1
    sig[df["Close"] < swing_low]  = -1
    return sig

# ---- PDF-6 : 6. Opening Range Breakout (ORB 30-min) ------------------------
def sig_orb(df):
    sig = pd.Series(0, index=df.index)
    for date, grp in df.groupby(df.index.date):
        if len(grp) < 4:
            continue
        first_bars = grp.iloc[:4]   # first ~30 min (4 × some tf bars)
        orb_high = first_bars["High"].max()
        orb_low  = first_bars["Low"].min()
        rest = grp.iloc[4:]
        longs  = rest[rest["Close"] > orb_high].index
        shorts = rest[rest["Close"] < orb_low].index
        sig[longs]  = 1
        sig[shorts] = -1
    return sig

# ---- PDF-7 : 7. Day High Breakout -------------------------------------------
def sig_day_high_breakout(df):
    sig = pd.Series(0, index=df.index)
    for date, grp in df.groupby(df.index.date):
        running_high = grp["High"].cummax().shift(1)
        running_low  = grp["Low"].cummin().shift(1)
        longs  = grp[grp["Close"] > running_high].index
        shorts = grp[grp["Close"] < running_low].index
        sig[longs]  = 1
        sig[shorts] = -1
    return sig

# ---- PDF-8 : 8. Gap Filling -------------------------------------------------
def sig_gap_fill(df):
    prev_close = df["Close"].shift(1)
    gap_up   = df["Open"] > prev_close * 1.003
    gap_down = df["Open"] < prev_close * 0.997
    # Trade opposite: gap-up → short (fade gap), gap-down → long
    sig = pd.Series(0, index=df.index)
    sig[gap_down] = 1
    sig[gap_up]   = -1
    return sig

# ---- PDF-9 : 9. MA Trend Following (price crosses above 20 EMA) ------------
def sig_ma_trend(df):
    e20 = ema(df["Close"], 20)
    e8  = ema(df["Close"], 8)
    prev_e20 = e20.shift(1)
    prev_c   = df["Close"].shift(1)
    cross_up   = (prev_c <= prev_e20) & (df["Close"] > e20) & (e8 > e20)
    cross_down = (prev_c >= prev_e20) & (df["Close"] < e20) & (e8 < e20)
    sig = pd.Series(0, index=df.index)
    sig[cross_up]   = 1
    sig[cross_down] = -1
    return sig

# ---- PDF-10 : 10. PPT (Pivot Point Test) ─ price tests central pivot --------
def sig_ppt(df):
    pivot = (df["High"].shift(1) + df["Low"].shift(1) + df["Close"].shift(1)) / 3
    near_pivot = (df["Low"] <= pivot) & (df["High"] >= pivot)
    bullish = df["Close"] > df["Open"]
    bearish = df["Close"] < df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[near_pivot & bullish] = 1
    sig[near_pivot & bearish] = -1
    return sig

# ---- PDF-11 : 11. GCR ─ Green Candle Retracement ---------------------------
def sig_gcr(df):
    big_green = (df["Close"].shift(1) > df["Open"].shift(1)) & \
                ((df["Close"].shift(1) - df["Open"].shift(1)) >
                 df["Close"].shift(1).rolling(20).mean() * 0.005)
    pullback  = df["Low"] <= df["Open"].shift(1)
    bullish   = df["Close"] > df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[big_green & pullback & bullish] = 1
    return sig

# ---- PDF-12 : 12. RCR ─ Red Candle Retracement ------------------------------
def sig_rcr(df):
    big_red   = (df["Close"].shift(1) < df["Open"].shift(1)) & \
                ((df["Open"].shift(1) - df["Close"].shift(1)) >
                 df["Close"].shift(1).rolling(20).mean() * 0.005)
    pullback  = df["High"] >= df["Open"].shift(1)
    bearish   = df["Close"] < df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[big_red & pullback & bearish] = -1
    return sig

# ---- PDF-13 : 13. M Pattern ─ double-top short ------------------------------
def sig_m_pattern(df, n=10):
    rolling_high = df["High"].rolling(n).max()
    near_top = (df["High"] >= rolling_high * 0.998)
    # second touch of high + bearish candle
    prev_near = near_top.shift(n // 2)
    bearish   = df["Close"] < df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[prev_near & near_top & bearish] = -1
    return sig

# ---- PDF-14 : 14. W Pattern ─ double-bottom long ----------------------------
def sig_w_pattern(df, n=10):
    rolling_low = df["Low"].rolling(n).min()
    near_bottom = (df["Low"] <= rolling_low * 1.002)
    prev_near   = near_bottom.shift(n // 2)
    bullish     = df["Close"] > df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[prev_near & near_bottom & bullish] = 1
    return sig

# ---- PDF-15 : 15. ODR ─ Open Drive Rejection --------------------------------
def sig_odr(df):
    prev_close = df["Close"].shift(1)
    gap_up   = (df["Open"] > prev_close * 1.002)
    gap_down = (df["Open"] < prev_close * 0.998)
    # reject: gap-up then close back below open
    reject_up   = gap_up   & (df["Close"] < df["Open"])
    reject_down = gap_down & (df["Close"] > df["Open"])
    sig = pd.Series(0, index=df.index)
    sig[reject_up]   = -1
    sig[reject_down] = 1
    return sig

# ---- PDF-16 : 16. GCBO ─ Green Candle Breakout (close > big green candle's L) 
def sig_gcbo(df):
    big_green_low = df["Low"].shift(1)
    close_below   = df["Close"].shift(1) < big_green_low.shift(1)
    bearish_now   = df["Close"] < df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[close_below & bearish_now] = -1
    return sig

# ---- PDF-17 : 17. RCBO ─ Red Candle Breakout (close > big red candle's H) --
def sig_rcbo(df):
    big_red_high = df["High"].shift(1)
    close_above  = df["Close"].shift(1) > big_red_high.shift(1)
    bullish_now  = df["Close"] > df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[close_above & bullish_now] = 1
    return sig

# ---- PDF-18 : 18. Virgin CPR Reversal ─ use 5-bar CPR as proxy -------------
def sig_virgin_cpr(df, n=5):
    pivot = (df["High"].rolling(n).max() + df["Low"].rolling(n).min() +
             df["Close"].rolling(n).mean()) / 3
    prev_pivot = pivot.shift(n)
    # price reaches virgin CPR and reverses
    near_prev_pivot = (df["Low"] <= prev_pivot * 1.001) & (df["High"] >= prev_pivot * 0.999)
    bullish = df["Close"] > df["Open"]
    bearish = df["Close"] < df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[near_prev_pivot & bullish] = 1
    sig[near_prev_pivot & bearish] = -1
    return sig

# ---- PDF-19 : 19. Gap Down Rejection ----------------------------------------
def sig_gap_down_rejection(df):
    prev_close = df["Close"].shift(1)
    gap_down   = df["Open"] < prev_close * 0.997
    bullish    = df["Close"] > df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[gap_down & bullish] = 1
    return sig

# ---- PDF-20 : 20. Gap Up Rejection ------------------------------------------
def sig_gap_up_rejection(df):
    prev_close = df["Close"].shift(1)
    gap_up  = df["Open"] > prev_close * 1.003
    bearish = df["Close"] < df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[gap_up & bearish] = -1
    return sig

# ---- PDF-21 : 21. CPR Reversal ─ price reverses at 20-bar pivot level ------
def sig_cpr_reversal(df, n=20):
    pivot = (df["High"].rolling(n).max() + df["Low"].rolling(n).min() +
             df["Close"].rolling(n).mean()) / 3
    near = (df["Low"] <= pivot * 1.001) & (df["High"] >= pivot * 0.999)
    bullish = df["Close"] > df["Open"]
    bearish = df["Close"] < df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[near & bullish] = 1
    sig[near & bearish] = -1
    return sig

# ---- PDF-22 : 22. Extreme Candle Reversal -----------------------------------
def sig_extreme_candle_reversal(df, pct=0.02):
    body     = (df["Close"] - df["Open"]).abs()
    avg_body = body.rolling(20).mean()
    extreme  = body > avg_body * 2
    bullish  = df["Close"] > df["Open"]
    bearish  = df["Close"] < df["Open"]
    sig = pd.Series(0, index=df.index)
    # extreme red → next bar long; extreme green → next bar short
    sig[extreme.shift(1) & bearish.shift(1) & bullish] = 1
    sig[extreme.shift(1) & bullish.shift(1) & bearish] = -1
    return sig

# ---- PDF-23 : 23. Supply Zone Reversal ─ reject prior swing high ------------
def sig_supply_zone_reversal(df, n=20):
    swing_high = df["High"].rolling(n).max().shift(1)
    swing_low  = df["Low"].rolling(n).min().shift(1)
    near_high  = (df["High"] >= swing_high * 0.999) & (df["Close"] < swing_high)
    near_low   = (df["Low"]  <= swing_low  * 1.001) & (df["Close"] > swing_low)
    bearish    = df["Close"] < df["Open"]
    bullish    = df["Close"] > df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[near_high & bearish] = -1
    sig[near_low  & bullish] = 1
    return sig

# ---- PDF-24 : 24. Gap Border Rejection ─ previous gap acts as resistance ----
def sig_gap_border_rejection(df, n=5):
    gap_borders = df["Open"].shift(1)
    near = (df["High"] >= gap_borders * 0.999) & (df["Low"] <= gap_borders * 1.001)
    bearish = df["Close"] < df["Open"]
    bullish = df["Close"] > df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[near & bearish] = -1
    sig[near & bullish] = 1
    return sig

# ---- PDF-25 : 25. Fake Breakout (false break of day high/low) ---------------
def sig_fake_breakout(df, n=10):
    sig = pd.Series(0, index=df.index)
    for date, grp in df.groupby(df.index.date):
        running_high = grp["High"].shift(1).cummax()
        running_low  = grp["Low"].shift(1).cummin()
        # fake breakout up: high exceeded but close back below
        fb_up   = (grp["High"] > running_high) & (grp["Close"] < running_high)
        # fake breakout down: low exceeded but close back above
        fb_down = (grp["Low"] < running_low)   & (grp["Close"] > running_low)
        sig[fb_up]   = -1
        sig[fb_down] = 1
    return sig

# ---- Scalping PDF strategies (from 1-min scalping PDF) ----------------------

# S1: Counter Bull Traders Trap (downtrend: red closes below extreme green)
def sig_counter_bull_trap(df):
    big_green   = (df["Close"].shift(1) > df["Open"].shift(1)) & \
                  ((df["Close"].shift(1) - df["Open"].shift(1)) >
                   df["Close"].shift(1).rolling(20).mean() * 0.003)
    red_candle  = df["Close"] < df["Open"]
    close_below = df["Close"] < df["Close"].shift(1)
    e20 = ema(df["Close"], 20)
    downtrend   = df["Close"] < e20
    sig = pd.Series(0, index=df.index)
    sig[big_green & red_candle & close_below & downtrend] = -1
    return sig

# S2: Counter Bear Traders Trap (uptrend: green closes above extreme red)
def sig_counter_bear_trap(df):
    big_red    = (df["Close"].shift(1) < df["Open"].shift(1)) & \
                 ((df["Open"].shift(1) - df["Close"].shift(1)) >
                  df["Close"].shift(1).rolling(20).mean() * 0.003)
    green_candle = df["Close"] > df["Open"]
    close_above  = df["Close"] > df["Close"].shift(1)
    e20 = ema(df["Close"], 20)
    uptrend = df["Close"] > e20
    sig = pd.Series(0, index=df.index)
    sig[big_red & green_candle & close_above & uptrend] = 1
    return sig

# S3: V Reversal – 5+ consecutive red candles then first green
def sig_v_reversal(df, streak=4):
    consec_red = (df["Close"] < df["Open"])
    rolling_red = consec_red.rolling(streak).sum()
    green_now   = df["Close"] > df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[(rolling_red.shift(1) >= streak) & green_now] = 1
    # bearish version
    consec_green = (df["Close"] > df["Open"])
    rolling_green = consec_green.rolling(streak).sum()
    red_now = df["Close"] < df["Open"]
    sig[(rolling_green.shift(1) >= streak) & red_now] = -1
    return sig

# S4: Power Candle Pullback – big candle then pullback to candle edge
def sig_power_candle_pullback(df, mult=1.8):
    body = (df["Close"] - df["Open"]).abs()
    avg_body = body.rolling(20).mean()
    big_green = (df["Close"].shift(1) > df["Open"].shift(1)) & \
                (body.shift(1) > avg_body.shift(1) * mult)
    big_red   = (df["Close"].shift(1) < df["Open"].shift(1)) & \
                (body.shift(1) > avg_body.shift(1) * mult)
    pullback_to_low  = (df["Low"] <= df["Open"].shift(1)) & (df["Close"] > df["Open"])
    pullback_to_high = (df["High"] >= df["Open"].shift(1)) & (df["Close"] < df["Open"])
    sig = pd.Series(0, index=df.index)
    sig[big_green & pullback_to_low]  = 1
    sig[big_red   & pullback_to_high] = -1
    return sig

# S5: 3 EMA Trend (8/20/50) – price touches 20 EMA and bounces
def sig_3ema(df):
    e8  = ema(df["Close"], 8)
    e20 = ema(df["Close"], 20)
    e50 = ema(df["Close"], 50)
    uptrend   = (e8 > e20) & (e20 > e50)
    downtrend = (e8 < e20) & (e20 < e50)
    touch_e20_up   = (df["Low"] <= e20) & (df["Close"] > e20) & uptrend
    touch_e20_down = (df["High"] >= e20) & (df["Close"] < e20) & downtrend
    bullish = df["Close"] > df["Open"]
    bearish = df["Close"] < df["Open"]
    sig = pd.Series(0, index=df.index)
    sig[touch_e20_up   & bullish] = 1
    sig[touch_e20_down & bearish] = -1
    return sig

# ─────────────────────────────────────────────────────────────────────────────
# ALL STRATEGIES
# ─────────────────────────────────────────────────────────────────────────────

STRATEGIES = [
    ("S01_OpenDrive",           sig_open_drive),
    ("S02_MorningStar",         sig_morning_star),
    ("S03_EveningStar",         sig_evening_star),
    ("S04_CPR_Breakout",        sig_cpr_breakout),
    ("S05_SupplyZoneBreakout",  sig_supply_zone_breakout),
    ("S06_ORB",                 sig_orb),
    ("S07_DayHighBreakout",     sig_day_high_breakout),
    ("S08_GapFilling",          sig_gap_fill),
    ("S09_MATrendFollow",       sig_ma_trend),
    ("S10_PPT",                 sig_ppt),
    ("S11_GCR",                 sig_gcr),
    ("S12_RCR",                 sig_rcr),
    ("S13_MPattern",            sig_m_pattern),
    ("S14_WPattern",            sig_w_pattern),
    ("S15_ODR",                 sig_odr),
    ("S16_GCBO",                sig_gcbo),
    ("S17_RCBO",                sig_rcbo),
    ("S18_VirginCPR",           sig_virgin_cpr),
    ("S19_GapDownRejection",    sig_gap_down_rejection),
    ("S20_GapUpRejection",      sig_gap_up_rejection),
    ("S21_CPRReversal",         sig_cpr_reversal),
    ("S22_ExtremeCandle",       sig_extreme_candle_reversal),
    ("S23_SupplyZoneReversal",  sig_supply_zone_reversal),
    ("S24_GapBorderRejection",  sig_gap_border_rejection),
    ("S25_FakeBreakout",        sig_fake_breakout),
    ("S26_CounterBullTrap",     sig_counter_bull_trap),
    ("S27_CounterBearTrap",     sig_counter_bear_trap),
    ("S28_VReversal",           sig_v_reversal),
    ("S29_PowerCandlePullback", sig_power_candle_pullback),
    ("S30_3EMA",                sig_3ema),
]

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

SYMBOLS   = {"Nifty": "^NSEI", "BankNifty": "^NSEBANK"}
INTERVALS = {
    "1m":  7,
    "2m":  60,
    "5m":  60,
    "15m": 60,
}

all_results = []
all_trades  = []

for sym_name, ticker in SYMBOLS.items():
    for interval, days in INTERVALS.items():
        print(f"\n{'='*60}")
        print(f"  {sym_name} | {interval} | fetching {days} days ...")
        df = fetch(ticker, interval, days)
        if df.empty or len(df) < 50:
            print(f"  ⚠ insufficient data ({len(df)} bars) – skipping")
            continue
        print(f"  Bars loaded: {len(df)}  ({df.index[0].date()} → {df.index[-1].date()})")

        for strat_name, fn in STRATEGIES:
            label = f"{sym_name}|{interval}|{strat_name}"
            try:
                trades = backtest(df, fn, rr=2.0, sl_atr_mult=1.5, label=label)
                summ   = summarise(trades, label)
                all_results.append(summ)
                if not trades.empty:
                    all_trades.append(trades)
                print(f"  {strat_name:<35} trades={summ['trades']:>4}  "
                      f"WR={summ['win_rate']:>6}  PnL={summ['net_pnl']:>9.1f}  "
                      f"PF={summ['profit_factor']}")
            except Exception as e:
                print(f"  ✗ {strat_name}: {e}")

# ── write results ──────────────────────────────────────────────────────────
results_df = pd.DataFrame(all_results)
results_df.to_csv("/Users/rahulgirishkumar/TRADING/BlitzTrader/backtest_results.csv", index=False)

if all_trades:
    trades_df = pd.concat(all_trades, ignore_index=True)
    trades_df.to_csv("/Users/rahulgirishkumar/TRADING/BlitzTrader/backtest_trades.csv", index=False)

# ── ranked summary ─────────────────────────────────────────────────────────
print("\n\n" + "="*80)
print("  RESULTS RANKED BY NET PNL (top 20)")
print("="*80)
ranked = results_df[results_df["trades"] >= 5].copy()
ranked["net_pnl_num"] = pd.to_numeric(ranked["net_pnl"], errors="coerce")
ranked = ranked.sort_values("net_pnl_num", ascending=False).head(20)
print(ranked[["strategy","trades","win_rate","net_pnl","profit_factor","max_dd"]].to_string(index=False))

print(f"\nFull results → backtest_results.csv")
print(f"All trades  → backtest_trades.csv")
