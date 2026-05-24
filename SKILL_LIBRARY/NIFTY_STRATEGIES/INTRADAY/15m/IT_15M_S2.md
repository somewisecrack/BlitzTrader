# IT_15M_S2: NIFTY Intraday — SHORT 60-Day @ 15m

## Overview
| Field | Value |
|-------|-------|
| Instrument | NIFTY 50 Futures (intraday) |
| Timeframe | **60-Day @ 15m** |
| Direction | **SHORT** |
| Hold Period | **4 bar(s) × 15m** |
| Entry | Next bar open after all conditions met |
| Exit | Close of bar +4 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **EMA21 < EMA50 [medium downtrend]**  `e21_vs_e50 <= 0.0`
2. **Volume ≥ 1.5× avg [strong participation]**  `vol_ratio >= 1.5`
3. **Price below opening range low [ORB SHORT]**  `below_ors >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **80.0%** |
| OOS Trades | 10 |
| In-Sample Win Rate | 73.1% |
| OOS Return (net) | +1.9% |
| Binomial p-value | 0.0547 |

## Risk Management
- **Stop Loss**: trail at 0.5× ATR(14) from entry bar
- **Max hold**: 4 bars — always exit even if target not hit
- **No overnight**: exit at or before 15:15 IST (last 15 min reserved)
- **Min capital**: ₹6–7 lakh (1 NIFTY Futures lot); any amount for NIFTY ETF

## Notes
- Intraday momentum setup — trade WITH short-term trend, NOT against it
- All signals computed on **15m CLOSE** data available at bar close
- OOS period = last 35% of dataset
- Transaction cost: 0.10% round-trip (futures intraday)
