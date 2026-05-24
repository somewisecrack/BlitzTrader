# IT_1H_S5: NIFTY Intraday — SHORT 1-Year @ 1h

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

1. **Price 0.5%+ below VWAP [dominant bear]**  `vwap_dev <= -0.005`
2. **2+ consecutive down bars [momentum]**  `consec_dn >= 2.0`
3. **Price below opening range low [ORB SHORT]**  `below_ors >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **63.2%** |
| OOS Trades | 38 |
| In-Sample Win Rate | 72.6% |
| OOS Return (net) | -5.7% |
| Binomial p-value | 0.0717 |

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
