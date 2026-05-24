# BANKNIFTY_15M_S5: BANKNIFTY Intraday — SHORT 60-Day @ 15m

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

1. **Price 0.2%+ below VWAP**  `vwap_dev <= -0.002`
2. **BB%B < 0.45 [lower half]**  `bb_pct <= 0.45`
3. **≤1 BFSI stocks up [sector weakness]**  `bfsi_weak >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **73.1%** |
| OOS Trades | 26 |
| In-Sample Win Rate | 71.2% |
| OOS Cumulative Return | +2.6% |
| Binomial p-value | 0.0145 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 4 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹6–7 lakh for BankNifty Futures (1 lot)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from BFSI component stocks + NIFTY/VIX
- OOS period = last 35% of dataset; transaction cost included
