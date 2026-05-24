# IT_15M_S5: NIFTY Intraday — SHORT 60-Day @ 15m

## Overview
| Field | Value |
|-------|-------|
| Instrument | NIFTY 50 Futures (intraday) |
| Timeframe | **60-Day @ 15m** |
| Direction | **SHORT** |
| Hold Period | **12 bar(s) × 15m** |
| Entry | Next bar open after all conditions met |
| Exit | Close of bar +12 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **Volume ≥ 1.2× avg [participation]**  `vol_ratio >= 1.2`
2. **2+ consecutive down bars [momentum]**  `consec_dn >= 2.0`
3. **Price below opening range low [ORB SHORT]**  `below_ors >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **60.0%** |
| OOS Trades | 10 |
| In-Sample Win Rate | 75.0% |
| OOS Return (net) | +0.0% |
| Binomial p-value | 0.3770 |

## Risk Management
- **Stop Loss**: trail at 0.5× ATR(14) from entry bar
- **Max hold**: 12 bars — always exit even if target not hit
- **No overnight**: exit at or before 15:15 IST (last 15 min reserved)
- **Min capital**: ₹6–7 lakh (1 NIFTY Futures lot); any amount for NIFTY ETF

## Notes
- Intraday momentum setup — trade WITH short-term trend, NOT against it
- All signals computed on **15m CLOSE** data available at bar close
- OOS period = last 35% of dataset
- Transaction cost: 0.10% round-trip (futures intraday)
