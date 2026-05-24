# BN_SW_S01: BANKNIFTY Swing — LONG 5 trading day(s)

## Overview
| Field | Value |
|-------|-------|
| Instrument | **BANKNIFTY 50** (Futures or ETF) |
| Direction | **LONG** |
| Hold Period | **5 trading day(s)** |
| Entry | Next open after all conditions met at EOD |
| Exit | Close of day +5 |
| OOS Period | 2021-09-17 → 2025-04-30 |

## Entry Conditions
All must be TRUE at **market close**:

1. **BB%B < 0.15 [lower band zone]**  `bb_pct <= 0.15`
2. **NIFTY positive [parent positive]**  `nifty_pos >= 1.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **76.9%** |
| OOS Trades | 26 |
| In-Sample Win Rate | 74.5% (51 trades) |
| Avg Return / Trade | +2.04% |
| Binomial p-value | 0.0047 |
| ₹1L Simulation | ₹164,985  (+65.0%) |
| Sim Win / Loss | 20 / 6 |
| Avg Win / Avg Loss | ₹4,763 / ₹-5,044 |

## Risk Management
- **Stop Loss**: 1.5 × ATR(14) from entry price
- **Max concurrent positions**: 1 (no overlap)
- **Min capital for futures**: ₹8–10 lakh (1 BANKNIFTY Futures lot ≈ 15 units)
- **For ETF (BankBees)**: any capital

## Cross-Asset Context
- BFSI components used: HDFCBANK, ICICIBANK, SBIN, AXISBANK, KOTAKBANK
- Parent index: NIFTY 50
- Volatility gauge: VIX
- All cross-asset signals are daily close values

## Notes
- Signals are predominantly **mean-reversion** (LONG setups) or **momentum SHORT**
- OOS period: last 35% of 10-year synthetic dataset
- Transaction cost: 0.15% round-trip included
