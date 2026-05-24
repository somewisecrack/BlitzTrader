# IT_1H_S4: NIFTY Intraday — SHORT 1-Year @ 1h

## Overview
| Field | Value |
|-------|-------|
| Instrument | NIFTY 50 Futures (intraday) |
| Timeframe | **1-Year @ 1h** |
| Direction | **SHORT** |
| Hold Period | **1 bar(s) × 1h** |
| Entry | Next bar open after all conditions met |
| Exit | Close of bar +1 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **Price 0.2%+ below VWAP [strong bear]**  `vwap_dev <= -0.002`
2. **RSI14 ≤ 45 [clear bear momentum]**  `RSI_14 <= 45.0`
3. **RSI14 ≥ 30 [not oversold — room to fall]**  `RSI_14 >= 30.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **60.4%** |
| OOS Trades | 48 |
| In-Sample Win Rate | 73.4% |
| OOS Return (net) | -7.4% |
| Binomial p-value | 0.0967 |

## Risk Management
- **Stop Loss**: trail at 0.5× ATR(14) from entry bar
- **Max hold**: 1 bars — always exit even if target not hit
- **No overnight**: exit at or before 15:15 IST (last 15 min reserved)
- **Min capital**: ₹6–7 lakh (1 NIFTY Futures lot); any amount for NIFTY ETF

## Notes
- Intraday momentum setup — trade WITH short-term trend, NOT against it
- All signals computed on **1h CLOSE** data available at bar close
- OOS period = last 35% of dataset
- Transaction cost: 0.10% round-trip (futures intraday)
