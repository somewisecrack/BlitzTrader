---
description: Vikram Prabhu intraday scalping — Counter Bull Trap short setup for Nifty & Bank Nifty.
---

# VP-01: Counter Bull Trap (Short)
**Source**: Vikram Prabhu — Day-Trading Stocks PDF  
**Instruments**: NIFTY, BANKNIFTY  
**Timeframes**: 2m ✅ | 5m ✅ | 15m ✅  
**Direction**: Short only  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 138 | 37.0% | 1.06 | +128 |
| NIFTY | 5m | 229 | 40.6% | **1.36** | **+1276** |
| NIFTY | 15m | 118 | 34.7% | 0.97 | -106 |
| BANKNIFTY | 2m | 138 | 32.6% | 1.25 | +1161 |
| BANKNIFTY | 5m | 246 | 35.0% | 1.07 | +803 |
| BANKNIFTY | 15m | 116 | 40.5% | **1.43** | **+3090** |

**Best**: BANKNIFTY 15m (PF 1.43) and NIFTY 5m (PF 1.36)  
**Risk:Reward Used**: 1:2

## 🛠️ Setup Rules
### Context
- Price must be **below EMA20** (bearish bias).

### Entry Trigger (Short)
1. A strong **green (bullish) candle** appeared in the last 10 bars — identify the largest-bodied green candle.
2. Current candle closes **red (bearish)**.
3. Current close is **below the close of that extreme green candle**.
4. Stop Loss = current candle's **High**.

### Target
- 2× the risk distance.

## 💡 Notes
- Concept: Bulls pushed price up but failed to continue. Trap sprung when price falls back below that candle's close.
- Works best with **high-volume** distribution candles.
- Avoid on strong uptrend days.
