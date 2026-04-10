---
description: Pivot Point Bounce. Outstanding on BANKNIFTY short-TF (PF 2.14, 54% WR, backtested 2m; live uses 3m).
---

> **Live trading note (Apr 2026):** Backtests used 2m candles. Live engine runs on **3m candles** — treat all "2m" references as 3m.

# VP-24: Pivot Point Bounce ⭐ TOP PERFORMER
**Source**: 25 Day Trading Strategies PDF  
**Instruments**: BANKNIFTY (primary) | NIFTY ❌  
**Timeframes**: 2m ✅✅ | 5m ✅ | 15m ⚠️  
**Direction**: Both Long & Short  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 67 | 32.8% | 0.88 | -299 |
| NIFTY | 5m | 90 | 37.8% | 0.86 | -479 |
| NIFTY | 15m | 56 | 37.5% | 0.79 | -468 |
| BANKNIFTY | 2m | 63 | **54.0%** | **2.14** | **+4790** |
| BANKNIFTY | 5m | 112 | 44.6% | **1.30** | **+2701** |
| BANKNIFTY | 15m | 61 | 37.7% | 0.77 | -1379 |

**Best**: BANKNIFTY 2m (PF 2.14, 54% WR!)  
**Avoid**: NIFTY entirely and BNF 15m.

## 🛠️ Setup Rules
### Pivot Levels (from previous day H, L, C)
- **Pivot** = (H + L + C) / 3  |  **R1** = 2×Pivot − L  |  **S1** = 2×Pivot − H
- **R2** = Pivot + (H − L)  |  **S2** = Pivot − (H − L)

### Long Entry (Bounce off Support)
1. Price approaches Pivot, S1, or S2 (within 0.1%).
2. Bullish candle with **lower wick > body**.
3. Entry = close. SL = candle low − 0.2%. Target = 2× risk.

### Short Entry (Rejection at Resistance)
1. Price approaches Pivot, R1, or R2 (within 0.1%).
2. Bearish candle with **upper wick > body**.
3. Entry = close. SL = candle high + 0.2%. Target = 2× risk.

## 💡 Notes
- BANKNIFTY respects pivot levels far more than NIFTY — algo orders cluster at these levels.
- R2/S2 bounces are rarer but even higher probability.
- Combine VP-15 Evening Star at R1/R2 for maximum short confluence.
- Combine VP-14 Morning Star at S1/S2 for maximum long confluence.
