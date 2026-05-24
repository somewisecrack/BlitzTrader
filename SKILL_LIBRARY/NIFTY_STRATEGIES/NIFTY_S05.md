# NIFTY_S05: NIFTY Oversold Bounce — LONG 3d

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

1. **RSI(14) < 35 [near oversold]**  `RSI_14 <= 35`
2. **Stoch(5) < 20 [oversold]**  `STOCH5_rev >= 80`
3. **3-day return < -3%**  `ret_3d_raw <= -0.03`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **66.7%** |
| OOS Trades | 30 |
| In-Sample Win Rate | 80.4% |
| Avg Return per Trade | +0.81% |
| Binomial p-value | 0.0494 |
| ₹1L Simulation Final | ₹123,984 |
| Sim Win / Loss | 15 / 6 |

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
