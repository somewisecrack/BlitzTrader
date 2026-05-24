# BANKNIFTY_15M_S4: BANKNIFTY Intraday — SHORT 60-Day @ 15m

## Overview
| Field | Value |
|-------|-------|
| Instrument | **BANKNIFTY** (Futures intraday) |
| Timeframe | **60-Day @ 15m** |
| Direction | **SHORT** |
| Hold Period | **4 bar(s) × 15m** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +4 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **Below session open**  `price_vs_open <= 0.0`
2. **≤1 BFSI stocks up [sector weakness]**  `bfsi_weak >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **62.5%** |
| OOS Trades | 40 |
| In-Sample Win Rate | 74.2% |
| OOS Cumulative Return | +2.9% |
| Binomial p-value | 0.0769 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 4 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹6–7 lakh for BankNifty Futures (1 lot)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from BFSI component stocks + NIFTY/VIX
- OOS period = last 35% of dataset; transaction cost included
