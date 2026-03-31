---
description: CPR (Central Pivot Range) Reversal. Works best on BANKNIFTY 2m (PF 1.75).
---

# VP-20: CPR Reversal
**Source**: 25 Day Trading Strategies PDF  
**Instruments**: BANKNIFTY (primary)  
**Timeframes**: 2m ✅✅ | 5m ⚠️ | 15m ⚠️  
**Direction**: Both Long & Short  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 16 | 50.0% | 1.13 | +134 |
| NIFTY | 5m | 39 | 51.3% | 0.91 | -180 |
| NIFTY | 15m | 26 | 46.2% | 0.69 | -517 |
| BANKNIFTY | 2m | 19 | **52.6%** | **1.75** | **+2126** |
| BANKNIFTY | 5m | 45 | 40.0% | 0.84 | -974 |
| BANKNIFTY | 15m | 38 | 52.6% | 0.96 | -211 |

**Best**: BANKNIFTY 2m (PF 1.75, 52.6% WR). ~19 trades / 30 days.

## 🛠️ Setup Rules
### CPR Calculation (Previous Day)
- **Pivot** = (H + L + C) / 3
- **TC** = (Pivot + H) / 2
- **BC** = (Pivot + L) / 2

### Short Entry (Rejection at TC)
1. Price approaches TC (within 50% of CPR width).
2. Bearish candle with upper wick > body.
3. Entry = close. SL = TC + (TC - BC). Target = 2× risk.

### Long Entry (Bounce at BC)
1. Price approaches BC (within 50% of CPR width).
2. Bullish candle with lower wick > body.
3. Entry = close. SL = BC - (TC - BC). Target = 2× risk.

## 💡 Notes
- Most powerful on **Narrow CPR days** (CPR width < 50% of 10-day avg).
- BANKNIFTY 2m is the only combination worth systematic trading.
- Premium, low-frequency setup — treat each signal as high value.
