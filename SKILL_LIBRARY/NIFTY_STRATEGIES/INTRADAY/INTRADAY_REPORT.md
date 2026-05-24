# NIFTY Intraday Strategy Evaluation

Strategies from `SKILL_LIBRARY/NIFTY_STRATEGIES/` evaluated on synthetic
intraday NIFTY data. Conditions applied **as-is** (same period numbers).
Hold period = N bars (same integer as N days in daily version).
Transaction cost = 0.10% round-trip.

## OOS Results by Timeframe

### 1-Year @ 1h

| Strategy | IS Win% | IS n | OOS Win% | OOS n | OOS Return | p-val |
|----------|---------|------|----------|-------|------------|-------|
| S1: Oversold Bounce | 7.7% | 39 | **20.0%** | 25 | -9.9% | 0.9995 |
| S2: BB+INR Long | 33.3% | 6 | **25.0%** | 12 | -7.7% | 0.9807 |
| S3: Triple Overbought Short | 37.9% | 29 | **20.0%** | 10 | -9.0% | 0.9893 |
| S4: Month-End Short | 0.0% | 5 | **100.0%** | 2 | +3.7% | 0.2500 |
| S5: Crash-Bounce Long | 0.0% | 0 | **0.0%** | 0 | +0.0% | — |

### 60-Day @ 30m

| Strategy | IS Win% | IS n | OOS Win% | OOS n | OOS Return | p-val |
|----------|---------|------|----------|-------|------------|-------|
| S1: Oversold Bounce | 12.0% | 25 | **20.0%** | 10 | -4.4% | 0.9893 |
| S2: BB+INR Long | 37.5% | 8 | **33.3%** | 3 | -2.7% | 0.8750 |
| S3: Triple Overbought Short | 18.2% | 11 | **25.0%** | 8 | -5.7% | 0.9648 |
| S4: Month-End Short | 66.7% | 3 | **0.0%** | 0 | +0.0% | — |
| S5: Crash-Bounce Long | 0.0% | 0 | **0.0%** | 0 | +0.0% | — |

### 60-Day @ 15m

| Strategy | IS Win% | IS n | OOS Win% | OOS n | OOS Return | p-val |
|----------|---------|------|----------|-------|------------|-------|
| S1: Oversold Bounce | 5.9% | 34 | **20.0%** | 15 | -3.5% | 0.9963 |
| S2: BB+INR Long | 23.1% | 13 | **0.0%** | 6 | -2.9% | 1.0000 |
| S3: Triple Overbought Short | 25.0% | 28 | **13.3%** | 15 | -6.4% | 0.9995 |
| S4: Month-End Short | 33.3% | 6 | **0.0%** | 0 | +0.0% | — |
| S5: Crash-Bounce Long | 0.0% | 0 | **0.0%** | 0 | +0.0% | — |

### 60-Day @ 5m

| Strategy | IS Win% | IS n | OOS Win% | OOS n | OOS Return | p-val |
|----------|---------|------|----------|-------|------------|-------|
| S1: Oversold Bounce | 10.8% | 74 | **9.5%** | 42 | -4.9% | 1.0000 |
| S2: BB+INR Long | 19.4% | 31 | **7.4%** | 27 | -5.3% | 1.0000 |
| S3: Triple Overbought Short | 15.6% | 77 | **7.0%** | 43 | -9.2% | 1.0000 |
| S4: Month-End Short | 36.4% | 11 | **0.0%** | 0 | +0.0% | — |
| S5: Crash-Bounce Long | 0.0% | 0 | **0.0%** | 0 | +0.0% | — |

## Interpretation

- **RSI / Stochastic** conditions fire frequently on short bars → many trades, win rate regresses toward 50% as noise dominates.
- **S4 (Month-End Short)** may have few OOS trades on short bars because month-end occurs only ~12× per year.
- **1h** is the most meaningful intraday timeframe for these mean-reversion strategies (comparable lookback depth to daily).
- Strategies with **p < 0.05 OOS** retain genuine edge at that timeframe.
