# NSE Intraday Trading Rules — BlitzTrader

## Instruments
- **NIFTY** futures (lot size: 25) — front-month FUTIDX, e.g. NIFTY28APR26F
- **BANKNIFTY** futures (lot size: 15) — front-month FUTIDX, e.g. BANKNIFTY28APR26F

> **LIVE EXECUTION NOTE:** All entries and exits are placed on FUTURES contracts only.
> Options (CE/PE) are blocked at the guardrail level. Use the futures tsym (e.g. NIFTY28APR26F)
> directly in place_virtual_order(). Do NOT call get_option_chain() as part of the entry flow.

## Session Phases

### Opening (9:15 – 9:30)
- High volatility, wide spreads. Do NOT enter in the first 5 minutes.
- Use this window to assess opening direction, gap behavior, and initial range.

### Core Session (9:30 – 14:30)
- Primary trading window. Look for setups with clear structure.
- Best setups tend to occur between 9:45 and 11:30.

### Late Session (14:30 – 15:05)
- Only take high-conviction setups with reduced position size.
- No new entries after 15:05.

### Close (15:05 – 15:15)
- Wind down only. Close all positions by 15:15.

## Entry Rules

### Breakout Long
- Price breaks above previous day high (PDH) or a consolidation range
- Confirm with: volume spike, VIX stable or declining, BANKNIFTY confirming
- Entry: BUY NIFTY/BANKNIFTY futures (tsym e.g. NIFTY28APR26F) on pullback to breakout level
- Stop: Below breakout level minus 0.5× ATR (futures price level)
- Target: 1.5× to 2× risk

### Breakdown Short
- Price breaks below previous day low (PDL) or support
- Confirm with: VIX rising, breadth negative, NIFTY/BANKNIFTY diverging
- Entry: SELL NIFTY/BANKNIFTY futures (tsym e.g. NIFTY28APR26F) on pullback to breakdown level
- Stop: Above breakdown level plus 0.5× ATR (futures price level)
- Target: 1.5× to 2× risk

### Mean Reversion
- Price extended 1.5× ATR from VWAP
- RSI divergence on 5-min chart
- Entry: Counter-trend trade on futures at extreme, using ATR-based stop
- Stop: Beyond the extreme (futures price level)
- Target: VWAP retest

## Risk Rules
- Max 5% capital per trade (₹15,000)
- Max 2 simultaneous positions
- If daily loss hits 5% (₹15,000), stop all trading
- Always define stop loss BEFORE entry
- Trade futures with defined stop-loss (risk = |entry - SL| × quantity)

## Market Regime Filters
- **VIX < 13**: Low vol environment. Breakouts may fail. Prefer mean reversion.
- **VIX 13-18**: Normal. Both breakout and mean reversion viable.
- **VIX > 18**: High vol. Wide stops needed. Reduce position size.
- **VIX > 25**: Extreme. Consider sitting out or trading only with defined risk.

## What NOT to Do
- No trading in first 5 minutes
- No averaging down losing positions
- No revenge trades after a loss
- No "hope trades" — every position needs a defined stop
- No fighting the trend — if NIFTY is clearly trending, do not fade it
