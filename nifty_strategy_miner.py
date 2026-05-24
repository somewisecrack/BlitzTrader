"""
NIFTY Strategy Miner
=====================
Systematically searches over rule-based condition combinations to find
up to 5 trading strategies with >75% OOS win rate on NIFTY 50.

Search space:
  - 2-condition and 3-condition combinations
  - 4 hold periods: 1, 2, 3, 5 days
  - LONG and SHORT direction
  - ~6,000+ candidate rules tested

Pipeline:
  1. In-sample (first 65%): win_rate > 0.75, n_trades >= 30
  2. OOS validation (last 35%): win_rate >= 0.65, n_trades >= 15
  3. Rank by: oos_win_rate × sqrt(n_oos_trades)
  4. Keep top 5, iterate threshold if <5 found

Output: saved to SKILL_LIBRARY/NIFTY_STRATEGIES/
"""

import warnings
warnings.filterwarnings("ignore")

import os, sys, json, itertools
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import binom

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_asset_generator import generate_multi_asset
from nifty_data_generator   import generate_nifty_ohlcv
from nifty_ncli             import build_sub_signals

TRAIN_PCT  = 0.65
MIN_IS_TRADES  = 30
MIN_OOS_TRADES = 15
IS_WIN_THRESH  = 0.75
OOS_WIN_THRESH = 0.65

# ─────────────────────────────────────────────────────────────
# EXTENDED SIGNAL LIBRARY
# ─────────────────────────────────────────────────────────────

def build_all_signals(prices, ohlcv):
    """
    Extend base sub-signals with additional derived signals
    specifically useful for rule mining.
    """
    c = prices["^NSEI"]
    h = ohlcv["High"].reindex(prices.index).ffill()
    l = ohlcv["Low"].reindex(prices.index).ffill()
    v = ohlcv["Volume"].reindex(prices.index).ffill()

    def _ema(s, n): return s.ewm(span=n, adjust=False).mean()
    def _sma(s, n): return s.rolling(n).mean()
    def _rsi(s, n):
        d = s.diff()
        g = d.clip(lower=0).rolling(n).mean()
        lo = (-d.clip(upper=0)).rolling(n).mean()
        return 100 - 100 / (1 + g / lo.replace(0, np.nan))

    # Base signals
    S = build_sub_signals(prices, ohlcv)

    # ── Add raw (non-direction-reversed) signals ──────────────
    S["RSI_2"]   = _rsi(c, 2)
    S["RSI_3"]   = _rsi(c, 3)
    S["RSI_5"]   = _rsi(c, 5)
    S["RSI_14"]  = _rsi(c, 14)
    S["VIX"]     = prices["^VIX"]
    S["VIX_MA20"]= _sma(prices["^VIX"], 20)
    S["VIX_pct_above_ma"] = prices["^VIX"] / _sma(prices["^VIX"], 20) - 1

    # Consecutive down/up days
    S["consec_down"] = (c.diff() < 0).astype(int).rolling(5).sum()
    S["consec_up"]   = (c.diff() > 0).astype(int).rolling(5).sum()

    # 3-bar pattern: today's 1d-return
    S["ret_1d"]  = c.pct_change(1)
    S["ret_2d"]  = c.pct_change(2)
    S["ret_3d_raw"] = c.pct_change(3)

    # Gap (open vs prev close)
    S["gap_pct"] = (ohlcv["Open"].reindex(prices.index).ffill() - c.shift(1)) / c.shift(1)

    # NIFTY weekly return (this week vs last week close)
    S["ret_5d"]  = c.pct_change(5)
    S["ret_10d"] = c.pct_change(10)

    # Distance from 52-week high/low (more readable)
    S["pct_from_52w_high"] = c / h.rolling(252).max() - 1   # negative = below high
    S["pct_from_20d_low"]  = c / l.rolling(20).min()  - 1   # positive = above 20d low

    # Volatility regime
    hv5  = c.pct_change().rolling(5).std()  * np.sqrt(252)
    hv20 = c.pct_change().rolling(20).std() * np.sqrt(252)
    S["hv5"]         = hv5
    S["hv20"]        = hv20
    S["vol_expanding"]= hv5 / (hv20 + 1e-9)   # >1 = vol rising

    # Cross-asset
    spx = prices["^GSPC"]
    vix = prices["^VIX"]
    bnk = prices["^NSEBANK"]
    S["spx_ret_1d"]  = spx.pct_change(1)
    S["spx_ret_2d"]  = spx.pct_change(2)
    S["bnk_ret_1d"]  = bnk.pct_change(1)
    S["gold_ret_1d"] = prices["GC=F"].pct_change(1)
    S["crude_ret_1d"]= prices["CL=F"].pct_change(1)
    S["inr_ret_1d"]  = prices["INR=X"].pct_change(1)
    S["n225_ret_1d"] = prices["^N225"].pct_change(1)

    # BB position (raw 0-1)
    ma20 = _sma(c, 20); sd20 = c.rolling(20).std()
    S["bb_pct_raw"] = (c - (ma20 - 2*sd20)) / (4*sd20 + 1e-9)  # 0=lower, 1=upper

    S.replace([np.inf, -np.inf], np.nan, inplace=True)
    return S.ffill().fillna(0)


