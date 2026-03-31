---
description: Power Candle Pullback. Retest of a large momentum candle's base. Best NIFTY 15m.
---

# VP-09: Power Candle Pullback
**Source**: 25 Day Trading Strategies PDF  
**Instruments**: NIFTY (15m) | BANKNIFTY (2m)  
**Timeframes**: 2m ⚠️ | 5m ⚠️ | 15m ✅  
**Direction**: Both Long & Short  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 96 | 33.3% | 0.90 | -75 |
| NIFTY | 5m | 73 | 38.4% | 1.06 | +47 |
| NIFTY | 15m | 18 | 44.4% | **1.41** | **+158** |
| BANKNIFTY | 2m | 79 | 39.2% | **1.29** | **+511** |
| BANKNIFTY | 5m | 62 | 25.8% | 0.64 | -754 |
| BANKNIFTY | 15m | 17 | 41.2% | 1.26 | +259 |

**Best**: NIFTY 15m (PF 1.41) | BANKNIFTY 2m (PF 1.29)

## 🛠️ Setup Rules
**Power Candle**: body > 75% of total range AND range > 1.3× the 5-period rolling average range.

### Long Setup
1. A **bullish Power Candle** within the last 15 bars.
2. Price pulls back to **touch the Power Candle's low**.
3. Pullback candle: closes above the PC low + lower wick > body (rejection).
4. Entry = close. SL = current candle's low. Target = 2× risk.

### Short Setup
1. A **bearish Power Candle** within the last 15 bars.
2. Price rallies back up to **touch the Power Candle's high**.
3. Rejection: closes below PC high + upper wick > body.
4. Entry = close. SL = current candle's high. Target = 2× risk.

## 💡 Notes
- Power candles represent institutional moves — their lows/highs act as support/resistance on the pullback.
- Too many false signals on 2m for NIFTY and 5m for BANKNIFTY.
- Best combined with a Pivot or EMA level at the Power Candle's base.
