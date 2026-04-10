---
description: Counter Bear Trap long setup for Nifty & Bank Nifty. Best on NIFTY short timeframe.
---

> **Live trading note (Apr 2026):** Backtests used 2m candles. Live engine runs on **3m candles** — treat all "2m" references as 3m.

# VP-02: Counter Bear Trap (Long)
**Source**: Day-Trading Stocks PDF  
**Instruments**: NIFTY, BANKNIFTY  
**Timeframes**: 2m ✅ | 5m ⚠️ | 15m ⚠️  
**Direction**: Long only  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 149 | 32.9% | 1.23 | +446 |
| NIFTY | 5m | 230 | 32.6% | 0.86 | -583 |
| NIFTY | 15m | 80 | 37.5% | 0.84 | -369 |
| BANKNIFTY | 2m | 144 | 28.5% | 1.01 | +33 |
| BANKNIFTY | 5m | 214 | 29.9% | 0.76 | -2772 |
| BANKNIFTY | 15m | 110 | 36.4% | 0.85 | -1108 |

**Best**: NIFTY 2m only (PF 1.23). Underperforms on 5m/15m.

## 🛠️ Setup Rules
### Context
- Price must be **above EMA20** (bullish bias).

### Entry Trigger (Long)
1. A strong **red (bearish) candle** in the last 10 bars — identify the largest-bodied red candle.
2. Current candle closes **green (bullish)**.
3. Current close is **above the close of that extreme red candle**.
4. Stop Loss = current candle's **Low**.

### Target
- 2× the risk distance.

## 💡 Notes
- Mirror of VP-01. Bears got trapped when price reversed above the large red candle's close.
- Significantly weaker than the Bull Trap version. Best on NIFTY short timeframe (backtested 2m; live uses 3m).
