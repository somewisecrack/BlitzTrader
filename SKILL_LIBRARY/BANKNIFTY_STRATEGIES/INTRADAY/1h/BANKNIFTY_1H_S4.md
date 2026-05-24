# BANKNIFTY_1H_S4: BANKNIFTY Intraday — SHORT 1-Year @ 1h

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
2. **Below session open**  `price_vs_open <= 0.0`
3. **HDFCBANK down 0.3%+ [largest weight]**  `hdfcbank_ret <= -0.003`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **72.8%** |
| OOS Trades | 92 |
| In-Sample Win Rate | 71.4% |
| OOS Cumulative Return | +11.6% |
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