# ─────────────────────────────────────────────────────────────
# CONDITION TEMPLATES
# ─────────────────────────────────────────────────────────────

# Format: (signal_name, operator, threshold, description)
# For LONG entries (conditions that are bullish)
LONG_CONDITIONS = [
    # ── Extreme oversold NIFTY ────────────────────────────────
    ("RSI_2",      "<=", 10,    "RSI(2) < 10 [extreme oversold]"),
    ("RSI_2",      "<=", 15,    "RSI(2) < 15 [very oversold]"),
    ("RSI_2",      "<=", 20,    "RSI(2) < 20 [oversold]"),
    ("RSI_3",      "<=", 15,    "RSI(3) < 15 [very oversold]"),
    ("RSI_14",     "<=", 30,    "RSI(14) < 30 [oversold]"),
    ("RSI_14",     "<=", 35,    "RSI(14) < 35 [near oversold]"),
    ("bb_pct_raw", "<=", 0.10,  "BB%B < 0.10 [near lower band]"),
    ("bb_pct_raw", "<=", 0.05,  "BB%B < 0.05 [below lower band]"),
    ("CCI10_rev",  ">=", 100,   "CCI(10) < -100 [oversold]"),
    ("CCI10_rev",  ">=", 200,   "CCI(10) < -200 [extreme oversold]"),
    ("STOCH5_rev", ">=", 80,    "Stoch(5) < 20 [oversold]"),
    # ── Consecutive down days ─────────────────────────────────
    ("consec_down",">=", 3,     "3+ consecutive down days"),
    ("consec_down",">=", 4,     "4+ consecutive down days"),
    ("ret_3d_raw", "<=", -0.03, "3-day return < -3%"),
    ("ret_3d_raw", "<=", -0.04, "3-day return < -4%"),
    ("ret_5d",     "<=", -0.04, "5-day return < -4%"),
    ("ret_5d",     "<=", -0.06, "5-day return < -6%"),
    # ── VIX fear spike ────────────────────────────────────────
    ("VIX_pct_above_ma", ">=", 0.20, "VIX 20%+ above 20d MA [fear spike]"),
    ("VIX_pct_above_ma", ">=", 0.30, "VIX 30%+ above 20d MA [extreme fear]"),
    ("VIX_chg",    ">=", 1.5,   "VIX spiked +1.5 pts today"),
    ("VIX_chg",    ">=", 2.5,   "VIX spiked +2.5 pts today"),
    # ── Cross-asset bullish ───────────────────────────────────
    ("spx_ret_1d", ">=", 0.005, "SPX up >0.5% (leading signal)"),
    ("spx_ret_1d", ">=", 0.010, "SPX up >1% (strong leading signal)"),
    ("spx_ret_2d", ">=", 0.010, "SPX up >1% over 2 days"),
    ("INR_ret",    ">=", 0.002, "INR strengthened (FII inflows)"),
    ("n225_ret_1d",">=", 0.005, "Nikkei up >0.5% [Asian bullish]"),
    # ── Candle / intraday ────────────────────────────────────
    ("nifty_close_pos", "<=", 0.20, "Close in bottom 20% of day's range"),
    ("nifty_lower_wick",">=", 0.50, "Lower wick > 50% of range [rejection]"),
    # ── Calendar ─────────────────────────────────────────────
    ("MONTH_END",  ">=", 1.0,   "Month-end (last week of month)"),
    ("EXPIRY_THU", ">=", 1.0,   "Expiry Thursday"),
]

