"""
Intraday NIFTY Strategy Evaluator
====================================
Evaluates the 5 rule-based strategies (from nifty_strategy_miner.py) on
synthetic intraday NIFTY bars at four timeframes:

  * 1 year  @ 1h  bars   (252 trading days × 6 bars  ≈ 1512 bars)
  * 60 days @ 30m bars   (60 days × 12 bars           ≈  720 bars)
  * 60 days @ 15m bars   (60 days × 25 bars           ≈ 1500 bars)
  * 60 days @ 5m  bars   (60 days × 75 bars           ≈ 4500 bars)

Bars are synthesised from the existing daily synthetic NIFTY data
(Brownian bridge anchored to daily OHLC). All signals are computed fresh
on the intraday series with the same period numbers as in the daily
strategies.  Hold = N bars (same integer as N days in the daily version).
"""

import numpy as np
import pandas as pd
import json, os
from pathlib import Path
from scipy.stats import binomtest

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from nifty_data_generator import generate_nifty_ohlcv

# ── output ─────────────────────────────────────────────────────────────
OUTDIR = Path("SKILL_LIBRARY/NIFTY_STRATEGIES/INTRADAY")
OUTDIR.mkdir(parents=True, exist_ok=True)

# ── intraday costs (NIFTY Futures) ─────────────────────────────────────
COST_RT = 0.0010   # 0.10% round-trip (no overnight STT benefit needed intraday)

# ── NIFTY session: 09:15 – 15:30 IST  (375 min) ──────────────────────
SESSION_OPEN_H, SESSION_OPEN_M   = 9,  15
SESSION_CLOSE_H, SESSION_CLOSE_M = 15, 30

TIMEFRAME_CFG = {
    "1h":  dict(freq_min=60, days=252),
    "30m": dict(freq_min=30, days=60),
    "15m": dict(freq_min=15, days=60),
    "5m":  dict(freq_min=5,  days=60),
}

# ── 5 strategy definitions (from all_strategies.json) ─────────────────
STRATEGIES = [
    dict(id="S1", name="Oversold Bounce",      direction="LONG",  hold=3,
         conditions=[("RSI_2","<=",10), ("bb_pct","<=",0.05), ("STOCH5_rev",">=",80)]),
    dict(id="S2", name="BB+INR Long",          direction="LONG",  hold=5,
         conditions=[("bb_pct","<=",0.10), ("INR_ret",">=",0.002)]),
    dict(id="S3", name="Triple Overbought Short", direction="SHORT", hold=5,
         conditions=[("RSI_3",">=",85), ("RSI_14",">=",70), ("bb_pct",">=",0.90)]),
    dict(id="S4", name="Month-End Short",      direction="SHORT", hold=5,
         conditions=[("bb_pct",">=",0.90), ("MONTH_END",">=",1.0)]),
    dict(id="S5", name="Crash-Bounce Long",    direction="LONG",  hold=3,
         conditions=[("RSI_14","<=",35), ("STOCH5_rev",">=",80), ("ret3","<=",-0.03)]),
]


# ══════════════════════════════════════════════════════════════════════
# 1. INTRADAY BAR GENERATOR
# ══════════════════════════════════════════════════════════════════════

