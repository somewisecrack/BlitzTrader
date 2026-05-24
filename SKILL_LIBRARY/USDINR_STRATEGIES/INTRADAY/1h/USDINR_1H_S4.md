# USDINR_1H_S4: USDINR Intraday — SHORT 1-Year @ 1h

## Overview
| Field | Value |
|-------|-------|
| Instrument | **USDINR** (Futures intraday) |
| Timeframe | **1-Year @ 1h** |
| Direction | **SHORT** |
| Hold Period | **3 bar(s) × 1h** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +3 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **Composite macro = INR strength day**  `inr_strong_day >= 1.0`
2. **Gold down 0.5%+ [risk-on, USD weak]**  `gold_ret <= -0.005`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **64.1%** |
| OOS Trades | 39 |
| In-Sample Win Rate | 71.2% |
| OOS Cumulative Return | +0.9% |
| Binomial p-value | 0.0541 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 3 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹2–3 lakh for USDINR Futures (1 lot = $1000)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from macro drivers: crude, DXY, gold, SPX
- OOS period = last 35% of dataset; transaction cost included
