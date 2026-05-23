"""
₹1,00,000 One-Year NCLI Strategy Simulation
=============================================
Instrument : NIFTY 50 ETF (e.g. NiftyBees / NIFTYBEES)
Capital    : ₹1,00,000
Period     : last 252 trading days in the OOS window
Strategy   : NCLI₁ (n=1d) — long when NCLI>30, short when NCLI<-30
             Also runs NCLI₂ (n=2d) for comparison

Indian market costs applied per round trip:
  STT          : 0.025% on sell (delivery)
  Exchange fees: 0.00325%
  Stamp duty   : 0.015% on buy
  Brokerage    : ₹40 per round trip (₹20 each leg, flat fee broker)
  GST on brok  : 18%  → ₹47.2 per RT
  Slippage     : 0.05% per side (conservative for NIFTY ETF)
  Total RT cost: ~0.15% of trade value (conservative)
"""

import sys, os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_asset_generator import generate_multi_asset
from nifty_data_generator   import generate_nifty_ohlcv
from nifty_ncli             import (build_sub_signals, compute_ic_weights,
                                     build_ncli)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ncli_output")
os.makedirs(OUT, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────
STARTING_CAPITAL  = 100_000          # ₹1,00,000
NCLI_THRESHOLD    = 30               # enter long/short when |NCLI| > this
TRAIN_PCT         = 0.65             # must match nifty_ncli.py
RT_COST_PCT       = 0.15 / 100       # round-trip cost as fraction of trade value
BROKERAGE_RT      = 47.2             # flat brokerage per round trip (incl GST)

def simulate(prices, ohlcv, ncli_series, n_hold, label,
             threshold=NCLI_THRESHOLD, capital=STARTING_CAPITAL,
             sim_days=252):
    """
    Simulate ₹1,00,000 over last `sim_days` trading days of OOS period.
    Each signal holds for n_hold days then exits.
    No overlapping positions.
    """
    nifty_close = prices["^NSEI"]

    # OOS window
    split    = int(len(prices) * TRAIN_PCT)
    oos_idx  = prices.index[split:]

    # Last sim_days of OOS
    if len(oos_idx) > sim_days:
        sim_idx = oos_idx[-sim_days:]
    else:
        sim_idx = oos_idx

    ncli_sim  = ncli_series.reindex(sim_idx)
    price_sim = nifty_close.reindex(sim_idx)

    # ── Trade log ─────────────────────────────────────────────
    equity      = capital
    equity_curve= [equity]
    trades      = []
    hold_until  = -1     # day index until which current position runs

    prices_arr  = price_sim.values
    ncli_arr    = ncli_sim.values
    dates_arr   = sim_idx

    for i in range(len(prices_arr) - n_hold):
        if i < hold_until:
            # Still in a position — record equity mark-to-market
            equity_curve.append(equity)
            continue

        sig = ncli_arr[i]
        if abs(sig) <= threshold:
            equity_curve.append(equity)
            continue

        direction  = 1 if sig > 0 else -1   # +1=long, -1=short
        entry_px   = prices_arr[i]
        exit_px    = prices_arr[i + n_hold]

        # Units we can buy (whole ETF units)
        units      = int(equity / entry_px)
        if units == 0:
            equity_curve.append(equity)
            continue

        trade_val  = units * entry_px

        # P&L before costs
        gross_pnl  = direction * units * (exit_px - entry_px)

        # Transaction costs
        cost       = trade_val * RT_COST_PCT + BROKERAGE_RT

        net_pnl    = gross_pnl - cost
        equity    += net_pnl

        # Record
        trades.append({
            "entry_date"  : dates_arr[i],
            "exit_date"   : dates_arr[i + n_hold],
            "direction"   : "LONG" if direction == 1 else "SHORT",
            "entry_px"    : round(entry_px, 2),
            "exit_px"     : round(exit_px, 2),
            "units"       : units,
            "trade_val"   : round(trade_val, 2),
            "gross_pnl"   : round(gross_pnl, 2),
            "cost"        : round(cost, 2),
            "net_pnl"     : round(net_pnl, 2),
            "equity_after": round(equity, 2),
            "ncli"        : round(sig, 1),
        })

        hold_until = i + n_hold
        # Fill equity during hold period
        for j in range(n_hold):
            if i + j < len(prices_arr):
                equity_curve.append(equity)

    # Pad equity curve to sim_days
    while len(equity_curve) < sim_days:
        equity_curve.append(equity)
    equity_curve = equity_curve[:sim_days]

    trades_df = pd.DataFrame(trades)

    # ── Buy-and-hold benchmark ────────────────────────────────
    bh_units    = int(capital / prices_arr[0])
    bh_equity   = capital + bh_units * (prices_arr - prices_arr[0])
    bh_final    = capital + bh_units * (prices_arr[-1] - prices_arr[0])

    # ── Stats ─────────────────────────────────────────────────
    final_equity = equity_curve[-1]
    ret_pct      = (final_equity - capital) / capital * 100
    bh_ret_pct   = (bh_final - capital) / capital * 100

    if len(trades_df) > 0:
        win_trades   = (trades_df["net_pnl"] > 0).sum()
        loss_trades  = (trades_df["net_pnl"] <= 0).sum()
        win_rate     = win_trades / len(trades_df) * 100
        avg_win      = trades_df.loc[trades_df["net_pnl"]>0, "net_pnl"].mean() if win_trades else 0
        avg_loss     = trades_df.loc[trades_df["net_pnl"]<=0,"net_pnl"].mean() if loss_trades else 0
        total_cost   = trades_df["cost"].sum()
        profit_factor= (-trades_df.loc[trades_df["net_pnl"]>0,"net_pnl"].sum() /
                         trades_df.loc[trades_df["net_pnl"]<=0,"net_pnl"].sum()
                         if loss_trades and trades_df.loc[trades_df["net_pnl"]<=0,"net_pnl"].sum() != 0
                         else float("inf"))
    else:
        win_rate = avg_win = avg_loss = total_cost = 0
        profit_factor = 0

    eq_series = pd.Series(equity_curve, index=sim_idx[:len(equity_curve)])
    dd        = ((eq_series - eq_series.cummax()) / eq_series.cummax() * 100).min()

    stats = {
        "label"          : label,
        "period_start"   : str(sim_idx[0].date()),
        "period_end"     : str(sim_idx[-1].date()),
        "trading_days"   : len(sim_idx),
        "starting_cap"   : capital,
        "final_equity"   : round(final_equity, 2),
        "net_profit"     : round(final_equity - capital, 2),
        "return_pct"     : round(ret_pct, 2),
        "bh_final"       : round(bh_final, 2),
        "bh_return_pct"  : round(bh_ret_pct, 2),
        "n_trades"       : len(trades_df),
        "n_long"         : int((trades_df["direction"]=="LONG").sum())  if len(trades_df) else 0,
        "n_short"        : int((trades_df["direction"]=="SHORT").sum()) if len(trades_df) else 0,
        "win_rate_pct"   : round(win_rate, 1),
        "avg_win_rs"     : round(avg_win, 2),
        "avg_loss_rs"    : round(avg_loss, 2),
        "profit_factor"  : round(profit_factor, 2),
        "total_costs_rs" : round(total_cost, 2),
        "max_drawdown_pct": round(dd, 2),
    }

    return stats, trades_df, eq_series, pd.Series(bh_equity,
                                                    index=price_sim.index[:len(bh_equity)])


def make_simulation_chart(stats1, trades1, equity1, bh1,
                          stats2, trades2, equity2, bh2):
    """One comprehensive chart showing both strategies."""
    C_long  = "#1B5E20"
    C_short = "#B71C1C"
    C_bh    = "#90A4AE"
    C1      = "#1565C0"
    C2      = "#6A1B9A"

    fig = plt.figure(figsize=(18, 16))
    gs  = gridspec.GridSpec(4, 2, hspace=0.45, wspace=0.3)
    fig.suptitle(
        f"₹1,00,000 → ?   |   NIFTY Composite Leading Indicator (NCLI) Strategy\n"
        f"Period: {stats1['period_start']} → {stats1['period_end']}  "
        f"(1 year OOS backtest)  |  Instrument: NIFTY ETF  |  "
        f"Transaction cost: {RT_COST_PCT*100:.2f}% + ₹{BROKERAGE_RT:.0f} flat per round trip",
        fontsize=11, fontweight="bold"
    )

    # ── Row 0: Equity curves ──────────────────────────────────
    for col, stats, eq, bh, color, label in [
        (0, stats1, equity1, bh1, C1, "NCLI₁  (hold 1 day)"),
        (1, stats2, equity2, bh2, C2, "NCLI₂  (hold 2 days)"),
    ]:
        ax = fig.add_subplot(gs[0, col])
        ax.plot(eq.index,  eq.values,  color=color, lw=2,
                label=f"NCLI strategy  →  ₹{stats['final_equity']:,.0f}")
        ax.plot(bh.index, bh.values, color=C_bh, lw=1.2, ls="--",
                label=f"Buy & Hold      →  ₹{stats['bh_final']:,.0f}")
        ax.axhline(stats["starting_cap"], color="grey", lw=0.8, ls=":")
        ax.fill_between(eq.index, eq.values, stats["starting_cap"],
                        where=eq.values >= stats["starting_cap"],
                        alpha=0.15, color=C_long)
        ax.fill_between(eq.index, eq.values, stats["starting_cap"],
                        where=eq.values < stats["starting_cap"],
                        alpha=0.15, color=C_short)
        ret_tag = f"+{stats['return_pct']:.1f}%" if stats["return_pct"]>=0 \
                  else f"{stats['return_pct']:.1f}%"
        ax.set_title(f"{label}\n"
                     f"Return: {ret_tag}  |  "
                     f"Trades: {stats['n_trades']}  |  "
                     f"Win: {stats['win_rate_pct']:.0f}%  |  "
                     f"MaxDD: {stats['max_drawdown_pct']:.1f}%",
                     fontsize=9)
        ax.set_ylabel("Portfolio Value (₹)"); ax.legend(fontsize=8)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"₹{x:,.0f}"))
        ax.grid(alpha=0.25)

    # ── Row 1: Per-trade P&L bars ─────────────────────────────
    for col, trades, color, label in [
        (0, trades1, C1, "NCLI₁"),
        (1, trades2, C2, "NCLI₂"),
    ]:
        ax = fig.add_subplot(gs[1, col])
        if len(trades):
            pnls   = trades["net_pnl"].values
            dirs   = trades["direction"].values
            colors = [C_long if d=="LONG" else C_short for d in dirs]
            ax.bar(range(len(pnls)), pnls, color=colors, alpha=0.8, width=0.7)
            ax.axhline(0, color="grey", lw=0.8)
            # Running cumulative
            ax2 = ax.twinx()
            ax2.plot(np.cumsum(pnls), color=color, lw=1.5, label="Cumulative P&L")
            ax2.set_ylabel("Cumulative P&L (₹)", color=color, fontsize=8)
        ax.set_title(f"{label}: Per-Trade Net P&L\n"
                     f"Avg win: ₹{trades['net_pnl'][trades['net_pnl']>0].mean():.0f}  "
                     f"Avg loss: ₹{trades['net_pnl'][trades['net_pnl']<=0].mean():.0f}" if len(trades) else f"{label}: No trades",
                     fontsize=9)
        ax.set_xlabel("Trade number"); ax.set_ylabel("Net P&L per trade (₹)")
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(fc=C_long,label="Long"),Patch(fc=C_short,label="Short")],
                  fontsize=8, loc="upper left")
        ax.grid(alpha=0.2)

    # ── Row 2: Monthly returns ────────────────────────────────
    for col, eq, color, label in [
        (0, equity1, C1, "NCLI₁"),
        (1, equity2, C2, "NCLI₂"),
    ]:
        ax = fig.add_subplot(gs[2, col])
        monthly = eq.resample("ME").last().pct_change().dropna() * 100
        bar_colors = [C_long if r >= 0 else C_short for r in monthly.values]
        bars = ax.bar(range(len(monthly)), monthly.values,
                      color=bar_colors, alpha=0.85)
        ax.axhline(0, color="grey", lw=0.8)
        for bar, r in zip(bars, monthly.values):
            ax.text(bar.get_x()+bar.get_width()/2,
                    r + (0.3 if r >= 0 else -0.8),
                    f"{r:.1f}%", ha="center", fontsize=7.5)
        ax.set_xticks(range(len(monthly)))
        ax.set_xticklabels([d.strftime("%b\n%y") for d in monthly.index], fontsize=8)
        ax.set_title(f"{label}: Monthly Returns", fontsize=9)
        ax.set_ylabel("Return (%)"); ax.grid(alpha=0.2)

    # ── Row 3: Summary scorecard ──────────────────────────────
    ax = fig.add_subplot(gs[3, :])
    ax.axis("off")

    rows = [
        ["Metric", "NCLI₁ (hold 1 day)", "NCLI₂ (hold 2 days)", "Buy & Hold"],
        ["Starting capital", "₹1,00,000", "₹1,00,000", "₹1,00,000"],
        ["Ending value", f"₹{stats1['final_equity']:,.0f}",
                        f"₹{stats2['final_equity']:,.0f}",
                        f"₹{stats1['bh_final']:,.0f}"],
        ["Net profit / loss",
            f"₹{stats1['net_profit']:+,.0f}  ({stats1['return_pct']:+.1f}%)",
            f"₹{stats2['net_profit']:+,.0f}  ({stats2['return_pct']:+.1f}%)",
            f"₹{stats1['bh_final']-100000:+,.0f}  ({stats1['bh_return_pct']:+.1f}%)"],
        ["Total trades", str(stats1["n_trades"]),  str(stats2["n_trades"]),  "—"],
        ["Long / Short", f"{stats1['n_long']} / {stats1['n_short']}",
                        f"{stats2['n_long']} / {stats2['n_short']}", "—"],
        ["Win rate",    f"{stats1['win_rate_pct']:.1f}%",
                        f"{stats2['win_rate_pct']:.1f}%", "—"],
        ["Avg win / Avg loss",
            f"₹{stats1['avg_win_rs']:,.0f}  /  ₹{stats1['avg_loss_rs']:,.0f}",
            f"₹{stats2['avg_win_rs']:,.0f}  /  ₹{stats2['avg_loss_rs']:,.0f}", "—"],
        ["Total transaction cost", f"₹{stats1['total_costs_rs']:,.0f}",
                                   f"₹{stats2['total_costs_rs']:,.0f}", "~₹200"],
        ["Max drawdown",  f"{stats1['max_drawdown_pct']:.1f}%",
                          f"{stats2['max_drawdown_pct']:.1f}%",
                          f"{((pd.Series(bh1.values)/bh1.values[0]-1).min()*100):.1f}%"],
    ]

    tbl = ax.table(cellText=rows[1:], colLabels=rows[0],
                   cellLoc="center", loc="center",
                   bbox=[0, 0, 1, 1])
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    for (r, c), cell in tbl.get_celld().items():
        if r == 0:
            cell.set_facecolor("#263238")
            cell.set_text_props(color="white", fontweight="bold")
        elif r == 2:   # ending value row
            cell.set_facecolor("#E8F5E9" if c in [1,2] else "#ECEFF1")
        elif r % 2 == 0:
            cell.set_facecolor("#F5F5F5")
        cell.set_edgecolor("#CFD8DC")

    plt.savefig(os.path.join(OUT, "NCLI_simulation.png"), dpi=130)
    plt.close()
    print(f"  Chart saved → {OUT}/NCLI_simulation.png")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 62)
    print("  ₹1,00,000 ONE-YEAR NCLI SIMULATION")
    print("=" * 62)

    print("\nLoading data and rebuilding NCLI …")
    prices = generate_multi_asset(seed=42)
    ohlcv  = generate_nifty_ohlcv(seed=42).reindex(prices.index).ffill()
    S      = build_sub_signals(prices, ohlcv)
    ic_w   = compute_ic_weights(S, prices["^NSEI"], train_end_pct=TRAIN_PCT)
    ncli_1, _, _ = build_ncli(S, ic_w, 1, top_k=15)
    ncli_2, _, _ = build_ncli(S, ic_w, 2, top_k=15)

    print("\nRunning simulations …")
    stats1, trades1, eq1, bh1 = simulate(prices, ohlcv, ncli_1, 1, "NCLI₁")
    stats2, trades2, eq2, bh2 = simulate(prices, ohlcv, ncli_2, 2, "NCLI₂")

    print("\nGenerating chart …")
    make_simulation_chart(stats1, trades1, eq1, bh1,
                          stats2, trades2, eq2, bh2)

    # ── Print summary ──────────────────────────────────────────
    print("\n" + "=" * 62)
    print("  SIMULATION RESULTS")
    print("=" * 62)
    print(f"\n  Period : {stats1['period_start']} → {stats1['period_end']}")
    print(f"  Days   : {stats1['trading_days']} trading days (~1 year OOS)")
    print(f"  Costs  : {RT_COST_PCT*100:.2f}% of trade value + "
          f"₹{BROKERAGE_RT:.0f} flat brokerage per round trip")
    print()

    for s in [stats1, stats2]:
        profit_sign = "+" if s["net_profit"] >= 0 else ""
        print(f"  ── {s['label']} ──────────────────────────────────────")
        print(f"  Starting capital   : ₹{s['starting_cap']:>10,.0f}")
        print(f"  Final value        : ₹{s['final_equity']:>10,.0f}")
        print(f"  Net profit / loss  :  {profit_sign}₹{abs(s['net_profit']):,.0f}  "
              f"({profit_sign}{s['return_pct']:.1f}%)")
        print(f"  Buy-and-hold value : ₹{s['bh_final']:>10,.0f}  "
              f"({'+' if s['bh_return_pct']>=0 else ''}{s['bh_return_pct']:.1f}%)")
        print(f"  ─")
        print(f"  Total trades       : {s['n_trades']}  "
              f"(long={s['n_long']}, short={s['n_short']})")
        print(f"  Win rate           : {s['win_rate_pct']:.1f}%")
        print(f"  Avg winning trade  : +₹{s['avg_win_rs']:,.0f}")
        print(f"  Avg losing trade   :  ₹{s['avg_loss_rs']:,.0f}")
        print(f"  Profit factor      : {s['profit_factor']:.2f}x")
        print(f"  Transaction costs  : ₹{s['total_costs_rs']:,.0f} total")
        print(f"  Max drawdown       : {s['max_drawdown_pct']:.1f}%")
        print()

    print(f"  Chart → {OUT}/NCLI_simulation.png")
    print("=" * 62)


if __name__ == "__main__":
    main()