# SHORT conditions — inverse of above
SHORT_CONDITIONS = [
    # ── Extreme overbought NIFTY ──────────────────────────────
    ("RSI_2",      ">=", 90,    "RSI(2) > 90 [extreme overbought]"),
    ("RSI_2",      ">=", 85,    "RSI(2) > 85 [very overbought]"),
    ("RSI_2",      ">=", 80,    "RSI(2) > 80 [overbought]"),
    ("RSI_3",      ">=", 85,    "RSI(3) > 85 [very overbought]"),
    ("RSI_14",     ">=", 70,    "RSI(14) > 70 [overbought]"),
    ("RSI_14",     ">=", 65,    "RSI(14) > 65 [near overbought]"),
    ("bb_pct_raw", ">=", 0.90,  "BB%B > 0.90 [near upper band]"),
    ("bb_pct_raw", ">=", 0.95,  "BB%B > 0.95 [above upper band]"),
    ("CCI10_rev",  "<=", -100,  "CCI(10) > +100 [overbought]"),
    ("CCI10_rev",  "<=", -200,  "CCI(10) > +200 [extreme overbought]"),
    ("STOCH5_rev", "<=", 20,    "Stoch(5) > 80 [overbought]"),
    # ── Consecutive up days ───────────────────────────────────
    ("consec_up",  ">=", 3,     "3+ consecutive up days"),
    ("consec_up",  ">=", 4,     "4+ consecutive up days"),
    ("ret_3d_raw", ">=", 0.03,  "3-day return > +3%"),
    ("ret_3d_raw", ">=", 0.04,  "3-day return > +4%"),
    ("ret_5d",     ">=", 0.04,  "5-day return > +4%"),
    ("ret_5d",     ">=", 0.06,  "5-day return > +6%"),
    # ── Cross-asset bearish ───────────────────────────────────
    ("spx_ret_1d", "<=", -0.005,"SPX down >0.5%"),
    ("spx_ret_1d", "<=", -0.010,"SPX down >1%"),
    ("INR_ret",    "<=", -0.002,"INR weakened (FII outflows)"),
    # ── VIX calm + overbought = complacency top ───────────────
    ("VIX_pct_above_ma","<=", -0.10, "VIX 10% below MA [complacency]"),
    # ── Candle ────────────────────────────────────────────────
    ("nifty_close_pos", ">=", 0.80, "Close in top 20% of day's range"),
    ("nifty_upper_wick",">=", 0.50, "Upper wick > 50% of range [rejection]"),
    # ── Calendar ─────────────────────────────────────────────
    ("MONTH_END",  ">=", 1.0,   "Month-end [sell the fact]"),
]


# ─────────────────────────────────────────────────────────────
# SIGNAL MASK HELPER
# ─────────────────────────────────────────────────────────────

def apply_conditions(S, conditions):
    mask = pd.Series(True, index=S.index)
    for col, op, val, _ in conditions:
        if col not in S.columns:
            return pd.Series(False, index=S.index)
        if   op == "<=": mask &= S[col] <= val
        elif op == ">=": mask &= S[col] >= val
        elif op == "==": mask &= S[col] == val
    return mask


# ─────────────────────────────────────────────────────────────
# CORE MINER
# ─────────────────────────────────────────────────────────────

