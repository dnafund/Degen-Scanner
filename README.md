# Degen Scanner

Telegram bot that monitors CEX (Centralized Exchange) withdrawal messages and detects clusters of wallets funded within a short time window — indicating coordinated degen activity on Solana.

## How It Works

```
CEX Withdrawal Bot (e.g., Ray Onyx)
        │
        ▼
  Source Group (Telegram)
        │
        ▼
  ┌─────────────┐
  │ Degen Scanner│──── user account reads messages (Telethon MTProto)
  │              │──── bot account sends alerts
  └──────┬───────┘
         │
    ┌────┴────┐
    ▼         ▼
 Cluster   Custom
 Engine    Pattern
    │      Engine
    ▼         ▼
  Alert Group (Telegram)
```

1. **Monitor** — Reads messages from a Telegram group where a CEX withdrawal bot posts transfer notifications
2. **Parse** — Extracts wallet address, CEX name, SOL amount, TX link from each message
3. **Cluster** — Groups wallets by CEX source within a sliding time window
4. **Detect** — Identifies patterns: rapid fire, increasing amounts, same amount, similar amounts
5. **Alert** — Sends formatted alerts with Solscan links to your output Telegram group

## Features

- **Cluster Detection** — Per-CEX sliding window groups wallets funded in quick succession
- **Pattern Recognition** — Detects 4 patterns: rapid fire, increasing, same amount, similar
- **Custom Rules** — Create your own pattern rules via Telegram commands (`/addpattern`)
- **Whale Splitter** — Monitors large withdrawals (>12 SOL) and checks on-chain if they split to multiple wallets
- **Admin System** — Admin-only commands with auto-detection from group admins + hardcoded superadmin IDs
- **CLI Tool** — Send bot commands from terminal without opening Telegram (`python cli.py scan`)
- **Auto-scan** — Scans every 30 minutes (configurable), or manually via `/scan`
- **No database** — Fully in-memory with JSON config, uses Telethon SQLite session only

## Prerequisites

