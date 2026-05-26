# BlitzTrader

An autonomous intraday trading system for NSE index **futures**, deployed on Google Cloud Platform. All trade decisions are deterministic Python. Gemini is the live entry gatekeeper and post-market research assistant.

## What it does

- Runs every trading day via a systemd timer on a GCP VM (8:20 AM IST)
- Logs into Shoonya (Finvasia) broker API and resolves front-month futures contracts at startup
- **Futures trading**: Scans, selects, executes, and manages intraday index futures trades (₹10L capital)
- **Two-stage entry gate**: Python hard guardrails → Gemini gatekeeper (5s timeout, APPROVE/REJECT only)
- **Self-improvement loop**: Post-market script pipeline proposes, backtests, and promotes filter hypotheses — promoted filters are loaded automatically next session
- Sends real-time alerts and responds to commands via Telegram bot
- Gemini used only for: live entry gating, free-form Telegram chat, EOD summaries, and post-market hypothesis proposals

## Architecture

```
systemd timer (8:20 AM IST, Mon–Fri)
        │
        ▼
    main.py
    ├── Startup
    │     ├── Login + resolve futures tokens
    │     ├── Load cross-session memory
    │     └── Load promoted futures filters (wiki/promoted_filters/)
    │
    ├── Trading loop (every 60s)  ← Python-only, no LLM in decisions
    │     ├── Futures signals    — VP strategy scanner + filter application
    │     ├── Two-stage gate     — Python review → Gemini gatekeeper
    │     ├── OrderExecution     — RMS margin checks, virtual fill
    │     ├── WebSocket feed     — live Shoonya price stream
    │     └── Telegram polling   — commands + alerts
    │
    └── EOD phase
          ├── Force-close all positions (3:15 PM IST)
          ├── Reconcile P&L
          ├── Write trading journal
          └── Gemini EOD summary

──────────────────────────────────────────────────
Post-market self-improvement pipeline (run manually or via cron)
──────────────────────────────────────────────────

journal / logs
    │
    ▼
scripts/evaluate_futures_day.py
    │   Parses daily journal → compact structured review (Markdown)
    │   Stored in wiki/daily_reviews/YYYY-MM-DD.md
    ▼
scripts/propose_futures_hypotheses.py
    │   Sends compact review to Gemini (one attempt, post-market only)
    │   Gemini proposes ≤3 candidate futures filter hypotheses (strict JSON)
    │   Python validates schema, rejects equity/invented-strategy content
    │   Python enforces: strategy must appear in review AND be in SUPPORTED_STRATEGIES
    │   On any failure → writes audit artifact to wiki/hypotheses/no_proposals/
    │   Hypotheses written to wiki/hypotheses/HYP-YYYYMMDD-NNN.json
    ▼
scripts/backtest_futures_hypothesis.py
    │   Downloads OHLCV from yfinance only (^NSEI / ^NSEBANK)
    │   Runs real VP strategy signals via tools/futures_strategy_engine.py
    │   Simulates filter: baseline vs filtered trade stats
    │   Writes result to wiki/backtest_results/HYP-*.json
    ▼
scripts/promote_futures_hypothesis.py
    │   Checks promotion thresholds (profit factor ≥10% improvement, ≥10 filtered trades)
    │   Writes promoted filter to wiki/promoted_filters/FILT-*.json
    │   Updates hypothesis status to "promoted"
    ▼
next live session startup → main.py loads wiki/promoted_filters/ automatically
```

## Entry gate invariants

```
Python hard guardrails  ─── reject ──▶  SKIP (never reaches Gemini)
        │ pass
        ▼
Gemini gatekeeper (5s timeout)
        │ APPROVE                   │ REJECT / timeout / error
        ▼                           ▼
  place order                   discard signal
```

- **Gemini** is fail-closed: any error, timeout, or malformed response → signal rejected.
- **Python** owns ALL exits: stop-loss, trailing stop, target, EOD force-close, manual `/abort`.

## Key features

### Futures Trading
| Feature | Detail |
|---|---|
| Engine | Python-only (no LLM in decisions) |
| Capital | ₹10,00,000 virtual |
| Risk | 1 lot/trade, 5% per trade (₹50k max), 5% daily stop |
| Instruments | NIFTY, BANKNIFTY futures (front-month) |
| Position caps | Max 2 open; no pyramiding |
| Execution guard | CE/PE hard-blocked; bare logical names hard-blocked |
| Promoted filters | Loaded from `wiki/promoted_filters/` at startup; applied in `_review_signal_python()` |

### Gemini Gatekeeper
| Feature | Detail |
|---|---|
| Model | `gemini-2.5-flash-lite` (configurable) |
| Timeout | 5s hard limit |
| Required fields | `decision`, `confidence` [0–1], `reason`, `risk_notes`, `conditions_checked` (list), `must_not_override_python_guardrails` (must be `true`) |
| Forbidden fields | `stop_loss`, `target`, `quantity`, `leverage`, `capital`, `lot_size` — response rejected if present |
| Fail-closed | Any error, timeout, or validation failure → signal rejected |

### Futures Self-Improvement Loop
| Feature | Detail |
|---|---|
| Review | Python parses journal → compact Markdown (≤2,500 chars sent to Gemini) |
| Proposal | Gemini proposes ≤3 filter hypotheses; Python validates every field |
| Strategy gate | Strategy must be seen in review **and** in `SUPPORTED_STRATEGIES` |
| Backtesting | yfinance OHLCV + real VP signals + forward-walk simulation |
| Promotion | Profit factor improves ≥10%, ≥10 filtered trades, drawdown no worse |
| Audit trail | `wiki/hypotheses/no_proposals/YYYY-MM-DD.json` written on any LLM failure |