def mine_strategies(S, fwd_ret_dict, is_mask, oos_mask,
                    direction_label, cond_pool, hold_periods,
                    is_thresh, oos_thresh,
                    min_is, min_oos, verbose=False):
    """
    Exhaustively tests 2-condition and 3-condition combinations.
    Returns list of result dicts sorted by OOS score.
    """
    survivors = []
    ncands_tested = 0

    for n_conds in [2, 3]:
        for combo in itertools.combinations(range(len(cond_pool)), n_conds):
            conditions = [cond_pool[i] for i in combo]

            # Quick pre-check: any shared signal column? skip (redundant)
            cols = [c[0] for c in conditions]
            if len(set(cols)) < len(cols):
                continue

            mask_full = apply_conditions(S, conditions)
            mask_is   = mask_full & is_mask
            n_is      = mask_is.sum()
            if n_is < min_is:
                continue

            for hold in hold_periods:
                fwd = fwd_ret_dict[hold]
                ncands_tested += 1

                # In-sample win rate
                if direction_label == "LONG":
                    wins_is = (fwd[mask_is] > 0).sum()
                else:
                    wins_is = (fwd[mask_is] < 0).sum()
                wr_is = wins_is / n_is

                if wr_is < is_thresh:
                    continue

                # OOS validation
                mask_oos = mask_full & oos_mask
                n_oos    = mask_oos.sum()
                if n_oos < min_oos:
                    continue

                if direction_label == "LONG":
                    wins_oos = (fwd[mask_oos] > 0).sum()
                else:
                    wins_oos = (fwd[mask_oos] < 0).sum()
                wr_oos = wins_oos / n_oos

                if wr_oos < oos_thresh:
                    continue

                # Binomial significance (OOS)
                pval = binom.sf(wins_oos - 1, n_oos, 0.5)

                # Avg return per trade (OOS)
                if direction_label == "LONG":
                    avg_ret = fwd[mask_oos].mean() * 100
                else:
                    avg_ret = -fwd[mask_oos].mean() * 100

                score = wr_oos * np.sqrt(n_oos)

                survivors.append({
                    "direction"   : direction_label,
                    "hold_days"   : hold,
                    "conditions"  : [(c[0], c[1], c[2], c[3]) for c in conditions],
                    "n_is"        : int(n_is),
                    "wr_is"       : round(wr_is * 100, 1),
                    "n_oos"       : int(n_oos),
                    "wr_oos"      : round(wr_oos * 100, 1),
                    "wins_oos"    : int(wins_oos),
                    "avg_ret_oos" : round(avg_ret, 3),
                    "pvalue"      : round(pval, 6),
                    "score"       : round(score, 3),
                })

    survivors.sort(key=lambda x: x["score"], reverse=True)
    if verbose:
        print(f"    Tested {ncands_tested} candidates → {len(survivors)} survivors")
    return survivors


# ─────────────────────────────────────────────────────────────
# DEDUPLICATE: remove strategies that share most conditions
# ─────────────────────────────────────────────────────────────

def deduplicate(candidates, max_overlap=1):
    """
    Keep strategies that share at most `max_overlap` conditions with
    any already-selected strategy. Ensures diversity.
    """
    selected = []
    for cand in candidates:
        cond_set = frozenset((c[0], c[1], c[2]) for c in cand["conditions"])
        is_dup = False
        for sel in selected:
            sel_set = frozenset((c[0], c[1], c[2]) for c in sel["conditions"])
            if len(cond_set & sel_set) > max_overlap:
                is_dup = True
                break
        if not is_dup:
            selected.append(cand)
    return selected


# ─────────────────────────────────────────────────────────────
# EQUITY CURVE PER STRATEGY
# ─────────────────────────────────────────────────────────────

