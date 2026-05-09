# BlitzTrader

An autonomous intraday trading bot for NSE index **futures** and **statistical arbitrage pairs**, deployed on Google Cloud Platform. Runs deterministic Python-only decision loops with optional Gemini integration for chat and EOD analysis.

## What it does

- Runs every trading day via a systemd timer on a GCP VM
- Logs into Shoonya (Finvasia) broker API at session start
- Resolves front-month NIFTY, BANKNIFTY, and FINNIFTY futures contracts at startup
- **Futures trading**: Scans, selects, executes, and manages intraday index futures trades (₹10L capital)
- **Pairs trading**: Detects cointegrated pairs via Johansen test, monitors z-score mean-reversion, scales positions with Kalman filter hedging (₹5L base capital with 2x leverage = ₹10L gross)
- Uses Python scanners (60s interval) with deterministic execution guardrails — no LLM in trade decisions
- Uses the live bid/ask book for executable market-order fills
- Sends real-time alerts and responds via Telegram bot
- Writes a daily trading journal and maintains cross-session memory
- Uses Gemini only for free-form Telegram chat and end-of-day summarization

## Architecture

```
systemd timer (9:00 AM IST, Mon–Fri)
        │
        ▼
    main.py
    ├── Startup phase
    │     ├── Login + resolve futures tokens
    │     ├── Load cross-session memory
    │     └── Pairs scanner (yfinance + cointegration test)
    ├── Trading loop (every 60s)
    │     ├── Futures signals    — technical scanner + Python entry/exit logic
    │     ├── Pairs signals      — z-score monitoring + position scaling + trailing stops
    │     ├── OrderExecution    — simultaneous futures + pairs, RMS margin checks
    │     ├── WebSocket feed    — live Shoonya price stream
    │     └── Telegram polling  — user commands + alerts
    └── EOD phase
          ├── Force close all positions (3:15 PM IST)
          ├── Reconcile P&L for futures + pairs independently
          └── Write journal + Gemini reflection
```

## Key features

### Futures Trading
| Feature | Detail |
|---|---|
| Engine | Python-only (no LLM in decisions) |
| Capital | ₹10,00,000 virtual |
| Risk | 1 lot/trade, 5% per trade (₹50k max), 5% daily stop |
| Instruments | NIFTY, BANKNIFTY, FINNIFTY futures (front-month) |
| Position caps | Max 3 open; no pyramiding |
| Daily trade cap | Max 10 entries |
| Execution | CE/PE hard-blocked; bare names hard-blocked |

### Pairs Trading (Statistical Arbitrage)
| Feature | Detail |
|---|---|
| Capital | ₹5,00,000 base (2x leverage = ₹10,00,000 gross) |
| Universe | NIFTY 50 (yfinance: 50 symbols, 2,450 pairs tested daily) |
| Cointegration | Johansen test + CADF validation |
| Signals | Z-score mean-reversion (threshold: abs(z) > 2.0) + Hurst < 0.45 |
| Position scaling | Up to 10 pairs concurrent, ₹1,00,000 gross per pair |
| Hedging | Kalman filter beta for leg ratio balancing |
| Exits | Target/SL on z-score thresholds + trailing stops + EOD forced close |

### General
| Feature | Detail |
|---|---|
| Broker | Shoonya (Finvasia) |
| Margin | RMS `GetOrderMargin` queried before entries |
| Trading window | 9:15 AM – 3:05 PM IST |
| Telegram | Real-time alerts + commands + Gemini chat |
| Memory | Cross-session `journals/memory.md` |
| Gemini | Free-form chat + EOD summary only |

## Execution enforcement

**Futures module** — Two hard guardrails in Python:

1. `place_virtual_order()` rejects any symbol ending in `CE` or `PE`.
2. `place_virtual_order()` rejects bare logical names (`NIFTY`, `BANKNIFTY`, etc.) — must use resolved futures tsym (e.g. `NIFTY28APR26F`).

