# BlitzTrader

An autonomous intraday **futures** trading bot for NSE index futures (`NIFTY`, `BANKNIFTY`, `FINNIFTY`), deployed on Google Cloud Platform.

## What it does

- Runs every trading day via a systemd timer on a GCP VM
- Logs into Shoonya (Finvasia) broker API at session start
- Resolves front-month NIFTY, BANKNIFTY, and FINNIFTY futures contracts at startup
- Uses Python to scan, select, execute, and manage **futures trades only**
- Uses the live bid/ask book for simulated market-order fills
- Sends real-time alerts and responds via a Telegram bot
- Writes a daily trading journal and maintains cross-session memory so it learns from past sessions
- Uses Gemini only for free-form Telegram chat and end-of-day summarization

## Architecture

```
systemd timer (9:00 AM IST, Mon–Fri)
        │
        ▼
    main.py
    ├── Startup phase      — login, resolve futures tokens, load memory, set session goals
    ├── Trading loop       — Python scanner every 60s + deterministic execution + Telegram poll
    │     ├── MarketData   — signal scanner, indicators, cache
    │     ├── OrderExec    — Python guardrails, fills, SL/target/trailing, EOD close
    │     └── LiveFeed     — WebSocket price stream from Shoonya
    └── EOD phase          — close positions, write journal, Gemini summary
```

## Key features

| Feature | Detail |
|---|---|
| Trading engine | Python-only |
| Gemini role | Free-form chat + EOD summary only |
| Broker | Shoonya (Finvasia) via `NorenRestApiPy` |
| Exchange | NFO — NIFTY, BANKNIFTY, and FINNIFTY front-month futures |
| Capital | ₹10,00,000 virtual tracked |
| Risk | Exactly 1 futures lot per trade, 5% per trade (₹50,000 max), 5% daily stop (₹50,000) |
| Margin | Shoonya RMS `GetOrderMargin` is queried before virtual entries; returned broker margin is stored on positions |
| Instruments | NIFTY, BANKNIFTY, and FINNIFTY futures; exact lot size resolved from Shoonya contract metadata at startup |
| Position caps | Max 3 open positions; no pyramiding, one open position per instrument |
| Daily trade cap | Max 10 total entries/day; completed trades + open positions + pending entries count |
| Execution | Futures-only guardrail: CE/PE and bare index names are hard-blocked |
| Trading window | 9:15 AM – 3:05 PM IST (CAUTION in first 15 min) |
| Telegram | Real-time alerts + simple status replies + Gemini chat fallback |
| Memory | Cross-session `journals/memory.md` |

## Execution enforcement

BlitzTrader is **futures-only**. Two hard guardrails are enforced in Python:

1. `place_virtual_order()` rejects any symbol ending in `CE` or `PE`.
2. `place_virtual_order()` rejects bare logical names (`NIFTY`, `BANKNIFTY`, etc.) — the agent must use the resolved futures tsym (e.g. `NIFTY28APR26F`).

The active futures tsym is resolved at startup via `get_front_month_futures_token()` and surfaced in every iteration context under `ACTIVE FUTURES INSTRUMENTS`.

## Strategies

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
