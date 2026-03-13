"""
Degen Scanner - Telegram Bot
Two-client architecture:
- user_client: Reads source group message history (requires user session)
- bot_client: Sends alerts + handles /commands

Commands:
  /start         - Welcome message
  /scan          - Trigger manual cluster scan
  /scanbig       - Trigger manual whale splitter scan
  /whalequeue    - View whale queue (top 20)
  /whalestats    - Whale splitter statistics
  /whalecheck    - Check a wallet on-chain
  /whalethreshold- View/set MIN_WHALE_SOL
  /whaleremove   - Remove wallet from queue
  /whaleclear    - Purge stale or clear all
  /summary       - Activity summary (3h/12h/24h)
  /status        - Bot stats
  /patterns      - List custom pattern rules
  /addpattern    - Guided wizard to add a custom rule
  /delpattern    - Delete a custom rule
  /togglepattern - Enable/disable a rule
  /analyze       - Analyze Pump.fun token pre-bond buyers
  /devtrace      - Trace dev wallets (funding + profit clustering)
  /teach         - Teach bot about dev wallets (/teach <mint> <w1,w2>)
  /teachlist     - List teach cases
  /teachstats    - Teach statistics
  /teachremove   - Remove a teach case
  /exchanges     - List known CEX exchanges
  /help          - Show available commands

Usage:
    python -X utf8 bot.py
"""

import logging
import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

# UTC+7 timezone
UTC7 = timezone(timedelta(hours=7))

from telethon import TelegramClient, events, Button
from telethon.tl.functions.bots import SetBotCommandsRequest, ResetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telethon.events import StopPropagation
from telethon.errors import FloodWaitError

from config import (
    API_ID, API_HASH, BOT_TOKEN, SOURCE_GROUP_ID, SOURCE_GROUP_IDS, ALERT_GROUP_ID, WHALE_ALERT_GROUP_ID,
    PUMP_ALERT_GROUP_ID, HELIUS_API_KEYS, MORALIS_API_KEYS,
    LOG_LEVEL, ADMIN_IDS,
    TIME_WINDOW_MINUTES, MIN_CLUSTER_SIZE, SCAN_INTERVAL_MINUTES,
    SESSION_NAME, CUSTOM_RULES_FILE,
)
from parser import parse_message_with_entities, is_transfer_message
from cluster import ClusterEngine
from alerter import (
    format_cluster_alert, format_whale_split_alert, format_custom_pattern_alert,
    format_alert_page, cleanup_cache as cleanup_alert_cache,
    format_cex_withdrawal_alert, format_token_transfer_alert,
)
from helius import HeliusClient
from splitter import SplitterEngine
from pump_analyzer import PumpAnalyzer, format_pump_analysis
from dev_tracer import DevTracer
from dev_tracer_fmt import format_trace_result
from token_data import TokenDataClient
from pump_portal import PumpPortalClient
from pump_scanner import (
    ScannerState, TelegramSender, scan_full,
    _on_ws_new_token, _on_ws_migration,
)
from storage import RuleStore, VALID_PATTERN_TYPES
from custom_rules import CustomPatternEngine
from db import init_db, load_stats, save_stat, increment_stat, log_activity, query_activity

# ─── Wallet Display Helpers ───────────────────────────────────────

def _w(addr: str) -> str:
    """Format wallet/mint for display: first4...last4"""
    if len(addr) <= 10:
        return addr
    return f"{addr[:4]}...{addr[-4:]}"


def _wc(addr: str) -> str:
    """Format wallet/mint as <code> tag."""
    return f"<code>{_w(addr)}</code>"


def _wl(addr: str) -> str:
    """Format wallet with <code> + Solscan link."""
    return f"<code>{_w(addr)}</code> <a href='https://solscan.io/account/{addr}'>🔗</a>"


# ─── Logging ─────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("DegenScanner")
logging.getLogger("telethon").setLevel(logging.WARNING)


# ─── Global State (SQLite-backed) ─────────────────────────────────

init_db()
stats = load_stats()

# Load learned CEX wallets from DB
from pump_analyzer import load_dynamic_cex
load_dynamic_cex()

# log_activity and query_activity imported from db.py

engine = ClusterEngine(
    time_window_minutes=TIME_WINDOW_MINUTES,
    min_cluster_size=MIN_CLUSTER_SIZE,
)

# ─── Custom Pattern Engine ─────────────────────────────────────

rule_store = RuleStore(CUSTOM_RULES_FILE)
custom_engine = CustomPatternEngine(rule_store)

# ─── Wizard State for /addpattern ──────────────────────────────

# user_id → {step, data, expires_at}
wizard_states: dict[int, dict] = {}
WIZARD_TIMEOUT = 300  # 5 minutes

# ─── Teach Wizard State ───────────────────────────────────────
# user_id → {"step": "mint"|"wallets", "mint": str, "expires_at": float}
teach_wizard_states: dict[int, dict] = {}

WIZARD_STEPS = [
    "name", "cex", "pattern_type", "amount_range",
    "time_window", "max_interval", "min_wallets", "confirm",
]

# ─── Whale Splitter Pipeline ────────────────────────────────────

helius_client = HeliusClient(api_keys=HELIUS_API_KEYS)
splitter = SplitterEngine(helius=helius_client)
pump_analyzer = PumpAnalyzer(helius=helius_client)
dev_tracer = DevTracer(helius=helius_client)

WHALE_ENABLED = len(HELIUS_API_KEYS) > 0

# ─── Dev Scanner (ATH autoscan + DevTrace) ────────────────────
token_data_client = TokenDataClient(moralis_api_keys=MORALIS_API_KEYS)
scanner_state = ScannerState(
    path=os.path.join(os.path.dirname(__file__), "data", "bot_scanner_state.json")
)
dev_sender = TelegramSender(bot_token=BOT_TOKEN, chat_id=PUMP_ALERT_GROUP_ID)


last_scan_time = None
scan_lock = asyncio.Lock()

# Resolved entity objects — set at startup, used for send_message
# (raw integer IDs can fail with "Invalid Peer" after session restart)
alert_entity = None
whale_alert_entity = None
pump_alert_entity = None


# ─── Safe Message Fetcher (FloodWait protection) ─────────────────

MAX_FETCH_RETRIES = 3

async def safe_iter_messages(client, group_id, offset_date, since, limit=500):
    """
    Fetch messages with FloodWait retry protection.

    Instead of crashing on Telegram rate limit, waits and retries.
    Limit reduced from 2000 to 500 — 30-min window rarely exceeds 500 msgs.
    """
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            messages = []
            async for msg in client.iter_messages(
                group_id, offset_date=offset_date, limit=limit
            ):
                if msg.date < since:
                    break
                if msg.text and is_transfer_message(msg.text):
                    messages.append(msg)
            return messages
        except FloodWaitError as e:
            wait_sec = e.seconds + 5  # extra buffer
            logger.warning(
                f"⏳ FloodWait: Telegram rate limited for {e.seconds}s "
                f"(attempt {attempt + 1}/{MAX_FETCH_RETRIES}). "
                f"Waiting {wait_sec}s..."
            )
            await asyncio.sleep(wait_sec)
        except Exception as e:
            logger.error(f"Error fetching messages: {e}")
            return []

    logger.error(f"Failed to fetch messages after {MAX_FETCH_RETRIES} retries")
    return []


async def safe_send_alert(bot_client, entity, text, parse_mode='html', buttons=None):
    """Send alert with FloodWait retry protection."""
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            await bot_client.send_message(
                entity, text,
                parse_mode=parse_mode,
                link_preview=False,
                buttons=buttons,
            )
            return True
        except FloodWaitError as e:
            wait_sec = e.seconds + 2
            logger.warning(
                f"⏳ FloodWait on send: waiting {wait_sec}s "
                f"(attempt {attempt + 1}/{MAX_FETCH_RETRIES})"
            )
            await asyncio.sleep(wait_sec)
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
            return False
    logger.error("Failed to send alert after retries")
    return False


# ─── Admin Check ─────────────────────────────────────────────────

_admin_cache: set[int] = set()
_admin_cache_time: float = 0
ADMIN_CACHE_TTL = 300  # refresh every 5 min

ADMIN_DENIED_MSG = "🔒 Admin only."


async def refresh_admin_cache(client):
    """Fetch group admins from ALERT_GROUP_ID and cache them."""
    global _admin_cache, _admin_cache_time
    try:
        from telethon.tl.types import ChannelParticipantsAdmins
        target = alert_entity if alert_entity else ALERT_GROUP_ID
        participants = await client.get_participants(
            target, filter=ChannelParticipantsAdmins,
        )
        _admin_cache = {p.id for p in participants}
        # Merge hardcoded ADMIN_IDS as superadmins
        _admin_cache.update(ADMIN_IDS)
        _admin_cache_time = time.time()
        logger.info(f"🔐 Admin cache refreshed: {len(_admin_cache)} admins")
    except Exception as e:
        logger.error(f"Failed to fetch admin list: {e}")
        # Fallback to hardcoded ADMIN_IDS
        if ADMIN_IDS:
            _admin_cache = set(ADMIN_IDS)


async def is_admin(client, user_id: int) -> bool:
    """Check if user is a group admin or in ADMIN_IDS."""
    # Hardcoded superadmins always pass
    if ADMIN_IDS and user_id in ADMIN_IDS:
        return True
    # Refresh cache if expired
    if time.time() - _admin_cache_time > ADMIN_CACHE_TTL:
        await refresh_admin_cache(client)
    return user_id in _admin_cache


# ─── Validation ──────────────────────────────────────────────────

def validate_config():
    """Validate required configuration."""
    errors = []
    if not API_ID or API_ID == 0:
        errors.append("TELEGRAM_API_ID is not set")
    if not API_HASH:
        errors.append("TELEGRAM_API_HASH is not set")
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is not set (get from @BotFather)")
    if not SOURCE_GROUP_IDS:
        errors.append("SOURCE_GROUP_IDS is not set")
    if not ALERT_GROUP_ID or ALERT_GROUP_ID == 0:
        errors.append("ALERT_GROUP_ID is not set")

    if errors:
        for err in errors:
            logger.error(f"  - {err}")
        return False
    return True


# ─── Scan Function ───────────────────────────────────────────────

