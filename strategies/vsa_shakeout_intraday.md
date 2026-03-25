# VSA Elite Strategy: Shakeout (Intraday)

A fast-paced institutional accumulation signal for volatile low-priced stocks.

## Attributes
- **Name**: VSA Shakeout (Intraday)
- **Type**: Intraday Trading
- **Status**: Elite
- **Verdict**: High-Speed Accumulation
- **Primary Market**: Stocks ($0-10)
- **Timeframe**: 15-minute
- **Win Rate**: 74%
- **Avg PnL**: 1.1% per trade

## Technical Rules
- **Pattern**: Down Bar with Wide Spread.
- **Closing Position**: Top Close (indicating strong recovery within the bar).
- **Volume**: Ultra High Volume relative to recent 15m bars.
- **Next Bar**: Ideally should close higher to confirm the "shakeout" is over.

## Backtest Evidence
- **Result**: 1.1% Avg PnL on 15m timeframe for stocks under $10.
- **Audit File**: `vsa_audit_results_15m.csv`

## Execution
1. **Trigger**: Wide spread down bar, ultra high volume, close near highs on 15m chart.
2. **Entry**: Market order on next bar open if confirmed.
3. **Stop Loss**: Low of the Shakeout bar.
4. **Target**: Bar-based exit (10 bars) or fixed intraday profit target.