def generate_intraday(daily_df: pd.DataFrame, freq_min: int,
                      days: int, seed: int = 42) -> pd.DataFrame:
    """
    Synthesise intraday OHLCV bars from the last `days` days of daily_df.
    Uses a Brownian-bridge path pinned to daily Open and Close, with
    excursions scaled to match the daily High and Low.
    """
    rng = np.random.default_rng(seed)

    session_min = (SESSION_CLOSE_H * 60 + SESSION_CLOSE_M) - (SESSION_OPEN_H * 60 + SESSION_OPEN_M)
    n_bars = session_min // freq_min          # bars per day

    tail = daily_df.tail(days)
    records = []

    for date, row in tail.iterrows():
        O = row["Open"]
        H = row["High"]
        L = row["Low"]
        C = row["Close"]
        V = row.get("Volume", 5e5)

        day_dt = pd.Timestamp(date)
        times = [
            day_dt.replace(hour=SESSION_OPEN_H, minute=SESSION_OPEN_M)
            + pd.Timedelta(minutes=freq_min * k)
            for k in range(n_bars)
        ]

        # ── Brownian bridge: log-price path from log(O) → log(C) ──────
        log_O, log_C = np.log(max(O, 1e-6)), np.log(max(C, 1e-6))
        t   = np.linspace(0, 1, n_bars + 1)[1:]   # (0,1]
        raw = rng.standard_normal(n_bars)
        B   = np.cumsum(raw) / np.sqrt(n_bars)    # scaled BM at t=1 → N(0,1)
        bridge = B - t * B[-1]                     # bridge endpoint → 0

        # Calibrate noise so 2-sigma range ≈ daily H-L
        hl_log = np.log(max(H, C+1e-3)) - np.log(max(L, O-1e-3))
        noise_scale = hl_log / (2 * 2)            # ±2σ spans daily range

        log_path = log_O + (log_C - log_O) * t + noise_scale * bridge
        prices   = np.exp(log_path)

        prev_close = O
        for k in range(n_bars):
            bO = prev_close
            bC = prices[k]
            bH = max(bO, bC) * (1 + abs(rng.normal(0, 0.0002)))
            bL = min(bO, bC) * (1 - abs(rng.normal(0, 0.0002)))
            bV = (V / n_bars) * (0.5 + rng.exponential(0.5))
            records.append(dict(datetime=times[k],
                                Open=bO, High=bH, Low=bL, Close=bC, Volume=bV))
            prev_close = bC

    df = pd.DataFrame(records).set_index("datetime")
    return df


# ══════════════════════════════════════════════════════════════════════
# 2. SIGNAL BUILDER
# ══════════════════════════════════════════════════════════════════════

def _rsi(series: pd.Series, n: int) -> pd.Series:
    d   = series.diff()
    up  = d.clip(lower=0).ewm(com=n - 1, min_periods=n).mean()
    dn  = (-d.clip(upper=0)).ewm(com=n - 1, min_periods=n).mean()
    rs  = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _stoch(h: pd.Series, l: pd.Series, c: pd.Series, k: int = 5) -> pd.Series:
    lo = l.rolling(k).min()
    hi = h.rolling(k).max()
    return 100 * (c - lo) / (hi - lo + 1e-10)


def _bb_pct(c: pd.Series, n: int = 20) -> pd.Series:
    ma  = c.rolling(n).mean()
    std = c.rolling(n).std()
    return (c - (ma - 2 * std)) / (4 * std + 1e-10)


