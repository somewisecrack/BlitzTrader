"""
S&P 500 Momentum vs. Reversion backtest.

1. Pull S&P 500 tickers from Wikipedia.
2. Rank 1-day moves on the last trading day of 2025 (2025-12-31).
3. Build fixed top_20 (biggest gainers) and bot_20 (biggest losers) baskets.
4. Simulate two dollar-neutral portfolios (Momentum & Reversion) from 2026-01-01
   with monthly equal-weight rebalancing and intra-month weight drift.
5. Compare to SPY buy-and-hold.
6. Plot the three equity curves.
7. Print a summary performance table.
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yfinance as yf

# ---------------------------------------------------------------------------
# 1. S&P 500 tickers from Wikipedia
# ---------------------------------------------------------------------------
WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

print("Fetching S&P 500 constituents from Wikipedia...")
tables = pd.read_html(WIKI_URL)
sp500 = tables[0]
tickers = sp500["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
print(f"  Retrieved {len(tickers)} tickers.")

# ---------------------------------------------------------------------------
# 2. Rank 1-day % moves on last trading day of 2025
# ---------------------------------------------------------------------------
print("\nDownloading 2025-12-20 -> 2026-01-01 window for ranking...")
rank_px = yf.download(
    tickers,
    start="2025-12-20",
    end="2026-01-01",
    auto_adjust=True,
    progress=False,
)["Close"]

# Keep only trading days, drop tickers with no data
rank_px = rank_px.dropna(axis=1, how="all")

# Last available trading day of 2025 (should be 2025-12-31)
last_day = rank_px.index[-1]
prior_day = rank_px.index[-2]
print(f"  Ranking day:  {last_day.date()}  (prior close {prior_day.date()})")

pct_1d = (rank_px.loc[last_day] / rank_px.loc[prior_day] - 1.0) * 100.0
pct_1d = pct_1d.dropna().sort_values(ascending=False)

top_20 = pct_1d.head(20)
bot_20 = pct_1d.tail(20).sort_values()  # largest losses first

print("\n=== TOP 20 (largest 1-day gain on {}) ===".format(last_day.date()))
for t, v in top_20.items():
    print(f"  {t:<7} {v:+7.2f}%")

print("\n=== BOT 20 (largest 1-day loss on {}) ===".format(last_day.date()))
for t, v in bot_20.items():
    print(f"  {t:<7} {v:+7.2f}%")

top_list = top_20.index.tolist()
bot_list = bot_20.index.tolist()

# ---------------------------------------------------------------------------
# 3. Download the basket + SPY for the simulation window
# ---------------------------------------------------------------------------
today = pd.Timestamp.today().normalize()
print(f"\nDownloading basket + SPY: 2026-01-01 -> {today.date()} ...")

basket = top_list + bot_list + ["SPY"]
px = yf.download(
    basket,
    start="2026-01-01",
    end=(today + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
    auto_adjust=True,
    progress=False,
)["Close"]

px = px.dropna(how="all")
# Forward-fill occasional single-name gaps so weight drift stays well-defined
px = px.ffill()

spy = px["SPY"].dropna()
stock_px = px[top_list + bot_list].dropna(how="all")

print(f"  Simulation days: {len(stock_px)}  "
      f"({stock_px.index[0].date()} -> {stock_px.index[-1].date()})")

# ---------------------------------------------------------------------------
# 4. Dollar-neutral portfolio simulation
# ---------------------------------------------------------------------------
# Structure: 0.5 gross long, 0.5 gross short, equal weight within each leg.
#   Momentum : LONG top_20, SHORT bot_20
#   Reversion: SHORT top_20, LONG bot_20
# Rebalance on the 1st trading day of each calendar month; drift within month.
#   equity(t) = E_month_start * (1 + sum_i w_i * cum_ret_i(t))

LEG_GROSS = 0.5
w_long = LEG_GROSS / 20.0    # +0.025 each long name
w_short = -LEG_GROSS / 20.0  # -0.025 each short name


def simulate(prices: pd.DataFrame, long_names, short_names) -> pd.Series:
    """Simulate a dollar-neutral portfolio with monthly equal-weight resets."""
    fresh_w = pd.Series(0.0, index=prices.columns)
    fresh_w[long_names] = w_long
    fresh_w[short_names] = w_short

    dates = prices.index
    # Identify first trading day of each calendar month
    month_key = dates.to_period("M")
    is_month_start = pd.Series(month_key, index=dates).ne(
        pd.Series(month_key, index=dates).shift(1)
    )

    equity = pd.Series(index=dates, dtype=float)
    E_month_start = 1.0
    base_px = prices.iloc[0]  # price snapshot at current month start

    for i, d in enumerate(dates):
        if i == 0 or is_month_start.iloc[i]:
            # Reset: roll equity forward, re-snapshot base prices & weights
            if i != 0:
                E_month_start = equity.iloc[i - 1]
            base_px = prices.loc[d]
        cum_ret = prices.loc[d] / base_px - 1.0
        port_ret = (fresh_w * cum_ret).sum()
        equity.loc[d] = E_month_start * (1.0 + port_ret)

    return equity


momentum = simulate(stock_px, long_names=top_list, short_names=bot_list)
reversion = simulate(stock_px, long_names=bot_list, short_names=top_list)

# ---------------------------------------------------------------------------
# 5. SPY buy-and-hold, normalized to start = 1.0 on the same grid
# ---------------------------------------------------------------------------
spy_curve = (spy / spy.iloc[0]).reindex(stock_px.index).ffill()

# ---------------------------------------------------------------------------
# 6. Plot
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(12, 7))
ax.plot(momentum.index, momentum.values, label="Momentum (LONG top20 / SHORT bot20)",
        color="#1f77b4", linewidth=2)
ax.plot(reversion.index, reversion.values, label="Reversion (SHORT top20 / LONG bot20)",
        color="#d62728", linewidth=2)
ax.plot(spy_curve.index, spy_curve.values, label="SPY buy & hold",
        color="#2ca02c", linewidth=2, linestyle="--")
ax.axhline(1.0, color="black", linewidth=1, alpha=0.6)

ax.set_title("S&P 500 Momentum vs. Reversion — Dollar-Neutral Baskets (fixed 2025-12-31 ranking)",
             fontsize=13)
ax.set_xlabel("Date")
ax.set_ylabel("Equity (start = 1.0)")
ax.legend(loc="best", fontsize=10)
ax.grid(True, alpha=0.3)
fig.autofmt_xdate()
fig.tight_layout()

OUT_PNG = "momentum_reversion_backtest.png"
fig.savefig(OUT_PNG, dpi=130)
print(f"\nSaved plot to {OUT_PNG}")

# ---------------------------------------------------------------------------
# 7. Performance summary
# ---------------------------------------------------------------------------
TRADING_DAYS = 252


def perf_stats(equity: pd.Series) -> dict:
    equity = equity.dropna()
    rets = equity.pct_change().dropna()
    n = len(equity)
    total_ret = equity.iloc[-1] / equity.iloc[0] - 1.0
    ann_ret = (equity.iloc[-1] / equity.iloc[0]) ** (TRADING_DAYS / max(n - 1, 1)) - 1.0
    ann_vol = rets.std() * np.sqrt(TRADING_DAYS)
    sharpe = (rets.mean() * TRADING_DAYS) / ann_vol if ann_vol > 0 else np.nan
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = drawdown.min()
    return {
        "Total Return": total_ret,
        "Annualized Return": ann_ret,
        "Annualized Vol": ann_vol,
        "Sharpe (rf=0)": sharpe,
        "Max Drawdown": max_dd,
    }


summary = pd.DataFrame({
    "Momentum": perf_stats(momentum),
    "Reversion": perf_stats(reversion),
    "SPY B&H": perf_stats(spy_curve),
}).T

fmt = summary.copy()
for col in ["Total Return", "Annualized Return", "Annualized Vol", "Max Drawdown"]:
    fmt[col] = fmt[col].map(lambda x: f"{x*100:+.2f}%")
fmt["Sharpe (rf=0)"] = fmt["Sharpe (rf=0)"].map(lambda x: f"{x:.2f}")

print("\n=== PERFORMANCE SUMMARY "
      f"({stock_px.index[0].date()} -> {stock_px.index[-1].date()}) ===")
print(fmt.to_string())
