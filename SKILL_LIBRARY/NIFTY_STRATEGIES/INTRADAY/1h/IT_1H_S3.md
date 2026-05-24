# IT_1H_S3: NIFTY Intraday — SHORT 1-Year @ 1h

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

1. **RSI14 ≥ 30 [not oversold — room to fall]**  `RSI_14 >= 30.0`
2. **2+ consecutive down bars [momentum]**  `consec_dn >= 2.0`
3. **Break below 10-bar low [BD]**  `breakdown_10 >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **66.7%** |
| OOS Trades | 45 |
| In-Sample Win Rate | 72.3% |
| OOS Return (net) | -2.3% |
| Binomial p-value | 0.0178 |

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
