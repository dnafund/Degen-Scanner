"""
Degen Scanner - Configuration
Loads settings from .env file
"""

import json
import os
from dotenv import load_dotenv

load_dotenv()

# Telegram API credentials (get from https://my.telegram.org)
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

# Telegram Bot Token (get from @BotFather)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Group IDs
# Support multiple source groups (comma-separated)
_source_raw = os.getenv("SOURCE_GROUP_IDS", os.getenv("SOURCE_GROUP_ID", "0"))
SOURCE_GROUP_IDS: list[int] = [
    int(x.strip()) for x in _source_raw.split(",") if x.strip() and x.strip() != "0"
]
# Keep single ID for backward compat
SOURCE_GROUP_ID = SOURCE_GROUP_IDS[0] if SOURCE_GROUP_IDS else 0
ALERT_GROUP_ID = int(os.getenv("ALERT_GROUP_ID", "0"))
WHALE_ALERT_GROUP_ID = int(os.getenv("WHALE_ALERT_GROUP_ID", "0")) or ALERT_GROUP_ID
PUMP_ALERT_GROUP_ID = int(os.getenv("PUMP_ALERT_GROUP_ID", "0"))

# Pump Scanner — private bot for commands (DM to owner)
PUMP_BOT_TOKEN = os.getenv("PUMP_BOT_TOKEN", "") or BOT_TOKEN
PUMP_ALERT_CHAT_ID = int(os.getenv("PUMP_ALERT_CHAT_ID", "0")) or PUMP_ALERT_GROUP_ID

# Dev Alert — send devtrace results to a group via laocongquetsan bot
DEV_ALERT_BOT_TOKEN = os.getenv("DEV_ALERT_BOT_TOKEN", "") or BOT_TOKEN
DEV_ALERT_CHAT_ID = int(os.getenv("DEV_ALERT_CHAT_ID", "0"))

# Helius API (free tier — https://helius.dev)
# Required for whale splitter to check on-chain transfers
# Keys loaded from: .env (comma-separated) + tools/helius_keys.json
_helius_raw = os.getenv("HELIUS_API_KEYS", os.getenv("HELIUS_API_KEY", ""))
HELIUS_API_KEYS: list[str] = [
    k.strip() for k in _helius_raw.split(",") if k.strip()
]

# Merge keys from JSON file (dedup, preserving order)
_helius_json_path = os.path.join(os.path.dirname(__file__), "tools", "helius_keys.json")
if os.path.exists(_helius_json_path):
    try:
        with open(_helius_json_path, "r") as _f:
            _json_keys = [entry["api_key"] for entry in json.load(_f) if entry.get("api_key")]
        _existing = set(HELIUS_API_KEYS)
        for _k in _json_keys:
            if _k not in _existing:
                HELIUS_API_KEYS.append(_k)
                _existing.add(_k)
    except Exception:
        pass

# Whale Splitter — uses same source groups, only needs Helius key
# Keep WHALE_GROUP_ID for backward compat but not required
WHALE_GROUP_ID = int(os.getenv("WHALE_GROUP_ID", "0"))

# Clustering settings
TIME_WINDOW_MINUTES = int(os.getenv("TIME_WINDOW_MINUTES", "15"))  # 10-20 min range
MIN_CLUSTER_SIZE = int(os.getenv("MIN_CLUSTER_SIZE", "2"))  # Minimum wallets to trigger alert

# Scan interval (minutes) - how often to check for new messages
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))

# Session name for Telethon
SESSION_NAME = os.getenv("SESSION_NAME", "degen_scanner")

# Custom pattern rules storage path
CUSTOM_RULES_FILE = os.getenv(
    "CUSTOM_RULES_FILE",
    os.path.join(os.path.dirname(__file__), "data", "custom_rules.json"),
)

# Admin user IDs (comma-separated) — only these users can modify bot
ADMIN_IDS: set[int] = set()
_admin_raw = os.getenv("ADMIN_IDS", "")
if _admin_raw.strip():
    ADMIN_IDS = {int(x.strip()) for x in _admin_raw.split(",") if x.strip()}

# ─── Webhook Settings ──────────────────────────────────────────
WEBHOOK_ENABLED = os.getenv("WEBHOOK_ENABLED", "false").lower() in ("true", "1", "yes")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # ngrok/Cloudflare tunnel public URL
WEBHOOK_KEY_RESERVE = int(os.getenv("WEBHOOK_KEY_RESERVE", "10"))  # first N keys for webhooks

# ─── Free RPC Providers (supplement Helius) ───────────────────
# Shyft.to — parsed txns + Solana RPC (10 RPC/s free)
SHYFT_API_KEY = os.getenv("SHYFT_API_KEY", "")

# Alchemy — Solana RPC (30M CU/month free)
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")

# Ankr — Solana RPC (200M credits/month free)
ANKR_API_KEY = os.getenv("ANKR_API_KEY", "")

# Chainstack — Solana RPC (3M calls/month free)
CHAINSTACK_API_KEY = os.getenv("CHAINSTACK_API_KEY", "")

# Birdeye — Token analytics (30K CU/month free)
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")

# Bitquery — GraphQL blockchain data (1K calls/day free)
BITQUERY_API_KEY = os.getenv("BITQUERY_API_KEY", "")

# Build list of free RPC endpoints for basic calls
FREE_RPC_ENDPOINTS: list[str] = []
if SHYFT_API_KEY:
    FREE_RPC_ENDPOINTS.append(f"https://rpc.shyft.to?api_key={SHYFT_API_KEY}")
if ALCHEMY_API_KEY:
    FREE_RPC_ENDPOINTS.append(f"https://solana-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}")
if ANKR_API_KEY:
    FREE_RPC_ENDPOINTS.append(f"https://rpc.ankr.com/solana/{ANKR_API_KEY}")
if CHAINSTACK_API_KEY:
    FREE_RPC_ENDPOINTS.append(f"https://solana-mainnet.core.chainstack.com/{CHAINSTACK_API_KEY}")

# Moralis API (token discovery — https://moralis.io)
# Supports multiple keys (comma-separated) for round-robin rotation
_moralis_raw = os.getenv("MORALIS_API_KEYS", os.getenv("MORALIS_API_KEY", ""))
MORALIS_API_KEYS: list[str] = [
    k.strip() for k in _moralis_raw.split(",") if k.strip()
]

# Merge keys from JSON file (dedup, preserving order)
_moralis_json_path = os.path.join(os.path.dirname(__file__), "tools", "moralis_keys.json")
if os.path.exists(_moralis_json_path):
    try:
        with open(_moralis_json_path, "r") as _f:
            _json_keys = [entry["api_key"] for entry in json.load(_f) if entry.get("api_key")]
        _existing_m = set(MORALIS_API_KEYS)
        for _k in _json_keys:
            if _k not in _existing_m:
                MORALIS_API_KEYS.append(_k)
                _existing_m.add(_k)
    except Exception:
        pass

# Backward compat
MORALIS_API_KEY = MORALIS_API_KEYS[0] if MORALIS_API_KEYS else ""

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
