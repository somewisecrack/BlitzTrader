# USDINR_15M_S2: USDINR Intraday — SHORT 60-Day @ 15m

## Overview
| Field | Value |
|-------|-------|
| Instrument | **USDINR** (Futures intraday) |
| Timeframe | **60-Day @ 15m** |
| Direction | **SHORT** |
| Hold Period | **8 bar(s) × 15m** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +8 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **Below session open**  `price_vs_open <= 0.0`
2. **Composite macro = INR strength day**  `inr_strong_day >= 1.0`
3. **Gold down 0.5%+ [risk-on, USD weak]**  `gold_ret <= -0.005`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **60.0%** |
| OOS Trades | 10 |
| In-Sample Win Rate | 72.0% |
| OOS Cumulative Return | +0.0% |
| Binomial p-value | 0.3770 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 8 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹2–3 lakh for USDINR Futures (1 lot = $1000)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from macro drivers: crude, DXY, gold, SPX
- OOS period = last 35% of dataset; transaction cost included
