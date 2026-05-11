# BlitzTrader Futures Strategy Wiki

## System Architecture — Critical Boundaries

### 1. Live Trading is 100% Python-Driven

All live trading decisions — signal generation, entry, exit, stop-loss, target — are executed
exclusively by Python code. No external system, model, or agent influences live trade selection.

### 2. LLM / Gemini Role (Limited and Postmarket Only)

Gemini/LLM is available **only** for:
- Telegram chat and Q&A
- End-of-day (EOD) analysis and summaries
- Postmarket research and wiki librarian duties

**Gemini/LLM is NOT a live gatekeeper.** It cannot approve trades, reject trades, place orders,
modify stops, or override any Python guardrail — ever.

### 3. LLM is a Postmarket Research Librarian

LLM interaction with this wiki is strictly postmarket and read/write to wiki files only. It may:
- Help draft hypotheses and strategy cards
- Summarize backtest results
- Suggest filter ideas for human review

It may NOT trigger or influence any live system component.

### 4. Prohibited LLM Actions (Hard Limits)

The following are explicitly prohibited and will be rejected by system validation:

- Approving or rejecting live trades
- Placing, modifying, or cancelling orders
- Modifying stop-losses or targets
- Overriding Python guardrails
- Acting as a live gatekeeper in any form

Any hypothesis or filter referencing "gemini gatekeeper", "live approval", "live rejection", or
"live gatekeeper" will be automatically rejected by the validation layer.

### 5. Gemini/LLM is NOT Added as a Live Gatekeeper

This is a hard architectural constraint. The wiki system does not create any pathway for LLM
involvement in live trade decisions. Promotion of a filter to live use means Python applies it —
not LLM.

### 6. Promotion Flow

```
hypothesis (proposed)
    --> backtest run in Python (yfinance only, no broker connection)
    --> backtest_result written to wiki/backtest_results/
    --> if passed: promoted filter written to wiki/promoted_filters/
    --> Python FuturesFilterLoader applies filter to live signals
```

LLM may assist in drafting the hypothesis. Python runs the backtest. Python applies the filter.

### 7. Scope: Futures Only

This wiki applies exclusively to futures instruments:

| Symbol     | yfinance Ticker          |
|------------|--------------------------|
| NIFTY      | ^NSEI                    |
| BANKNIFTY  | ^NSEBANK                 |
| FINNIFTY   | NIFTY_FIN_SERVICE.NS     |

No other symbols are in scope.

### 8. Pairs Trading — Explicitly Out of Scope

Pairs trading is **out of scope** for this wiki. The wiki system never touches pairs/ files,
pairs strategies, cointegration analysis, z-score signals, spread calculations, or hedge ratios.

Any hypothesis containing pairs-related fields (cointegration, z_score, spread, hedge_ratio,
coint_pvalue) will be automatically rejected.

### 9. Wiki Directory Structure

```
wiki/
  README.md                  -- this file
  daily_reviews/             -- EOD review notes (postmarket, human or LLM-assisted)
  strategy_cards/            -- strategy documentation cards
  hypotheses/                -- proposed filter hypotheses (YAML or JSON)
  backtest_results/          -- Python backtest outputs (JSON)
  promoted_filters/          -- active promoted filters applied by Python live
  metrics/                   -- performance tracking and attribution data
```

### 10. Signal and Indicator Fields

**Signal fields:** symbol, strategy, direction, interval, stop_loss, target, lot_size,
execution_symbol

**Indicator fields:** current_price, ema20, adx14, rsi14, avg_volume_20, ema_stacked_bull,
ema_stacked_bear, price_above_vwap (may not always be present)

**Supported filter block_when fields:**
rsi14_lt, rsi14_gt, adx14_lt, adx14_gt, ema_stacked_bull, ema_stacked_bear,
price_below_vwap, price_above_vwap

---

*This wiki is a research and documentation layer. The live trading engine remains autonomous,
Python-only, and unaffected by any content written here — until a filter is explicitly promoted
and Python picks it up via FuturesFilterLoader.*