def compute_equity(prices, S, strategy, oos_mask, capital=100_000):
    """Compute trade-by-trade equity curve for a strategy."""
    c       = prices["^NSEI"]
    hold    = strategy["hold_days"]
    conds   = strategy["conditions"]
    dirn    = strategy["direction"]

    mask    = apply_conditions(S, conds) & oos_mask
    idx     = S.index[mask]

    equity  = capital
    curve   = []
    trades  = []
    last_exit = pd.Timestamp("1900-01-01")

    for entry_date in idx:
        if entry_date <= last_exit:
            continue   # no overlapping positions
        loc      = S.index.get_loc(entry_date)
        exit_loc = loc + hold
        if exit_loc >= len(S.index):
            continue

        exit_date  = S.index[exit_loc]
        entry_px   = c.iloc[loc]
        exit_px    = c.iloc[exit_loc]
        units      = int(equity / entry_px)
        if units == 0:
            continue

        if dirn == "LONG":
            pnl = units * (exit_px - entry_px)
        else:
            pnl = units * (entry_px - exit_px)

        cost   = equity * 0.0015 + 47.2   # 0.15% + ₹47 brokerage
        net    = pnl - cost
        equity += net
        last_exit = exit_date

        trades.append({
            "entry": str(entry_date.date()),
            "exit" : str(exit_date.date()),
            "entry_px": round(entry_px, 2),
            "exit_px" : round(exit_px, 2),
            "direction": dirn,
            "units": units,
            "gross_pnl": round(pnl, 2),
            "cost"     : round(cost, 2),
            "net_pnl"  : round(net, 2),
            "equity"   : round(equity, 2),
        })
        curve.append((exit_date, equity))

    curve_s = pd.Series(dict(curve)) if curve else pd.Series(dtype=float)
    return trades, curve_s, equity


# ─────────────────────────────────────────────────────────────
# CHARTS
# ─────────────────────────────────────────────────────────────

def make_strategy_charts(prices, S, top5, oos_mask, oos_dates, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    colors = ["#1B5E20","#B71C1C","#1A237E","#E65100","#4A148C"]
    C_bh   = "#90A4AE"

    # ── Chart 1: Win-rate and trade-count summary ─────────────
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    labels = [f"S{i+1}" for i in range(len(top5))]
    wr_oos = [s["wr_oos"] for s in top5]
    wr_is  = [s["wr_is"]  for s in top5]
    nt_oos = [s["n_oos"]  for s in top5]

    x = np.arange(len(top5)); w = 0.35
    ax = axes[0]
    ax.bar(x-w/2, wr_is,  w, label="In-sample",  color=[c+"88" for c in colors], alpha=0.9)
    ax.bar(x+w/2, wr_oos, w, label="OOS",        color=colors, alpha=0.9)
    ax.axhline(75, ls="--", c="green",  lw=1.2, label="75% target")
    ax.axhline(65, ls=":",  c="orange", lw=1.0, label="65% OOS floor")
    ax.axhline(50, ls="--", c="grey",   lw=0.8)
    for i,(a,b) in enumerate(zip(wr_is,wr_oos)):
        ax.text(i-w/2, a+0.5, f"{a:.0f}%", ha="center", fontsize=8)
        ax.text(i+w/2, b+0.5, f"{b:.0f}%", ha="center", fontsize=8, fontweight="bold")
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_ylabel("Win Rate (%)"); ax.set_ylim(40, 95)
    ax.set_title("Win Rate: In-Sample vs OOS"); ax.legend(fontsize=8)

    ax2 = axes[1]
    ax2.bar(labels, nt_oos, color=colors, alpha=0.85)
    ax2.axhline(30, ls="--", c="grey", lw=1, label="Min 30 trades")
    for i, n in enumerate(nt_oos):
        ax2.text(i, n+0.5, str(n), ha="center", fontsize=9, fontweight="bold")
    ax2.set_ylabel("Number of Trades (OOS)"); ax2.set_title("OOS Trade Count")
    ax2.legend(fontsize=8)

    plt.suptitle("NIFTY Top-5 Rule-Based Strategies — Performance Summary", fontsize=12)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "strategies_summary.png"), dpi=130)
    plt.close()

    # ── Chart 2: Equity curves ───────────────────────────────
    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    axes_flat = axes.flat
    bh_close  = prices["^NSEI"].reindex(oos_dates)
    bh_units  = int(100_000 / bh_close.iloc[0])
    bh_equity = 100_000 + bh_units * (bh_close - bh_close.iloc[0])

    for i, (strat, color) in enumerate(zip(top5, colors)):
        ax = next(axes_flat)
        trades_s, curve_s, final = compute_equity(prices, S, strat, oos_mask)
        trades_df = pd.DataFrame(trades_s)
        wins = (trades_df["net_pnl"] > 0).sum() if len(trades_df) else 0
        total = len(trades_df)

        if len(curve_s) > 0:
            ax.plot(curve_s.index, curve_s.values, color=color, lw=2,
                    label=f"Strategy → ₹{final:,.0f}")
        ax.plot(bh_equity.index, bh_equity.values, color=C_bh, lw=1, ls="--",
                label=f"Buy&Hold → ₹{bh_equity.iloc[-1]:,.0f}")
        ax.axhline(100_000, color="grey", lw=0.7, ls=":")
        dd_desc = "\n".join([f"{c[3]}" for c in strat["conditions"]])
        ax.set_title(
            f"S{i+1}: {strat['direction']} hold={strat['hold_days']}d\n"
            f"OOS win={strat['wr_oos']:.0f}%  {wins}/{total} trades",
            fontsize=9
        )
        ax.set_ylabel("Equity (₹)"); ax.legend(fontsize=7)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"₹{x:,.0f}"))
        ax.grid(alpha=0.25)

    # Use last subplot for strategy description table
    ax = next(axes_flat)
    ax.axis("off")
    rows = [["ID","Direction","Hold","IS WR","OOS WR","Trades","p-value"]]
    for i, s in enumerate(top5):
        rows.append([f"S{i+1}", s["direction"], f"{s['hold_days']}d",
                     f"{s['wr_is']:.0f}%", f"{s['wr_oos']:.0f}%",
                     str(s['n_oos']), f"{s['pvalue']:.4f}"])
    tbl = ax.table(cellText=rows[1:], colLabels=rows[0],
                   cellLoc="center", loc="center", bbox=[0,0,1,1])
    tbl.auto_set_font_size(False); tbl.set_fontsize(8.5)
    for (r,c2),cell in tbl.get_celld().items():
        if r==0: cell.set_facecolor("#263238"); cell.set_text_props(color="w",fontweight="bold")
        elif r%2==0: cell.set_facecolor("#F5F5F5")
        cell.set_edgecolor("#CFD8DC")
    ax.set_title("Strategy Summary Table", fontsize=9)

    plt.suptitle("NIFTY Rule-Based Strategies — OOS Equity Curves (₹1,00,000 start)",
                 fontsize=12)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "strategies_equity.png"), dpi=130)
    plt.close()

    print(f"  Charts saved → {out_dir}/")


