# BlitzTrader

An autonomous AI trading agent for NSE intraday options (NIFTY / BANKNIFTY), powered by Claude Haiku and deployed on Google Cloud Platform.

## What it does

- Runs every trading day via a systemd timer on a GCP VM
- Logs into Shoonya (Finvasia) broker API at session start
- Uses a Claude Haiku ReAct loop to analyze market conditions and place / manage options trades
- Sends real-time alerts and responds to commands via a Telegram bot
- Writes a daily trading journal and maintains cross-session memory so it learns from past sessions

## Architecture

```
systemd timer (9:00 AM IST, Mon–Fri)
        │
        ▼
    main.py
    ├── Startup phase      — load memory, strategy docs, set session goals
    ├── Trading loop       — 15-min scheduled iterations + 3s Telegram poll
    │     ├── AgentLoop    — Claude Haiku ReAct (Reason → Tool → Observe)
    │     ├── ToolRegistry — market data, order execution, journaling, Telegram
    │     └── LiveFeed     — WebSocket price stream from Shoonya
    └── EOD phase          — close positions, write journal, update memory
```

## Key features

| Feature | Detail |
|---|---|
| Model | `claude-haiku-4-5-20251001` |
| Broker | Shoonya (Finvasia) via `NorenRestApiPy` |
| Exchange | NSE — NIFTY & BANKNIFTY options |
| Capital | ₹3,00,000 virtual tracked |
| Risk | 2% per trade (₹6,000 max), 5% daily stop (₹15,000) |
| Instruments | NIFTY 25-lot, BANKNIFTY 15-lot |
| Trading window | 9:30 AM – 3:05 PM IST (no entries in first 15 min) |
| Telegram | Real-time alerts + conversational Q&A |
| Memory | Cross-session `journals/memory.md` |

## Strategies

All intraday, NSE-specific:

- **80-20 Reversal** — gap open reversal when price opens in top/bottom 20% of prior range
- **Momentum Pinball** — RSI < 30 / > 70 mean-reversion with first-hour breakout confirmation
- **VPA Hanging Man** — volume-price analysis: hanging man candle with climax volume
- **VPA No Demand** — narrow spread up-bar on low volume = weakness signal
- **VSA Shakeout** — smart money shakeout pattern (intraday variant, 74% WR)
- **VSA Upthrust / Hidden Upthrust / Buying Climax / Bag Holding** — VSA confluence signals
- **NSE Intraday Rules** — VIX regime awareness, option symbol formatting, position sizing

## Setup

### Prerequisites

- Python 3.10+
- GCP VM (e2-medium, `asia-south1` recommended)
- Shoonya trading account with API access
- Telegram bot token (via @BotFather)
- Anthropic API key

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
ANTHROPIC_API_KEY
```

## Telegram commands

| Command | Action |
|---|---|
| `/status` | Current positions, P&L, session goals |
| `/pause` | Pause new entries (exits still active) |
| `/resume` | Resume trading |
| `/abort` | Emergency stop — close all positions immediately |
| _(any message)_ | Claude responds conversationally |

## Cost

Approximately **$15–20 / month** on Claude Haiku 4.5 with 15-minute iteration intervals and per-iteration context reset.

## Disclaimer

This software is for **educational and research purposes only**. Automated trading carries significant financial risk. Past performance of any strategy does not guarantee future results. Use at your own risk.
