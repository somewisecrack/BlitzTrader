# NIFTY_S03: NIFTY Mean-Reversion Short — SHORT 5d

## Overview
| Field | Value |
|-------|-------|
| Instrument | NIFTY 50 (ETF or Futures) |
| Direction | **SHORT** |
| Hold Period | **5 trading day(s)** |
| Entry | Next open after all conditions met at EOD |
| Exit | Close of day +5 |

## Entry Conditions
All conditions must be TRUE at market close to enter next open:

1. **RSI(3) > 85 [very overbought]**  `RSI_3 >= 85`
2. **RSI(14) > 70 [overbought]**  `RSI_14 >= 70`
3. **BB%B > 0.90 [near upper band]**  `bb_pct_raw >= 0.9`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **76.0%** |
| OOS Trades | 25 |
| In-Sample Win Rate | 75.4% |
| Avg Return per Trade | +3.00% |
| Binomial p-value | 0.0073 |
| ₹1L Simulation Final | ₹132,996 |
| Sim Win / Loss | 11 / 5 |

## Risk Management
- **Stop Loss**: -1.5% × ATR(14) from entry
- **Max trades open**: 1 at a time (no overlapping positions)
- **Min capital for NIFTY Futures**: ₹6–7 lakh (1 lot margin)
- **For ETF (NiftyBees)**: Any capital

## Notes
- All conditions computed on **daily CLOSE** data
- Signals are mean-reversion based — enter after the market has stretched
- OOS period: last 35% of 10-year dataset
- Transaction costs of 0.15% included in equity simulation