async def scan_messages(bot_client, user_client):
    """Scan Box 1-12 for CEX withdrawal clusters. (/scan)"""
    global last_scan_time

    async with scan_lock:
        now = datetime.now(timezone.utc)
        cluster_since = last_scan_time or (now - timedelta(minutes=SCAN_INTERVAL_MINUTES))
        stats["scans"] = increment_stat("scans")
        log_activity("scan")

        # Custom rules may need a wider fetch window
        max_custom_min = rule_store.get_max_time_window()
        if max_custom_min > 0:
            custom_since = now - timedelta(minutes=max_custom_min)
        else:
            custom_since = cluster_since

        # Fetch = widest window needed
        fetch_since = min(cluster_since, custom_since)

        cluster_local = cluster_since.astimezone(UTC7)
        now_local = now.astimezone(UTC7)
        logger.info(f"🔍 Scan #{stats['scans']}: {cluster_local:%H:%M} -> {now_local:%H:%M} (UTC+7)")
        if fetch_since < cluster_since:
            custom_local = custom_since.astimezone(UTC7)
            logger.info(f"📐 Custom rules fetch: {custom_local:%H:%M} -> {now_local:%H:%M} (UTC+7)")

        clusters_found = 0
        raw_messages = []

        try:
            # Step 1: Collect transfer messages via user_client (bot can't read history)
            # Uses wider window (fetch_since) to cover custom rule needs
            # FloodWait protection: safe_iter_messages handles rate limiting
            for source_gid in SOURCE_GROUP_IDS:
                try:
                    group_msgs = await safe_iter_messages(
                        user_client, source_gid,
                        offset_date=now, since=fetch_since, limit=500,
                    )
                    raw_messages.extend(group_msgs)
                except Exception as e:
                    logger.warning(f"Failed to read group {source_gid}: {e}")

            # Step 2: Sort oldest first (critical for time window)
            raw_messages.sort(key=lambda m: m.date)

            # Step 3: Add transfers and detect in time-based chunks
            # The engine uses a 15-min sliding window. If we add 30 min
            # of transfers then detect once, the first 15 min get cleaned
            # out before detection. Fix: detect every half-window.
            DETECT_INTERVAL_SEC = TIME_WINDOW_MINUTES * 60 / 2  # ~7.5 min
            last_detect_ts = None

            async def _flush_detections():
                """Detect all pending clusters + custom patterns, send alerts."""
                nonlocal clusters_found

                # Loop detect_clusters until exhausted (multiple clusters per CEX)
                while True:
                    clusters = engine.detect_clusters()
                    if not clusters:
                        break
                    for cluster in clusters:
                        clusters_found += 1
                        stats["clusters_detected"] = increment_stat("clusters_detected")
                        log_activity("cluster", cex=cluster.cex, wallets=cluster.wallet_count)
                        alert_text, buttons, _ = format_cluster_alert(cluster)
                        if await safe_send_alert(bot_client, alert_entity, alert_text, buttons=buttons or None):
                            logger.info(
                                f"🚨 Alert sent: {cluster.cex} | "
                                f"{cluster.pattern.label} | {cluster.wallet_count} wallets"
                            )

                # Custom pattern matches
                custom_matches = custom_engine.detect_matches()
                for match in custom_matches:
                    stats["clusters_detected"] = increment_stat("clusters_detected")
                    clusters_found += 1
                    log_activity("pattern_match", rule=match.rule.name, wallets=match.wallet_count)
                    alert_text, buttons, _ = format_custom_pattern_alert(match)
                    if await safe_send_alert(bot_client, alert_entity, alert_text, buttons=buttons or None):
                        logger.info(
                            f"📐 Custom alert sent: {match.rule.name} | "
                            f"{match.wallet_count} wallets"
                        )

            for msg in raw_messages:
                stats["messages_received"] = increment_stat("messages_received")
                timestamp = msg.date.replace(tzinfo=None)
                transfer = parse_message_with_entities(
                    msg.raw_text, msg.entities, timestamp
                )

                if not transfer:
                    continue

                stats["transfers_parsed"] = increment_stat("transfers_parsed")
                log_activity("transfer", cex=transfer.cex, amount=transfer.amount)

                # Auto-learn CEX wallet address
                if transfer.cex_wallet and transfer.cex:
                    from pump_analyzer import register_cex_wallet
                    register_cex_wallet(transfer.cex_wallet, transfer.cex)

                # Custom engine: receives ALL transfers (wider window)
                custom_engine.add_transfer(transfer)
                rule_store.update_cex(transfer.cex)

                # Cluster engine: only receives transfers within cluster window
                if msg.date >= cluster_since:
                    engine.add_transfer(transfer)

                # Whale splitter: queue transfers ≥12 SOL with * in message
                if WHALE_ENABLED and transfer.amount >= 12.0 and '*' in (msg.raw_text or ''):
                    if splitter.add_whale(transfer):
                        stats["whale_queued"] = increment_stat("whale_queued")
                        log_activity("whale_queued", cex=transfer.cex, amount=transfer.amount)

                # Detect periodically to catch clusters before cleanup
                if last_detect_ts is None:
                    last_detect_ts = timestamp
                elif (timestamp - last_detect_ts).total_seconds() >= DETECT_INTERVAL_SEC:
                    await _flush_detections()
                    last_detect_ts = timestamp

            # Save CEX registry after batch
            rule_store.save_cex_registry()

            # Step 4: Final detection flush
            await _flush_detections()

            # Step 5: Whale splitter — check all queued wallets
            if WHALE_ENABLED:
                try:
                    whale_splits = await splitter.check_pending()
                    for split in whale_splits:
                        stats["whale_splits"] = increment_stat("whale_splits")
                        log_activity("whale_split", cex=split.cex, amount=split.deposit_amount, subs=split.sub_wallet_count)
                        alert_text, buttons, _ = format_whale_split_alert(split)
                        try:
                            await bot_client.send_message(
                                whale_alert_entity,
                                alert_text,
                                parse_mode='html',
                                link_preview=False,
                                buttons=buttons or None,
                            )
                            logger.info(
                                f"🐋 Whale alert sent: {split.cex} | "
                                f"{split.deposit_amount:.2f} SOL → "
                                f"{split.sub_wallet_count} sub-wallets"
                            )
                        except Exception as e:
                            logger.error(f"Failed to send whale alert: {e}", exc_info=True)
                except Exception as e:
                    logger.error(f"Whale check error: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Scan error: {e}", exc_info=True)

        last_scan_time = now

        whale_info = f", {stats['whale_queued']} whales queued" if WHALE_ENABLED else ""
        logger.info(f"📊 Scan done: {len(raw_messages)} msgs, {clusters_found} clusters{whale_info}")
        return clusters_found


# ─── Manual Whale Check (/scanbig) ─────────────────────────────

async def manual_whale_check(bot_client):
    """Manual whale check — re-check all queued wallets now."""
    if not WHALE_ENABLED:
        return 0

    splits_found = 0
    try:
        whale_splits = await splitter.check_pending()
        for split in whale_splits:
            splits_found += 1
            stats["whale_splits"] = increment_stat("whale_splits")
            log_activity("whale_split", cex=split.cex, amount=split.deposit_amount, subs=split.sub_wallet_count)
            alert_text, buttons, _ = format_whale_split_alert(split)
            if await safe_send_alert(bot_client, whale_alert_entity, alert_text, buttons=buttons or None):
                logger.info(
                    f"🐋 Whale alert sent: {split.cex} | "
                    f"{split.deposit_amount:.2f} SOL → "
                    f"{split.sub_wallet_count} sub-wallets"
                )

        # Step 2: Scan Group 2 via user_client (bot can't read history)
        # FloodWait protection: safe_iter_messages handles rate limiting
        whale_messages = await safe_iter_messages(
            user_client, WHALE_GROUP_ID,
            offset_date=now, since=scan_since, limit=500,
        )

        whale_messages.sort(key=lambda m: m.date)

        for msg in whale_messages:
            timestamp = msg.date.replace(tzinfo=None)
            transfer = parse_message_with_entities(
                msg.raw_text, msg.entities, timestamp
            )
            if transfer:
                if splitter.add_whale(transfer):
                    whales_queued += 1
                    stats["whale_queued"] = increment_stat("whale_queued")
                    log_activity("whale_queued", cex=transfer.cex, amount=transfer.amount)

        # Step 3: Check queued whales (v2 engine auto-rotates in check_pending)
        new_splits = await splitter.check_pending()
        for split in new_splits:
            splits_found += 1
            stats["whale_splits"] = increment_stat("whale_splits")
            log_activity("whale_split", cex=split.cex, amount=split.deposit_amount, subs=split.sub_wallet_count)

            alert_text, buttons, _ = format_whale_split_alert(split)
            if await safe_send_alert(bot_client, whale_alert_entity, alert_text, buttons=buttons or None):
                logger.info(
                    f"🐋 Whale alert sent: {split.cex} | "
                    f"{split.deposit_amount:.2f} SOL → "
                    f"{split.sub_wallet_count} sub-wallets"
                )

    except Exception as e:
        logger.error(f"Manual whale check error: {e}")

    return splits_found


# ─── First Run: Load 24h Whale History ─────────────────────────

async def load_whale_history(user_client):
    """On first run/restart, scan 24h of messages to populate whale queue."""
    if not WHALE_ENABLED:
        return 0

    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    whales_added = 0
    seen_wallets = set()  # dedup across groups

    logger.info("🐋 Loading 24h whale history from source groups...")

    for source_gid in SOURCE_GROUP_IDS:
        try:
            async for msg in user_client.iter_messages(
                source_gid, offset_date=now, limit=5000
            ):
                if msg.date < since_24h:
                    break
                if not msg.text or not is_transfer_message(msg.text):
                    continue
                if '*' not in (msg.raw_text or ''):
                    continue

                timestamp = msg.date.replace(tzinfo=None)
                transfer = parse_message_with_entities(
                    msg.raw_text, msg.entities, timestamp
                )
                if not transfer or transfer.amount < 12.0:
                    continue

                # Dedup across groups
                if transfer.wallet in seen_wallets:
                    continue
                seen_wallets.add(transfer.wallet)

                if splitter.add_whale(transfer):
                    whales_added += 1

        except Exception as e:
            logger.warning(f"Failed to read group {source_gid} for whale history: {e}")

    logger.info(f"🐋 Whale history loaded: {whales_added} whales from 24h")
    return whales_added


# ─── Periodic Scan Loop ─────────────────────────────────────────

async def periodic_scan_loop(bot_client, user_client):
    """Run cluster + whale scans every SCAN_INTERVAL_MINUTES."""
    await asyncio.sleep(10)

    # First run: load 24h whale history
    try:
        await load_whale_history(user_client)
    except Exception as e:
        logger.error(f"Failed to load whale history: {e}")

    logger.info(f"🚀 First scan starting...")

    while True:
        try:
            await scan_messages(bot_client, user_client)
        except Exception as e:
            logger.error(f"Periodic scan error: {e}")

        # Clean up expired pagination cache entries
        cleanup_alert_cache()

        logger.info(f"💤 Next scan in {SCAN_INTERVAL_MINUTES} min...")
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)


# ─── Main ────────────────────────────────────────────────────────

