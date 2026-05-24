# BANKNIFTY_15M_S3: BANKNIFTY Intraday — SHORT 60-Day @ 15m

## Overview
| Field | Value |
|-------|-------|
| Instrument | **BANKNIFTY** (Futures intraday) |
| Timeframe | **60-Day @ 15m** |
| Direction | **SHORT** |
| Hold Period | **8 bar(s) × 15m** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +8 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **Price at/below VWAP**  `vwap_dev <= 0.0`
2. **HDFCBANK down 0.3%+ [largest weight]**  `hdfcbank_ret <= -0.003`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **66.7%** |
| OOS Trades | 36 |
| In-Sample Win Rate | 73.8% |
| OOS Cumulative Return | +4.9% |
| Binomial p-value | 0.0326 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 8 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹6–7 lakh for BankNifty Futures (1 lot)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from BFSI component stocks + NIFTY/VIX
- OOS period = last 35% of dataset; transaction cost included
