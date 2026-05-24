# IT_5M_S5: NIFTY Intraday — SHORT 60-Day @ 5m

## Overview
| Field | Value |
|-------|-------|
| Instrument | NIFTY 50 Futures (intraday) |
| Timeframe | **60-Day @ 5m** |
| Direction | **SHORT** |
| Hold Period | **3 bar(s) × 5m** |
| Entry | Next bar open after all conditions met |
| Exit | Close of bar +3 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **EMA9 < EMA21 by 0.2% [firm downtrend]**  `ema9_vs_e21 <= -0.002`
2. **EMA21 < EMA50 [medium downtrend]**  `e21_vs_e50 <= 0.0`
3. **3+ consecutive down bars [strong momentum]**  `consec_dn >= 3.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **70.0%** |
| OOS Trades | 20 |
| In-Sample Win Rate | 76.2% |
| OOS Return (net) | -6.2% |
| Binomial p-value | 0.0577 |

## Risk Management
- **Stop Loss**: trail at 0.5× ATR(14) from entry bar
- **Max hold**: 3 bars — always exit even if target not hit
- **No overnight**: exit at or before 15:15 IST (last 15 min reserved)
- **Min capital**: ₹6–7 lakh (1 NIFTY Futures lot); any amount for NIFTY ETF

## Notes
- Intraday momentum setup — trade WITH short-term trend, NOT against it
- All signals computed on **5m CLOSE** data available at bar close
- OOS period = last 35% of dataset
- Transaction cost: 0.10% round-trip (futures intraday)