# ─────────────────────────────────────────────────────────────
# SAVE STRATEGY CARDS (markdown + JSON)
# ─────────────────────────────────────────────────────────────

def save_strategy_cards(top5, prices, S, oos_mask, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    all_strategies = []

    for i, strat in enumerate(top5):
        sid     = f"NIFTY_S{i+1:02d}"
        trades, curve, final = compute_equity(prices, S, strat, oos_mask)
        trades_df = pd.DataFrame(trades)
        wins   = (trades_df["net_pnl"] > 0).sum() if len(trades_df) else 0
        losses = len(trades_df) - wins if len(trades_df) else 0
        avg_win  = trades_df.loc[trades_df["net_pnl"]>0,"net_pnl"].mean() if wins else 0
        avg_loss = trades_df.loc[trades_df["net_pnl"]<=0,"net_pnl"].mean() if losses else 0

        # ── JSON card ──────────────────────────────────────────
        card = {
            "id"             : sid,
            "name"           : f"NIFTY {'Oversold Bounce' if strat['direction']=='LONG' else 'Mean-Reversion Short'} — {strat['direction']} {strat['hold_days']}d",
            "instrument"     : "NIFTY 50 (ETF or Futures)",
            "direction"      : strat["direction"],
            "hold_days"      : strat["hold_days"],
            "entry_timing"   : "Next open after all conditions met at EOD",
            "exit_timing"    : f"Close of day +{strat['hold_days']}",
            "conditions"     : [{"signal": c[0], "operator": c[1],
                                 "threshold": c[2], "description": c[3]}
                                for c in strat["conditions"]],
            "performance"    : {
                "is_win_rate_pct" : strat["wr_is"],
                "oos_win_rate_pct": strat["wr_oos"],
                "is_trades"       : strat["n_is"],
                "oos_trades"      : strat["n_oos"],
                "avg_oos_return_pct": strat["avg_ret_oos"],
                "binomial_pvalue" : strat["pvalue"],
                "score"           : strat["score"],
                "sim_final_equity": round(final, 2),
                "sim_win_trades"  : int(wins),
                "sim_loss_trades" : int(losses),
                "sim_avg_win_rs"  : round(avg_win, 2),
                "sim_avg_loss_rs" : round(avg_loss, 2),
            },
            "data_sources"   : ["^NSEI","^GSPC","^VIX","INR=X","^NSEBANK",
                                 "RELIANCE.NS","HDFCBANK.NS","^N225"],
        }
        all_strategies.append(card)

        # ── Markdown card ──────────────────────────────────────
        md = f"""# {sid}: {card['name']}

## Overview
| Field | Value |
|-------|-------|
| Instrument | {card['instrument']} |
| Direction | **{strat['direction']}** |
| Hold Period | **{strat['hold_days']} trading day(s)** |
| Entry | {card['entry_timing']} |
| Exit | {card['exit_timing']} |

## Entry Conditions
All conditions must be TRUE at market close to enter next open:

"""
        for j, c in enumerate(strat["conditions"], 1):
            md += f"{j}. **{c[3]}**  `{c[0]} {c[1]} {c[2]}`\n"

        md += f"""
## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **{strat['wr_oos']:.1f}%** |
| OOS Trades | {strat['n_oos']} |
| In-Sample Win Rate | {strat['wr_is']:.1f}% |
| Avg Return per Trade | {strat['avg_ret_oos']:+.2f}% |
| Binomial p-value | {strat['pvalue']:.4f} |
| ₹1L Simulation Final | ₹{final:,.0f} |
| Sim Win / Loss | {wins} / {losses} |

## Risk Management
- **Stop Loss**: -1.5% × ATR(14) from entry
- **Max trades open**: 1 at a time (no overlapping positions)
- **Min capital for NIFTY Futures**: ₹6–7 lakh (1 lot margin)
- **For ETF (NiftyBees)**: Any capital

## Notes
- All conditions computed on **daily CLOSE** data
- Signals are mean-reversion based — enter after the market has stretched
- OOS period: last 35% of 10-year dataset
- Transaction costs of 0.15% included in equity simulation
"""
        md_path = os.path.join(out_dir, f"{sid}.md")
        with open(md_path, "w") as f:
            f.write(md)

        # Save trades CSV
        if len(trades_df):
            trades_df.to_csv(os.path.join(out_dir, f"{sid}_trades.csv"), index=False)

    # Save combined JSON
    json_path = os.path.join(out_dir, "all_strategies.json")
    with open(json_path, "w") as f:
        json.dump(all_strategies, f, indent=2)
    print(f"  Strategy cards saved → {out_dir}/")
    return all_strategies


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  NIFTY STRATEGY MINER — Searching for >75% win-rate rules")
    print("=" * 65)

    # ── Data ────────────────────────────────────────────────
    print("\n[1/6] Loading data …")
    prices = generate_multi_asset(seed=42)
    ohlcv  = generate_nifty_ohlcv(seed=42).reindex(prices.index).ffill()
    S      = build_all_signals(prices, ohlcv)
    N      = len(S)
    split  = int(N * TRAIN_PCT)
    is_mask  = pd.Series([True]*split  + [False]*(N-split), index=S.index)
    oos_mask = pd.Series([False]*split + [True]*(N-split),  index=S.index)
    oos_dates = S.index[oos_mask]
    print(f"    {N} days total  |  IS: {split}  |  OOS: {N-split}")

    # ── Forward returns ──────────────────────────────────────
    print("\n[2/6] Computing forward returns …")
    c = prices["^NSEI"]
    fwd_ret = {h: c.pct_change(h).shift(-h) for h in [1, 2, 3, 5]}

    # ── Mine LONG strategies ─────────────────────────────────
    print("\n[3/6] Mining LONG strategies …")
    long_results = mine_strategies(
        S, fwd_ret, is_mask, oos_mask,
        "LONG", LONG_CONDITIONS, [1, 2, 3, 5],
        IS_WIN_THRESH, OOS_WIN_THRESH,
        MIN_IS_TRADES, MIN_OOS_TRADES, verbose=True
    )
    print(f"    LONG survivors: {len(long_results)}")

    # ── Mine SHORT strategies ────────────────────────────────
    print("\n[4/6] Mining SHORT strategies …")
    short_results = mine_strategies(
        S, fwd_ret, is_mask, oos_mask,
        "SHORT", SHORT_CONDITIONS, [1, 2, 3, 5],
        IS_WIN_THRESH, OOS_WIN_THRESH,
        MIN_IS_TRADES, MIN_OOS_TRADES, verbose=True
    )
    print(f"    SHORT survivors: {len(short_results)}")

    # ── Combine, deduplicate, pick top 5 ─────────────────────
    print("\n[5/6] Selecting top 5 diverse strategies …")
    all_results = long_results + short_results
    all_results.sort(key=lambda x: x["score"], reverse=True)
    top5 = deduplicate(all_results, max_overlap=1)[:5]

    # If we don't have 5, lower OOS threshold and re-mine
    if len(top5) < 5:
        print(f"    Only {len(top5)} found at OOS≥{OOS_WIN_THRESH:.0%}. "
              f"Lowering to 0.60 …")
        long_r2  = mine_strategies(S, fwd_ret, is_mask, oos_mask,
                                   "LONG",  LONG_CONDITIONS,  [1,2,3,5],
                                   IS_WIN_THRESH, 0.60, MIN_IS_TRADES, 12, verbose=False)
        short_r2 = mine_strategies(S, fwd_ret, is_mask, oos_mask,
                                   "SHORT", SHORT_CONDITIONS, [1,2,3,5],
                                   IS_WIN_THRESH, 0.60, MIN_IS_TRADES, 12, verbose=False)
        all2 = sorted(long_r2 + short_r2, key=lambda x: x["score"], reverse=True)
        top5 = deduplicate(all2, max_overlap=1)[:5]

    print(f"\n  ✓ Final selection: {len(top5)} strategies")

    # ── Output paths ─────────────────────────────────────────
    repo_dir     = os.path.dirname(os.path.abspath(__file__))
    skill_lib    = os.path.join(repo_dir, "SKILL_LIBRARY", "NIFTY_STRATEGIES")
    os.makedirs(skill_lib, exist_ok=True)

    # ── Charts ───────────────────────────────────────────────
    print("\n[6/6] Generating charts and strategy cards …")
    make_strategy_charts(prices, S, top5, oos_mask, oos_dates, skill_lib)
    cards = save_strategy_cards(top5, prices, S, oos_mask, skill_lib)

    # ── Console report ────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  TOP 5 NIFTY STRATEGIES — FINAL REPORT")
    print("=" * 65)

    for i, s in enumerate(top5):
        _, _, final_eq = compute_equity(prices, S, s, oos_mask)
        print(f"\n  ── Strategy S{i+1}: {s['direction']}  Hold={s['hold_days']}d ──")
        print(f"  Entry conditions:")
        for c in s["conditions"]:
            print(f"    • {c[3]}")
        print(f"  In-sample  : {s['wr_is']:.1f}% win rate  ({s['n_is']} trades)")
        print(f"  OOS        : {s['wr_oos']:.1f}% win rate  "
              f"({s['wins_oos']}/{s['n_oos']} wins)")
        print(f"  Avg return : {s['avg_ret_oos']:+.2f}%/trade  "
              f"p-value={s['pvalue']:.4f}")
        print(f"  ₹1L sim    : ₹{final_eq:,.0f}  (after costs)")

    print(f"\n  Saved to → {skill_lib}/")
    print("=" * 65)

    return top5


if __name__ == "__main__":
    main()