**Pairs module** — Independent capital pool (₹5L base, 2x leverage = ₹10L gross) with separate P&L tracking. No cross-contamination with futures capital.

## Pairs Trading: Statistical Arbitrage

The pairs module scans NIFTY 50 daily for cointegrated pairs using:

1. **Johansen cointegration test** — 0-lag Johansen on recent price history (60–365 days by interval)
2. **CADF validation** — Augmented Dickey-Fuller confirmation on the spread (p-value < 0.1)
3. **Z-score filtering** — abs(z_score) > 2.0 and Hurst exponent < 0.45 (mean-reversion signal)
4. **Half-life ranking** — Monte Carlo ensemble to estimate spread mean-reversion half-life

Selected pairs are ranked by win probability; top 10 are opened at 9:15 AM IST with:

- **Hedging**: Kalman filter estimated beta for ratio balancing (e.g., 1 INFY long : 0.8 TCS short)
- **Position sizing**: ₹1,00,000 gross notional per pair = ₹50,000 base + ₹50,000 leveraged hedge
- **Exits**: Z-score target thresholds (±1.0), 0.5% trailing stop, 3:15 PM forced EOD close

All pairs decisions are **Python-only** — no Gemini in the scanner or trade logic. Position P&L is tracked independently from futures.

## Futures Strategies

All intraday, executed on NIFTY / BANKNIFTY / FINNIFTY futures:

- **80-20 Reversal** — gap open reversal when price opens in top/bottom 20% of prior range
- **Momentum Pinball** — LBR/RSI < 30 / > 70 mean-reversion with first-hour breakout confirmation
- **VPA Hanging Man** — volume-price analysis: hanging man candle with climax volume
- **VPA No Demand** — narrow spread up-bar on low volume = weakness signal
- **VSA Shakeout** — smart money shakeout pattern (intraday variant, 74% WR)
- **VSA Upthrust / Hidden Upthrust / Buying Climax / Bag Holding** — VSA confluence signals

## Setup

### Prerequisites

- Python 3.10+
- GCP VM (e2-medium, `asia-south1` recommended)
- Shoonya trading account with API access
- Telegram bot token (via @BotFather)
- Gemini API key

### Install

```bash
git clone https://github.com/somewisecrack/BlitzTrader
cd BlitzTrader
pip install -r requirements.txt
cp .env.example .env
# fill in .env with your credentials
```

### Run locally

```bash
python main.py
```

### Deploy to GCP

See `setup.sh` for automated GCP setup. Key steps:

```bash
# On the VM — run once
chmod +x setup.sh && sudo ./setup.sh

# Copy systemd units
sudo cp blitztrader.service blitztrader.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blitztrader.timer
```

The timer fires at **9:00 AM IST, Monday–Friday**.

## Environment variables

See `.env.example` for the full list. Required:

```
SHOONYA_USER_ID
SHOONYA_PASSWORD
SHOONYA_API_KEY
SHOONYA_TOTP_SECRET
SHOONYA_VENDOR_CODE
TELEGRAM_BOT_TOKEN
TELEGRAM_AUTHORIZED_USER_ID
GEMINI_API_KEY
```

## Telegram commands

| Command | Action |
|---|---|
| `/status` | Current positions, P&L, session goals |
| `/pause` | Pause new entries (exits still active) |
| `/resume` | Resume trading |
| `/abort` | Emergency stop — close all positions immediately |
| _(any message)_ | Gemini responds conversationally |

## Gemini usage

Gemini is intentionally kept off the trading path:

- Python scanner runs every 60 seconds with **no Gemini call**
- Python decides whether to enter, reject, trail, or close
- Gemini is used only for:
  - free-form Telegram chat
  - end-of-day summary / reflection

## Disclaimer

This software is for **educational and research purposes only**. Automated trading carries significant financial risk. Past performance of any strategy does not guarantee future results. Use at your own risk.
