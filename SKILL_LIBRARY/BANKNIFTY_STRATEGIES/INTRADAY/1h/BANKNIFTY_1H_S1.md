# BANKNIFTY_1H_S1: BANKNIFTY Intraday — SHORT 1-Year @ 1h

## Overview
| Field | Value |
|-------|-------|
| Instrument | **BANKNIFTY** (Futures intraday) |
| Timeframe | **1-Year @ 1h** |
| Direction | **SHORT** |
| Hold Period | **1 bar(s) × 1h** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +1 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **RSI14 ≥ 30 [not oversold]**  `RSI_14 >= 30.0`
2. **≤1 BFSI stocks up [sector weakness]**  `bfsi_weak >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **80.6%** |
| OOS Trades | 93 |
| In-Sample Win Rate | 73.1% |
| OOS Cumulative Return | +19.3% |
| Binomial p-value | 0.0000 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 1 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹6–7 lakh for BankNifty Futures (1 lot)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from BFSI component stocks + NIFTY/VIX
- OOS period = last 35% of dataset; transaction cost included
