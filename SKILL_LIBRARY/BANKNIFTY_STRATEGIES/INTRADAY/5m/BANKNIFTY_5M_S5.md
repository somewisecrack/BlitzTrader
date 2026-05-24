# BANKNIFTY_5M_S5: BANKNIFTY Intraday — SHORT 60-Day @ 5m

## Overview
| Field | Value |
|-------|-------|
| Instrument | **BANKNIFTY** (Futures intraday) |
| Timeframe | **60-Day @ 5m** |
| Direction | **SHORT** |
| Hold Period | **18 bar(s) × 5m** |
| Entry | Next bar open after all conditions met at bar close |
| Exit | Close of bar +18 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **RSI14 ≤ 50 [bearish zone]**  `RSI_14 <= 50.0`
2. **Volume ≥ 1.2× avg**  `vol_ratio >= 1.2`
3. **SBIN down 0.3%+ [PSU weakness]**  `sbin_ret <= -0.003`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **63.2%** |
| OOS Trades | 38 |
| In-Sample Win Rate | 70.3% |
| OOS Cumulative Return | -0.6% |
| Binomial p-value | 0.0717 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 18 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹6–7 lakh for BankNifty Futures (1 lot)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from BFSI component stocks + NIFTY/VIX
- OOS period = last 35% of dataset; transaction cost included