- **Python 3.10+**
- **Telegram user account** — Required to read messages (Bot API cannot read messages from other bots)
- **Telegram bot** — Created via [@BotFather](https://t.me/BotFather) for sending alerts and handling commands
- **Telegram API credentials** — Get from [my.telegram.org](https://my.telegram.org)
- **Two Telegram groups:**
  - Source group — Where the CEX withdrawal bot posts (e.g., Ray Onyx Wallet Tracker)
  - Alert group — Where Degen Scanner sends alerts (add your bot here as admin)
- **Optional:** Helius API key (free tier) for whale splitter on-chain checks

## Setup

### Step 1: Clone the repo

```bash
git clone https://github.com/dnafund/Degen-Scanner.git
cd Degen-Scanner
```

### Step 2: Install dependencies

```bash
pip install -r requirements.txt
```

### Step 3: Get Telegram API credentials

1. Go to [my.telegram.org](https://my.telegram.org)
2. Log in with your phone number
3. Go to **API development tools**
4. Create an application — note the `API_ID` and `API_HASH`

### Step 4: Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow the prompts
3. Copy the **BOT_TOKEN**
4. Add your bot to the **Alert group** and make it **admin**

### Step 5: Get Group IDs

You need IDs for your Telegram groups. Options:
- Forward a message from the group to [@userinfobot](https://t.me/userinfobot)
- Check the URL on Telegram Web (`-100` + number in URL)
- Use Telethon script

Group IDs are negative numbers (e.g., `-1001234567890`).

### Step 6: Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_api_hash_here
BOT_TOKEN=your_bot_token_here
SOURCE_GROUP_ID=-1001234567890
ALERT_GROUP_ID=-1001234567891

# Optional: whale monitoring
WHALE_GROUP_ID=-1001234567892
HELIUS_API_KEY=your_helius_key

# Tuning
TIME_WINDOW_MINUTES=15
MIN_CLUSTER_SIZE=2
SCAN_INTERVAL_MINUTES=30
```

### Step 7: Run the bot

```bash
python bot.py
```

On **first run**, Telethon will prompt for:
1. Your **phone number** (the user account that will read messages)
2. **Verification code** sent to your Telegram
3. **2FA password** (if enabled)

After login, a `.session` file is created — you won't need to log in again.

### Step 8 (Optional): Deploy to VPS

```bash
# Copy these files to your VPS:
# - All .py source files
# - .env (with your config)
# - degen_scanner.session (auth session from Step 7)
# - bot_session.session
# - requirements.txt

# On VPS:
pip install -r requirements.txt
python bot.py

# Or run in background:
nohup python bot.py > bot.log 2>&1 &
```

The `.session` file contains your login auth — the bot will work on VPS without re-login, even if your local PC is off.

## Bot Commands

Send these to your bot in the **Alert group** (admin-only commands marked with 🔒):

| Command | Description |
|---------|-------------|
| `/scan` | 🔒 Trigger manual cluster scan |
| `/scanbig` | 🔒 Trigger manual whale splitter scan |
| `/status` | Show bot status and stats |
| `/patterns` | List all custom pattern rules |
| `/addpattern` | 🔒 Create a new custom rule (guided wizard) |
| `/delpattern <id>` | 🔒 Delete a custom rule |
| `/togglepattern <id>` | 🔒 Enable/disable a rule |
| `/exchanges` | List known CEX names from scanned data |
| `/help` | Show available commands |

**Admin access:** Group admins are auto-detected (refreshed every 5 min). You can also set `ADMIN_IDS` in `.env` for superadmin access.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_API_ID` | — | Telegram API ID (required) |
| `TELEGRAM_API_HASH` | — | Telegram API Hash (required) |
| `BOT_TOKEN` | — | Bot token from @BotFather (required) |
| `SOURCE_GROUP_ID` | — | Group to read transfers from (required) |
| `ALERT_GROUP_ID` | — | Group to send alerts to (required) |
| `WHALE_GROUP_ID` | `0` | Group for large withdrawals (0 = disabled) |
| `HELIUS_API_KEY` | — | Helius API key for on-chain checks (optional) |
| `TIME_WINDOW_MINUTES` | `15` | Clustering window in minutes |
| `MIN_CLUSTER_SIZE` | `2` | Min wallets to trigger alert |
| `SCAN_INTERVAL_MINUTES` | `30` | Auto-scan interval in minutes |
| `SESSION_NAME` | `degen_scanner` | Telethon session file name |
| `ADMIN_IDS` | — | Comma-separated Telegram user IDs for superadmin access |
| `LOG_LEVEL` | `INFO` | Logging: DEBUG, INFO, WARNING, ERROR |

## Custom Pattern Rules

Create rules to detect specific patterns beyond default clustering:

**Pattern types:**
- `increasing` — Amounts strictly increase over time (e.g., 0.5 → 1.0 → 1.5 → 2.0)
- `decreasing` — Amounts strictly decrease over time
- `exact_amount` — All amounts within ±X% of median
- `rapid_fire` — Just needs N wallets in time window (any amounts)

**Example:** Detect 3+ wallets withdrawing increasing amounts (0.5-3.0 SOL) from MEXC within 15 minutes:

```
/addpattern
→ Name: MEXC Tăng Dần
→ CEX: mexc
→ Pattern: increasing
→ Amount: 0.5-3.0
→ Window: 15 min
→ Min wallets: 3
```

Custom rules support wider time windows than the default scan interval — the bot automatically fetches enough message history to cover your longest rule.

## CLI Tool

Send bot commands from the terminal without opening Telegram:

```bash
python cli.py scan          # /scan — trigger cluster scan
python cli.py scanbig       # /scanbig — trigger whale scan
python cli.py status        # /status — show bot stats
python cli.py patterns      # /patterns — list custom rules
python cli.py exchanges     # /exchanges — known exchanges
python cli.py send <text>   # send any text to the bot
python cli.py logs [N]      # show last N lines of bot_stderr.log
```

**First-time setup:** Copy your authenticated session file:

```bash
cp degen_scanner.session cli_session.session
```

The CLI uses a separate session (`cli_session`) to avoid SQLite lock conflicts with the running bot.

## Project Structure

```
Degen Scanner/
├── bot.py              # Main entry point, Telegram commands, scan loop
├── cli.py              # CLI tool — send bot commands from terminal
├── parser.py           # Message parser (extracts wallet, CEX, amount, TX)
├── cluster.py          # ClusterEngine — per-CEX sliding window detection
├── custom_rules.py     # CustomPatternEngine — user-defined pattern rules
├── storage.py          # RuleStore — JSON CRUD for custom rules + CEX registry
├── alerter.py          # Alert formatters (cluster + custom pattern)
├── splitter.py         # Whale splitter — detects wallet splitting on-chain
├── helius.py           # Helius API client for on-chain data
├── config.py           # Configuration loader (.env)
├── .env.example        # Environment template
├── .gitignore
├── requirements.txt
└── data/               # Runtime data (auto-created, gitignored)
    └── custom_rules.json
```

## Architecture Notes

- **Two Telegram clients**: User account (Telethon/MTProto) reads messages, Bot account sends alerts. This is required because Telegram's Bot API cannot read messages from other bots in groups.
- **Sliding window detection**: Each CEX has its own time window. Transfers are grouped per-CEX and cleaned up as they age out.
- **Periodic detection**: During each scan, detection runs every half-window to prevent data loss from cleanup (e.g., every 7.5 min for a 15-min window).
- **Custom rules**: Support independent time windows per rule. The fetch window auto-expands to cover the longest custom rule.

## License

MIT