## Futures Strategies

All intraday, executed on NIFTY / BANKNIFTY futures:

| Code | Name | Type |
|---|---|---|
| VP-01 | Counter Bull Trap | Reversal |
| VP-02 | Counter Bear Trap | Reversal |
| VP-05 | 3EMA Trend | Trend-follow |
| VP-07 | Wicks Pullback | Pullback |
| VP-14 | Morning Star | Candlestick reversal |
| VP-15 | Evening Star | Candlestick reversal |
| VP-18 | M-Pattern Double Top | Pattern reversal |
| VP-19 | W-Pattern Double Bottom | Pattern reversal |
| VP-21 | Extreme Candle Reversal | Volatility reversal |

## Wiki directory layout

```
wiki/
├── README.md                    — governance rules and Gemini boundaries
├── daily_reviews/               — YYYY-MM-DD.md  (compact session reviews)
├── hypotheses/
│   ├── HYP-YYYYMMDD-NNN.json   — proposed filter candidates
│   └── no_proposals/
│       └── YYYY-MM-DD.json     — audit artifact when no hypotheses generated
├── backtest_results/            — HYP-*.json  (backtest outputs)
├── promoted_filters/            — FILT-*.json  (loaded live at startup)
├── strategy_cards/              — per-strategy performance notes
└── metrics/                     — session-level aggregate stats
```

## Repository layout

```
BlitzTrader/
├── main.py                      — session orchestrator (startup, loop, EOD)
├── agent_loop.py                — Gemini multi-turn chat (Telegram + EOD only)
├── config.py                    — all tunable constants and env vars
├── context_builder.py           — Gemini prompt assembly helpers
├── tools/
│   ├── futures_filter_loader.py — load & apply promoted filters at runtime
│   ├── futures_hypothesis.py    — hypothesis/backtest schema helpers
│   ├── futures_strategy_engine.py — pure-Python VP signal scanner (no broker dep)
│   ├── gemini_gatekeeper.py     — entry gate: APPROVE/REJECT with 5s timeout
│   ├── market_data.py           — live signal scanner (broker-connected)
│   ├── order_execution.py       — Shoonya order placement + RMS checks
│   └── ...
├── scripts/
│   ├── evaluate_futures_day.py       — journal → compact review
│   ├── propose_futures_hypotheses.py — Gemini hypothesis proposer
│   ├── backtest_futures_hypothesis.py — yfinance backtest
│   └── promote_futures_hypothesis.py  — promote passing filters
├── tests/                       — full test suite
└── wiki/                        — research artifacts (see above)
```

## Setup

### Prerequisites

- Python 3.10+
- GCP VM (e2-medium, `asia-south1` recommended)
- Shoonya (Finvasia) trading account with API access
- Telegram bot token (via @BotFather)
- Gemini API key (`google-genai>=1.0.0`)

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

```bash
# On the VM — run once
chmod +x setup.sh && sudo ./setup.sh

# Copy systemd units
sudo cp blitztrader.service blitztrader.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blitztrader.timer
```

The timer fires at **8:20 AM IST, Monday–Friday**, giving time for startup before the 9:15 AM market open.

### Run the post-market pipeline

```bash
# After market close — run in order:
python3 scripts/evaluate_futures_day.py
python3 scripts/propose_futures_hypotheses.py          # calls Gemini once
python3 scripts/backtest_futures_hypothesis.py --hypothesis wiki/hypotheses/HYP-*.json
python3 scripts/promote_futures_hypothesis.py  --hypothesis wiki/hypotheses/HYP-*.json

# Restart BlitzTrader — promoted filters load automatically at startup
```

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

Optional (tune Gemini model/cost):
```
GEMINI_MODEL                        # default: gemini-2.5-flash
GEMINI_GATEKEEPER_MODEL             # default: gemini-2.5-flash-lite
GEMINI_GATEKEEPER_TIMEOUT_SECONDS   # default: 5
GEMINI_SCHEDULED_MODEL              # default: gemini-2.5-flash-lite (proposal script)
```

## Telegram commands

| Command | Action |
|---|---|
| `/status` | Current positions, P&L, session goals |
| `/pause` | Pause new entries (exits still active) |
| `/resume` | Resume trading |
| `/abort` | Emergency stop — close all positions immediately |
| _(any message)_ | Gemini responds conversationally |

## Security invariants

- **No market-data fallbacks**: backtesting uses yfinance only; live prices come from Shoonya feed exclusively.
- **No LLM overrides**: Gemini cannot change stop-loss, target, quantity, capital, or leverage.
- **Fail-closed gatekeeper**: if Gemini is unavailable, times out, or returns invalid schema → signal rejected.
- **Python owns all exits**: SL, trailing stop, target, EOD force-close, and manual abort are all deterministic Python.

## Tests

```bash
python3 -m pytest tests/ -q
```

Key test files:

| File | Covers |
|---|---|
| `test_gemini_gatekeeper.py` | Schema validation, forbidden fields, confidence range, guardrail ack |
| `test_gatekeeper_entry_flow.py` | Two-stage gate integration, Python→Gemini flow |
| `test_futures_wiki.py` | Hypothesis/backtest schema validation, journal parsing |
| `test_futures_filter_loader.py` | Filter loading, application, JSON-only loading |
| `test_futures_hypothesis_backtest.py` | Script importability, main.py wiring, strategy engine |
| `test_propose_futures_hypotheses.py` | Gemini SDK mock, no-proposals artifact, strategy gate |

## Disclaimer

This software is for **educational and research purposes only**. Automated trading carries significant financial risk. Past performance of any strategy does not guarantee future results. Use at your own risk.
