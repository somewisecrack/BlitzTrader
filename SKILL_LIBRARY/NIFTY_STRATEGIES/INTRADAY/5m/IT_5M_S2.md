# IT_5M_S2: NIFTY Intraday — SHORT 60-Day @ 5m

## Overview
| Field | Value |
|-------|-------|
| Instrument | NIFTY 50 Futures (intraday) |
| Timeframe | **60-Day @ 5m** |
| Direction | **SHORT** |
| Hold Period | **18 bar(s) × 5m** |
| Entry | Next bar open after all conditions met |
| Exit | Close of bar +18 |

## Entry Conditions
All conditions must be TRUE at bar close:

1. **Volume ≥ 1.5× avg [strong participation]**  `vol_ratio >= 1.5`
2. **2+ consecutive down bars [momentum]**  `consec_dn >= 2.0`
3. **Price below opening range low [ORB SHORT]**  `below_ors >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **69.2%** |
| OOS Trades | 13 |
| In-Sample Win Rate | 71.9% |
| OOS Return (net) | -0.8% |
| Binomial p-value | 0.1334 |

## Risk Management
- **Stop Loss**: trail at 0.5× ATR(14) from entry bar
- **Max hold**: 18 bars — always exit even if target not hit
- **No overnight**: exit at or before 15:15 IST (last 15 min reserved)
- **Min capital**: ₹6–7 lakh (1 NIFTY Futures lot); any amount for NIFTY ETF

## Notes
- Intraday momentum setup — trade WITH short-term trend, NOT against it
- All signals computed on **5m CLOSE** data available at bar close
- OOS period = last 35% of dataset
- Transaction cost: 0.10% round-trip (futures intraday)
