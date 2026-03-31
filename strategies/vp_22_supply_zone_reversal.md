---
description: Supply Zone Reversal. Best on NIFTY only (BANKNIFTY underperforms).
---

# VP-22: Supply Zone Reversal
**Source**: 25 Day Trading Strategies PDF  
**Instruments**: NIFTY only (avoid BANKNIFTY)  
**Timeframes**: 2m ✅ | 5m ⚠️ | 15m ✅✅  
**Direction**: Short only  

## 📊 Backtest Performance (60 days, yfinance)
| Symbol | TF | Trades | Win% | PF | Net Pts |
|---|---|---|---|---|---|
| NIFTY | 2m | 50 | 44.0% | **1.35** | **+577** |
| NIFTY | 5m | 45 | 46.7% | 1.04 | +63 |
| NIFTY | 15m | 11 | **54.5%** | **1.71** | **+216** |
| BANKNIFTY | 2m | 56 | 32.1% | 0.81 | -1120 |
| BANKNIFTY | 5m | 58 | 37.9% | 0.86 | -646 |
| BANKNIFTY | 15m | 17 | 41.2% | 0.98 | -15 |

**Best**: NIFTY 15m (PF 1.71, 54.5% WR) | NIFTY 2m (PF 1.35)  
**Avoid**: BANKNIFTY entirely.

## 🛠️ Setup Rules
1. In last 40 bars (excluding last 5), find all **swing highs**.
2. Supply Zone = highest swing high ± 0.2%.
3. Price enters zone (close within zone, up to 0.2% below top).
4. Bearish candle with upper wick > body (rejection).
5. Entry = close. SL = zone top + 0.2%. Target = 2× risk.

## 💡 Notes
- Institutions previously sold here — they tend to sell again on the return.
- BANKNIFTY's volatility breaks supply zones too frequently. NIFTY only.
- 15m provides cleaner zone identification with fewer false signals.
