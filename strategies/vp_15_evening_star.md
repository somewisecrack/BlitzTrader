---
description: Evening Star 3-candle bearish reversal. Top performer on BANKNIFTY short-TF (PF 1.89, backtested 2m; live uses 3m).
---

> **Live trading note (Apr 2026):** Backtests used 2m candles. Live engine runs on **3m candles** — treat all "2m" references as 3m.

# VP-15: Evening Star (Bearish Reversal) ⭐
**Source**: 25 Day Trading Strategies PDF  
**Instruments**: NIFTY, BANKNIFTY  
**Timeframes**: 2m ✅✅ | 5m ⚠️ | 15m ✅  
**Direction**: Short only  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 104 | 41.3% | **1.78** | **+871** |
| NIFTY | 5m | 104 | 34.6% | 0.91 | -199 |
| NIFTY | 15m | 39 | 35.9% | **1.44** | **+475** |
| BANKNIFTY | 2m | 108 | 43.5% | **1.89** | **+3094** |
| BANKNIFTY | 5m | 104 | 33.7% | 0.98 | -95 |
| BANKNIFTY | 15m | 32 | 37.5% | 1.30 | +837 |

**Best**: BANKNIFTY 2m (PF 1.89, +3094!) | NIFTY 2m (PF 1.78)

## 🛠️ Setup Rules (3-Candle)
1. **Candle 1**: Large bullish (green) — strong buying.
2. **Candle 2**: Small body/Doji (body ratio < 30%) — exhaustion at top.
3. **Candle 3**: Large bearish (red), body > 50% of C1's body, closes below EMA20.
4. Entry = close of C3. SL = highest high of C1 & C2. Target = 2× risk.

## 💡 Notes
- Dramatically outperforms Morning Star. Indices sell off more aggressively than they rally.
- Best at resistance (R1/R2 Pivots, supply zones, prior swing highs).
- Skip on 5m — mixed results.
