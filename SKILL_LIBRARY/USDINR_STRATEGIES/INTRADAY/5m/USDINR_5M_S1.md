# USDINR_5M_S1: USDINR Intraday — LONG 60-Day @ 5m

## Overview
| Field | Value |
|-------|-------|
| Instrument | **USDINR** (Futures intraday) |
| Timeframe | **60-Day @ 5m** |
| Direction | **LONG** |
| Hold Period | **12 bar(s) × 5m** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +12 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **DXY up 0.2%+ [dollar strong]**  `dxy_ret >= 0.002`
2. **ATR above avg [volatile session]**  `atr_ratio >= 1.2`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **58.8%** |
| OOS Trades | 17 |
| In-Sample Win Rate | 71.4% |
| OOS Cumulative Return | +1.2% |
| Binomial p-value | 0.3145 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 12 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹2–3 lakh for USDINR Futures (1 lot = $1000)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from macro drivers: crude, DXY, gold, SPX
- OOS period = last 35% of dataset; transaction cost included
