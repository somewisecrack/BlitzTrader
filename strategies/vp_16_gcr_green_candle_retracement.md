---
description: GCR (Green Candle Retracement). 50% pullback re-entry. Short timeframe only (backtested 2m; live uses 3m).
---

> **Live trading note (Apr 2026):** Backtests used 2m candles. Live engine runs on **3m candles** — treat all "2m" references as 3m.

# VP-16: GCR — Green Candle Retracement
**Source**: 25 Day Trading Strategies PDF  
**Instruments**: NIFTY, BANKNIFTY  
**Timeframes**: 2m ⚠️ | 5m ❌ | 15m ⚠️  
**Direction**: Long only  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 71 | 32.4% | 1.16 | +154 |
| NIFTY | 5m | 41 | 22.0% | 0.43 | -570 |
| NIFTY | 15m | 10 | 40.0% | 0.58 | -160 |
| BANKNIFTY | 2m | 61 | 37.7% | 1.16 | +447 |
| BANKNIFTY | 5m | 46 | 34.8% | 0.85 | -403 |
| BANKNIFTY | 15m | 14 | 28.6% | 1.07 | +72 |

**Verdict**: Only marginally positive on 2m. Avoid 5m and 15m.

## 🛠️ Setup Rules
1. A **bullish Power Candle** in the last 10 bars.
2. Price retraces to **50% of that candle's body** (midpoint of open-to-close).
3. Current candle is **bullish**, closes above that 50% level.
4. Price must be **above EMA20**.
5. Entry = close. SL = Power Candle's low. Target = 2× risk.

## 💡 Notes
- Power green candle = institutional buy. 50% level = where remaining buy orders sit.
- 5m results notably poor. Best combined with a Pivot or EMA at the 50% level.
