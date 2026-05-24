# USDINR_1H_S2: USDINR Intraday — SHORT 1-Year @ 1h

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

1. **Price at/below VWAP [INR strong]**  `vwap_dev <= 0.0`
2. **DXY down 0.2%+ [dollar weak]**  `dxy_ret <= -0.002`
3. **Composite macro = INR strength day**  `inr_strong_day >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **71.9%** |
| OOS Trades | 32 |
| In-Sample Win Rate | 75.7% |
| OOS Cumulative Return | +0.8% |
| Binomial p-value | 0.0100 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 2 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹2–3 lakh for USDINR Futures (1 lot = $1000)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from macro drivers: crude, DXY, gold, SPX
- OOS period = last 35% of dataset; transaction cost included
