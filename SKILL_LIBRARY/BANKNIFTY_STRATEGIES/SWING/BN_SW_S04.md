# BN_SW_S04: BANKNIFTY Swing — SHORT 5 trading day(s)

## Overview
| Field | Value |
|-------|-------|
| Instrument | **BANKNIFTY 50** (Futures or ETF) |
| Direction | **SHORT** |
| Hold Period | **5 trading day(s)** |
| Entry | Next open after all conditions met at EOD |
| Exit | Close of day +5 |
| OOS Period | 2021-09-17 → 2025-04-30 |

## Entry Conditions
All must be TRUE at **market close**:

1. **3d return > +3% [extended rally]**  `ret3 >= 0.03`
2. **HDFCBANK down 0.3%+ [heavyweight]**  `hdfcbank_ret <= -0.003`
3. **Price 4%+ above MA50 [extended]**  `c_vs_ma50 >= 4.0`

## Performance (Out-of-Sample)
| Metric | Value |
|--------|-------|
| OOS Win Rate | **76.2%** |
| OOS Trades | 21 |
| In-Sample Win Rate | 76.8% (56 trades) |
| Avg Return / Trade | +1.47% |
| Binomial p-value | 0.0133 |
| ₹1L Simulation | ₹133,203  (+33.2%) |
| Sim Win / Loss | 16 / 5 |
| Avg Win / Avg Loss | ₹4,086 / ₹-6,433 |

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
