# BANKNIFTY_5M_S4: BANKNIFTY Intraday — SHORT 60-Day @ 5m

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

1. **EMA9 < EMA21 [downtrend]**  `ema9_vs_e21 <= 0.0`
2. **Volume ≥ 1.5× avg**  `vol_ratio >= 1.5`
3. **HDFCBANK down 0.3%+ [largest weight]**  `hdfcbank_ret <= -0.003`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **67.6%** |
| OOS Trades | 37 |
| In-Sample Win Rate | 71.7% |
| OOS Cumulative Return | +3.9% |
| Binomial p-value | 0.0235 |

## Risk Management
- **Stop Loss**: 0.5 × ATR(14) from entry bar
- **Max hold**: 18 bars — exit unconditionally
- **No overnight** positions — close by 15:15 IST
- **Min capital**: ₹6–7 lakh for BankNifty Futures (1 lot)

## Notes
- Intraday momentum setup — trade WITH short-term trend direction
- Cross-asset filters from BFSI component stocks + NIFTY/VIX
- OOS period = last 35% of dataset; transaction cost included
