---
description: RCR (Red Candle Retracement). 50% pullback short re-entry. NIFTY 2m/5m only.
---

# VP-17: RCR — Red Candle Retracement
**Source**: 25 Day Trading Strategies PDF  
**Instruments**: NIFTY (primary)  
**Timeframes**: 2m ⚠️ | 5m ⚠️ | 15m ⚠️  
**Direction**: Short only  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 82 | 36.6% | 1.10 | +109 |
| NIFTY | 5m | 61 | 42.6% | 1.06 | +74 |
| NIFTY | 15m | 17 | 35.3% | 0.61 | -270 |
| BANKNIFTY | 2m | 75 | 32.0% | 0.92 | -255 |
| BANKNIFTY | 5m | 42 | 38.1% | 1.07 | +166 |
| BANKNIFTY | 15m | 8 | 37.5% | 1.03 | +19 |

**Verdict**: Marginally positive on NIFTY 2m/5m. Avoid 15m and BANKNIFTY 2m.

## 🛠️ Setup Rules
1. A **bearish Power Candle** in the last 10 bars.
2. Price retraces up to **50% of that candle's body** (midpoint of open-to-close).
3. Current candle is **bearish**, closes back below that 50% level.
4. Price must be **below EMA20**.
5. Entry = close. SL = Power Candle's high. Target = 2× risk.

## 💡 Notes
- Mirror of VP-16. 50% level = where remaining sell orders from institutions are sitting.
- Best used with a Pivot resistance (R1/R2) or supply zone at the 50% level.