def build_signals(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l = df["Close"], df["High"], df["Low"]
    sig = pd.DataFrame(index=df.index)

    sig["RSI_2"]  = _rsi(c, 2)
    sig["RSI_3"]  = _rsi(c, 3)
    sig["RSI_14"] = _rsi(c, 14)

    sig["bb_pct"]    = _bb_pct(c, 20)
    stoch            = _stoch(h, l, c, 5)
    sig["STOCH5_rev"] = 100 - stoch        # high value = oversold

    sig["ret3"] = c.pct_change(3)          # 3-bar return

    # MONTH_END: True for all bars on the last trading day of each month
    dates = pd.Series(df.index.date, index=df.index)
    unique_dates = sorted(set(dates))
    last_of_month = {}
    for d in unique_dates:
        key = (d.year, d.month)
        last_of_month[key] = d
    month_end_set = set(last_of_month.values())
    sig["MONTH_END"] = dates.isin(month_end_set).astype(float).values

    # INR_ret: daily value broadcast to all intraday bars of that day
    rng2 = np.random.default_rng(77)
    inr_by_date = {d: rng2.normal(0, 0.003) for d in unique_dates}
    sig["INR_ret"] = [inr_by_date[dt.date()] for dt in df.index]

    return sig


# ══════════════════════════════════════════════════════════════════════
# 3. STRATEGY EVALUATOR
# ══════════════════════════════════════════════════════════════════════

def evaluate(df: pd.DataFrame, sig: pd.DataFrame,
             strategy: dict, is_frac: float = 0.65) -> dict:
    n      = len(df)
    is_end = int(n * is_frac)

    direction = strategy["direction"]
    hold      = strategy["hold"]

    # Build entry mask
    mask = pd.Series(True, index=sig.index)
    for col, op, thresh in strategy["conditions"]:
        if col not in sig.columns:
            continue
        if op == "<=":
            mask &= sig[col] <= thresh
        else:
            mask &= sig[col] >= thresh

    closes    = df["Close"].values
    in_trade  = False
    exit_bar  = -1
    trades    = []

    for i in range(n):
        if in_trade and i >= exit_bar:
            in_trade = False

        if in_trade:
            continue

        if not mask.iloc[i]:
            continue

        ent_i = i
        ex_i  = min(i + hold, n - 1)
        if ex_i == ent_i:
            continue

        ent_p = closes[ent_i]
        ex_p  = closes[ex_i]

        raw_ret = (ex_p - ent_p) / ent_p if direction == "LONG" else (ent_p - ex_p) / ent_p
        net_ret = raw_ret - COST_RT

        trades.append(dict(
            entry_bar=ent_i,
            exit_bar=ex_i,
            entry_dt=str(df.index[ent_i]),
            exit_dt=str(df.index[ex_i]),
            direction=direction,
            entry_price=round(ent_p, 2),
            exit_price=round(ex_p, 2),
            net_ret=round(net_ret, 5),
            win=int(net_ret > 0),
            sample="IS" if ent_i < is_end else "OOS",
        ))
        in_trade = True
        exit_bar = ex_i

    is_t  = [t for t in trades if t["sample"] == "IS"]
    oos_t = [t for t in trades if t["sample"] == "OOS"]

    is_wr  = float(np.mean([t["win"] for t in is_t]))  if is_t  else 0.0
    oos_wr = float(np.mean([t["win"] for t in oos_t])) if oos_t else 0.0

    p_val  = None
    if oos_t:
        wins  = sum(t["win"] for t in oos_t)
        p_val = binomtest(wins, len(oos_t), 0.5, alternative="greater").pvalue

    # ── simple equity sim on OOS ────────────────────────────────────
    equity = 1.0
    eq_curve = [1.0]
    for t in oos_t:
        equity *= (1 + t["net_ret"])
        eq_curve.append(equity)

    return dict(
        is_trades=len(is_t),
        oos_trades=len(oos_t),
        is_wr=round(is_wr, 4),
        oos_wr=round(oos_wr, 4),
        p_value=round(p_val, 5) if p_val is not None else None,
        oos_return=round(equity - 1, 4),
        eq_curve=eq_curve,
        trades=trades,
    )


# ══════════════════════════════════════════════════════════════════════
# 4. MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    print("=" * 65)
    print("  NIFTY INTRADAY STRATEGY EVALUATOR")
    print("=" * 65)

    # ── Generate daily backbone ────────────────────────────────────────
    print("\n[1/3] Generating synthetic daily data …")
    daily_df = generate_nifty_ohlcv(seed=42)
    print(f"      Daily data: {len(daily_df)} rows "
          f"({daily_df.index[0].date()} → {daily_df.index[-1].date()})")

    # ── Evaluate all timeframes ────────────────────────────────────────
    print("\n[2/3] Evaluating strategies on intraday bars …\n")
    results = {}   # {tf: {s_id: eval_result}}

    tf_labels = {"1h": "1-Year @ 1h", "30m": "60-Day @ 30m",
                 "15m": "60-Day @ 15m", "5m": "60-Day @ 5m"}

    for tf, cfg in TIMEFRAME_CFG.items():
        freq_min = cfg["freq_min"]
        days     = cfg["days"]
        session  = 375 // freq_min   # bars per day
        total_bars = days * session

        print(f"  ── {tf_labels[tf]} ({session} bars/day, ~{total_bars} total bars) ──")
        intra_df = generate_intraday(daily_df, freq_min=freq_min, days=days)
        sig      = build_signals(intra_df)

        results[tf] = {}
        for strat in STRATEGIES:
            res = evaluate(intra_df, sig, strat)
            results[tf][strat["id"]] = res

            oos_wr_pct = res["oos_wr"] * 100
            p_str = f"p={res['p_value']:.4f}" if res["p_value"] else "n/a"
            print(f"    {strat['id']} {strat['name']:24s}  "
                  f"IS={res['is_wr']*100:4.1f}%({res['is_trades']:3d})  "
                  f"OOS={oos_wr_pct:4.1f}%({res['oos_trades']:3d})  "
                  f"ret={res['oos_return']*100:+5.1f}%  {p_str}")
        print()

    # ── Save trade CSVs ───────────────────────────────────────────────
    for tf in results:
        for sid in results[tf]:
            trades = results[tf][sid]["trades"]
            if trades:
                csv_path = OUTDIR / f"{sid}_{tf}_trades.csv"
                pd.DataFrame(trades).to_csv(csv_path, index=False)

    # ── Charts ────────────────────────────────────────────────────────
    print("[3/3] Generating charts …")

    tfs   = list(TIMEFRAME_CFG.keys())
    s_ids = [s["id"] for s in STRATEGIES]

    # ── Heat-map: OOS win rate ─────────────────────────────────────────
    wr_matrix = np.array([
        [results[tf][sid]["oos_wr"] * 100 for sid in s_ids]
        for tf in tfs
    ])
    trade_matrix = np.array([
        [results[tf][sid]["oos_trades"] for sid in s_ids]
        for tf in tfs
    ])

    fig, axes = plt.subplots(1, 2, figsize=(14, 4))
    fig.suptitle("Intraday Strategy Evaluation — NIFTY 50", fontsize=14, fontweight="bold")

    # Win-rate heatmap
    ax = axes[0]
    im = ax.imshow(wr_matrix, vmin=40, vmax=85, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(len(s_ids)));  ax.set_xticklabels(s_ids)
    ax.set_yticks(range(len(tfs)));    ax.set_yticklabels([tf_labels[t] for t in tfs])
    ax.set_title("OOS Win Rate  (%)")
    for i in range(len(tfs)):
        for j in range(len(s_ids)):
            wr   = wr_matrix[i, j]
            n_tr = trade_matrix[i, j]
            color = "white" if wr < 48 or wr > 72 else "black"
            ax.text(j, i, f"{wr:.0f}%\n(n={n_tr})",
                    ha="center", va="center", fontsize=9, color=color)
    plt.colorbar(im, ax=ax)

    # Trade count heatmap
    ax = axes[1]
    im2 = ax.imshow(trade_matrix, cmap="Blues", aspect="auto")
    ax.set_xticks(range(len(s_ids)));  ax.set_xticklabels(s_ids)
    ax.set_yticks(range(len(tfs)));    ax.set_yticklabels([tf_labels[t] for t in tfs])
    ax.set_title("OOS Trade Count")
    for i in range(len(tfs)):
        for j in range(len(s_ids)):
            ax.text(j, i, str(int(trade_matrix[i, j])),
                    ha="center", va="center", fontsize=10)
    plt.colorbar(im2, ax=ax)

    plt.tight_layout()
    heatmap_path = OUTDIR / "intraday_heatmap.png"
    plt.savefig(heatmap_path, dpi=150, bbox_inches="tight")
    plt.close()

    # ── Equity curves: one subplot per strategy ────────────────────────
    fig, axes = plt.subplots(len(STRATEGIES), 1, figsize=(14, 4 * len(STRATEGIES)))
    fig.suptitle("OOS Equity Curves — Intraday (All Timeframes)", fontsize=14, fontweight="bold")

    colors = {"1h": "#1f77b4", "30m": "#ff7f0e", "15m": "#2ca02c", "5m": "#d62728"}

    for row, strat in enumerate(STRATEGIES):
        ax = axes[row]
        sid = strat["id"]
        for tf in tfs:
            eq = results[tf][sid]["eq_curve"]
            if len(eq) > 1:
                ax.plot(np.array(eq) - 1, label=tf_labels[tf],
                        color=colors[tf], alpha=0.85)
        ax.axhline(0, color="black", linewidth=0.7, linestyle="--")
        ax.set_title(f"{sid}: {strat['name']}  ({strat['direction']} hold={strat['hold']} bars)",
                     fontsize=11)
        ax.set_ylabel("Cumulative Return")
        ax.legend(fontsize=8)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y*100:.0f}%"))

    plt.tight_layout()
    equity_path = OUTDIR / "intraday_equity.png"
    plt.savefig(equity_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"      Charts saved → {OUTDIR}/")

    # ── Save JSON summary ─────────────────────────────────────────────
    summary = {}
    for tf in tfs:
        summary[tf] = {}
        for sid in s_ids:
            r = results[tf][sid]
            summary[tf][sid] = dict(
                is_wr_pct=round(r["is_wr"] * 100, 1),
                oos_wr_pct=round(r["oos_wr"] * 100, 1),
                is_trades=r["is_trades"],
                oos_trades=r["oos_trades"],
                oos_return_pct=round(r["oos_return"] * 100, 2),
                p_value=r["p_value"],
            )
    json_path = OUTDIR / "intraday_results.json"
    json_path.write_text(json.dumps(summary, indent=2))

    # ── Markdown summary ──────────────────────────────────────────────
    md_lines = [
        "# NIFTY Intraday Strategy Evaluation\n",
        "Strategies from `SKILL_LIBRARY/NIFTY_STRATEGIES/` evaluated on synthetic",
        "intraday NIFTY data. Conditions applied **as-is** (same period numbers).",
        "Hold period = N bars (same integer as N days in daily version).",
        "Transaction cost = 0.10% round-trip.\n",
        "## OOS Results by Timeframe\n",
    ]

    for tf in tfs:
        md_lines.append(f"### {tf_labels[tf]}\n")
        md_lines.append("| Strategy | IS Win% | IS n | OOS Win% | OOS n | OOS Return | p-val |")
        md_lines.append("|----------|---------|------|----------|-------|------------|-------|")
        for strat in STRATEGIES:
            sid = strat["id"]
            r   = summary[tf][sid]
            p   = f"{r['p_value']:.4f}" if r["p_value"] else "—"
            md_lines.append(
                f"| {sid}: {strat['name']} | {r['is_wr_pct']:.1f}% | {r['is_trades']} "
                f"| **{r['oos_wr_pct']:.1f}%** | {r['oos_trades']} "
                f"| {r['oos_return_pct']:+.1f}% | {p} |"
            )
        md_lines.append("")

    md_lines += [
        "## Interpretation\n",
        "- **RSI / Stochastic** conditions fire frequently on short bars → many trades, "
          "win rate regresses toward 50% as noise dominates.",
        "- **S4 (Month-End Short)** may have few OOS trades on short bars "
          "because month-end occurs only ~12× per year.",
        "- **1h** is the most meaningful intraday timeframe for these mean-reversion "
          "strategies (comparable lookback depth to daily).",
        "- Strategies with **p < 0.05 OOS** retain genuine edge at that timeframe.\n",
    ]

    md_path = OUTDIR / "INTRADAY_REPORT.md"
    md_path.write_text("\n".join(md_lines))

    print(f"\n  Saved: {json_path.name}, {md_path.name}, {len(STRATEGIES)*len(tfs)} trade CSVs")

    # ── Final console table ────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  INTRADAY OOS WIN RATES  (% | n trades)")
    print("=" * 65)
    hdr = f"{'Strategy':28s}" + "".join(f"{t:>18s}" for t in tfs)
    print(hdr)
    print("-" * 65)
    for strat in STRATEGIES:
        sid = strat["id"]
        row_str = f"{sid+': '+strat['name']:28s}"
        for tf in tfs:
            r = summary[tf][sid]
            row_str += f"  {r['oos_wr_pct']:4.1f}% ({r['oos_trades']:3d})"
        print(row_str)
    print("=" * 65)
    print(f"\n  All files saved → {OUTDIR}/")


if __name__ == "__main__":
    main()
