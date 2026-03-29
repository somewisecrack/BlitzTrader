---
description: Vikram Prabhu — VP Strategy Index. Master reference for all 21 backtested intraday strategies.
---

# Vikram Prabhu Intraday Strategy Index
**Source PDFs**: 1-Minute Scalping in Nifty & Bank Nifty | 25 Day Trading Strategies | Day-Trading Stocks  
**Backtested**: 60 days | NIFTY & BANKNIFTY | 2m, 5m, 15m | 1:2 Risk:Reward

---

## ⭐ Tier 1 — Highest Priority (PF ≥ 1.5 on best setting)

| # | Strategy | Best Instrument | Best TF | PF | Net Pts | File |
|---|---|---|---|---|---|---|
| 07 | Wicks Pullback | **BANKNIFTY** | 2m | 1.96 | +5171 | `vp_07_wicks_pullback.md` |
| 24 | Pivot Point Bounce | **BANKNIFTY** | 2m | 2.14 | +4790 | `vp_24_pivot_point_bounce.md` |
| 15 | Evening Star | **BANKNIFTY** | 2m | 1.89 | +3094 | `vp_15_evening_star.md` |
| 21 | Extreme Candle Reversal | **BANKNIFTY** | 15m | 3.08 | +1900 | `vp_21_extreme_candle_reversal.md` |
| 05 | 3-EMA Trend | **NIFTY** | 2m | 1.84 | +885 | `vp_05_3ema_trend.md` |
| 13 | Open Drive (OD) | **Both** | 5m/15m | 21.7 | +1775 | `vp_13_open_drive.md` |
| 08 | V Reversal | **BANKNIFTY** | 2m | 1.75 | +1116 | `vp_08_v_reversal.md` |

## ✅ Tier 2 — Good Edge (PF 1.2–1.5)

| # | Strategy | Best Instrument | Best TF | PF | Net Pts | File |
|---|---|---|---|---|---|---|
| 01 | Counter Bull Trap | BANKNIFTY | 15m | 1.43 | +3090 | `vp_01_counter_bull_trap.md` |
| 18 | M Pattern (Double Top) | BANKNIFTY | 2m | 1.61 | +3358 | `vp_18_m_pattern_double_top.md` |
| 19 | W Pattern (Double Bot) | NIFTY | 5m | 1.52 | +749 | `vp_19_w_pattern_double_bottom.md` |
| 10 | First Candle Open | BANKNIFTY | 15m | 1.57 | +1333 | `vp_10_first_candle_open.md` |
| 20 | CPR Reversal | BANKNIFTY | 2m | 1.75 | +2126 | `vp_20_cpr_reversal.md` |
| 22 | Supply Zone Reversal | NIFTY | 15m | 1.71 | +216 | `vp_22_supply_zone_reversal.md` |

## ⚠️ Tier 3 — Marginal / Context-Dependent (PF 1.0–1.2)
> Use these only with strong confluence (e.g., at a Pivot level or clear EMA zone). Do not trade blind.

| # | Strategy | Best Setting | File |
|---|---|---|---|
| 09 | Power Candle Pullback | NIFTY 15m | `vp_09_power_candle_pullback.md` |
| 16 | GCR | NIFTY/BNF 2m | `vp_16_gcr_green_candle_retracement.md` |
| 17 | RCR | NIFTY 2m/5m | `vp_17_rcr_red_candle_retracement.md` |
| 14 | Morning Star | Confluence only at support | `vp_14_morning_star.md` |
| 02 | Counter Bear Trap | NIFTY 2m only | `vp_02_counter_bear_trap.md` |

> **Note**: VP-24 Pivot Point Bounce works only for BANKNIFTY — do not apply to NIFTY.

---

## Overall Backtest Stats
- **Total Trades**: 7,888 | **Win Rate**: 37.5% | **PF**: 1.12 | **Net Pts**: +39,281
- **Period**: 60 days yfinance data (30 days for 2m)
- **Risk:Reward**: 1:2 (fixed)
- **Max Trades/Day**: 5
- **Exit**: SL hit / Target hit / 15:15 EOD

---

## Quick Reference: Best Combinations by Instrument

### NIFTY Best Setups
1. `05 3-EMA Trend` on **2m** (PF 1.84)
2. `01 Counter Bull Trap` on **5m** (PF 1.36)
3. `07 Wicks Pullback` on **2m** (PF 1.56)
4. `13 Open Drive` on **5m/15m** (PF 5–11)

### BANKNIFTY Best Setups
1. `24 Pivot Point Bounce` on **2m** (PF 2.14)
2. `07 Wicks Pullback` on **2m** (PF 1.96)
3. `21 Extreme Candle Reversal` on **15m** (PF 3.08)
4. `15 Evening Star` on **2m** (PF 1.89)
5. `13 Open Drive` on **15m** (PF 21.7!)
