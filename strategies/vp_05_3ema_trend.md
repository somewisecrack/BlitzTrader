---
description: 3-EMA Trend Following pullback. Best on NIFTY 2m (50% WR, PF 1.84).
---

# VP-05: 3-EMA Trend Following ⭐
**Source**: 1-Minute Scalping PDF  
**Instruments**: NIFTY (primary) | BANKNIFTY 5m  
**Timeframes**: 2m ✅✅ | 5m ✅ (BNF) | 15m ⚠️  
**Direction**: Both Long & Short  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 76 | **50.0%** | **1.84** | **+885** |
| NIFTY | 5m | 86 | 33.7% | 0.85 | -313 |
| NIFTY | 15m | 28 | 39.3% | 0.95 | -45 |
| BANKNIFTY | 2m | 94 | 36.2% | 1.16 | +612 |
| BANKNIFTY | 5m | 96 | 42.7% | **1.22** | **+1110** |
| BANKNIFTY | 15m | 40 | 40.0% | 0.96 | -157 |

**Best**: NIFTY 2m (PF 1.84, 50% WR — highest win rate in the library)

## 🛠️ Setup Rules
**Requires**: EMA20, EMA50, EMA100.

### Long Entry (Uptrend)
1. **Trend filter**: Close > EMA20 > EMA50 > EMA100 (all stacked bullishly).
2. Pin bar at **EMA20**: wicks into it, closes above, lower wick > body → SL = EMA50.
3. OR pin bar at **EMA50**: closes above EMA50, lower wick > body → SL = EMA100.
4. Entry = close. Target = 2× risk.

### Short Entry (Downtrend)
1. **Trend filter**: Close < EMA20 < EMA50 < EMA100.
2. Pin bar at **EMA20**: closes below it, upper wick > body → SL = EMA50.
3. OR pin bar at **EMA50**: closes below EMA50, upper wick > body → SL = EMA100.

## 💡 Notes
- Only valid when all 3 EMAs are clearly stacked. Skip if tangled or crossing.
- If EMA gaps are very wide, skip — SL will be too large.
