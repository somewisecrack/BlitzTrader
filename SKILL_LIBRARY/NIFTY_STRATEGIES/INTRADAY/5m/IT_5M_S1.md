# IT_5M_S1: NIFTY Intraday — SHORT 60-Day @ 5m

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
2. **3+ consecutive down bars [strong momentum]**  `consec_dn >= 3.0`
3. **Price below opening range low [ORB SHORT]**  `below_ors >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **86.7%** |
| OOS Trades | 15 |
| In-Sample Win Rate | 80.0% |
| OOS Return (net) | -2.5% |
| Binomial p-value | 0.0037 |

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
