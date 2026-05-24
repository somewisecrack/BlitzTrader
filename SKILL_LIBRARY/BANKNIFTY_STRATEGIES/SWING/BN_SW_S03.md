# BN_SW_S03: BANKNIFTY Swing — LONG 7 trading day(s)

## Overview
| Field | Value |
|-------|-------|
| Instrument | **BANKNIFTY 50** (Futures or ETF) |
| Direction | **LONG** |
| Hold Period | **7 trading day(s)** |
| Entry | Next open after all conditions met at EOD |
| Exit | Close of day +7 |
| OOS Period | 2021-09-17 → 2025-04-30 |

## Entry Conditions
All must be TRUE at **market close**:

1. **BB%B < 0.15 [lower band zone]**  `bb_pct <= 0.15`
2. **KOTAKBANK up 0.3%+ [quality bank]**  `kotakbank_ret >= 0.003`
3. **1d return < -2% [sharp down day]**  `ret1 <= -0.02`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **77.3%** |
| OOS Trades | 22 |
| In-Sample Win Rate | 76.0% (25 trades) |
| Avg Return / Trade | +2.96% |
| Binomial p-value | 0.0085 |
| ₹1L Simulation | ₹185,159  (+85.2%) |
| Sim Win / Loss | 17 / 5 |
| Avg Win / Avg Loss | ₹6,714 / ₹-5,795 |

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