async def main():
    if not validate_config():
        logger.error("Fix .env and try again.")
        return

    # user_client: reads message history from source groups
    # bot_client: sends alerts + handles commands
    user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    bot_client = TelegramClient("bot_session", API_ID, API_HASH)

    # ─── Pagination Callback Handler ─────────────────────────

    @bot_client.on(events.CallbackQuery(pattern=rb'^pg:'))
    async def pagination_handler(event):
        """Handle pagination button presses on alert messages."""
        try:
            data = event.data.decode('utf-8')
            parts = data.split(':')
            if len(parts) != 3:
                return
            _, alert_id, page_str = parts
            page = int(page_str)

            result = format_alert_page(alert_id, page)
            if result is None:
                await event.answer("Alert expired", alert=True)
                return

            text, buttons = result
            await event.edit(text, parse_mode='html', link_preview=False, buttons=buttons or None)
        except Exception as e:
            logger.error(f"Pagination callback error: {e}")
            await event.answer("Error loading page", alert=True)

    @bot_client.on(events.CallbackQuery(pattern=rb'^tr:'))
    async def trace_handler(event):
        """Handle trace button - trace forward from source wallet."""
        try:
            data = event.data.decode('utf-8')
            parts = data.split(':', 1)
            if len(parts) != 2 or not parts[1]:
                return
            source = parts[1]
            msg_id = event.message_id

            await event.answer()
            status_msg = await event.respond(
                f"⏳ Đang trace <code>{source[:4]}...{source[-4:]}</code> ...",
                parse_mode='html', reply_to=msg_id,
            )

            from trace_bfs import trace_forward
            all_wallets = await trace_forward(helius_client, source)

            if len(all_wallets) <= 1:
                await status_msg.edit("Không tìm thấy ví đích.")
            else:
                wallet_block = "\n".join(all_wallets)
                await status_msg.edit(
                    f"<pre>{wallet_block}</pre>",
                    parse_mode='html',
                    link_preview=False,
                )
        except Exception as e:
            logger.error(f"Trace callback error: {e}", exc_info=True)
            try:
                await event.respond(f"❌ Trace error: {e}")
            except Exception:
                pass

    @bot_client.on(events.CallbackQuery(pattern=rb'^noop$'))
    async def noop_handler(event):
        """Ignore clicks on the page counter button."""
        await event.answer()

    # ─── Bot Command Handlers ────────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/start(?:@\w+)?$'))
    async def start_handler(event):
        await event.respond(
            "🔍 <b>Degen Scanner</b> is running!\n\n"
            "I scan CEX withdrawals and detect coordinated wallet clusters.\n\n"
            "Commands:\n"
            "/scan - Scan clusters\n"
            "/scanbig - Scan whale splitters\n"
            "/status - Bot stats\n"
            "/keystats - Check API key credits\n"
            "/help - Help",
            parse_mode='html',
        )

    @bot_client.on(events.NewMessage(pattern=r'^/scan(?:@\w+)?$'))
    async def scan_handler(event):
        if scan_lock.locked():
            await event.respond("⏳ Scan already in progress...")
            return
        await event.respond("🔍 Scanning now...")
        clusters = await scan_messages(bot_client, user_client)
        await event.respond(
            f"✅ Scan complete! Found <b>{clusters}</b> cluster(s).",
            parse_mode='html',
        )

    @bot_client.on(events.NewMessage(pattern=r'^/scanbig(?:@\w+)?$'))
    async def scanbig_handler(event):
        if not WHALE_ENABLED:
            await event.respond(
                "🐋 Whale splitter is <b>disabled</b>.\n"
                "Set HELIUS_API_KEYS in .env to enable.",
                parse_mode='html',
            )
            return
        if scan_lock.locked():
            await event.respond("⏳ Scan already in progress...")
            return

        sp_stats = splitter.get_stats()
        await event.respond(
            f"🐋 Checking {sp_stats['queue_size']} whale wallet(s)..."
        )
        async with scan_lock:
            splits_found = await manual_whale_check(bot_client)
        await event.respond(
            f"✅ Whale check done! "
            f"<b>{splits_found}</b> split(s) detected.\n"
            f"Queue: {splitter.get_stats()['queue_size']} wallets remaining.",
            parse_mode='html',
        )

    # ─── Whale Queue/Stats/Management Commands ────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/whalequeue(?:@\w+)?$'))
    async def whalequeue_handler(event):
        if not WHALE_ENABLED:
            await event.respond("🐋 Whale splitter is <b>disabled</b>.", parse_mode='html')
            return

        queue = splitter.get_queue()
        if not queue:
            await event.respond("🐋 Whale queue is empty.")
            return

        now = datetime.utcnow()
        lines = []
        for i, qw in enumerate(queue[:20], 1):
            age = now - qw.added_at
            if age.days > 0:
                age_str = f"{age.days}d"
            else:
                hours = int(age.total_seconds() // 3600)
                age_str = f"{hours}h" if hours > 0 else f"{int(age.total_seconds() // 60)}m"
            lines.append(
                f"{i}. {_wc(qw.wallet)} - "
                f"<b>{qw.amount:.1f}</b> SOL ({qw.cex}) - {age_str}"
            )

        text = "🐋 <b>Whale Queue</b>\n\n" + "\n".join(lines)
        if len(queue) > 20:
            text += f"\n\n<i>... and {len(queue) - 20} more</i>"
        text += f"\n\nTotal: <b>{len(queue)}</b> wallets"

        await event.respond(text, parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/whalestats(?:@\w+)?$'))
    async def whalestats_handler(event):
        if not WHALE_ENABLED:
            await event.respond("🐋 Whale splitter is <b>disabled</b>.", parse_mode='html')
            return

        sp = splitter.get_stats()
        h = sp["helius"]

        text = (
            "🐋 <b>Whale Splitter Stats</b>\n\n"
            f"📋 Queue size: <b>{sp['queue_size']}</b>\n"
            f"📥 Total queued: {sp['total_queued']}\n"
            f"🔍 Total checked: {sp['total_checked']}\n"
            f"🚨 Splits detected: {sp['total_splits']}\n"
            f"💱 Removed (swap): {sp['total_removed_swap']}\n"
            f"🔔 Alerted wallets: {sp['alerted_wallets']}\n\n"
            f"🔑 <b>Helius API</b>\n"
            f"Keys: {h['total_keys']}\n"
            f"Active key: #{h['active_key']}\n"
            f"Daily calls: {h['daily_calls']}\n\n"
            f"⚙️ MIN_WHALE_SOL: <b>{splitter.get_min_sol()}</b>"
        )

        await event.respond(text, parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/whalecheck(?:@\w+)?\s'))
    async def whalecheck_handler(event):
        if not WHALE_ENABLED:
            await event.respond("🐋 Whale splitter is <b>disabled</b>.", parse_mode='html')
            return

        parts = event.raw_text.strip().split()
        if len(parts) < 2:
            await event.respond("Usage: <code>/whalecheck &lt;wallet&gt;</code>", parse_mode='html')
            return

        wallet = parts[1].strip()
        if len(wallet) < 32:
            await event.respond("❌ Invalid wallet address.")
            return

        await event.respond(f"🔍 Checking {_wc(wallet)}", parse_mode='html')

        try:
            activity = await helius_client.check_wallet_activity(wallet)

            if activity == "split":
                outgoing = await helius_client.get_outgoing_sol_transfers(wallet)
                if outgoing:
                    lines = []
                    for t in outgoing[:10]:
                        lines.append(
                            f"  → {_wc(t.to_wallet)} "
                            f"<b>{t.amount_sol:.2f}</b> SOL"
                        )
                    text = (
                        f"🚨 <b>SPLIT detected</b> for {_wc(wallet)}\n\n"
                        + "\n".join(lines)
                    )
                    if len(outgoing) > 10:
                        text += f"\n  <i>... +{len(outgoing) - 10} more</i>"
                else:
                    text = f"🚨 Split detected but no transfer details available."
            elif activity == "swap":
                text = f"💱 Wallet {_wc(wallet)} has <b>swap/DEX</b> activity."
            elif activity == "idle":
                text = f"😴 Wallet {_wc(wallet)} is <b>idle</b> (no activity)."
            elif activity == "wrap":
                text = f"🔄 Wallet {_wc(wallet)} only <b>wrapped SOL</b>."
            else:
                text = f"⚠️ Error checking wallet. Try again later."

            await event.respond(text, parse_mode='html')
        except Exception as e:
            logger.error(f"Whalecheck error: {e}")
            await event.respond(f"❌ Error: {e}")

    @bot_client.on(events.NewMessage(pattern=r'^/whalethreshold(?:@\w+)?'))
    async def whalethreshold_handler(event):
        parts = event.raw_text.strip().split()
        if len(parts) < 2:
            await event.respond(
                f"⚙️ <b>MIN_WHALE_SOL</b>: <code>{splitter.get_min_sol()}</code>\n\n"
                f"Usage: <code>/whalethreshold 15</code>",
                parse_mode='html',
            )
            return

        try:
            value = float(parts[1])
            if not (1.0 <= value <= 1000.0):
                await event.respond("❌ Range: 1.0 - 1000.0")
                return

            old_val = splitter.get_min_sol()
            splitter.set_min_sol(value)

            # Persist to .env
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            try:
                if os.path.exists(env_path):
                    with open(env_path, "r") as f:
                        lines = f.readlines()
                    found = False
                    for i, line in enumerate(lines):
                        if line.startswith("MIN_WHALE_SOL="):
                            lines[i] = f"MIN_WHALE_SOL={value}\n"
                            found = True
                            break
                    if not found:
                        lines.append(f"MIN_WHALE_SOL={value}\n")
                    with open(env_path, "w") as f:
                        f.writelines(lines)
                else:
                    with open(env_path, "a") as f:
                        f.write(f"MIN_WHALE_SOL={value}\n")
            except Exception as e:
                logger.warning(f"Failed to persist MIN_WHALE_SOL to .env: {e}")

            await event.respond(
                f"✅ MIN_WHALE_SOL: <code>{old_val}</code> → <code>{value}</code>",
                parse_mode='html',
            )
        except ValueError:
            await event.respond("❌ Invalid number.")

    @bot_client.on(events.NewMessage(pattern=r'^/whaleremove(?:@\w+)?\s'))
    async def whaleremove_handler(event):
        parts = event.raw_text.strip().split()
        if len(parts) < 2:
            await event.respond("Usage: <code>/whaleremove &lt;wallet_or_prefix&gt;</code>", parse_mode='html')
            return

        wallet = parts[1].strip()
        if splitter.remove_wallet(wallet):
            await event.respond(
                f"✅ Removed wallet matching {_wc(wallet)} from queue.",
                parse_mode='html',
            )
        else:
            await event.respond(f"❌ No wallet matching {_wc(wallet)} in queue.", parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/whaleclear(?:@\w+)?'))
    async def whaleclear_handler(event):
        parts = event.raw_text.strip().split()
        mode = parts[1].strip().lower() if len(parts) > 1 else ""

        if mode == "stale":
            removed = splitter.clear_queue(max_age_days=7)
            await event.respond(
                f"🗑️ Purged <b>{removed}</b> stale whale(s) (>7 days).",
                parse_mode='html',
            )
        elif mode == "all":
            removed = splitter.clear_queue(max_age_days=None)
            await event.respond(
                f"🗑️ Cleared <b>{removed}</b> whale(s) from queue.",
                parse_mode='html',
            )
        else:
            await event.respond(
                "Usage:\n"
                "<code>/whaleclear stale</code> - Remove wallets >7 days\n"
                "<code>/whaleclear all</code> - Clear entire queue",
                parse_mode='html',
            )

    # ─── Summary Command ─────────────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/summary(?:@\w+)?'))
    async def summary_handler(event):
        parts = event.raw_text.strip().split()
        # Default 24h
        hours = 24
        if len(parts) >= 2:
            raw = parts[1].strip().lower()
            try:
                if raw.endswith("w"):
                    hours = int(raw.rstrip("w")) * 24 * 7
                elif raw.endswith("d"):
                    hours = int(raw.rstrip("d")) * 24
                else:
                    hours = int(raw.rstrip("h"))
                hours = max(1, min(hours, 168))
            except ValueError:
                await event.respond(
                    "Usage: <code>/summary [time]</code>\n"
                    "Ex: /summary 3h, /summary 72h, /summary 1d, /summary 1w",
                    parse_mode='html',
                )
                return

        filtered = query_activity(hours)

        scans = sum(1 for e in filtered if e["type"] == "scan")
        transfers = sum(1 for e in filtered if e["type"] == "transfer")
        clusters = sum(1 for e in filtered if e["type"] == "cluster")
        pattern_matches = sum(1 for e in filtered if e["type"] == "pattern_match")
        whales_queued = sum(1 for e in filtered if e["type"] == "whale_queued")
        whale_splits = sum(1 for e in filtered if e["type"] == "whale_split")

        # Top CEXes by transfer count
        cex_counts: dict[str, int] = {}
        cex_volume: dict[str, float] = {}
        for e in filtered:
            if e["type"] == "transfer":
                cex = e.get("cex", "?")
                cex_counts[cex] = cex_counts.get(cex, 0) + 1
                cex_volume[cex] = cex_volume.get(cex, 0) + e.get("amount", 0)

        top_cex = sorted(cex_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        total_volume = sum(cex_volume.values())

        if hours >= 24 and hours % 24 == 0:
            period_label = f"{hours // 24}d"
        else:
            period_label = f"{hours}h"

        text = (
            f"📊 <b>Summary — last {period_label}</b>\n\n"
            f"🔄 Scans: <b>{scans}</b>\n"
            f"🔀 Transfers: <b>{transfers}</b>\n"
            f"💰 Volume: <b>{total_volume:,.1f}</b> SOL\n"
            f"🚨 Clusters: <b>{clusters}</b>\n"
        )

        if pattern_matches:
            text += f"📐 Pattern matches: <b>{pattern_matches}</b>\n"

        if WHALE_ENABLED:
            text += (
                f"\n🐋 <b>Whale</b>\n"
                f"Queued: <b>{whales_queued}</b>\n"
                f"Splits: <b>{whale_splits}</b>\n"
            )

        if top_cex:
            text += "\n📈 <b>Top CEX</b>\n"
            for cex, count in top_cex:
                vol = cex_volume.get(cex, 0)
                text += f"  {cex}: {count} txs ({vol:,.1f} SOL)\n"

        if not filtered:
            text += "\n<i>No activity recorded yet.</i>"

        await event.respond(text, parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/status(?:@\w+)?$'))
    async def status_handler(event):
        uptime = ""
        if stats["started_at"]:
            delta = datetime.now(timezone.utc) - stats["started_at"]
            hours = int(delta.total_seconds() // 3600)
            mins = int((delta.total_seconds() % 3600) // 60)
            uptime = f"{hours}h {mins}m"

        engine_stats = engine.get_stats()

        text = (
            "📊 <b>Degen Scanner Status</b>\n\n"
            f"⏱ Uptime: {uptime}\n"
            f"🔄 Scans: {stats['scans']}\n"
            f"📨 Messages: {stats['messages_received']}\n"
            f"🔀 Transfers: {stats['transfers_parsed']}\n"
            f"🚨 Clusters: {stats['clusters_detected']}\n\n"
            f"⚙️ <b>Settings</b>\n"
            f"⏰ Window: {TIME_WINDOW_MINUTES} min\n"
            f"👛 Min wallets: {MIN_CLUSTER_SIZE}\n"
            f"🔄 Scan every: {SCAN_INTERVAL_MINUTES} min\n\n"
            f"🔑 <b>API Keys</b>\n"
            f"Helius: {len(HELIUS_API_KEYS)} keys\n"
            f"Moralis: {len(MORALIS_API_KEYS)} keys"
        )

        # Key health (show cached if available)
        hh = helius_client._health_cache
        mh = token_data_client._health_cache
        if hh:
            text += (
                f"\n\n🔋 <b>Key Credits</b>\n"
                f"Helius: {hh['active']} active / {hh['no_credits']} no credits"
            )
        if mh:
            text += f"\nMoralis: {mh['active']} active / {mh['no_credits']} no credits"
        if hh or mh:
            text += "\n<i>(cached — /keystats to refresh)</i>"

        # Teach stats
        try:
            import teach_store
            ts = teach_store.get_teach_stats()
            text += (
                f"\n\n🎓 <b>Teach System</b>\n"
                f"Cases: {ts.get('total_cases', 0)}\n"
                f"Patterns: {ts.get('total_patterns', 0)}\n"
                f"Known wallets: {ts.get('total_wallets', 0)}"
            )
        except Exception:
            pass

        # Cluster Engine (CEX)
        text += (
            f"\n\n🧠 <b>Cluster Engine</b>\n"
            f"Active CEXes: {engine_stats['active_cex_count']}\n"
            f"In windows: {engine_stats['total_in_windows']}\n"
            f"Alerted: {engine_stats['alerted_wallets']}"
        )

        # Custom pattern stats
        cp_stats = custom_engine.get_stats()
        if cp_stats["rules_total"] > 0:
            text += (
                f"\n\n📐 <b>Custom Patterns</b>\n"
                f"Rules: {cp_stats['rules_enabled']}/{cp_stats['rules_total']} enabled\n"
                f"Buffered: {cp_stats['total_buffered']}\n"
                f"Matched: {cp_stats['alerted_count']}"
            )

        if WHALE_ENABLED:
            sp_stats = splitter.get_stats()
            text += (
                f"\n\n🐋 <b>Whale Splitter</b>\n"
                f"Queue: {sp_stats['queue_size']} wallets\n"
                f"Splits found: {stats['whale_splits']}\n"
                f"Removed (swap): {sp_stats['total_removed_swap']}\n"
                f"Helius: {sp_stats['helius']['daily_calls']} calls "
                f"| key {sp_stats['helius']['active_key']}/{sp_stats['helius']['total_keys']}"
            )
        else:
            text += "\n\n🐋 Whale Splitter: <i>disabled</i>"

        await event.respond(text, parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/keystats(?:@\w+)?$'))
    async def keystats_handler(event):
        """Check API key credit health (Helius + Moralis)."""
        msg = await event.respond(
            "🔍 Checking API key credits...\n"
            f"Helius: {len(HELIUS_API_KEYS)} keys\n"
            f"Moralis: {len(MORALIS_API_KEYS)} keys\n"
            "<i>This may take 20-30 seconds on first run...</i>",
            parse_mode='html',
        )

        hh, mh = await asyncio.gather(
            helius_client.check_keys_health(force=True),
            token_data_client.check_keys_health(force=True),
        )

        text = (
            "🔋 <b>API Key Credits</b>\n\n"
            f"<b>Helius</b> ({hh['total']} keys)\n"
            f"  ✅ Active: {hh['active']}\n"
            f"  💀 No credits: {hh['no_credits']}\n"
        )
        if hh['errors']:
            text += f"  ❓ Errors: {hh['errors']}\n"

        text += (
            f"\n<b>Moralis</b> ({mh['total']} keys)\n"
            f"  ✅ Active: {mh['active']}\n"
            f"  💀 No credits: {mh['no_credits']}\n"
        )
        if mh['errors']:
            text += f"  ❓ Errors: {mh['errors']}\n"

        text += "\n<i>Cached for 1 hour. /keystats to recheck.</i>"

        await bot_client.edit_message(msg, text, parse_mode='html')

    # ─── Pump Analyzer Command ───────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/analyze(?:@\w+)?\s'))
    async def analyze_handler(event):
        """Analyze a Pump.fun token: find pre-bond buyers, trace CEX funding, cluster."""
        if not WHALE_ENABLED:
            await event.respond(
                "❌ Helius API keys required for /analyze.",
                parse_mode='html',
            )
            return

        parts = event.raw_text.strip().split()
        if len(parts) < 2:
            await event.respond(
                "Usage: <code>/analyze &lt;token_mint&gt;</code>",
                parse_mode='html',
            )
            return

        mint = parts[1].strip()
        if len(mint) < 32:
            await event.respond("❌ Invalid token mint address.")
            return

        progress = await event.reply(
            f"🔬 Analyzing {_wc(mint)}\n"
            f"⏳ Fetching pre-bond buyers...",
            parse_mode='html',
        )

        try:
            result = await pump_analyzer.analyze(mint)

            # Update progress
            try:
                await progress.edit(
                    f"🔬 Analyzing {_wc(mint)}\n"
                    f"✅ Done! {result.total_buys} buys, "
                    f"{result.unique_buyers} unique buyers",
                    parse_mode='html',
                )
            except Exception:
                pass

            # Send formatted results — reply to original command
            messages = format_pump_analysis(result)
            target = pump_alert_entity or event.chat
            for i, msg in enumerate(messages):
                reply_id = event.id if i == 0 else None
                await bot_client.send_message(
                    target, msg,
                    parse_mode='html',
                    link_preview=False,
                    reply_to=reply_id,
                )

        except Exception as e:
            logger.error(f"Analyze error: {e}", exc_info=True)
            await event.reply(f"❌ Error: {e}")

    # ─── Dev Wallet Tracer Command ────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/(?:trace|devtrace)(?:@\w+)?\s'))
    async def trace_handler(event):
        """Trace dev wallets for a Pump.fun token: funding, profit, clustering."""
        if not WHALE_ENABLED:
            await event.respond(
                "❌ Helius API keys required for /devtrace.",
                parse_mode='html',
            )
            return

        parts = event.raw_text.strip().split()
        if len(parts) < 2:
            await event.respond(
                "Usage: <code>/devtrace &lt;token_mint&gt;</code>",
                parse_mode='html',
            )
            return

        mint = parts[1].strip()
        if len(mint) < 32:
            await event.respond("❌ Invalid token mint address.")
            return

        progress = await event.reply(
            f"🕵️ Tracing dev wallets for {_wc(mint)}\n"
            f"⏳ Fetching bonding curve buyers...",
            parse_mode='html',
        )

        try:
            result = await dev_tracer.trace(mint)

            # Update progress
            try:
                buyers_count = len(result.all_buyers)
                clusters_count = len(result.clusters)
                await progress.edit(
                    f"🕵️ Tracing {_wc(mint)}\n"
                    f"✅ Done! {buyers_count} buys, "
                    f"{clusters_count} clusters found",
                    parse_mode='html',
                )
            except Exception:
                pass

            # Send formatted results — reply to original command
            if result.clusters:
                messages = format_trace_result(result)
                target = pump_alert_entity or event.chat
                for i, msg in enumerate(messages):
                    reply_id = event.id if i == 0 else None
                    await bot_client.send_message(
                        target, msg,
                        parse_mode='html',
                        link_preview=False,
                        reply_to=reply_id,
                    )
            else:
                await event.reply("✅ No dev clusters detected")

        except Exception as e:
            logger.error(f"Trace error: {e}", exc_info=True)
            await event.respond(f"❌ Error: {e}")

    # ─── Trace Forward / Backward ───────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/trace1(?:@\w+)?\s'))
    async def trace1_handler(event):
        """Trace forward — BFS: follow ALL outgoing SOL transfers, up to 5 hops."""
        _addr_re = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
        addrs = _addr_re.findall(event.raw_text)
        wallet = None
        for a in addrs:
            if len(a) >= 32 and a not in ("trace1",):
                wallet = a
                break
        if not wallet:
            await event.respond("Usage: <code>/trace1 &lt;wallet&gt;</code>", parse_mode='html')
            return
        # Extract label: text between /trace1 and the wallet address
        _rest = event.raw_text.split(maxsplit=1)[1] if len(event.raw_text.split(maxsplit=1)) > 1 else ""
        label = _rest.replace(wallet, "").strip().strip("\n").strip()
        await event.reply(f"➡️ Đuổi tiến {_wc(wallet)}", parse_mode='html')

        try:
            from trace_bfs import trace_forward
            all_wallets = await trace_forward(helius_client, wallet)

            if len(all_wallets) <= 1:
                await event.reply("Không tìm thấy ví đích.", parse_mode='html')
            elif label:
                add_block = "\n".join(f"/add {w} {label} {idx}" for idx, w in enumerate(all_wallets, 1))
                await event.reply(f"<pre>{add_block}</pre>", parse_mode='html', link_preview=False)
                wallet_block = "\n".join(all_wallets)
                await event.respond(f"<pre>{wallet_block}</pre>", parse_mode='html', link_preview=False)
            else:
                wallet_block = "\n".join(all_wallets)
                await event.reply(f"<pre>{wallet_block}</pre>", parse_mode='html', link_preview=False)
        except Exception as e:
            logger.error(f"/trace1 error: {e}", exc_info=True)
            await event.reply(f"❌ Trace forward failed: {e}")

    @bot_client.on(events.NewMessage(pattern=r'^/trace2(?:@\w+)?\s'))
    async def trace2_handler(event):
        """Trace backward — where does money come FROM."""
        _addr_re = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
        addrs = _addr_re.findall(event.raw_text)
        wallet = None
        for a in addrs:
            if len(a) >= 32 and a not in ("trace2",):
                wallet = a
                break
        if not wallet:
            await event.respond("Usage: <code>/trace2 &lt;wallet&gt;</code>", parse_mode='html')
            return
        await event.reply(f"⬅️ Đuổi lùi {_wc(wallet)}", parse_mode='html')

        try:
            from pump_analyzer import lookup_cex_name
            chain, current, visited = [], wallet, set()
            for hop in range(5):
                if current in visited:
                    break
                visited.add(current)
                key = helius_client._keys[hop % len(helius_client._keys)] if helius_client._keys else ""
                if not key:
                    break
                funding = await helius_client.get_wallet_incoming_sol(wallet=current, key=key, lookback_hours=168)
                if not funding:
                    break
                cex = lookup_cex_name(funding["from_wallet"])
                chain.append({"from": funding["from_wallet"], "to": current, "amount": funding["amount_sol"], "cex": cex})
                if cex:
                    break
                current = funding["from_wallet"]

            text = f"⬅️ <b>Trace Backward (nguồn tiền)</b>\n\n"
            text += f"Target: <code>{wallet}</code>\n"
            text += f"<a href='https://solscan.io/account/{wallet}'>Solscan</a>\n\n"

            if not chain:
                text += "No incoming SOL transfers found (7 day lookback)."
            else:
                text += f"<b>Trace ({len(chain)} hops):</b>\n\n"
                for i, hop in enumerate(chain, 1):
                    if hop["cex"]:
                        source = f"🏦 <b>{hop['cex']}</b>"
                    else:
                        source = _wl(hop['from'])
                    text += (
                        f"  {i}. {source}\n"
                        f"     → {_wl(hop['to'])}\n"
                        f"     💰 {hop['amount']:.3f} SOL\n"
                    )

                final = chain[-1]
                if final["cex"]:
                    text += f"✅ Origin: <b>{final['cex']}</b>"
                else:
                    text += f"❓ Origin: Unknown ({len(chain)} hops)"

            target = pump_alert_entity or event.chat
            await bot_client.send_message(target, text, parse_mode='html', link_preview=False, reply_to=event.id)
        except Exception as e:
            logger.error(f"/trace2 error: {e}", exc_info=True)
            await event.respond(f"❌ Trace backward failed: {e}")

    # ─── Backfill CEX wallets ─────────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/backfill(?:@\w+)?(?:\s|$)'))
    async def backfill_handler(event):
        """Backfill CEX wallets from source group history."""
        parts = event.raw_text.strip().split()
        limit = int(parts[1]) if len(parts) > 1 else 5000

        from db import load_cex_wallets, save_cex_wallet
        before = load_cex_wallets()
        await event.reply(
            f"CEX wallets before: {len(before)}\n"
            f"Scanning {limit} messages from {len(SOURCE_GROUP_IDS)} groups...",
        )

        total_parsed = 0
        cex_found = {}

        for group_id in SOURCE_GROUP_IDS:
            count = 0
            try:
                async for msg in user_client.iter_messages(group_id, limit=limit):
                    if not msg.raw_text or not is_transfer_message(msg.raw_text):
                        continue
                    transfer = parse_message_with_entities(
                        msg.raw_text, msg.entities or [], msg.date,
                    )
                    if not transfer or not transfer.cex_wallet or not transfer.cex:
                        continue
                    total_parsed += 1
                    addr = transfer.cex_wallet
                    name = transfer.cex
                    if addr not in cex_found:
                        cex_found[addr] = name
                        save_cex_wallet(addr, name)
                    count += 1
            except Exception as e:
                logger.error(f"Backfill group {group_id}: {e}")

        # Reload dynamic CEX cache
        load_dynamic_cex()
        after = load_cex_wallets()
        new_count = len(after) - len(before)

        exchange_counts = {}
        for name in after.values():
            exchange_counts[name] = exchange_counts.get(name, 0) + 1
        breakdown = "\n".join(
            f"  {name}: {c}" for name, c in sorted(exchange_counts.items(), key=lambda x: -x[1])
        )

        await event.respond(
            f"Backfill done!\n\n"
            f"Transfers parsed: {total_parsed}\n"
            f"Unique CEX wallets: {len(cex_found)}\n"
            f"New wallets added: {new_count}\n"
            f"Total in DB: {len(after)}\n\n"
            f"By exchange:\n{breakdown}",
        )

    @bot_client.on(events.NewMessage(pattern=r'^/cexlist(?:@\w+)?$'))
    async def cexlist_handler(event):
        """Show learned CEX wallets."""
        from db import load_cex_wallets
        cex = load_cex_wallets()
        if not cex:
            await event.respond("CEX wallet DB empty. Run /backfill first.")
            return
        exchange_counts = {}
        for name in cex.values():
            exchange_counts[name] = exchange_counts.get(name, 0) + 1
        breakdown = "\n".join(
            f"  {name}: {c}" for name, c in sorted(exchange_counts.items(), key=lambda x: -x[1])
        )
        await event.respond(f"CEX wallets in DB: {len(cex)}\n\n{breakdown}")

    @bot_client.on(events.NewMessage(pattern=r'^/reloadcex(?:@\w+)?$'))
    async def reloadcex_handler(event):
        """Reload CEX wallet cache from DB."""
        load_dynamic_cex()
        from db import load_cex_wallets
        cex = load_cex_wallets()
        await event.respond(f"Reloaded {len(cex)} CEX wallets.")

    # ─── Dev Scanner Commands (ATH autoscan) ──────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/devon(?:@\w+)?$'))
    async def devon_handler(event):
        """Enable ATH autoscan."""
        scanner_state.autoscan_enabled = True
        await event.respond("✅ Dev autoscan <b>ON</b>", parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/devoff(?:@\w+)?$'))
    async def devoff_handler(event):
        """Disable ATH autoscan."""
        scanner_state.autoscan_enabled = False
        await event.respond("⏸ Dev autoscan <b>OFF</b>", parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/devscan(?:@\w+)?$'))
    async def devscan_handler(event):
        """Trigger immediate ATH scan."""
        if scanner_state.scan_running:
            await event.respond("⚠️ Scan đang chạy rồi, đợi xong nhé.")
            return
        await event.respond("🚀 Bắt đầu ATH scan...")
        asyncio.create_task(scan_full(
            token_data_client, dev_sender, scanner_state, helius_client,
            dev_sender=dev_sender,
        ))

    @bot_client.on(events.NewMessage(pattern=r'^/setath(?:@\w+)?\s'))
    async def setath_handler(event):
        """Set ATH threshold."""
        parts = event.raw_text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await event.respond(
                f"ATH threshold: <b>${scanner_state.min_ath:,.0f}</b>\n"
                f"VD: <code>/setath 50000</code>",
                parse_mode='html',
            )
            return
        try:
            new_ath = float(parts[1].strip().replace(",", "").replace("$", ""))
            scanner_state.min_ath = new_ath
            for ts in scanner_state._tokens.values():
                ts.ath_checked = False
            await event.respond(
                f"✅ ATH threshold: <b>${scanner_state.min_ath:,.0f}</b>\n"
                f"Tất cả token sẽ check lại lần scan sau.",
                parse_mode='html',
            )
        except ValueError:
            await event.respond("❌ VD: <code>/setath 50000</code>", parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/devstatus(?:@\w+)?$'))
    async def devstatus_handler(event):
        """Show dev scanner status."""
        total = scanner_state.total_tokens
        analyzed = scanner_state.analyzed_count
        alerted = scanner_state.alerted_count
        ath_checked = sum(1 for t in scanner_state._tokens.values() if t.ath_checked)
        qualified = sum(1 for t in scanner_state._tokens.values()
                       if t.ath_market_cap >= scanner_state.min_ath)
        await event.respond(
            f"🕵️ <b>Dev Scanner Status</b>\n\n"
            f"Auto scan: {'✅ ON' if scanner_state.autoscan_enabled else '⏸ OFF'}\n"
            f"Scan running: {'🔄 Yes' if scanner_state.scan_running else '⏹ No'}\n"
            f"ATH threshold: ${scanner_state.min_ath:,.0f}\n\n"
            f"Tracked: {total:,}\n"
            f"ATH checked: {ath_checked:,}\n"
            f"ATH ≥ ${scanner_state.min_ath:,.0f}: {qualified}\n"
            f"DevTraced: {analyzed}\n"
            f"Alerted: {alerted}\n\n"
            f"Moralis keys: {len(MORALIS_API_KEYS)}\n"
            f"Helius keys: {len(HELIUS_API_KEYS)}",
            parse_mode='html',
        )

    # ─── Teach Commands ──────────────────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/teach(?:@\w+)?(?:\s|$)'))
    async def teach_handler(event):
        """Teach bot about dev wallets for a token."""
        import html as html_mod
        from teach_pipeline import TeachingPipeline
        import teach_store

        parts = event.raw_text.strip().split(maxsplit=2)
        if len(parts) < 3:
            # Start wizard mode
            uid = event.sender_id
            teach_wizard_states[uid] = {
                "step": "mint",
                "expires_at": time.time() + WIZARD_TIMEOUT,
            }
            await event.reply(
                "🎓 <b>Teach Wizard</b>\n\n"
                "Bước 1/2 — Gửi <b>mint address</b> của token:",
                parse_mode='html',
                buttons=Button.force_reply(),
            )
            raise StopPropagation

        mint = parts[1].strip()
        wallets_raw = parts[2].strip()

        if len(mint) < 32:
            await event.respond("❌ Invalid mint address.")
            return

        taught_wallets = [
            w.strip() for w in wallets_raw.split(",") if w.strip()
        ]
        if not taught_wallets:
            await event.respond("❌ Cần ít nhất 1 ví dev.")
            return

        progress_msg = await event.reply(
            f"🎓 Bắt đầu học {_wc(mint)} "
            f"({len(taught_wallets)} ví dev)\n"
            f"⏳ Starting...",
            parse_mode='html',
        )

        pipeline = TeachingPipeline(helius=helius_client)

        async def on_progress(step: int, msg: str):
            if step in (0, 1, 7, 8, 9, 10):
                try:
                    await progress_msg.edit(
                        f"🎓 Đang học {_wc(mint)}\n"
                        f"⏳ Step {step}: {msg}",
                        parse_mode='html',
                    )
                except Exception:
                    pass

        result = await pipeline.teach(
            mint=mint,
            taught_wallets=taught_wallets,
            on_progress=on_progress,
        )

        if result.error:
            await event.respond(f"❌ Teach failed: {result.error}")
            return

        name = html_mod.escape(result.token_name or "Unknown")
        symbol = html_mod.escape(result.token_symbol or "???")
        pattern_types = set(p["pattern_type"] for p in result.patterns)
        pattern_str = ", ".join(sorted(pattern_types)) if pattern_types else "none"

        text = (
            f"✅ <b>TEACH RESULT — {name} (${symbol})</b>\n\n"
            f"📚 Ví cung cấp: <b>{len(result.taught_wallets)}</b>  |  "
            f"🔍 Phát hiện thêm: <b>{len(result.discovered_wallets)}</b>\n"
            f"💰 Funders: <b>{len(result.funders)}</b>  |  "
            f"📥 Collectors: <b>{len(result.collectors)}</b>\n"
            f"🔑 Patterns: {pattern_str}\n"
            f"Case ID: <b>#{result.case_id}</b>"
        )

        cid2 = result.case_id
        target = pump_alert_entity or event.chat
        await safe_send_alert(
            bot_client, target, text,
            buttons=[
                [
                    Button.inline("📋 Wallets", f"teach_view:{cid2}:wallets".encode()),
                    Button.inline("💰 Funders", f"teach_view:{cid2}:funders".encode()),
                    Button.inline("📥 Collectors", f"teach_view:{cid2}:collectors".encode()),
                ],
                [
                    Button.inline("✅ Hữu ích", f"teach_fb:{cid2}:good".encode()),
                    Button.inline("❌ Không chính xác", f"teach_fb:{cid2}:bad".encode()),
                ],
            ],
        )

    @bot_client.on(events.CallbackQuery(pattern=rb'^teach_view:'))
    async def teach_view_handler(event):
        """Handle teach view buttons (wallets/funders/collectors)."""
        import teach_store

        try:
            data = event.data.decode('utf-8')
            parts = data.split(":")
            if len(parts) != 3:
                return
            case_id = int(parts[1])
            view_type = parts[2]

            case = teach_store.get_case(case_id)
            if not case:
                await event.answer(f"Case #{case_id} not found", alert=True)
                return

            name = case.token_name or case.token_symbol or _w(case.mint)
            chat = await event.get_chat()

            if view_type == "wallets":
                all_w = list(dict.fromkeys(case.taught_wallets + case.discovered_wallets))
                if not all_w:
                    await event.answer("Không có wallets", alert=True)
                    return
                header = (
                    f"📋 <b>Wallets — {name}</b> (case #{case_id})\n"
                    f"Taught: {len(case.taught_wallets)} | "
                    f"Discovered: {len(case.discovered_wallets)}\n\n"
                )
                for i in range(0, len(all_w), 80):
                    batch = all_w[i:i + 80]
                    part = f" (part {i // 80 + 1})" if len(all_w) > 80 else ""
                    txt = (header if i == 0 else f"📋 <b>Wallets{part}</b>\n")
                    txt += f"<pre>{'\\n'.join(batch)}</pre>"
                    await bot_client.send_message(chat, txt, parse_mode='html')

            elif view_type == "funders":
                if not case.funders:
                    await event.answer("Không có funders", alert=True)
                    return
                lines = [f"💰 <b>Funders — {name}</b> (case #{case_id})\n"]
                for i, f in enumerate(case.funders, 1):
                    url = f"https://solscan.io/account/{f}"
                    lines.append(f"{i}. <code>{f[:6]}...{f[-4:]}</code> <a href='{url}'>🔗</a>")
                await bot_client.send_message(chat, "\n".join(lines), parse_mode='html', link_preview=False)

            elif view_type == "collectors":
                if not case.collectors:
                    await event.answer("Không có collectors", alert=True)
                    return
                lines = [f"📥 <b>Collectors — {name}</b> (case #{case_id})\n"]
                for i, c in enumerate(case.collectors, 1):
                    url = f"https://solscan.io/account/{c}"
                    lines.append(f"{i}. <code>{c[:6]}...{c[-4:]}</code> <a href='{url}'>🔗</a>")
                await bot_client.send_message(chat, "\n".join(lines), parse_mode='html', link_preview=False)

            await event.answer()
        except Exception as e:
            logger.error(f"Teach view error: {e}")
            await event.answer("Error loading data", alert=True)

    @bot_client.on(events.CallbackQuery(pattern=rb'^teach_fb:'))
    async def teach_feedback_handler(event):
        """Handle teach feedback buttons."""
        import teach_store

        try:
            data = event.data.decode('utf-8')
            parts = data.split(":")
            if len(parts) != 3:
                await event.answer("Invalid callback data")
                return

            case_id = int(parts[1])
            feedback = parts[2]

            if feedback not in ("good", "bad"):
                await event.answer("Invalid feedback")
                return

            teach_store.update_feedback(case_id, feedback)

            if feedback == "good":
                teach_store.adjust_pattern_confidence(case_id, delta=0.2)
                await event.answer("✅ Đánh dấu hữu ích, confidence +0.2")
            else:
                teach_store.adjust_pattern_confidence(case_id, delta=-0.3)
                await event.answer("❌ Đánh dấu sai, confidence -0.3")

        except Exception as e:
            logger.error(f"Teach feedback error: {e}")
            await event.answer("Error processing feedback")

    @bot_client.on(events.NewMessage(pattern=r'^/teachlist(?:@\w+)?$'))
    async def teachlist_handler(event):
        """Show recent teach cases."""
        import html as html_mod
        import teach_store

        cases = teach_store.list_cases(limit=10)
        if not cases:
            await event.reply("📚 Chưa có teach case nào.")
            return

        lines = ["📚 <b>Teach Cases</b>\n"]
        for c in cases:
            fb_icon = {"good": "✅", "bad": "❌", "pending": "⏳"}.get(c.feedback, "❓")
            name = html_mod.escape(c.token_name or c.token_symbol or c.mint[:12])
            lines.append(
                f"#{c.id} {fb_icon} <b>{name}</b>\n"
                f"  Taught: {len(c.taught_wallets)} | "
                f"Discovered: {len(c.discovered_wallets)} | "
                f"Funders: {len(c.funders)}"
            )

        await event.reply("\n".join(lines), parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/teachstats(?:@\w+)?$'))
    async def teachstats_handler(event):
        """Show teach system statistics."""
        import teach_store

        stats = teach_store.get_teach_stats()
        pt = stats.get("pattern_types", {})
        pt_lines = "\n".join(f"  {k}: {v}" for k, v in pt.items()) if pt else "  (none)"

        text = (
            f"📊 <b>Teach Statistics</b>\n\n"
            f"<b>Cases:</b>\n"
            f"  Total: <b>{stats['total_cases']}</b>\n"
            f"  ✅ Good: {stats['good']}\n"
            f"  ❌ Bad: {stats['bad']}\n"
            f"  ⏳ Pending: {stats['pending']}\n\n"
            f"<b>Patterns:</b>\n"
            f"  Active: <b>{stats['active_patterns']}</b>\n"
            f"{pt_lines}\n\n"
            f"<b>Known wallets:</b> {stats['known_wallets']}"
        )
        await event.reply(text, parse_mode='html')

    @bot_client.on(events.NewMessage(pattern=r'^/teachremove(?:@\w+)?\s'))
    async def teachremove_handler(event):
        """Remove a teach case."""
        import teach_store

        parts = event.raw_text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await event.reply(
                "Usage: <code>/teachremove &lt;case_id&gt;</code>",
                parse_mode='html',
            )
            return

        try:
            case_id = int(parts[1].strip())
        except ValueError:
            await event.reply("❌ Case ID phải là số.")
            return

        if teach_store.delete_case(case_id):
            await event.reply(f"🗑 Case #{case_id} đã xóa.", parse_mode='html')
        else:
            await event.reply(f"❌ Không tìm thấy case #{case_id}")

    # ─── Help ────────────────────────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/help(?:@\w+)?$'))
    async def help_handler(event):
        whale_section = ""
        if WHALE_ENABLED:
            whale_section = (
                "\n<b>Whale Splitter:</b>\n"
                "/scanbig - Scan whale splitters\n"
                "/whalequeue - View whale queue\n"
                "/whalestats - Splitter statistics\n"
                "/whalecheck &lt;wallet&gt; - Check wallet on-chain\n"
                "/whalethreshold [val] - View/set threshold\n"
                "/whaleremove &lt;wallet&gt; - Remove from queue\n"
                "/whaleclear stale|all - Purge queue\n"
                "\n<b>Token Analysis:</b>\n"
                "/analyze &lt;mint&gt; - Analyze Pump.fun token buyers\n"
                "/devtrace &lt;mint&gt; - Trace dev wallets (funding + profit)\n"
                "\n<b>Dev Scanner (ATH autoscan):</b>\n"
                "/devon - Bật autoscan\n"
                "/devoff - Tắt autoscan\n"
                "/devscan - Quét ATH ngay\n"
                "/setath &lt;value&gt; - Set ATH threshold\n"
                "/devstatus - Trạng thái scanner\n"
                "\n<b>Teach (tự học dev wallet):</b>\n"
                "/teach &lt;mint&gt; &lt;w1,w2&gt; - Dạy bot ví dev\n"
                "/teachlist - Danh sách teach cases\n"
                "/teachstats - Thống kê teach\n"
                "/teachremove &lt;id&gt; - Xóa teach case\n"
            )
        await event.respond(
            "🔍 <b>Degen Scanner Commands</b>\n\n"
            "/scan - Scan clusters\n"
            "/summary [hours] - Activity summary\n"
            "/status - Show bot statistics\n"
            "/keystats - Check API key credits\n"
            f"{whale_section}\n"
            "<b>Custom Patterns:</b>\n"
            "/patterns - List custom rules\n"
            "/addpattern - Add new rule (wizard)\n"
            "/delpattern &lt;id&gt; - Delete a rule\n"
            "/togglepattern &lt;id&gt; - Enable/disable\n"
            "/exchanges - Known exchanges\n\n"
            "<b>CEX:</b>\n"
            "/backfill [limit] - Nạp ví sàn từ source group\n"
            "/cexlist - Xem ví sàn đã học\n"
            "/reloadcex - Reload CEX cache\n\n"
            "<b>Settings:</b>\n"
            "/setwindow &lt;min&gt; - Set cluster time window\n"
            "/setspread &lt;value&gt; - Set amount spread filter (SOL)\n\n"
            "/help - This message\n\n"
            f"Bot auto-scans every {SCAN_INTERVAL_MINUTES} minutes.",
            parse_mode='html',
        )



    # ─── Settings Commands ────────────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/setspread'))
    async def setspread_handler(event):
        """Set MAX_AMOUNT_SPREAD for cluster detection."""
        try:
            logger.info(f"📏 /setspread from {event.sender_id}: {event.raw_text}")
            import cluster as cluster_module

            parts = event.raw_text.strip().split()
            if len(parts) < 2:
                await event.respond(
                    f"📏 <b>Amount Spread</b>\n\n"
                    f"Current: <code>{cluster_module.MAX_AMOUNT_SPREAD}</code> SOL\n\n"
                    f"Usage: <code>/setspread 0.5</code>\n"
                    f"Range: 0.1 - 10.0",
                    parse_mode='html',
                )
                return

            value = float(parts[1])
            if not (0.1 <= value <= 10.0):
                await event.respond("❌ Range: 0.1 - 10.0")
                return

            cluster_module.MAX_AMOUNT_SPREAD = value

            # Save to .env so it persists across restarts
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            try:
                with open(env_path, "r") as f:
                    env_content = f.read()

                import re as _re
                if _re.search(r'^MAX_AMOUNT_SPREAD=', env_content, _re.MULTILINE):
                    env_content = _re.sub(
                        r'^MAX_AMOUNT_SPREAD=.*$',
                        f'MAX_AMOUNT_SPREAD={value}',
                        env_content,
                        flags=_re.MULTILINE,
                    )
                else:
                    env_content += f'\nMAX_AMOUNT_SPREAD={value}\n'

                with open(env_path, "w") as f:
                    f.write(env_content)
            except Exception as e:
                logger.warning(f"Failed to save spread to .env: {e}")

            await event.respond(
                f"✅ Amount spread = <b>{value}</b> SOL (saved)",
                parse_mode='html',
            )
            logger.info(f"📏 MAX_AMOUNT_SPREAD changed to {value}")

        except ValueError:
            await event.respond("❌ Invalid number. Example: <code>/setspread 0.5</code>", parse_mode='html')
        except Exception as e:
            logger.error(f"❌ /setspread error: {e}", exc_info=True)
            await event.respond(f"❌ Error: {e}")

    @bot_client.on(events.NewMessage(pattern=r'^/setwindow'))
    async def setwindow_handler(event):
        """Set TIME_WINDOW_MINUTES for cluster detection."""
        try:
            logger.info(f"⏰ /setwindow from {event.sender_id}: {event.raw_text}")
            parts = event.raw_text.strip().split()
            if len(parts) < 2:
                current = int(engine.time_window.total_seconds() / 60)
                await event.respond(
                    f"⏰ <b>Time Window</b>\n\n"
                    f"Current: <code>{current}</code> min\n\n"
                    f"Usage: <code>/setwindow 10</code>\n"
                    f"Range: 1 - 60",
                    parse_mode='html',
                )
                return

            value = int(parts[1])
            if not (1 <= value <= 60):
                await event.respond("❌ Range: 1 - 60 minutes")
                return

            engine.time_window = timedelta(minutes=value)

            # Save to .env
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            try:
                with open(env_path, "r") as f:
                    env_content = f.read()

                import re as _re
                if _re.search(r'^TIME_WINDOW_MINUTES=', env_content, _re.MULTILINE):
                    env_content = _re.sub(
                        r'^TIME_WINDOW_MINUTES=.*$',
                        f'TIME_WINDOW_MINUTES={value}',
                        env_content,
                        flags=_re.MULTILINE,
                    )
                else:
                    env_content += f'\nTIME_WINDOW_MINUTES={value}\n'

                with open(env_path, "w") as f:
                    f.write(env_content)
            except Exception as e:
                logger.warning(f"Failed to save window to .env: {e}")

            await event.respond(
                f"✅ Time window = <b>{value}</b> min (saved)\n"
                f"Clusters gom ví trong {value} phút.",
                parse_mode='html',
            )
            logger.info(f"⏰ TIME_WINDOW changed to {value}min by {event.sender_id}")
        except ValueError:
            await event.respond("❌ Invalid number. Example: <code>/setwindow 10</code>", parse_mode='html')
        except Exception as e:
            logger.error(f"❌ /setwindow error: {e}", exc_info=True)
            await event.respond(f"❌ Error: {e}")

    # ─── Custom Pattern Commands ─────────────────────────────

    @bot_client.on(events.NewMessage(pattern=r'^/patterns(?:@\w+)?$'))
    async def patterns_handler(event):
        """List all custom pattern rules."""
        try:
            rules = rule_store.get_all_rules()
            if not rules:
                await event.respond(
                    "📐 <b>Custom Pattern Rules</b>\n\n"
                    "No rules defined yet.\n"
                    "Use /addpattern to create one.",
                    parse_mode='html',
                )
                return

            lines = ["📐 <b>Custom Pattern Rules</b>\n"]
            for r in rules:
                status = "✅" if r.enabled else "⏸"
                amt = ""
                if r.amount_min is not None or r.amount_max is not None:
                    lo = f"{r.amount_min:.2f}" if r.amount_min is not None else "0"
                    hi = f"{r.amount_max:.2f}" if r.amount_max is not None else "∞"
                    amt = f" | {lo}-{hi} SOL"

                interval = ""
                if r.max_interval_seconds is not None:
                    interval = f" | gap≤{r.max_interval_seconds}s"

                lines.append(
                    f"\n{status} <b>{r.name}</b>\n"
                    f"   ID: <code>{r.id}</code>\n"
                    f"   CEX: {r.cex} | {r.pattern_type}\n"
                    f"   ⏰ {r.time_window_minutes}m | 👛 ≥{r.min_wallets}{amt}{interval}"
                )

            lines.append(
                "\n\n/togglepattern &lt;id&gt; - Toggle on/off\n"
                "/delpattern &lt;id&gt; - Delete rule"
            )
            await event.respond("\n".join(lines), parse_mode='html')
        except Exception as e:
            logger.error(f"❌ /patterns error: {e}", exc_info=True)
            await event.respond(f"❌ Error: {e}")

    @bot_client.on(events.NewMessage(pattern=r'^/delpattern(?:@\w+)?(?:\s|$)'))
    async def delpattern_handler(event):
        """Delete a custom pattern rule."""
        try:
            parts = event.raw_text.split(maxsplit=1)
            if len(parts) < 2:
                await event.respond(
                    "Usage: /delpattern &lt;rule_id&gt;\n"
                    "Use /patterns to see rule IDs.",
                    parse_mode='html',
                )
                return

            rule_id = parts[1].strip()
            rule = rule_store.get_rule(rule_id)
            if not rule:
                await event.respond(f"❌ Rule not found: <code>{rule_id}</code>", parse_mode='html')
                return

            rule_store.delete_rule(rule_id)
            await event.respond(
                f"🗑 Deleted: <b>{rule.name}</b>\n"
                f"ID: <code>{rule_id}</code>",
                parse_mode='html',
            )
        except Exception as e:
            logger.error(f"❌ /delpattern error: {e}", exc_info=True)
            await event.respond(f"❌ Error: {e}")

    @bot_client.on(events.NewMessage(pattern=r'^/togglepattern(?:@\w+)?(?:\s|$)'))
    async def togglepattern_handler(event):
        """Toggle a custom pattern rule on/off."""
        try:
            parts = event.raw_text.split(maxsplit=1)
            if len(parts) < 2:
                await event.respond(
                    "Usage: /togglepattern &lt;rule_id&gt;\n"
                    "Use /patterns to see rule IDs.",
                    parse_mode='html',
                )
                return

            rule_id = parts[1].strip()
            new_state = rule_store.toggle_rule(rule_id)
            if new_state is None:
                await event.respond(f"❌ Rule not found: <code>{rule_id}</code>", parse_mode='html')
                return

            rule = rule_store.get_rule(rule_id)
            status = "✅ Enabled" if new_state else "⏸ Disabled"
            name = rule.name if rule else rule_id
            await event.respond(
                f"{status}: <b>{name}</b>",
                parse_mode='html',
            )
        except Exception as e:
            logger.error(f"❌ /togglepattern error: {e}", exc_info=True)
            await event.respond(f"❌ Error: {e}")

    @bot_client.on(events.NewMessage(pattern=r'^/exchanges(?:@\w+)?$'))
    async def exchanges_handler(event):
        """List known CEX exchanges from auto-discovery."""
        try:
            cex_list = rule_store.get_cex_list()
            if not cex_list:
                await event.respond(
                    "📊 <b>Known Exchanges</b>\n\n"
                    "No exchanges tracked yet.\n"
                    "Run /scan first to discover exchanges.",
                    parse_mode='html',
                )
                return

            lines = ["📊 <b>Known Exchanges</b>\n"]
            for i, entry in enumerate(cex_list[:30], 1):
                last = ""
                if entry.last_seen:
                    try:
                        dt = datetime.fromisoformat(entry.last_seen)
                        dt_local = dt.replace(tzinfo=timezone.utc).astimezone(UTC7)
                        last = f" | last: {dt_local:%m/%d %H:%M}"
                    except Exception:
                        pass
                lines.append(f"{i}. <b>{entry.display}</b> — {entry.count} txns{last}")

            if len(cex_list) > 30:
                lines.append(f"\n<i>... and {len(cex_list) - 30} more</i>")

            await event.respond("\n".join(lines), parse_mode='html')
        except Exception as e:
            logger.error(f"❌ /exchanges error: {e}", exc_info=True)
            await event.respond(f"❌ Error: {e}")

    # ─── Addpattern Wizard ─────────────────────────────────────

    def _clean_expired_wizards():
        """Remove expired wizard sessions."""
        now = time.time()
        expired = [uid for uid, s in wizard_states.items() if s.get("expires_at", 0) < now]
        for uid in expired:
            del wizard_states[uid]

    def _wizard_prompt(step: str, data: dict) -> str:
        """Generate prompt text for a wizard step."""
        if step == "name":
            return (
                "📐 <b>Add Custom Pattern Rule</b>\n\n"
                "<b>Step 1/7:</b> Rule name?\n\n"
                "Example: <i>MEXC tăng dần</i>"
            )

        if step == "cex":
            cex_names = rule_store.get_cex_names()
            known = ""
            if cex_names:
                top = cex_names[:10]
                known = "\n\nKnown exchanges:\n" + "\n".join(
                    f"  • {n}" for n in top
                )
            return (
                f"<b>Step 2/7:</b> CEX filter?\n\n"
                f"Type a keyword (e.g., <i>mexc</i>) or <code>*</code> for any.{known}"
            )

        if step == "pattern_type":
            return (
                "<b>Step 3/7:</b> Pattern type?\n\n"
                "1. <b>increasing</b> — amounts go up\n"
                "2. <b>decreasing</b> — amounts go down\n"
                "3. <b>exact_amount</b> — same amount ±tolerance\n"
                "4. <b>rapid_fire</b> — just count wallets\n\n"
                "Reply with number or name."
            )

        if step == "amount_range":
            return (
                "<b>Step 4/7:</b> Amount range (SOL)?\n\n"
                "Format: <code>min-max</code> (e.g., <code>0.5-2.5</code>)\n"
                "Or <code>skip</code> to allow any amount."
            )

        if step == "time_window":
            return (
                "<b>Step 5/7:</b> Time window (minutes)?\n\n"
                "Range: 1-60. Example: <code>15</code>"
            )

        if step == "max_interval":
            return (
                "<b>Step 6/7:</b> Max interval between wallets (seconds)?\n\n"
                "Max gap between two consecutive transfers.\n"
                "Example: <code>60</code> or <code>skip</code> to disable."
            )

        if step == "min_wallets":
            return (
                "<b>Step 7/7:</b> Minimum wallets to trigger?\n\n"
                "Range: 2-50. Example: <code>3</code>"
            )

        if step == "confirm":
            d = data
            amt = "any"
            if d.get("amount_min") is not None or d.get("amount_max") is not None:
                lo = f"{d['amount_min']:.2f}" if d.get("amount_min") is not None else "0"
                hi = f"{d['amount_max']:.2f}" if d.get("amount_max") is not None else "∞"
                amt = f"{lo}-{hi} SOL"

            interval = "none" if d.get("max_interval") is None else f"{d['max_interval']}s"

            tol = ""
            if d.get("tolerance") is not None:
                tol = f"\n   Tolerance: ±{d['tolerance']}%"

            return (
                "📋 <b>Confirm Rule:</b>\n\n"
                f"   Name: <b>{d['name']}</b>\n"
                f"   CEX: {d['cex']}\n"
                f"   Pattern: {d['pattern_type']}\n"
                f"   Amount: {amt}{tol}\n"
                f"   Window: {d['time_window']}m\n"
                f"   Max interval: {interval}\n"
                f"   Min wallets: {d['min_wallets']}\n\n"
                "Reply <code>yes</code> to save, <code>no</code> to cancel."
            )

        return ""

    async def _handle_teach_wizard(event, uid: int, twiz: dict):
        """Handle teach wizard step responses."""
        import html as html_mod
        from teach_pipeline import TeachingPipeline
        import teach_store

        step = twiz["step"]
        text = event.raw_text.strip()
        sol_re = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

        if step == "mint":
            if not sol_re.match(text):
                await event.reply(
                    "❌ Địa chỉ không hợp lệ. Gửi lại <b>mint address</b>:",
                    parse_mode='html',
                    buttons=Button.force_reply(),
                )
                return
            twiz["mint"] = text
            twiz["step"] = "wallets"
            twiz["expires_at"] = time.time() + WIZARD_TIMEOUT
            await event.reply(
                f"✅ Mint: <code>{text[:12]}...</code>\n\n"
                f"Bước 2/2 — Gửi <b>danh sách ví dev</b> "
                f"(cách nhau bằng dấu phẩy hoặc xuống dòng):",
                parse_mode='html',
                buttons=Button.force_reply(),
            )
            return

        if step == "wallets":
            raw = text.replace("\n", ",")
            taught_wallets = [w.strip() for w in raw.split(",") if w.strip()]
            invalid = [w for w in taught_wallets if not sol_re.match(w)]

            if invalid:
                await event.reply(
                    f"❌ Ví không hợp lệ: <code>{invalid[0][:20]}...</code>\n"
                    f"Gửi lại danh sách ví dev:",
                    parse_mode='html',
                    buttons=Button.force_reply(),
                )
                return

            if not taught_wallets:
                await event.reply(
                    "❌ Cần ít nhất 1 ví dev. Gửi lại:",
                    parse_mode='html',
                    buttons=Button.force_reply(),
                )
                return

            mint = twiz["mint"]
            del teach_wizard_states[uid]

            # Run pipeline (same as teach_handler direct mode)
            progress_msg = await event.reply(
                f"🎓 Bắt đầu học {_wc(mint)} "
                f"({len(taught_wallets)} ví dev)\n"
                f"⏳ Starting...",
                parse_mode='html',
            )

            pipeline = TeachingPipeline(helius=helius_client)

            async def on_progress(step_n: int, msg: str):
                if step_n in (0, 1, 7, 8, 9, 10):
                    try:
                        await progress_msg.edit(
                            f"🎓 Đang học {_wc(mint)}\n"
                            f"⏳ Step {step_n}: {msg}",
                            parse_mode='html',
                        )
                    except Exception:
                        pass

            result = await pipeline.teach(
                mint=mint,
                taught_wallets=taught_wallets,
                on_progress=on_progress,
            )

            if result.error:
                await event.respond(f"❌ Teach failed: {result.error}")
                return

            name = html_mod.escape(result.token_name or "Unknown")
            symbol = html_mod.escape(result.token_symbol or "???")
            pattern_types = set(p["pattern_type"] for p in result.patterns)
            pattern_str = ", ".join(sorted(pattern_types)) if pattern_types else "none"

            result_text = (
                f"✅ <b>TEACH RESULT — {name} (${symbol})</b>\n\n"
                f"📚 Ví cung cấp: <b>{len(result.taught_wallets)}</b>  |  "
                f"🔍 Phát hiện thêm: <b>{len(result.discovered_wallets)}</b>\n"
                f"💰 Funders: <b>{len(result.funders)}</b>  |  "
                f"📥 Collectors: <b>{len(result.collectors)}</b>\n"
                f"🔑 Patterns: {pattern_str}\n"
                f"Case ID: <b>#{result.case_id}</b>"
            )

            cid = result.case_id
            target = pump_alert_entity or event.chat
            await safe_send_alert(
                bot_client, target, result_text,
                buttons=[
                    [
                        Button.inline("📋 Wallets", f"teach_view:{cid}:wallets".encode()),
                        Button.inline("💰 Funders", f"teach_view:{cid}:funders".encode()),
                        Button.inline("📥 Collectors", f"teach_view:{cid}:collectors".encode()),
                    ],
                    [
                        Button.inline("✅ Hữu ích", f"teach_fb:{cid}:good".encode()),
                        Button.inline("❌ Không chính xác", f"teach_fb:{cid}:bad".encode()),
                    ],
                ],
            )

    async def _wizard_respond(event, text):
        """Send wizard prompt with ForceReply so bot receives replies in groups."""
        await event.respond(
            text,
            parse_mode='html',
            buttons=Button.force_reply(),
        )

    @bot_client.on(events.NewMessage(pattern=r'^/addpattern(?:@\w+)?$'))
    async def addpattern_handler(event):
        """Start the addpattern wizard."""
        logger.info(f"📝 /addpattern from {event.sender_id}")
        _clean_expired_wizards()
        uid = event.sender_id

        wizard_states[uid] = {
            "step": "name",
            "data": {},
            "expires_at": time.time() + WIZARD_TIMEOUT,
        }

        try:
            await _wizard_respond(event, _wizard_prompt("name", {}))
        except Exception as e:
            logger.error(f"ForceReply failed: {e}, falling back to plain respond")
            await event.respond(_wizard_prompt("name", {}), parse_mode='html')

        # Prevent wizard_message_handler from catching this same event
        # and deleting the wizard state we just created
        raise StopPropagation

    @bot_client.on(events.NewMessage(func=lambda e: not e.raw_text.startswith('/')))
    async def wizard_message_handler(event):
        """Handle wizard step responses (only non-command messages)."""
        _clean_expired_wizards()
        uid = event.sender_id

        # ─── Teach wizard ──────────────────────────
        if uid in teach_wizard_states:
            twiz = teach_wizard_states[uid]
            if time.time() > twiz.get("expires_at", 0):
                del teach_wizard_states[uid]
            else:
                await _handle_teach_wizard(event, uid, twiz)
                return

        if uid not in wizard_states:
            return

        state = wizard_states[uid]
        step = state["step"]
        data = state["data"]
        text = event.raw_text.strip()

        # Refresh timeout
        state["expires_at"] = time.time() + WIZARD_TIMEOUT

        # ─── Process each step ──────────────────────────────

        if step == "name":
            if len(text) > 100:
                await _wizard_respond(event, "❌ Name too long (max 100 chars). Try again:")
                return
            data["name"] = text
            state["step"] = "cex"
            await _wizard_respond(event, _wizard_prompt("cex", data))
            return

        if step == "cex":
            data["cex"] = text
            state["step"] = "pattern_type"
            await _wizard_respond(event, _wizard_prompt("pattern_type", data))
            return

        if step == "pattern_type":
            # Accept number or name
            type_map = {"1": "increasing", "2": "decreasing", "3": "exact_amount", "4": "rapid_fire"}
            pt = type_map.get(text, text.lower())
            if pt not in VALID_PATTERN_TYPES:
                await _wizard_respond(event, "❌ Invalid. Choose 1-4 or type name. Try again:")
                return
            data["pattern_type"] = pt

            # If exact_amount, ask for tolerance
            if pt == "exact_amount":
                state["step"] = "tolerance"
                await _wizard_respond(
                    event,
                    "<b>Tolerance %</b> for exact_amount?\n\n"
                    "Example: <code>5</code> means ±5% of median.\n"
                    "Default: <code>5</code>",
                )
                return

            state["step"] = "amount_range"
            await _wizard_respond(event, _wizard_prompt("amount_range", data))
            return

        if step == "tolerance":
            if text.lower() == "skip":
                data["tolerance"] = 5.0
            else:
                try:
                    val = float(text)
                    if val < 0 or val > 100:
                        await _wizard_respond(event, "❌ Must be 0-100. Try again:")
                        return
                    data["tolerance"] = val
                except ValueError:
                    await _wizard_respond(event, "❌ Invalid number. Try again:")
                    return
            state["step"] = "amount_range"
            await _wizard_respond(event, _wizard_prompt("amount_range", data))
            return

        if step == "amount_range":
            if text.lower() == "skip":
                data["amount_min"] = None
                data["amount_max"] = None
            else:
                try:
                    if "-" in text:
                        parts = text.split("-", 1)
                        lo = float(parts[0].strip())
                        hi = float(parts[1].strip())
                        if lo > hi:
                            await _wizard_respond(event, "❌ Min > Max. Try again:")
                            return
                        data["amount_min"] = lo
                        data["amount_max"] = hi
                    else:
                        # Single number = exact (use as both min and max)
                        val = float(text)
                        data["amount_min"] = val
                        data["amount_max"] = val
                except ValueError:
                    await _wizard_respond(event, "❌ Invalid format. Use <code>min-max</code> or <code>skip</code>.")
                    return

            state["step"] = "time_window"
            await _wizard_respond(event, _wizard_prompt("time_window", data))
            return

        if step == "time_window":
            try:
                val = int(text)
                if val < 1 or val > 60:
                    await _wizard_respond(event, "❌ Must be 1-60 minutes. Try again:")
                    return
                data["time_window"] = val
            except ValueError:
                await _wizard_respond(event, "❌ Invalid number. Try again:")
                return
            state["step"] = "max_interval"
            await _wizard_respond(event, _wizard_prompt("max_interval", data))
            return

        if step == "max_interval":
            if text.lower() == "skip":
                data["max_interval"] = None
            else:
                try:
                    val = int(text)
                    if val < 1 or val > 3600:
                        await _wizard_respond(event, "❌ Must be 1-3600 seconds. Try again:")
                        return
                    data["max_interval"] = val
                except ValueError:
                    await _wizard_respond(event, "❌ Invalid number. Try again:")
                    return
            state["step"] = "min_wallets"
            await _wizard_respond(event, _wizard_prompt("min_wallets", data))
            return

        if step == "min_wallets":
            try:
                val = int(text)
                if val < 2 or val > 50:
                    await _wizard_respond(event, "❌ Must be 2-50. Try again:")
                    return
                data["min_wallets"] = val
            except ValueError:
                await _wizard_respond(event, "❌ Invalid number. Try again:")
                return
            state["step"] = "confirm"
            await _wizard_respond(event, _wizard_prompt("confirm", data))
            return

        if step == "confirm":
            if text.lower() in ("yes", "y", "ok", "oke"):
                try:
                    rule = rule_store.add_rule(
                        name=data["name"],
                        cex=data["cex"],
                        pattern_type=data["pattern_type"],
                        time_window_minutes=data["time_window"],
                        min_wallets=data["min_wallets"],
                        amount_min=data.get("amount_min"),
                        amount_max=data.get("amount_max"),
                        amount_tolerance_pct=data.get("tolerance"),
                        max_interval_seconds=data.get("max_interval"),
                    )
                    del wizard_states[uid]
                    await event.respond(
                        f"✅ Rule saved!\n\n"
                        f"<b>{rule.name}</b>\n"
                        f"ID: <code>{rule.id}</code>\n\n"
                        f"Use /patterns to see all rules.",
                        parse_mode='html',
                    )
                except ValueError as e:
                    await event.respond(f"❌ Validation error: {e}")
                    del wizard_states[uid]
                return

            # Cancel
            del wizard_states[uid]
            await event.respond("❌ Cancelled.")
            return

    # ─── Start Both Clients ───────────────────────────────────

    logger.info("=" * 60)
    logger.info("🔍 DEGEN SCANNER - Starting Bot...")
    logger.info("=" * 60)

    async with user_client:
        await bot_client.start(bot_token=BOT_TOKEN)
        try:
            # Initial Helius key health check — remove dead keys
            logger.info("Checking Helius key health...")
            health = await helius_client.check_keys_health(force=True)
            logger.info(
                f"Helius: {health['active']} active, {health['no_credits']} dead, "
                f"{health.get('in_rotation', '?')} in rotation"
            )

            bot_me = await bot_client.get_me()
            logger.info(f"✅ Bot: @{bot_me.username}")

            # Register bot commands with Telegram
            bot_commands = [
                BotCommand(command="start", description="Welcome & info"),
                BotCommand(command="scan", description="Scan CEX withdrawal clusters"),
                BotCommand(command="scanbig", description="Scan whale splitters"),
                BotCommand(command="whalequeue", description="View whale queue"),
                BotCommand(command="whalestats", description="Whale splitter stats"),
                BotCommand(command="whalecheck", description="Check a wallet on-chain"),
                BotCommand(command="whalethreshold", description="View/set whale threshold"),
                BotCommand(command="whaleremove", description="Remove wallet from queue"),
                BotCommand(command="whaleclear", description="Purge stale or clear queue"),
                BotCommand(command="summary", description="Activity summary (3h/24h/72h/1w)"),
                BotCommand(command="status", description="Bot statistics"),
                BotCommand(command="keystats", description="Check API key credits"),
                BotCommand(command="patterns", description="List custom rules"),
                BotCommand(command="addpattern", description="Add custom rule"),
                BotCommand(command="delpattern", description="Delete a custom rule"),
                BotCommand(command="togglepattern", description="Enable/disable rule"),
                BotCommand(command="exchanges", description="Known exchanges"),
                BotCommand(command="analyze", description="Analyze Pump.fun token buyers"),
                BotCommand(command="devtrace", description="Trace dev wallets (funding+profit)"),
                BotCommand(command="trace", description="Alias for /devtrace"),
                BotCommand(command="trace1", description="Đuổi tiến (tiền đi đâu)"),
                BotCommand(command="trace2", description="Đuổi lùi (nguồn tiền)"),
                BotCommand(command="devon", description="Enable ATH autoscan"),
                BotCommand(command="devoff", description="Disable ATH autoscan"),
                BotCommand(command="devscan", description="Trigger ATH scan now"),
                BotCommand(command="setath", description="Set ATH threshold"),
                BotCommand(command="devstatus", description="Dev scanner status"),
                BotCommand(command="teach", description="Dạy bot ví dev (wizard)"),
                BotCommand(command="teachlist", description="Danh sách teach cases"),
                BotCommand(command="teachstats", description="Thống kê teach"),
                BotCommand(command="teachremove", description="Xóa teach case"),
                BotCommand(command="help", description="Show all commands"),
            ]
            try:
                # Reset old commands first, then set new ones
                await bot_client(ResetBotCommandsRequest(
                    scope=BotCommandScopeDefault(),
                    lang_code="",
                ))
                await bot_client(SetBotCommandsRequest(
                    scope=BotCommandScopeDefault(),
                    lang_code="",
                    commands=bot_commands,
                ))
                logger.info(f"✅ Registered {len(bot_commands)} bot commands")
            except Exception as e:
                logger.warning(f"Failed to register bot commands: {e}")

            # Verify all source groups — remove inaccessible ones
            valid_sources = []
            for gid in SOURCE_GROUP_IDS:
                try:
                    source = await user_client.get_entity(gid)
                    logger.info(f"✅ Source: {getattr(source, 'title', gid)}")
                    valid_sources.append(gid)
                except Exception as e:
                    logger.warning(f"❌ Removing dead source group {gid}: {e}")
            if not valid_sources:
                logger.error("No accessible source groups! Check SOURCE_GROUP_IDS.")
                return
            # Update global list to only active groups
            SOURCE_GROUP_IDS.clear()
            SOURCE_GROUP_IDS.extend(valid_sources)
            logger.info(f"📡 {len(valid_sources)} source groups active")

            try:
                global alert_entity, whale_alert_entity, pump_alert_entity
                alert_entity = await bot_client.get_entity(ALERT_GROUP_ID)
                logger.info(f"✅ Alert: {getattr(alert_entity, 'title', ALERT_GROUP_ID)}")

                if WHALE_ALERT_GROUP_ID != ALERT_GROUP_ID:
                    whale_alert_entity = await bot_client.get_entity(WHALE_ALERT_GROUP_ID)
                    logger.info(f"✅ Whale Alert: {getattr(whale_alert_entity, 'title', WHALE_ALERT_GROUP_ID)}")
                else:
                    whale_alert_entity = alert_entity

                if PUMP_ALERT_GROUP_ID:
                    try:
                        pump_alert_entity = await bot_client.get_entity(PUMP_ALERT_GROUP_ID)
                        logger.info(f"✅ Pump Alert: {getattr(pump_alert_entity, 'title', PUMP_ALERT_GROUP_ID)}")
                    except Exception as pe:
                        logger.warning(f"Pump alert group not accessible: {pe}")
                        pump_alert_entity = alert_entity
            except Exception as e:
                logger.error(f"Cannot access alert group: {e}")
                return

            if WHALE_ENABLED:
                logger.info(f"✅ Helius API: {len(HELIUS_API_KEYS)} key(s) configured")
                logger.info(f"🐋 Whale splitter: uses source groups (≥12 SOL + *)")
            else:
                logger.info("ℹ️  Whale splitter: disabled (set HELIUS_API_KEYS to enable)")

            stats["started_at"] = datetime.now(timezone.utc)
            save_stat("started_at", stats["started_at"])

            logger.info("")
            logger.info(f"⏰ Window: {TIME_WINDOW_MINUTES} min | Min: {MIN_CLUSTER_SIZE} wallets")
            logger.info(f"🔄 Auto-scan every {SCAN_INTERVAL_MINUTES} min")
            logger.info(f"🐋 Whale splitter: {'ENABLED' if WHALE_ENABLED else 'DISABLED'}")
            logger.info(f"🤖 Send /scan or /status to @{bot_me.username}")
            logger.info("Press Ctrl+C to stop.")
            logger.info("=" * 60)

            try:
                whale_q = splitter.get_stats()['queue_size']
                whale_status = f"🐋 Whale splitter: ENABLED ({whale_q} in queue)" if WHALE_ENABLED else "🐋 Whale splitter: disabled"
                custom_count = len(rule_store.get_enabled_rules())
                custom_status = f"📐 Custom rules: {custom_count} enabled" if custom_count else "📐 Custom rules: none"
                await bot_client.send_message(
                    alert_entity,
                    "🟢 <b>Degen Scanner Online!</b>\n\n"
                    f"📡 Source groups: {len(SOURCE_GROUP_IDS)}\n"
                    f"⏰ Window: {TIME_WINDOW_MINUTES} min\n"
                    f"👛 Min cluster: {MIN_CLUSTER_SIZE} wallets\n"
                    f"🔄 Scan every: {SCAN_INTERVAL_MINUTES} min\n"
                    f"{whale_status}\n"
                    f"{custom_status}\n\n"
                    f"🔑 Helius: {len(HELIUS_API_KEYS)} keys\n"
                    f"🔑 Moralis: {len(MORALIS_API_KEYS)} keys\n\n"
                    "Send /keystats to check key credits.",
                    parse_mode='html',
                )
            except Exception as e:
                logger.warning(f"Could not send startup message: {e}")

            scan_task = asyncio.create_task(
                periodic_scan_loop(bot_client, user_client)
            )

            # ─── Dev Scanner: WS + ATH autoscan loop ─────
            portal = PumpPortalClient(
                on_new_token=lambda d: _on_ws_new_token(scanner_state, d),
                on_migration=lambda d: _on_ws_migration(scanner_state, d),
            )
            ws_task = asyncio.create_task(portal.run_forever())

            async def _dev_scan_loop():
                """ATH autoscan loop — runs alongside bot."""
                import time as _time
                while True:
                    if not scanner_state.autoscan_enabled:
                        await asyncio.sleep(10)
                        continue
                    try:
                        await scan_full(
                            token_data_client, dev_sender, scanner_state,
                            helius_client, dev_sender=dev_sender,
                        )
                    except Exception as e:
                        logger.error(f"Dev scan failed: {e}", exc_info=True)
                    await asyncio.sleep(1800)  # 30 min

            dev_scan_task = asyncio.create_task(_dev_scan_loop())
            logger.info("🕵️ Dev Scanner: autoscan loop started (WS + ATH + DevTrace)")

            await bot_client.run_until_disconnected()

        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            # Stop dev scanner tasks
            try:
                portal.stop()
                ws_task.cancel()
                dev_scan_task.cancel()
            except Exception:
                pass
            await token_data_client.close()
            await helius_client.close()
            await bot_client.disconnect()
            try:
                whale_info = f", {stats['whale_splits']} whale splits" if WHALE_ENABLED else ""
                await bot_client.send_message(
                    alert_entity,
                    "🔴 <b>Degen Scanner Offline</b>\n\n"
                    f"📊 Session: {stats['scans']} scans, "
                    f"{stats['clusters_detected']} clusters"
                    f"{whale_info}",
                    parse_mode='html',
                )
            except Exception:
                pass
            logger.info("")
            logger.info("=" * 60)
            logger.info(f"📊 Scans: {stats['scans']} | "
                         f"Clusters: {stats['clusters_detected']} | "
                         f"Whale splits: {stats['whale_splits']}")
            logger.info("👋 Goodbye!")


if __name__ == "__main__":
    asyncio.run(main())
