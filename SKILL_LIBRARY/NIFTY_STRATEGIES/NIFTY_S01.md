# NIFTY_S01: NIFTY Oversold Bounce — LONG 3d

## Overview
| Field | Value |
|-------|-------|
| Instrument | NIFTY 50 (ETF or Futures) |
| Direction | **LONG** |
| Hold Period | **3 trading day(s)** |
| Entry | Next open after all conditions met at EOD |
| Exit | Close of day +3 |

## Entry Conditions
All conditions must be TRUE at market close to enter next open:

1. **RSI(2) < 10 [extreme oversold]**  `RSI_2 <= 10`
2. **BB%B < 0.05 [below lower band]**  `bb_pct_raw <= 0.05`
3. **Stoch(5) < 20 [oversold]**  `STOCH5_rev >= 80`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **71.4%** |
| OOS Trades | 35 |
| In-Sample Win Rate | 75.4% |
| Avg Return per Trade | +1.05% |
| Binomial p-value | 0.0083 |
| ₹1L Simulation Final | ₹130,834 |
| Sim Win / Loss | 16 / 8 |

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
