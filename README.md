# Dev Tracer Bot

Telegram bot that traces Pump.fun token deployers (devs) to detect insider wallet networks on Solana.

Given a token mint address, the bot traces the dev wallet's funding chain, finds all buyers, and detects convergence patterns — wallets that share funders or collectors, indicating coordinated insider activity.

## How It Works

```
Token Mint Address
        |
        v
  Dev Wallet Lookup (Pump.fun)
        |
        v
  Funding Chain Trace (who funded the dev?)
        |
        v
  Buyer Discovery (who bought this token?)
        |
        v
  Convergence Analysis
  (shared funders / shared collectors)
        |
        v
  Cluster Detection + Scoring
        |
        v
  Telegram Alert with full report
```

### 7-Step Trace Pipeline

1. **Dev Lookup** — Find the deployer wallet from Pump.fun transaction
2. **Funding Trace** — Follow SOL transfers backwards to find who funded the dev
3. **Buyer Discovery** — Get all wallets that bought the token (via bonding curve)
4. **Funding Analysis** — For each buyer, find their funder (CEX, personal wallet, etc.)
5. **Convergence Detection** — Find wallets that funded/collected from multiple buyers
6. **Cluster Building** — Group connected wallets into insider clusters
7. **Scoring & Report** — Score each cluster and generate Telegram alert

## Features

- **Dev Wallet Tracing** — Traces deployer funding chain up to N hops deep
- **Insider Detection** — Finds wallets sharing funders/collectors (convergence)
- **CEX Awareness** — Recognizes 30+ CEX hot wallets (Binance, OKX, Bybit, etc.)
- **Fee Vault Filtering** — Excludes GMGN, BullX, Photon and other router fee vaults
- **Pattern Learning** — `/teach` command learns from known insider cases for auto-detection
- **Auto-Scan** — Monitors PumpPortal WebSocket for new token launches
- **Multi-key Rotation** — Supports multiple Helius API keys with automatic rotation on rate limit

## Prerequisites

- **Python 3.10+**
- **Telegram bot** — Created via [@BotFather](https://t.me/BotFather)
- **Helius API keys** — Free tier at [helius.dev](https://helius.dev) (more keys = faster tracing)

## Setup

### 1. Clone & install

```bash
git clone https://github.com/dnafund/Degen-Scanner.git
cd Degen-Scanner
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Required
BOT_TOKEN=your_bot_token_here
HELIUS_API_KEYS=key1,key2,key3

# Where the bot sends alerts (your Telegram chat/group ID)
PUMP_ALERT_CHAT_ID=-1001234567890

# Optional: separate chat for dev trace results
DEV_ALERT_CHAT_ID=-1001234567890

# Optional: admin access
ADMIN_IDS=your_telegram_user_id
```

### 3. Run

```bash
python pump_scanner.py
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/devtrace <mint>` | Trace a token's dev wallet and detect insider clusters |
| `/trace <mint>` | Alias for `/devtrace` |
| `/teach <mint>` | Learn insider patterns from a known case |
| `/discuss <case_id>` | Interactive Q&A about a traced case |
| `/scan` | Toggle auto-scan for new Pump.fun launches |
| `/status` | Show bot status, key count, scan stats |
| `/help` | Show available commands |

## How `/devtrace` Works

```
/devtrace 7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr

Step 1/7: Looking up dev wallet...
  Dev: 5abc...Xf2d (funded by 9hqf...BYuu)

Step 2/7: Finding buyers (via bonding curve)...
  Found 847 buyers

Step 3/7: Tracing buyer funding...
  Checking 847 wallets... (progress updates)

Step 4/7: Convergence analysis...
  Found 12 shared funders, 5 shared collectors

Step 5/7: Building clusters...
  3 insider clusters detected

Step 6/7: Scoring...
  Cluster 1: INSIDER (score 8) — 15 wallets, 23% supply

Step 7/7: Done!
  [Full formatted report with Solscan links]
```

## Project Structure

```
Degen-Scanner/
├── pump_scanner.py      # Main bot — Telegram commands, auto-scan loop
├── dev_tracer.py        # Core tracing engine (7-step pipeline)
├── dev_tracer_fmt.py    # Format trace results for Telegram
├── pump_analyzer.py     # CEX wallets, fee vaults, bonding curve utils
├── pump_portal.py       # PumpPortal WebSocket client (new launches)
├── token_data.py        # Token metadata & holder data
├── helius.py            # Helius API client (multi-key rotation)
├── trace_bfs.py         # BFS wallet chain tracing
├── teach_pipeline.py    # Teaching pipeline (learn from cases)
├── teach_store.py       # Pattern storage (SQLite)
├── pattern_matcher.py   # Match learned patterns against new tokens
├── db.py                # Database helpers
├── config.py            # Configuration loader (.env)
├── .env.example         # Environment template
├── .gitignore
└── requirements.txt
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | Yes | Telegram bot token from @BotFather |
| `HELIUS_API_KEYS` | Yes | Comma-separated Helius API keys |
| `PUMP_ALERT_CHAT_ID` | Yes | Telegram chat ID for alerts |
| `DEV_ALERT_CHAT_ID` | No | Separate chat for dev trace results |
| `PUMP_BOT_TOKEN` | No | Separate bot token (defaults to BOT_TOKEN) |
| `MORALIS_API_KEYS` | No | Moralis keys for token discovery |
| `ADMIN_IDS` | No | Comma-separated admin user IDs |
| `LOG_LEVEL` | No | DEBUG, INFO, WARNING, ERROR (default: INFO) |

## License

MIT
