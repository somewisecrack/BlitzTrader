# USDINR_1H_S3: USDINR Intraday — SHORT 1-Year @ 1h

## Overview
| Field | Value |
|-------|-------|
| Instrument | **USDINR** (Futures intraday) |
| Timeframe | **1-Year @ 1h** |
| Direction | **SHORT** |
| Hold Period | **4 bar(s) × 1h** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +4 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **Below opening range low**  `below_ors >= 1.0`
2. **DXY down 0.2%+ [dollar weak]**  `dxy_ret <= -0.002`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **86.4%** |
| OOS Trades | 22 |
| In-Sample Win Rate | 86.2% |
| OOS Cumulative Return | +2.6% |
| Binomial p-value | 0.0004 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 4 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹2–3 lakh for USDINR Futures (1 lot = $1000)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from macro drivers: crude, DXY, gold, SPX
- OOS period = last 35% of dataset; transaction cost included
