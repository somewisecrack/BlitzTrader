# USDINR_1H_S5: USDINR Intraday — SHORT 1-Year @ 1h

## Overview
| Field | Value |
|-------|-------|
| Instrument | **USDINR** (Futures intraday) |
| Timeframe | **1-Year @ 1h** |
| Direction | **SHORT** |
| Hold Period | **2 bar(s) × 1h** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +2 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **RSI14 ≥ 30 [not oversold]**  `RSI_14 >= 30.0`
2. **BB%B < 0.45 [lower half]**  `bb_pct <= 0.45`
3. **3+ consecutive down bars**  `consec_dn >= 3.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **66.7%** |
| OOS Trades | 36 |
| In-Sample Win Rate | 72.0% |
| OOS Cumulative Return | -0.7% |
| Binomial p-value | 0.0326 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 2 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹2–3 lakh for USDINR Futures (1 lot = $1000)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from macro drivers: crude, DXY, gold, SPX
- OOS period = last 35% of dataset; transaction cost included
