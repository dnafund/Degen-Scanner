"""
Degen Scanner - Pump.fun Background Scanner

Dual-source token discovery with real-time WebSocket primary:

  PumpPortal WS (real-time) ──> state.upsert_token() ──┐
                                                         ├─> ScannerState
  Moralis (every 30 min, fallback) ──> upsert_token() ──┘

  Scan Cycle (every 5 min):
    Phase 1: Moralis (only every 6th cycle = 30 min fallback)
    Phase 2: DexScreener enrich (unenriched tokens < 1h old)
    Phase 3: Filter interesting candidates
    Phase 4: PumpAnalyzer cluster analysis (top 50)
    Phase 5: Telegram alerts

Run alongside bot.py as a separate process:
    python -X utf8 pump_scanner.py
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import html as html_lib
import re

import aiohttp

from config import (
    PUMP_BOT_TOKEN,
    PUMP_ALERT_CHAT_ID,
    DEV_ALERT_BOT_TOKEN,
    DEV_ALERT_CHAT_ID,
    HELIUS_API_KEYS,
    MORALIS_API_KEYS,
)
from helius import HeliusClient
from pump_analyzer import PumpAnalyzer, TokenAnalysisResult, CLUSTER_LABELS, load_dynamic_cex
from pump_portal import PumpPortalClient
from token_data import TokenDataClient

# ─── Logging ─────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pump_scanner")

# ─── Wallet Display Helpers ───────────────────────────────────

def _w(addr: str) -> str:
    """Format wallet/mint for display: first4...last4"""
    if len(addr) <= 10:
        return addr
    return f"{addr[:4]}...{addr[-4:]}"


def _wc(addr: str) -> str:
    """Format wallet/mint as <code> tag: <code>first4...last4</code>"""
    return f"<code>{_w(addr)}</code>"


def _wl(addr: str) -> str:
    """Format wallet with <code> + Solscan link."""
    return f"<code>{_w(addr)}</code> <a href='https://solscan.io/account/{addr}'>🔗</a>"


# ─── Constants / Thresholds ──────────────────────────────────

SCAN_INTERVAL_SECONDS = 1800         # 30 min (legacy, used for Moralis fallback)
ENRICH_INTERVAL_SECONDS = 300        # enrichment cycle every 5 min
MORALIS_FALLBACK_INTERVAL = 1800     # Moralis catch-up every 30 min
MAX_ENRICH_PER_CYCLE = 500           # cap DexScreener batch per cycle
MAX_ANALYSIS_PER_CYCLE = 50          # max tokens to analyze per cycle
MIN_ATH_MARKET_CAP = 15000           # $15K ATH minimum for autoscan
ATH_BATCH_SIZE = 200                 # Pump.fun ATH lookups per cycle
DEVTRACE_CONCURRENCY = 3             # parallel DevTrace tokens in Phase 3
MIN_VOLUME_24H = 5000                # $5K minimum volume (legacy, for manual /scan)
MIN_TXN_COUNT_24H = 50               # 50 txns minimum (legacy)
ALERT_MIN_CLUSTER_WALLETS = 2        # CEX cluster min wallets
ALERT_TIMING_CLUSTER_SIZE = 3        # timing cluster min wallets
ALERT_TOP5_CONCENTRATION = 0.50      # top 5 hold > 50%

STATE_FILE = os.path.join(
    os.path.dirname(__file__), "data", "pump_scanner_state.json"
)

# ─── Dataclasses ─────────────────────────────────────────────


@dataclass
class TokenState:
    """Tracked state for a single token."""
    address: str
    name: str = ""
    symbol: str = ""
    first_seen: float = 0.0        # unix timestamp (when bot first saw it)
    created_at: float = 0.0        # unix timestamp (when token was created on-chain)
    ath_price: float = 0.0
    ath_market_cap: float = 0.0    # ATH market cap from Pump.fun API
    ath_checked: bool = False       # whether ATH has been looked up
    volume_24h: float = 0.0
    liquidity: float = 0.0
    txn_count: int = 0
    market_cap: float = 0.0
    graduated: bool = False
    analyzed: bool = False          # True = DevTrace already ran
    alerted: bool = False
    cluster_count: int = 0
    last_updated: float = 0.0


# ─── Scanner State (JSON persistence) ────────────────────────


class ScannerState:
    """
    Persistent state for the scanner. Stores token tracking data
    in a JSON file to survive restarts.
    """

    def __init__(self, path: str = STATE_FILE):
        self._path = path
        self._tokens: dict[str, TokenState] = {}
        self.autoscan_enabled: bool = True
        self.min_ath: float = MIN_ATH_MARKET_CAP
        self.scan_running: bool = False  # lock to prevent concurrent scans
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Get valid field names from TokenState
            valid_fields = {f.name for f in TokenState.__dataclass_fields__.values()}
            for addr, d in data.items():
                # Filter out unknown fields (forward compat)
                filtered = {k: v for k, v in d.items() if k in valid_fields}
                self._tokens[addr] = TokenState(**filtered)
            logger.info(f"State loaded: {len(self._tokens)} tokens")
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            data = {addr: asdict(ts) for addr, ts in self._tokens.items()}
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def upsert_token(
        self,
        address: str,
        name: str = "",
        symbol: str = "",
        graduated: bool = False,
        created_at: float = 0.0,
    ) -> TokenState:
        """Insert or update a token. Returns the TokenState."""
        now = time.time()
        if address in self._tokens:
            ts = self._tokens[address]
            if name:
                ts.name = name
            if symbol:
                ts.symbol = symbol
            if graduated:
                ts.graduated = True
            if created_at and not ts.created_at:
                ts.created_at = created_at
            ts.last_updated = now
        else:
            ts = TokenState(
                address=address,
                name=name,
                symbol=symbol,
                first_seen=now,
                created_at=created_at,
                graduated=graduated,
                last_updated=now,
            )
            self._tokens[address] = ts
        return ts

    def update_market_data(
        self,
        address: str,
        price: float = 0,
        volume_24h: float = 0,
        txn_count: int = 0,
        liquidity: float = 0,
        market_cap: float = 0,
    ) -> None:
        """Update market data for a token."""
        ts = self._tokens.get(address)
        if not ts:
            return
        if price > ts.ath_price:
            ts.ath_price = price
        ts.volume_24h = volume_24h
        ts.txn_count = txn_count
        ts.liquidity = liquidity
        ts.market_cap = market_cap
        ts.last_updated = time.time()

    def mark_analyzed(self, address: str, cluster_count: int = 0) -> None:
        ts = self._tokens.get(address)
        if ts:
            ts.analyzed = True
            ts.cluster_count = cluster_count
            ts.last_updated = time.time()

    def mark_alerted(self, address: str) -> None:
        ts = self._tokens.get(address)
        if ts:
            ts.alerted = True
            ts.last_updated = time.time()

    def get_unanalyzed_interesting(
        self, limit: int = MAX_ANALYSIS_PER_CYCLE
    ) -> list[TokenState]:
        """
        Get tokens worth analyzing — not yet analyzed, created within 24h,
        and meeting thresholds.

        Priority: graduated first, then by volume descending.
        """
        cutoff_24h = time.time() - 24 * 3600
        candidates = []
        for ts in self._tokens.values():
            if ts.analyzed:
                continue
            # Only scan tokens created within last 24h
            if ts.created_at and ts.created_at < cutoff_24h:
                continue
            # Must meet at least one interest criterion
            if not (
                ts.graduated
                or ts.volume_24h >= MIN_VOLUME_24H
                or ts.txn_count >= MIN_TXN_COUNT_24H
            ):
                continue
            candidates.append(ts)

        # Sort: graduated first, then by volume
        candidates.sort(
            key=lambda t: (not t.graduated, -t.volume_24h)
        )
        return candidates[:limit]

    def cleanup_old(self, max_age_hours: int = 48) -> int:
        """Remove tokens older than max_age_hours. Returns count removed."""
        cutoff = time.time() - (max_age_hours * 3600)
        old_keys = [
            addr for addr, ts in self._tokens.items()
            if ts.first_seen < cutoff and not ts.alerted
        ]
        for key in old_keys:
            del self._tokens[key]
        if old_keys:
            logger.info(f"Cleaned up {len(old_keys)} old tokens")
        return len(old_keys)

    def save(self) -> None:
        """Explicit save (called at end of cycle)."""
        self._save()

    @property
    def total_tokens(self) -> int:
        return len(self._tokens)

    @property
    def analyzed_count(self) -> int:
        return sum(1 for ts in self._tokens.values() if ts.analyzed)

    @property
    def alerted_count(self) -> int:
        return sum(1 for ts in self._tokens.values() if ts.alerted)


# ─── Telegram Sender (Bot HTTP API) ─────────────────────────


class TelegramSender:
    """
    Send messages to Telegram via Bot HTTP API (no Telethon needed).
    Uses aiohttp directly for lightweight operation.
    """

    TELEGRAM_API = "https://api.telegram.org"

    def __init__(self, bot_token: str, chat_id: int):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def send(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_to: int = 0,
    ) -> bool:
        """Send a single message. Auto-splits if > 4000 chars."""
        if len(text) > 4000:
            return await self._send_split(text, parse_mode, reply_to)
        return await self._send_one(text, parse_mode, reply_to)

    async def _send_split(
        self, text: str, parse_mode: str, reply_to: int
    ) -> bool:
        """Split long message on newlines and send as multiple messages."""
        lines = text.split("\n")
        chunks, current = [], ""
        for line in lines:
            if len(current) + len(line) + 1 > 3800 and current:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

        ok = True
        for i, chunk in enumerate(chunks):
            rt = reply_to if i == 0 else 0
            if not await self._send_one(chunk, parse_mode, rt):
                ok = False
            if i < len(chunks) - 1:
                await asyncio.sleep(0.3)
        return ok

    async def _send_one(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_to: int = 0,
    ) -> bool:
        """Send a single message (must be ≤ 4096 chars)."""
        url = f"{self.TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
            payload["allow_sending_without_reply"] = True

        for attempt in range(3):
            try:
                session = await self._get_session()
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return True

                    body = await resp.json()

                    # FloodWait — Telegram asks us to wait
                    if resp.status == 429:
                        retry_after = body.get("parameters", {}).get(
                            "retry_after", 5
                        )
                        logger.warning(
                            f"Telegram FloodWait: {retry_after}s"
                        )
                        await asyncio.sleep(retry_after + 1)
                        continue

                    logger.error(
                        f"Telegram send failed {resp.status}: "
                        f"{body.get('description', '')}"
                    )
                    return False

            except Exception as e:
                logger.error(f"Telegram send error: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)

        return False

    async def send_chunks(
        self, chunks: list[str], reply_to: int = 0
    ) -> int:
        """
        Send multiple message chunks with delay between them.
        Only the first chunk replies to the original message.
        Returns count of successfully sent messages.
        """
        sent = 0
        for i, chunk in enumerate(chunks):
            rt = reply_to if i == 0 else 0
            if await self.send(chunk, reply_to=rt):
                sent += 1
            await asyncio.sleep(0.5)
        return sent

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


# ─── Alert Formatting ────────────────────────────────────────


def format_pump_scan_alert(
    token: TokenState,
    result: TokenAnalysisResult,
    dex_data: Optional[dict] = None,
) -> list[str]:
    """
    Format a scanner alert with market data + cluster info.
    Returns list of message chunks (each ≤ 3800 chars).
    """
    chunks: list[str] = []

    # ─── Header ───────────────────────────────────────
    name = html_lib.escape(token.name or result.token_name or "Unknown")
    symbol = html_lib.escape(token.symbol or result.token_symbol or "???")

    dex_link = f"https://dexscreener.com/solana/{token.address}"
    pump_link = f"https://pump.fun/{token.address}"

    header = (
        f"🔬 <b>PUMP SCANNER ALERT</b>\n\n"
        f"Token: <b>{name}</b> (${symbol})\n"
        f"Mint: <code>{token.address}</code>\n"
        f"<a href='{dex_link}'>DexScreener</a> | "
        f"<a href='{pump_link}'>Pump.fun</a>\n"
    )

    # ─── Market Data ──────────────────────────────────
    if dex_data:
        price = dex_data.get("priceUsd", 0)
        vol = dex_data.get("volume24h", 0)
        buys = dex_data.get("buys24h", 0)
        sells = dex_data.get("sells24h", 0)
        liq = dex_data.get("liquidity", 0)
        mcap = dex_data.get("marketCap", 0)

        header += (
            f"\n📊 <b>Market:</b>\n"
            f"  Price: ${price:,.6f}\n"
            f"  ATH: ${token.ath_price:,.6f}\n"
            f"  Vol 24h: ${vol:,.0f}\n"
            f"  Txns: {buys} buys / {sells} sells\n"
            f"  Liquidity: ${liq:,.0f}\n"
            f"  MCap: ${mcap:,.0f}\n"
        )

    # ─── Cluster Summary ──────────────────────────────
    if result.clusters:
        header += f"\n🚨 <b>Clusters ({len(result.clusters)}):</b>\n"

        for i, cluster in enumerate(result.clusters, 1):
            label = CLUSTER_LABELS.get(
                cluster.cluster_type, cluster.cluster_type
            )
            c_text = f"\n<b>{i}. {label}</b>"
            if cluster.cex_name:
                c_text += f" ({html_lib.escape(cluster.cex_name)})"
            c_text += f"\n  {html_lib.escape(cluster.notes)}\n"

            for w in cluster.wallets[:5]:
                sol_link = f"https://solscan.io/account/{w}"
                w_short = f"{w[:4]}...{w[-4:]}"
                # Find buy info
                matched_buy = None
                for buy in result.buys:
                    if buy.wallet == w:
                        matched_buy = buy
                        break
                if matched_buy:
                    c_text += (
                        f"  <code>{w_short}</code> - {matched_buy.sol_spent:.2f} SOL "
                        f"<a href='{sol_link}'>🔗</a>\n"
                    )
                else:
                    c_text += (
                        f"  <code>{w_short}</code> <a href='{sol_link}'>🔗</a>\n"
                    )

            if len(cluster.wallets) > 5:
                c_text += (
                    f"  <i>... +{len(cluster.wallets) - 5} more</i>\n"
                )

            # Check chunk size
            if len(header) + len(c_text) > 3800:
                chunks.append(header)
                header = (
                    f"🚨 <b>{name} (${symbol}) — Clusters (cont.)</b>\n"
                    f"{c_text}"
                )
            else:
                header += c_text

    # ─── Stats Footer ─────────────────────────────────
    stats = (
        f"\n📊 <b>{name}</b> — {result.total_buys} buys, "
        f"{result.unique_buyers} unique wallets, "
        f"{len(result.funding)} CEX funded"
    )
    if len(header) + len(stats) > 3800:
        chunks.append(header)
        header = stats
    else:
        header += stats

    chunks.append(header)
    return chunks


def _should_alert(
    result: TokenAnalysisResult, token: TokenState
) -> bool:
    """
    Determine if analysis result warrants an alert.

    Conditions (any one triggers):
      1. CEX cluster (same_source or cex_funded) with ≥ 2 wallets
      2. Timing cluster with ≥ 3 wallets
      3. Top 5 wallets hold > 50% of total buy volume
    """
    if result.error:
        return False

    # Condition 1: CEX cluster
    for cluster in result.clusters:
        if cluster.cluster_type in ("same_source", "cex_funded"):
            if len(cluster.wallets) >= ALERT_MIN_CLUSTER_WALLETS:
                return True

    # Condition 2: Timing cluster
    for cluster in result.clusters:
        if cluster.cluster_type.startswith("timing_"):
            if len(cluster.wallets) >= ALERT_TIMING_CLUSTER_SIZE:
                return True

    # Condition 3: Top 5 concentration
    if result.buys:
        wallet_totals: dict[str, float] = {}
        for buy in result.buys:
            wallet_totals[buy.wallet] = (
                wallet_totals.get(buy.wallet, 0) + buy.sol_spent
            )
        sorted_amounts = sorted(wallet_totals.values(), reverse=True)
        total = sum(sorted_amounts)
        if total > 0:
            top5 = sum(sorted_amounts[:5])
            if top5 / total >= ALERT_TOP5_CONCENTRATION:
                return True

    return False


# ─── Main Scan Cycle ─────────────────────────────────────────


async def scan_full(
    token_client: TokenDataClient,
    sender: TelegramSender,
    state: ScannerState,
    helius: HeliusClient,
    dev_sender: TelegramSender = None,
) -> dict:
    """
    Full ATH scan — runs as one continuous flow (no cycles):

      Phase 1: Moralis discovers ALL tokens from 24h
      Phase 2: Pump.fun ATH lookup for ALL unchecked tokens
      Phase 3: DevTrace ALL tokens with ATH >= $15k
    """
    # Prevent concurrent scans
    if state.scan_running:
        logger.warning("Scan already running — skipping")
        await sender.send("⚠️ Scan đang chạy rồi, đợi xong nhé.")
        return {}

    state.scan_running = True
    scan_start = time.time()
    stats = {
        "tokens_fetched": 0,
        "ath_checked": 0,
        "ath_qualified": 0,
        "devtraced": 0,
        "alerts_sent": 0,
        "errors": 0,
    }

    try:
        return await _scan_full_inner(
            token_client, sender, state, helius, scan_start, stats,
            dev_sender=dev_sender,
        )
    finally:
        state.scan_running = False


async def _scan_full_inner(
    token_client: TokenDataClient,
    sender: TelegramSender,
    state: "ScannerState",
    helius: HeliusClient,
    scan_start: float,
    stats: dict,
    dev_sender: TelegramSender = None,
) -> dict:
    """Inner scan logic — always called with scan_running lock held."""

    # ─── Phase 1: Moralis — discover ALL tokens from 24h ──
    logger.info("=" * 60)
    logger.info("Phase 1: Moralis — fetching ALL tokens from 24h...")
    await sender.send("⏳ Phase 1: Discovering tokens via Moralis...")

    try:
        raw_tokens = await token_client.fetch_all_recent_tokens(
            max_age_hours=24
        )
        stats["tokens_fetched"] = len(raw_tokens)
    except Exception as e:
        logger.error(f"Phase 1 failed: {e}")
        raw_tokens = []
        stats["errors"] += 1

    for token in raw_tokens:
        addr = token.get("tokenAddress", "")
        if addr:
            created_ts = 0.0
            created_str = token.get("createdAt", "")
            if created_str:
                try:
                    dt = datetime.fromisoformat(
                        created_str.replace("Z", "+00:00")
                    )
                    created_ts = dt.timestamp()
                except (ValueError, TypeError):
                    pass
            state.upsert_token(
                address=addr,
                name=token.get("name", ""),
                symbol=token.get("symbol", ""),
                graduated=token.get("_graduated", False),
                created_at=created_ts,
            )

    logger.info(f"Phase 1 done: {stats['tokens_fetched']} tokens")
    await sender.send(f"✅ Phase 1 done: <b>{stats['tokens_fetched']}</b> tokens from Moralis")

    # ─── Phase 2: Pump.fun ATH lookup — ALL unchecked ─────
    now = time.time()
    cutoff_24h = now - 24 * 3600
    unchecked = [
        ts for ts in state._tokens.values()
        if not ts.ath_checked
        and ts.created_at > cutoff_24h
    ]

    if unchecked:
        unchecked_mints = [ts.address for ts in unchecked]
        logger.info(f"Phase 2: ATH lookup for {len(unchecked_mints)} tokens...")
        await sender.send(f"⏳ Phase 2: Checking ATH for <b>{len(unchecked_mints)}</b> tokens...")
        async def _progress(done: int, total: int, found: int) -> None:
            logger.info(f"ATH lookup: {done}/{total} checked, {found} qualify")

        try:
            qualified, all_created = await token_client.lookup_ath_batch(
                mints=unchecked_mints,
                min_ath=state.min_ath,
                on_progress=_progress,
            )
            stats["ath_checked"] = len(unchecked_mints)

            # Build fast lookup
            qualified_map = {r["mint"]: r for r in qualified}
            for ts in unchecked:
                ts.ath_checked = True
                # Update created_at with REAL timestamp from Pump.fun
                real_ts = all_created.get(ts.address)
                if real_ts:
                    ts.created_at = real_ts
                q = qualified_map.get(ts.address)
                if q:
                    ts.ath_market_cap = q.get("ath_market_cap", 0)

            stats["ath_qualified"] = len(qualified)

        except Exception as e:
            logger.error(f"Phase 2 ATH lookup failed: {e}")
            stats["errors"] += 1

        logger.info(
            f"Phase 2 done: {stats['ath_checked']} checked, "
            f"{stats['ath_qualified']} qualified"
        )
        await sender.send(
            f"✅ Phase 2 done: <b>{stats['ath_qualified']}</b> tokens with ATH ≥ ${state.min_ath:,.0f}"
        )
    else:
        logger.info("Phase 2: No unchecked tokens")
        pass  # No unchecked tokens — silent

    # ─── Phase 3: DevTrace ALL qualified tokens ──────────
    to_trace = [
        ts for ts in state._tokens.values()
        if ts.ath_market_cap >= state.min_ath
        and not ts.analyzed
        and ts.created_at > cutoff_24h
    ]
    to_trace.sort(key=lambda t: -t.ath_market_cap)

    if to_trace:
        logger.info(f"Phase 3: DevTracing {len(to_trace)} tokens ({DEVTRACE_CONCURRENCY} concurrent)...")
        await sender.send(
            f"⏳ Phase 3: DevTracing <b>{len(to_trace)}</b> tokens "
            f"({DEVTRACE_CONCURRENCY} concurrent, ATH high → low)..."
        )
        # Phase 3 start — log only

        from dev_tracer import DevTracer
        from dev_tracer_fmt import format_trace_result

        tracer = DevTracer(helius=helius)
        sem = asyncio.Semaphore(DEVTRACE_CONCURRENCY)
        done_count = 0
        paused = False

        async def _trace_one(idx: int, token: TokenState) -> None:
            nonlocal done_count, paused
            if paused or not state.autoscan_enabled:
                paused = True
                return

            async with sem:
                if paused or not state.autoscan_enabled:
                    paused = True
                    return

                try:
                    logger.info(
                        f"  [{idx + 1}/{len(to_trace)}] DevTrace "
                        f"{token.symbol or token.address[:12]} "
                        f"(ATH ${token.ath_market_cap:,.0f})..."
                    )

                    result = await tracer.trace(token.address)
                    state.mark_analyzed(
                        token.address,
                        cluster_count=len(result.clusters),
                    )
                    stats["devtraced"] += 1

                    # Alert — send to dev alert group (skip if no clusters)
                    if result.clusters:
                        messages = format_trace_result(result)
                        if messages:
                            alert_target = dev_sender or sender
                            for msg in messages:
                                await alert_target.send(msg)
                            state.mark_alerted(token.address)
                            stats["alerts_sent"] += 1

                    # Pattern match — check against learned patterns
                    try:
                        from pattern_matcher import (
                            PatternMatcher, format_match_alert,
                        )
                        matcher = PatternMatcher()
                        match_result = matcher.check_token(
                            token.address, trace_result=result
                        )
                        if match_result.is_match:
                            alert_text = format_match_alert(
                                token.address,
                                token.name or result.token_name,
                                token.symbol or result.token_symbol,
                                match_result,
                            )
                            alert_target = dev_sender or sender
                            await alert_target.send(alert_text)
                            logger.info(
                                f"  Pattern match alert for "
                                f"{token.symbol or token.address[:12]}: "
                                f"{match_result.confidence_pct}%"
                            )
                    except Exception as pm_err:
                        logger.debug(f"Pattern match error: {pm_err}")

                except Exception as e:
                    logger.error(
                        f"  Error devtracing {token.address[:12]}: {e}",
                        exc_info=True,
                    )
                    stats["errors"] += 1

                done_count += 1
                # Progress every 20 tokens — log only
                if done_count % 20 == 0:
                    logger.info(
                        f"Progress: {done_count}/{len(to_trace)} traced, "
                        f"{stats['alerts_sent']} alerts sent"
                    )
                # Save state periodically
                if done_count % 50 == 0:
                    state.save()

        await asyncio.gather(*[_trace_one(i, t) for i, t in enumerate(to_trace)])

        if paused:
            logger.info(f"Scan paused at {done_count}/{len(to_trace)}: autoscan disabled")
    else:
        logger.info("Phase 3: No tokens to DevTrace")

    # ─── Done ────────────────────────────────────────
    state.cleanup_old(max_age_hours=48)
    state.save()

    elapsed = time.time() - scan_start
    elapsed_min = int(elapsed // 60)
    logger.info(
        f"Full scan complete in {elapsed:.0f}s: "
        f"{stats['tokens_fetched']} fetched, "
        f"{stats['ath_qualified']} qualified, "
        f"{stats['devtraced']} devtraced, "
        f"{stats['alerts_sent']} alerts"
    )
    await sender.send(
        f"✅ <b>Full scan complete</b> ({elapsed_min} min)\n\n"
        f"Tokens discovered: {stats['tokens_fetched']:,}\n"
        f"ATH checked: {stats['ath_checked']:,}\n"
        f"ATH ≥ ${state.min_ath:,.0f}: {stats['ath_qualified']}\n"
        f"DevTraced: {stats['devtraced']}\n"
        f"Alerts: {stats['alerts_sent']}\n"
        f"Errors: {stats['errors']}"
    )

    return stats


# ─── WebSocket Callbacks ─────────────────────────────────────


def _on_ws_new_token(state: ScannerState, data: dict) -> None:
    """Callback for PumpPortal new token events."""
    mint = data.get("mint", "")
    if not mint:
        return
    # WS new token = just created now
    state.upsert_token(
        address=mint,
        name=data.get("name", ""),
        symbol=data.get("symbol", ""),
        created_at=time.time(),
    )


def _on_ws_migration(state: ScannerState, data: dict) -> None:
    """Callback for PumpPortal migration (graduation) events."""
    mint = data.get("mint", "")
    if not mint:
        return
    state.upsert_token(
        address=mint,
        graduated=True,
    )


# ─── Scan Loop (periodic enrichment + analysis) ─────────────


async def _scan_loop(
    token_client: TokenDataClient,
    analyzer: PumpAnalyzer,
    sender: TelegramSender,
    state: ScannerState,
    portal: PumpPortalClient,
    helius: HeliusClient,
    dev_sender: TelegramSender = None,
) -> None:
    """
    Continuous ATH scan loop.

    Runs full scan (Moralis → ATH check → DevTrace) in one go,
    then waits 30 min before repeating to catch new tokens.
    """
    run_num = 0

    while True:
        # Skip if autoscan is disabled
        if not state.autoscan_enabled:
            await asyncio.sleep(10)
            continue

        run_num += 1

        # Log WS stats
        ws_stats = portal.get_stats()
        logger.info(
            f"\n{'='*40} Scan #{run_num} {'='*40}\n"
            f"  WS: {ws_stats['tokens_received']} tokens, "
            f"{ws_stats['migrations_received']} migrations, "
            f"connected={ws_stats['connected']}, "
            f"reconnects={ws_stats['reconnects']}"
        )
        logger.info(
            f"  State: {state.total_tokens} tracked, "
            f"{state.analyzed_count} analyzed, "
            f"{state.alerted_count} alerted"
        )

        # Periodically recheck dead keys (every 5 days)
        try:
            await helius._maybe_recheck_dead_keys()
        except Exception as e:
            logger.debug(f"Dead key recheck error: {e}")

        try:
            await scan_full(
                token_client, sender, state, helius,
                dev_sender=dev_sender,
            )
        except Exception as e:
            logger.error(f"Scan #{run_num} failed: {e}", exc_info=True)
            await sender.send(f"⚠️ Scan #{run_num} failed: {e}")

        # Wait 30 min then repeat (catch new tokens)
        logger.info("Scan complete. Next run in 30 min...")
        await asyncio.sleep(MORALIS_FALLBACK_INTERVAL)


# ─── Bot Command Handler (Telegram Polling) ─────────────────

# Solana address pattern (base58, 32-44 chars)
_SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Teach wizard state: chat_id → {"step": "mint"|"wallets", "mint": str, "expires": float}
_teach_wizard: dict[int, dict] = {}
_TEACH_WIZARD_TIMEOUT = 300  # 5 minutes

TELEGRAM_API = "https://api.telegram.org"

# Commands help text
_HELP_TEXT = (
    "🔬 <b>Degen Scanner Bot</b>\n\n"
    "<b>Scanner:</b>\n"
    "  <code>/on</code> — bật auto scan\n"
    "  <code>/off</code> — tắt auto scan\n"
    "  <code>/scan</code> — quét ngay lập tức\n"
    "  <code>/setath 50000</code> — đổi ngưỡng ATH (mặc định $15k)\n"
    "  <code>/status</code> — trạng thái scanner\n"
    "  <code>/keystats</code> — check credit API keys\n\n"
    "<b>Trace:</b>\n"
    "  <code>/devtrace &lt;mint&gt;</code> — bắt dev, tìm cụm ví dev/hunters\n"
    "  <code>/trace1 &lt;wallet&gt;</code> — đuổi tiến (tiền đi đâu)\n"
    "  <code>/trace2 &lt;wallet&gt;</code> — đuổi lùi (nguồn tiền)\n"
    "  <code>/cancel</code> — hủy tất cả lệnh đang chạy\n\n"
    "<b>Token:</b>\n"
    "  <code>/top &lt;mint&gt;</code> — top holders\n"
    "  <code>/fresh &lt;mint&gt;</code> — ví mới mua\n"
    "  <code>/bundle &lt;mint&gt;</code> — phát hiện bundle\n"
    "  <code>/wallet &lt;wallet&gt;</code> — tóm tắt ví\n\n"
    "<b>Teach:</b>\n"
    "  <code>/teach</code> — dạy bot ví dev (wizard từng bước)\n"
    "  <code>/teachlist</code> — danh sách cases\n"
    "  <code>/teachstats</code> — thống kê teach\n"
    "  <code>/teachremove &lt;id&gt;</code> — xóa case\n\n"
    "<b>Discuss:</b>\n"
    "  <code>/discuss &lt;case_id&gt;</code> — thảo luận case với AI\n"
    "  <code>/enddiscuss</code> — kết thúc thảo luận\n\n"
    "<b>CEX:</b>\n"
    "  <code>/cexlist</code> — xem ví sàn đã học\n"
    "  <code>/reloadcex</code> — reload CEX cache từ DB\n\n"
    "Hoặc paste mint address để scan."
)


async def _bot_polling(
    analyzer: PumpAnalyzer,
    sender: TelegramSender,
    state: ScannerState,
    token_client: TokenDataClient,
    helius: HeliusClient,
    portal: PumpPortalClient,
    dev_sender: TelegramSender = None,
) -> None:
    """
    Long-poll Telegram for incoming messages.
    Routes commands to appropriate handlers.
    """
    bot_url = f"{TELEGRAM_API}/bot{PUMP_BOT_TOKEN}"
    offset = 0
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    # Register bot commands with Telegram
    bot_commands = [
        {"command": "on", "description": "Bật auto scan"},
        {"command": "off", "description": "Tắt auto scan"},
        {"command": "scan", "description": "Quét ngay lập tức"},
        {"command": "setath", "description": "Đổi ngưỡng ATH (vd: /setath 50000)"},
        {"command": "status", "description": "Trạng thái scanner"},
        {"command": "keystats", "description": "Check credit API keys"},
        {"command": "devtrace", "description": "Bắt dev clusters"},
        {"command": "trace1", "description": "Đuổi tiến (tiền đi đâu)"},
        {"command": "trace2", "description": "Đuổi lùi (nguồn tiền)"},
        {"command": "top", "description": "Top holders"},
        {"command": "fresh", "description": "Ví mới mua"},
        {"command": "bundle", "description": "Phát hiện bundle"},
        {"command": "wallet", "description": "Tóm tắt ví"},
        {"command": "cancel", "description": "Hủy tất cả lệnh đang chạy"},
        {"command": "teach", "description": "Dạy bot ví dev (mint + wallets)"},
        {"command": "teachlist", "description": "Danh sách teach cases"},
        {"command": "teachstats", "description": "Thống kê teach"},
        {"command": "teachremove", "description": "Xóa teach case"},
        {"command": "discuss", "description": "Thảo luận case với AI"},
        {"command": "enddiscuss", "description": "Kết thúc thảo luận"},
        {"command": "cexlist", "description": "Xem ví sàn đã học"},
        {"command": "reloadcex", "description": "Reload CEX cache"},
        {"command": "help", "description": "Tất cả lệnh"},
    ]
    try:
        async with session.post(
            f"{bot_url}/setMyCommands",
            json={"commands": bot_commands},
        ) as resp:
            if resp.status == 200:
                logger.info(f"Registered {len(bot_commands)} bot commands")
            else:
                logger.warning(f"Failed to register commands: {resp.status}")
    except Exception as e:
        logger.warning(f"Command registration failed: {e}")

    logger.info("Bot polling started")

    # Command router: prefix → (handler, needs_address)
    command_handlers = {
        "/devtrace": ("devtrace", True),
        "/trace1": ("trace1", True),
        "/trace2": ("trace2", True),
        "/top": ("top", True),
        "/fresh": ("fresh", True),
        "/bundle": ("bundle", True),
        "/wallet": ("wallet", True),
    }

    # Track running async tasks for /cancel
    running_tasks: dict[str, asyncio.Task] = {}  # task_id -> Task

    try:
        while True:
            try:
                async with session.get(
                    f"{bot_url}/getUpdates",
                    params={
                        "offset": offset,
                        "timeout": 30,
                        "allowed_updates": json.dumps(
                            ["message", "callback_query"]
                        ),
                    },
                ) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(5)
                        continue
                    data = await resp.json()

                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    # Handle callback queries (inline button presses)
                    cb = update.get("callback_query")
                    if cb:
                        cb_data = cb.get("data", "")
                        cb_msg = cb.get("message", {})
                        cb_chat = cb_msg.get("chat", {}).get("id")
                        if cb_data.startswith("teach_discuss:"):
                            await _handle_teach_discuss(
                                cb_data, cb_chat, sender,
                            )
                        elif cb_data.startswith("teach_view:"):
                            await _handle_teach_view(
                                cb_data, cb_chat, sender,
                            )
                        elif cb_data.startswith("teach_fb:"):
                            await _handle_teach_feedback(
                                cb_data, cb_chat, sender, session, bot_url
                            )
                        cb_id = cb.get("id")
                        if cb_id:
                            try:
                                await session.post(
                                    f"{bot_url}/answerCallbackQuery",
                                    json={"callback_query_id": cb_id},
                                )
                            except Exception:
                                pass
                        continue

                    msg = update.get("message", {})
                    text = (msg.get("text") or "").strip()
                    chat_id = msg.get("chat", {}).get("id")
                    msg_id = msg.get("message_id", 0)

                    if not text or not chat_id:
                        continue

                    # ─── Teach wizard: handle step responses ──
                    if chat_id in _teach_wizard and not text.startswith("/"):
                        wiz = _teach_wizard[chat_id]
                        if time.time() > wiz.get("expires", 0):
                            del _teach_wizard[chat_id]
                        else:
                            await _handle_teach_wizard_step(
                                chat_id, text, wiz, sender, helius,
                                dev_sender=dev_sender, reply_to=msg_id,
                            )
                            continue

                    # ─── Discuss follow-up ──
                    if chat_id in _discuss_sessions and not text.startswith("/"):
                        ds = _discuss_sessions[chat_id]
                        if time.time() > ds.get("expires", 0):
                            del _discuss_sessions[chat_id]
                        else:
                            await _handle_discuss_followup(
                                chat_id, text, sender, reply_to=msg_id,
                            )
                            continue

                    # Parse command name (strip @botname suffix)
                    cmd_name = text.split()[0].lower().split("@")[0]

                    # ─── Static commands ──────────────────
                    if cmd_name in ("/start", "/help"):
                        await sender.send(_HELP_TEXT, reply_to=msg_id)
                        continue

                    if cmd_name == "/status":
                        await _cmd_status(sender, state, portal, helius, reply_to=msg_id)
                        continue

                    if cmd_name == "/keystats":
                        await sender.send(
                            "🔍 Checking API key credits...",
                            reply_to=msg_id,
                        )
                        hh, mh = await asyncio.gather(
                            helius.check_keys_health(force=True),
                            token_client.check_keys_health(force=True),
                        )
                        ks_text = (
                            "🔋 <b>API Key Credits</b>\n\n"
                            f"<b>Helius</b> ({hh['total']} keys)\n"
                            f"  ✅ Active: {hh['active']}\n"
                            f"  💀 No credits: {hh['no_credits']}\n"
                        )
                        if hh['errors']:
                            ks_text += f"  ❓ Errors: {hh['errors']}\n"
                        ks_text += (
                            f"\n<b>Moralis</b> ({mh['total']} keys)\n"
                            f"  ✅ Active: {mh['active']}\n"
                            f"  💀 No credits: {mh['no_credits']}\n"
                        )
                        if mh['errors']:
                            ks_text += f"  ❓ Errors: {mh['errors']}\n"
                        ks_text += "\n<i>Cached 1h. /keystats to recheck.</i>"
                        await sender.send(ks_text, reply_to=msg_id)
                        continue

                    if cmd_name == "/on":
                        state.autoscan_enabled = True
                        await sender.send("✅ Auto scan: <b>ON</b>", reply_to=msg_id)
                        logger.info("Autoscan ON")
                        continue

                    if cmd_name == "/off":
                        state.autoscan_enabled = False
                        await sender.send("⏸ Auto scan: <b>OFF</b>", reply_to=msg_id)
                        logger.info("Autoscan OFF")
                        continue

                    if cmd_name == "/cancel":
                        # Clean up finished tasks first
                        running_tasks = {
                            k: t for k, t in running_tasks.items()
                            if not t.done()
                        }
                        if not running_tasks:
                            await sender.send("ℹ️ Không có lệnh nào đang chạy", reply_to=msg_id)
                            continue
                        cancelled = []
                        for task_id, task in running_tasks.items():
                            task.cancel()
                            cancelled.append(task_id)
                        running_tasks.clear()
                        await sender.send(
                            f"🛑 Đã cancel {len(cancelled)} lệnh:\n"
                            + "\n".join(f"  • {c}" for c in cancelled),
                            reply_to=msg_id,
                        )
                        continue

                    if cmd_name == "/scan":
                        asyncio.create_task(
                            scan_full(token_client, sender, state, helius, dev_sender=dev_sender)
                        )
                        continue

                    if cmd_name == "/setath":
                        parts = text.split(maxsplit=1)
                        if len(parts) > 1:
                            try:
                                new_ath = float(parts[1].strip().replace(",", "").replace("$", ""))
                                state.min_ath = new_ath
                                for ts in state._tokens.values():
                                    ts.ath_checked = False
                                await sender.send(
                                    f"✅ ATH threshold: <b>${state.min_ath:,.0f}</b>\n"
                                    f"Tất cả token sẽ check lại lần scan sau.",
                                    reply_to=msg_id,
                                )
                            except ValueError:
                                await sender.send("❌ VD: <code>/setath 50000</code>", reply_to=msg_id)
                        else:
                            await sender.send(
                                f"ATH threshold: <b>${state.min_ath:,.0f}</b>\n"
                                f"VD: <code>/setath 50000</code>",
                                reply_to=msg_id,
                            )
                        continue

                    if cmd_name == "/cexlist":
                        from db import load_cex_wallets
                        cex = load_cex_wallets()
                        if not cex:
                            await sender.send("DB empty. Run /backfill on laocongquetsan first.", reply_to=msg_id)
                        else:
                            exchange_counts = {}
                            for name in cex.values():
                                exchange_counts[name] = exchange_counts.get(name, 0) + 1
                            breakdown = "\n".join(
                                f"  {name}: {c}" for name, c in sorted(exchange_counts.items(), key=lambda x: -x[1])
                            )
                            await sender.send(
                                f"CEX wallets in DB: {len(cex)}\n\n{breakdown}",
                                reply_to=msg_id,
                            )
                        continue

                    if cmd_name == "/reloadcex":
                        load_dynamic_cex()
                        from db import load_cex_wallets
                        cex = load_cex_wallets()
                        await sender.send(f"Reloaded {len(cex)} CEX wallets.", reply_to=msg_id)
                        continue

                    # ─── Commands with address arg ────────
                    handler_info = command_handlers.get(cmd_name)

                    if handler_info:
                        _, needs_addr = handler_info
                        # Find Solana address anywhere in message (multiline support)
                        _addr_search = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
                        all_addrs = _addr_search.findall(text)
                        # Skip command name fragments, take first valid base58 address
                        addr = ""
                        for _a in all_addrs:
                            if len(_a) >= 32 and _a != cmd_name.lstrip("/"):
                                addr = _a
                                break

                        if needs_addr and not addr:
                            await sender.send(
                                f"Usage: <code>{cmd_name} &lt;address&gt;</code>",
                                reply_to=msg_id,
                            )
                            continue

                        # Extract label for trace commands: text between command and address
                        _label = ""
                        if addr:
                            _rest = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
                            _label = _rest.replace(addr, "").strip().strip("\n").strip()

                        # Route to handler — long-running commands as tasks
                        if cmd_name in ("/devtrace", "/trace1", "/trace2"):
                            if cmd_name == "/devtrace":
                                coro = _cmd_devtrace(addr, sender, helius, dev_sender=dev_sender, reply_to=msg_id)
                            elif cmd_name == "/trace1":
                                coro = _cmd_trace_forward(addr, sender, helius, reply_to=msg_id, label=_label)
                            else:
                                coro = _cmd_trace(addr, sender, helius, reply_to=msg_id)
                            task_id = f"{cmd_name[1:]} {addr[:8]}…"
                            # Clean up finished tasks
                            running_tasks = {
                                k: t for k, t in running_tasks.items()
                                if not t.done()
                            }
                            task = asyncio.create_task(coro)
                            running_tasks[task_id] = task
                        elif cmd_name == "/top":
                            await _cmd_top(addr, sender, analyzer, token_client, reply_to=msg_id)
                        elif cmd_name == "/fresh":
                            await _cmd_fresh(addr, sender, analyzer, reply_to=msg_id)
                        elif cmd_name == "/bundle":
                            await _cmd_bundle(addr, sender, analyzer, reply_to=msg_id)
                        elif cmd_name == "/wallet":
                            await _cmd_wallet(addr, sender, helius, reply_to=msg_id)
                        continue

                    # ─── Teach commands ────────────────
                    if cmd_name == "/teach":
                        parts = text.split(maxsplit=2)
                        if len(parts) >= 3:
                            # Direct mode: /teach <mint> <wallets>
                            await _cmd_teach(text, sender, helius, dev_sender=dev_sender, reply_to=msg_id)
                        else:
                            # Wizard mode: start step-by-step
                            _teach_wizard[chat_id] = {
                                "step": "mint",
                                "expires": time.time() + _TEACH_WIZARD_TIMEOUT,
                            }
                            await sender.send(
                                "🎓 <b>Teach Wizard</b>\n\n"
                                "Bước 1/2 — Gửi <b>mint address</b> của token:",
                                reply_to=msg_id,
                            )
                        continue

                    if cmd_name == "/enddiscuss":
                        if chat_id in _discuss_sessions:
                            del _discuss_sessions[chat_id]
                            await sender.send("💬 Kết thúc thảo luận.", reply_to=msg_id)
                        else:
                            await sender.send("Không có session thảo luận nào.", reply_to=msg_id)
                        continue

                    if cmd_name == "/discuss":
                        parts = text.split(maxsplit=1)
                        if len(parts) > 1:
                            try:
                                cid = int(parts[1].strip())
                                await _handle_teach_discuss(
                                    f"teach_discuss:{cid}", chat_id, sender,
                                )
                            except ValueError:
                                await sender.send("Usage: <code>/discuss &lt;case_id&gt;</code>", reply_to=msg_id)
                        else:
                            await sender.send("Usage: <code>/discuss &lt;case_id&gt;</code>", reply_to=msg_id)
                        continue

                    if cmd_name == "/teachlist":
                        await _cmd_teachlist(sender, reply_to=msg_id)
                        continue

                    if cmd_name == "/teachstats":
                        await _cmd_teachstats(sender, reply_to=msg_id)
                        continue

                    if cmd_name == "/teachremove":
                        parts = text.split(maxsplit=1)
                        if len(parts) > 1:
                            try:
                                cid = int(parts[1].strip())
                                await _cmd_teachremove(cid, sender, reply_to=msg_id)
                            except ValueError:
                                await sender.send("Usage: <code>/teachremove &lt;case_id&gt;</code>", reply_to=msg_id)
                        else:
                            await sender.send("Usage: <code>/teachremove &lt;case_id&gt;</code>", reply_to=msg_id)
                        continue

                    # ─── Bare address = /devtrace ────────
                    if _SOL_ADDR_RE.match(text):
                        task_id = f"devtrace {text[:8]}…"
                        running_tasks = {
                            k: t for k, t in running_tasks.items()
                            if not t.done()
                        }
                        task = asyncio.create_task(
                            _cmd_devtrace(text, sender, helius, dev_sender=dev_sender, reply_to=msg_id)
                        )
                        running_tasks[task_id] = task

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Bot polling error: {e}")
                await asyncio.sleep(5)

    finally:
        await session.close()


# ─── Command Handlers ────────────────────────────────────────


async def _cmd_status(
    sender: TelegramSender,
    state: ScannerState,
    portal: PumpPortalClient,
    helius: Optional[HeliusClient] = None,
    reply_to: int = 0,
) -> None:
    """Show scanner stats + WS status + API keys."""
    ws = portal.get_stats()
    uptime_min = ws["uptime_seconds"] / 60

    last_msg = "N/A"
    if ws["last_message_ago"] is not None:
        last_msg = f"{ws['last_message_ago']:.0f}s ago"

    autoscan_status = "ON ✅" if state.autoscan_enabled else "OFF ⏸"

    text = (
        f"📊 <b>Scanner Status</b>\n\n"
        f"<b>WebSocket:</b>\n"
        f"  Connected: {'✅' if ws['connected'] else '❌'}\n"
        f"  Uptime: {uptime_min:.0f} min\n"
        f"  Tokens received: {ws['tokens_received']}\n"
        f"  Migrations: {ws['migrations_received']}\n"
        f"  Reconnects: {ws['reconnects']}\n"
        f"  Last message: {last_msg}\n\n"
        f"<b>State:</b>\n"
        f"  Tracked: {state.total_tokens}\n"
        f"  ATH checked: {sum(1 for t in state._tokens.values() if t.ath_checked)}\n"
        f"  ATH ≥ ${state.min_ath:,.0f}: {sum(1 for t in state._tokens.values() if t.ath_market_cap >= state.min_ath)}\n"
        f"  DevTraced: {state.analyzed_count}\n"
        f"  Alerted: {state.alerted_count}\n"
        f"  Auto scan: {autoscan_status}\n"
    )

    if helius:
        hs = helius.get_stats()
        text += (
            f"\n<b>Helius API:</b>\n"
            f"  Keys: {hs['total_keys']} total, "
            f"{hs['active_keys']} active, "
            f"{hs['dead_keys']} dead\n"
            f"  Daily calls: {hs['daily_calls']}"
        )
        # Show cached key health if available
        hh = helius._health_cache
        if hh:
            text += (
                f"\n  Health: {hh.get('active', '?')} active / "
                f"{hh.get('no_credits', '?')} no credits"
            )

    await sender.send(text, reply_to=reply_to)


async def _cmd_recent(sender: TelegramSender, state: ScannerState) -> None:
    """Show last 10 interesting tokens (graduated or high volume)."""
    tokens = []
    for ts in state._tokens.values():
        if ts.graduated or ts.volume_24h >= MIN_VOLUME_24H:
            tokens.append(ts)

    tokens.sort(key=lambda t: t.last_updated, reverse=True)
    tokens = tokens[:10]

    if not tokens:
        await sender.send("No interesting tokens yet. WS is still collecting...")
        return

    lines = ["🔥 <b>Recent Interesting Tokens</b>\n"]
    for i, t in enumerate(tokens, 1):
        grad = "🎓" if t.graduated else "🆕"
        vol = f"${t.volume_24h:,.0f}" if t.volume_24h else "—"
        mcap = f"${t.market_cap:,.0f}" if t.market_cap else "—"
        scanned = "✅" if t.analyzed else "⏳"
        lines.append(
            f"{i}. {grad} <b>{html_lib.escape(t.symbol or '???')}</b> {scanned}\n"
            f"   Vol: {vol} | MCap: {mcap}\n"
            f"   <code>{t.address}</code>"
        )

    await sender.send("\n".join(lines))


async def _cmd_scan(
    mint: str,
    analyzer: PumpAnalyzer,
    sender: TelegramSender,
    state: ScannerState,
    token_client: TokenDataClient,
) -> None:
    """Full cluster analysis on a token."""
    await sender.send(f"🔍 Scanning {_wc(mint)}")

    try:
        # Enrich with DexScreener first
        dex_data = await token_client.enrich_tokens([mint])
        dex_info = dex_data.get(mint, {})

        # Upsert into state
        ts = state.upsert_token(address=mint)
        if dex_info:
            state.update_market_data(
                address=mint,
                price=dex_info.get("priceUsd", 0),
                volume_24h=dex_info.get("volume24h", 0),
                txn_count=dex_info.get("txns24h", 0),
                liquidity=dex_info.get("liquidity", 0),
                market_cap=dex_info.get("marketCap", 0),
            )

        # Run PumpAnalyzer
        result = await analyzer.analyze(mint)
        state.mark_analyzed(mint, cluster_count=len(result.clusters))

        if result.error:
            await sender.send(f"⚠️ Analysis error: {result.error}")
            return

        chunks = format_pump_scan_alert(ts, result, dex_info or None)
        await sender.send_chunks(chunks)

    except Exception as e:
        logger.error(f"/scan error for {mint}: {e}", exc_info=True)
        await sender.send(f"❌ Scan failed: {e}")


async def _cmd_dev(
    mint: str,
    sender: TelegramSender,
    helius: HeliusClient,
    token_client: TokenDataClient,
) -> None:
    """Find dev wallet for a token and trace their other launches."""
    await sender.send(f"🕵️ Finding dev for {_wc(mint)}")

    try:
        from pump_analyzer import derive_bonding_curve_pda

        bonding_curve = derive_bonding_curve_pda(mint)

        # Get earliest transactions to find deployer
        txns = await helius.get_token_transactions(
            address=bonding_curve,
            source_filter="PUMP_FUN",
            max_pages=2,
        )

        if not txns:
            await sender.send("❌ No transactions found for this token")
            return

        # Sort by timestamp, first tx fee_payer = dev
        sorted_txns = sorted(txns, key=lambda t: t.get("timestamp", 0))
        dev_wallet = sorted_txns[0].get("feePayer", "")
        deploy_ts = sorted_txns[0].get("timestamp", 0)

        if not dev_wallet:
            await sender.send("❌ Could not identify dev wallet")
            return

        # Get DexScreener data for this token
        dex_data = await token_client.enrich_tokens([mint])
        dex_info = dex_data.get(mint, {})

        # Trace dev's funding source
        key = helius._keys[0] if helius._keys else ""
        funding = None
        if key:
            funding = await helius.get_wallet_incoming_sol(
                wallet=dev_wallet,
                key=key,
                before_timestamp=deploy_ts,
                lookback_hours=72,
            )

        # Check dev's other Pump.fun activity (recent txns)
        dev_txns = await helius.get_parsed_transactions(
            address=dev_wallet,
            tx_type="SWAP",
            max_pages=3,
        )

        # Find unique tokens dev interacted with
        dev_tokens: set[str] = set()
        for tx in (dev_txns or []):
            for event in tx.get("events", {}).get("swap", {}).get("tokenInputs", []):
                addr = event.get("mint", "")
                if addr and addr != mint:
                    dev_tokens.add(addr)
            for event in tx.get("events", {}).get("swap", {}).get("tokenOutputs", []):
                addr = event.get("mint", "")
                if addr and addr != mint:
                    dev_tokens.add(addr)
            # Also check token transfers
            for tt in tx.get("tokenTransfers", []):
                addr = tt.get("mint", "")
                if addr and addr != mint:
                    dev_tokens.add(addr)

        # Format response
        deploy_time = ""
        if deploy_ts:
            deploy_time = datetime.fromtimestamp(
                deploy_ts, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")

        text = (
            f"🕵️ <b>Dev Wallet Analysis</b>\n\n"
            f"Token: <code>{mint}</code>\n"
        )

        if dex_info:
            name = dex_info.get("name", "")
            text += (
                f"Price: ${dex_info.get('priceUsd', 0):,.6f}\n"
                f"MCap: ${dex_info.get('marketCap', 0):,.0f}\n"
                f"Vol 24h: ${dex_info.get('volume24h', 0):,.0f}\n\n"
            )

        text += (
            f"<b>Dev:</b> <code>{dev_wallet}</code>\n"
            f"Deployed: {deploy_time}\n"
            f"<a href='https://solscan.io/account/{dev_wallet}'>Solscan</a> | "
            f"<a href='https://dexscreener.com/solana/{mint}'>DexScreener</a>\n"
        )

        # Funding source
        if funding:
            from pump_analyzer import lookup_cex_name
            cex = lookup_cex_name(funding["from_wallet"])
            source_label = cex or f"{_wc(funding['from_wallet'])}"
            text += (
                f"\n💰 <b>Dev Funding:</b>\n"
                f"  Source: {source_label}\n"
                f"  Amount: {funding['amount_sol']:.3f} SOL\n"
            )
        else:
            text += "\n💰 <b>Dev Funding:</b> No CEX trace found\n"

        # Other tokens
        if dev_tokens:
            text += f"\n🪙 <b>Dev's other tokens ({len(dev_tokens)}):</b>\n"
            for i, addr in enumerate(list(dev_tokens)[:5], 1):
                text += f"  {i}. <code>{addr}</code>\n"
            if len(dev_tokens) > 5:
                text += f"  <i>... +{len(dev_tokens) - 5} more</i>\n"
        else:
            text += "\n🪙 <b>Dev's other tokens:</b> None found\n"

        # First N buyers after dev
        buy_count = min(len(sorted_txns) - 1, 5)
        if buy_count > 0:
            text += f"\n⚡ <b>First {buy_count} buyers after dev:</b>\n"
            for i, tx in enumerate(sorted_txns[1:buy_count + 1], 1):
                buyer = tx.get("feePayer", "")
                sol = 0.0
                for nt in tx.get("nativeTransfers", []):
                    if nt.get("toUserAccount") == bonding_curve:
                        sol += nt.get("amount", 0) / 1_000_000_000
                delta = tx.get("timestamp", 0) - deploy_ts
                text += (
                    f"  {i}. {_wc(buyer)} "
                    f"{sol:.3f} SOL (+{delta}s)\n"
                )

        await sender.send(text)

    except Exception as e:
        logger.error(f"/dev error for {mint}: {e}", exc_info=True)
        await sender.send(f"❌ Dev lookup failed: {e}")


async def _cmd_top(
    mint: str,
    sender: TelegramSender,
    analyzer: PumpAnalyzer,
    token_client: TokenDataClient,
    reply_to: int = 0,
) -> None:
    """Top holders + concentration analysis."""
    await sender.send(f"📊 Analyzing top holders for {_wc(mint)}", reply_to=reply_to)

    try:
        from pump_analyzer import derive_bonding_curve_pda

        bonding_curve = derive_bonding_curve_pda(mint)

        txns = await analyzer.helius.get_token_transactions(
            address=bonding_curve,
            source_filter="PUMP_FUN",
            max_pages=50,
        )

        if not txns:
            await sender.send("❌ No transactions found")
            return

        buys = analyzer._parse_buyers(txns, bonding_curve)
        if not buys:
            await sender.send("❌ No buy transactions found")
            return

        # Aggregate by wallet
        wallet_sol: dict[str, float] = {}
        wallet_first_buy: dict[str, int] = {}
        wallet_buy_count: dict[str, int] = {}
        for buy in buys:
            wallet_sol[buy.wallet] = wallet_sol.get(buy.wallet, 0) + buy.sol_spent
            wallet_buy_count[buy.wallet] = wallet_buy_count.get(buy.wallet, 0) + 1
            if buy.wallet not in wallet_first_buy:
                wallet_first_buy[buy.wallet] = buy.buy_index

        total_sol = sum(wallet_sol.values())
        sorted_wallets = sorted(wallet_sol.items(), key=lambda x: x[1], reverse=True)

        # Concentration metrics
        top5_sol = sum(s for _, s in sorted_wallets[:5])
        top10_sol = sum(s for _, s in sorted_wallets[:10])
        top5_pct = (top5_sol / total_sol * 100) if total_sol else 0
        top10_pct = (top10_sol / total_sol * 100) if total_sol else 0

        # DexScreener data
        dex_data = await token_client.enrich_tokens([mint])
        dex_info = dex_data.get(mint, {})

        text = (
            f"📊 <b>Top Holders Analysis</b>\n\n"
            f"Token: <code>{mint}</code>\n"
        )

        if dex_info:
            text += (
                f"MCap: ${dex_info.get('marketCap', 0):,.0f} | "
                f"Liq: ${dex_info.get('liquidity', 0):,.0f}\n"
            )

        text += (
            f"\nTotal buys: <b>{len(buys)}</b> "
            f"({len(wallet_sol)} unique wallets)\n"
            f"Total SOL: <b>{total_sol:.2f}</b>\n\n"
        )

        # Concentration warning
        if top5_pct >= 50:
            text += f"🚨 <b>HIGH CONCENTRATION</b>\n"
        elif top5_pct >= 30:
            text += f"⚠️ <b>MODERATE CONCENTRATION</b>\n"
        else:
            text += f"✅ <b>LOW CONCENTRATION</b>\n"

        text += (
            f"  Top 5: <b>{top5_pct:.1f}%</b> ({top5_sol:.2f} SOL)\n"
            f"  Top 10: <b>{top10_pct:.1f}%</b> ({top10_sol:.2f} SOL)\n\n"
        )

        # Top 15 holders
        text += "<b>Top 15 Holders:</b>\n"
        for i, (wallet, sol) in enumerate(sorted_wallets[:15], 1):
            pct = (sol / total_sol * 100) if total_sol else 0
            cnt = wallet_buy_count.get(wallet, 0)
            idx = wallet_first_buy.get(wallet, 0)
            text += (
                f"  {i:2d}. {_wc(wallet)} "
                f"<b>{sol:.3f}</b> SOL ({pct:.1f}%) "
                f"x{cnt} buy #{idx}\n"
            )

        await sender.send(text)

    except Exception as e:
        logger.error(f"/top error for {mint}: {e}", exc_info=True)
        await sender.send(f"❌ Top holders failed: {e}")


async def _cmd_fresh(
    mint: str,
    sender: TelegramSender,
    analyzer: PumpAnalyzer,
    reply_to: int = 0,
) -> None:
    """Detect fresh wallet buyers — wallets created just to buy this token."""
    await sender.send(f"🆕 Checking fresh wallets for {_wc(mint)}", reply_to=reply_to)

    try:
        from pump_analyzer import derive_bonding_curve_pda

        bonding_curve = derive_bonding_curve_pda(mint)

        txns = await analyzer.helius.get_token_transactions(
            address=bonding_curve,
            source_filter="PUMP_FUN",
            max_pages=20,
        )

        if not txns:
            await sender.send("❌ No transactions found")
            return

        buys = analyzer._parse_buyers(txns, bonding_curve)
        if not buys:
            await sender.send("❌ No buy transactions found")
            return

        # Check each buyer — is their first-ever tx this token buy?
        unique_wallets = list(dict.fromkeys(b.wallet for b in buys))[:50]

        semaphore = asyncio.Semaphore(15)
        fresh_wallets: list[dict] = []
        num_keys = len(analyzer.helius._keys)

        async def _check_fresh(idx: int, wallet: str) -> Optional[dict]:
            key = analyzer.helius._keys[idx % num_keys]
            async with semaphore:
                url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
                params = {"api-key": key, "limit": 5}
                try:
                    session = await analyzer.helius._get_session()
                    async with session.get(url, params=params) as resp:
                        if resp.status != 200:
                            return None
                        wallet_txns = await resp.json()
                except Exception:
                    return None

            if not wallet_txns:
                return None

            # If wallet has <= 3 total txns, it's fresh
            if len(wallet_txns) <= 3:
                # Find their buy info
                for buy in buys:
                    if buy.wallet == wallet:
                        return {
                            "wallet": wallet,
                            "sol_spent": buy.sol_spent,
                            "buy_index": buy.buy_index,
                            "total_txns": len(wallet_txns),
                        }
            return None

        tasks = [
            _check_fresh(i, w) for i, w in enumerate(unique_wallets)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, dict):
                fresh_wallets.append(r)

        # Sort by buy index (earliest first)
        fresh_wallets.sort(key=lambda x: x["buy_index"])

        total_fresh_sol = sum(f["sol_spent"] for f in fresh_wallets)
        total_sol = sum(b.sol_spent for b in buys)
        fresh_pct = (total_fresh_sol / total_sol * 100) if total_sol else 0

        text = (
            f"🆕 <b>Fresh Wallet Analysis</b>\n\n"
            f"Token: <code>{mint}</code>\n"
            f"Checked: {len(unique_wallets)} wallets\n\n"
        )

        if len(fresh_wallets) >= 5:
            text += f"🚨 <b>HIGH FRESH WALLET COUNT</b>\n"
        elif len(fresh_wallets) >= 3:
            text += f"⚠️ <b>SUSPICIOUS FRESH WALLETS</b>\n"
        else:
            text += f"✅ <b>LOW FRESH WALLET COUNT</b>\n"

        text += (
            f"  Fresh wallets: <b>{len(fresh_wallets)}</b>/{len(unique_wallets)}\n"
            f"  Fresh SOL: <b>{total_fresh_sol:.3f}</b> SOL "
            f"({fresh_pct:.1f}% of total)\n\n"
        )

        if fresh_wallets:
            text += "<b>Fresh Wallets:</b>\n"
            for i, fw in enumerate(fresh_wallets[:15], 1):
                text += (
                    f"  {i}. {_wc(fw['wallet'])} "
                    f"{fw['sol_spent']:.3f} SOL "
                    f"(buy #{fw['buy_index']}, {fw['total_txns']} txns)\n"
                )
            if len(fresh_wallets) > 15:
                text += f"  <i>... +{len(fresh_wallets) - 15} more</i>\n"
        else:
            text += "No fresh wallets detected."

        await sender.send(text)

    except Exception as e:
        logger.error(f"/fresh error for {mint}: {e}", exc_info=True)
        await sender.send(f"❌ Fresh wallet check failed: {e}")


async def _cmd_bundle(
    mint: str,
    sender: TelegramSender,
    analyzer: PumpAnalyzer,
    reply_to: int = 0,
) -> None:
    """Detect bundled buys — multiple buys in same block/slot."""
    await sender.send(f"📦 Checking bundles for {_wc(mint)}", reply_to=reply_to)

    try:
        from pump_analyzer import derive_bonding_curve_pda

        bonding_curve = derive_bonding_curve_pda(mint)

        txns = await analyzer.helius.get_token_transactions(
            address=bonding_curve,
            source_filter="PUMP_FUN",
            max_pages=20,
        )

        if not txns:
            await sender.send("❌ No transactions found")
            return

        buys = analyzer._parse_buyers(txns, bonding_curve)
        if not buys:
            await sender.send("❌ No buy transactions found")
            return

        # Group buys by timestamp (same second = likely same block)
        ts_groups: dict[int, list[TokenBuy]] = {}
        for buy in buys:
            ts_groups.setdefault(buy.timestamp, []).append(buy)

        # Find bundles: 2+ buys in exact same second
        bundles = [
            (ts, group) for ts, group in sorted(ts_groups.items())
            if len(group) >= 2
        ]

        # Also check 2-second window bundles
        sorted_buys = sorted(buys, key=lambda b: b.timestamp)
        window_bundles: list[list[TokenBuy]] = []
        i = 0
        while i < len(sorted_buys):
            group = [sorted_buys[i]]
            j = i + 1
            while j < len(sorted_buys) and sorted_buys[j].timestamp - sorted_buys[i].timestamp <= 2:
                group.append(sorted_buys[j])
                j += 1
            if len(group) >= 3:
                window_bundles.append(group)
                i = j
            else:
                i += 1

        total_bundle_sol = sum(
            b.sol_spent for _, group in bundles for b in group
        )
        total_sol = sum(b.sol_spent for b in buys)
        bundle_pct = (total_bundle_sol / total_sol * 100) if total_sol else 0

        text = (
            f"📦 <b>Bundle Detection</b>\n\n"
            f"Token: <code>{mint}</code>\n"
            f"Total buys: {len(buys)}\n\n"
        )

        if len(bundles) >= 3:
            text += f"🚨 <b>HEAVY BUNDLING DETECTED</b>\n"
        elif bundles:
            text += f"⚠️ <b>BUNDLES DETECTED</b>\n"
        else:
            text += f"✅ <b>NO BUNDLES</b>\n"

        text += (
            f"  Same-second groups: <b>{len(bundles)}</b>\n"
            f"  Bundled SOL: <b>{total_bundle_sol:.3f}</b> "
            f"({bundle_pct:.1f}%)\n"
            f"  2s window groups: <b>{len(window_bundles)}</b>\n\n"
        )

        if bundles:
            text += "<b>Bundles:</b>\n"
            for i, (ts, group) in enumerate(bundles[:10], 1):
                t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
                wallets = set(b.wallet for b in group)
                sol = sum(b.sol_spent for b in group)
                text += (
                    f"\n  <b>Bundle {i}</b> @ {t} "
                    f"({len(group)} buys, {len(wallets)} wallets, "
                    f"{sol:.3f} SOL)\n"
                )
                for b in group[:5]:
                    text += (
                        f"    • {_wc(b.wallet)} "
                        f"{b.sol_spent:.3f} SOL (#{b.buy_index})\n"
                    )
                if len(group) > 5:
                    text += f"    <i>... +{len(group) - 5} more</i>\n"

        await sender.send(text)

    except Exception as e:
        logger.error(f"/bundle error for {mint}: {e}", exc_info=True)
        await sender.send(f"❌ Bundle check failed: {e}")


async def _cmd_trace_forward(
    wallet: str,
    sender: TelegramSender,
    helius: HeliusClient,
    reply_to: int = 0,
    label: str = "",
) -> None:
    """Trace forward — BFS: follow ALL outgoing SOL transfers, up to 5 hops."""
    await sender.send(f"➡️ Đuổi tiến {_wc(wallet)}", reply_to=reply_to)

    try:
        from trace_bfs import trace_forward
        all_wallets = await trace_forward(helius, wallet)

        if len(all_wallets) <= 1:
            await sender.send("Không tìm thấy ví đích.", reply_to=reply_to)
        elif label:
            add_block = "\n".join(f"/add {w} {label} {idx}" for idx, w in enumerate(all_wallets, 1))
            await sender.send(f"<pre>{add_block}</pre>", reply_to=reply_to)
            wallet_block = "\n".join(all_wallets)
            await sender.send(f"<pre>{wallet_block}</pre>", reply_to=reply_to)
        else:
            wallet_block = "\n".join(all_wallets)
            await sender.send(f"<pre>{wallet_block}</pre>", reply_to=reply_to)

    except Exception as e:
        logger.error(f"/trace1 error for {wallet}: {e}", exc_info=True)
        await sender.send(f"❌ Trace forward failed: {e}")


async def _cmd_trace(
    wallet: str,
    sender: TelegramSender,
    helius: HeliusClient,
    reply_to: int = 0,
) -> None:
    """Trace backward — where does money come FROM (funding source)."""
    await sender.send(f"⬅️ Đuổi lùi {_wc(wallet)}", reply_to=reply_to)

    try:
        from pump_analyzer import lookup_cex_name

        chain: list[dict] = []
        current = wallet
        visited: set[str] = set()

        # Trace up to 5 hops back
        for hop in range(5):
            if current in visited:
                break
            visited.add(current)

            import random as _rnd
            key = _rnd.choice(helius._keys) if helius._keys else ""

            funding = await helius.get_wallet_incoming_sol(
                wallet=current,
                key=key,
                lookback_hours=168,  # 7 days
            )

            if not funding:
                break

            cex = lookup_cex_name(funding["from_wallet"])
            chain.append({
                "from": funding["from_wallet"],
                "to": current,
                "amount": funding["amount_sol"],
                "cex": cex,
            })

            if cex:
                break  # Found CEX, stop tracing

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

            final_source = chain[-1]
            if final_source["cex"]:
                text += f"\n✅ Origin: 🏦 <b>{final_source['cex']}</b>"
            else:
                text += f"\n❓ Origin: Unknown (no CEX match in {len(chain)} hops)"

        await sender.send(text)

    except Exception as e:
        logger.error(f"/trace error for {wallet}: {e}", exc_info=True)
        await sender.send(f"❌ Trace failed: {e}")


async def _cmd_devtrace(
    mint: str,
    sender: TelegramSender,
    helius: HeliusClient,
    dev_sender: TelegramSender = None,
    reply_to: int = 0,
) -> None:
    """Trace dev wallets for a Pump.fun token — full 7-step pipeline."""
    await sender.send(
        f"🕵️ Tracing dev wallets for {_wc(mint)}\n"
        f"⏳ Fetching bonding curve buyers...",
        reply_to=reply_to,
    )

    try:
        from dev_tracer import DevTracer
        from dev_tracer_fmt import format_trace_result

        tracer = DevTracer(helius=helius)
        result = await tracer.trace(mint)

        messages = format_trace_result(result)

        # Send result back to the chat where command was issued
        for msg in messages:
            await sender.send(msg)

    except Exception as e:
        logger.error(f"/devtrace error for {mint}: {e}", exc_info=True)
        await sender.send(f"❌ Dev trace failed: {e}")


async def _cmd_wallet(
    wallet: str,
    sender: TelegramSender,
    helius: HeliusClient,
    reply_to: int = 0,
) -> None:
    """Wallet activity summary — recent transactions, SOL balance hint."""
    await sender.send(f"👛 Checking wallet {_wc(wallet)}", reply_to=reply_to)

    try:
        # Get recent transactions
        txns = await helius.get_parsed_transactions(
            address=wallet,
            tx_type="",
            max_pages=2,
        )

        if not txns:
            await sender.send("❌ No transactions found for this wallet")
            return

        # Analyze activity
        total_txns = len(txns)
        pump_txns = 0
        swap_txns = 0
        sol_sent = 0.0
        sol_received = 0.0
        tokens_interacted: set[str] = set()

        for tx in txns:
            source = tx.get("source", "")
            if source == "PUMP_FUN":
                pump_txns += 1
            tx_type = tx.get("type", "")
            if tx_type == "SWAP":
                swap_txns += 1

            for nt in tx.get("nativeTransfers", []):
                if nt.get("fromUserAccount") == wallet:
                    sol_sent += nt.get("amount", 0) / 1_000_000_000
                if nt.get("toUserAccount") == wallet:
                    sol_received += nt.get("amount", 0) / 1_000_000_000

            for tt in tx.get("tokenTransfers", []):
                mint = tt.get("mint", "")
                if mint:
                    tokens_interacted.add(mint)

        # Time range
        timestamps = [tx.get("timestamp", 0) for tx in txns if tx.get("timestamp")]
        first_ts = min(timestamps) if timestamps else 0
        last_ts = max(timestamps) if timestamps else 0
        first_time = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y-%m-%d") if first_ts else "?"
        last_time = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if last_ts else "?"

        text = (
            f"👛 <b>Wallet Summary</b>\n\n"
            f"Address: <code>{wallet}</code>\n"
            f"<a href='https://solscan.io/account/{wallet}'>Solscan</a>\n\n"
            f"<b>Activity (last {total_txns} txns):</b>\n"
            f"  First seen: {first_time}\n"
            f"  Last active: {last_time}\n"
            f"  Pump.fun txns: <b>{pump_txns}</b>\n"
            f"  Swap txns: <b>{swap_txns}</b>\n"
            f"  Tokens touched: <b>{len(tokens_interacted)}</b>\n"
            f"  SOL sent: <b>{sol_sent:.3f}</b>\n"
            f"  SOL received: <b>{sol_received:.3f}</b>\n"
        )

        # Pump.fun activity assessment
        if pump_txns > total_txns * 0.7:
            text += "\n🚨 <b>PUMP.FUN FOCUSED</b> — mostly degen activity"
        elif pump_txns > 0:
            text += f"\n⚠️ Pump.fun activity: {pump_txns}/{total_txns} txns"

        if total_txns <= 5:
            text += "\n🆕 <b>FRESH WALLET</b> — very low activity"

        await sender.send(text)

    except Exception as e:
        logger.error(f"/wallet error for {wallet}: {e}", exc_info=True)
        await sender.send(f"❌ Wallet check failed: {e}")


# ─── Teach Wizard Step Handler ───────────────────────────────


async def _handle_teach_wizard_step(
    chat_id: int,
    text: str,
    wiz: dict,
    sender: "TelegramSender",
    helius: "HeliusClient",
    dev_sender: "TelegramSender" = None,
    reply_to: int = 0,
) -> None:
    """Handle a teach wizard step response."""
    step = wiz["step"]

    if step == "mint":
        mint = text.strip()
        if not _SOL_ADDR_RE.match(mint):
            await sender.send(
                "❌ Địa chỉ không hợp lệ. Gửi lại <b>mint address</b>:",
                reply_to=reply_to,
            )
            return
        wiz["mint"] = mint
        wiz["step"] = "wallets"
        wiz["expires"] = time.time() + _TEACH_WIZARD_TIMEOUT
        await sender.send(
            f"✅ Mint: {_wc(mint)}\n\n"
            f"Bước 2/2 — Gửi <b>danh sách ví dev</b> (cách nhau bằng dấu phẩy hoặc xuống dòng):",
            reply_to=reply_to,
        )
        return

    if step == "wallets":
        # Accept comma or newline separated
        raw = text.replace("\n", ",")
        taught_wallets = [w.strip() for w in raw.split(",") if w.strip()]
        invalid = [w for w in taught_wallets if not _SOL_ADDR_RE.match(w)]

        if invalid:
            await sender.send(
                f"❌ Ví không hợp lệ: <code>{invalid[0][:20]}...</code>\n"
                f"Gửi lại danh sách ví dev:",
                reply_to=reply_to,
            )
            return

        if not taught_wallets:
            await sender.send(
                "❌ Cần ít nhất 1 ví dev. Gửi lại:",
                reply_to=reply_to,
            )
            return

        mint = wiz["mint"]
        del _teach_wizard[chat_id]

        # Build the /teach command text and call existing handler
        wallets_str = ",".join(taught_wallets)
        cmd_text = f"/teach {mint} {wallets_str}"
        await _cmd_teach(cmd_text, sender, helius, dev_sender=dev_sender, reply_to=reply_to)


# ─── Teach Command Handlers ──────────────────────────────────


async def _cmd_teach(
    text: str,
    sender: TelegramSender,
    helius: HeliusClient,
    dev_sender: TelegramSender = None,
    reply_to: int = 0,
) -> None:
    """Handle /teach <mint> <wallet1,wallet2,...>"""
    import html as html_mod
    from teach_pipeline import TeachingPipeline, TeachResult
    import teach_store

    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        await sender.send(
            "Usage: <code>/teach &lt;mint&gt; &lt;wallet1,wallet2,...&gt;</code>\n\n"
            "VD: <code>/teach So11...xyz AbC1...w1,DeF2...w2</code>"
        )
        return

    mint = parts[1].strip()
    wallets_raw = parts[2].strip()

    if not _SOL_ADDR_RE.match(mint):
        await sender.send(f"❌ Invalid mint: <code>{mint[:50]}</code>")
        return

    taught_wallets = [
        w.strip() for w in wallets_raw.split(",") if w.strip()
    ]
    invalid = [w for w in taught_wallets if not _SOL_ADDR_RE.match(w)]
    if invalid:
        await sender.send(
            f"❌ Invalid wallet(s): <code>{invalid[0][:20]}...</code>"
        )
        return

    if not taught_wallets:
        await sender.send("❌ Cần ít nhất 1 ví dev.")
        return

    await sender.send(
        f"🎓 Bắt đầu học token {_wc(mint)} "
        f"({len(taught_wallets)} ví dev)",
        reply_to=reply_to,
    )

    pipeline = TeachingPipeline(helius=helius)
    alert_target = dev_sender or sender

    async def on_progress(step: int, msg: str):
        if step in (0, 1, 7, 8, 9, 10):
            await sender.send(f"⏳ Step {step}: {msg}")

    result = await pipeline.teach(
        mint=mint,
        taught_wallets=taught_wallets,
        on_progress=on_progress,
    )

    if result.error:
        await sender.send(f"❌ Teach failed: {result.error}")
        return

    # Format result message
    name = html_mod.escape(result.token_name or "Unknown")
    symbol = html_mod.escape(result.token_symbol or "???")

    pattern_types = set(p["pattern_type"] for p in result.patterns)
    pattern_str = ", ".join(sorted(pattern_types)) if pattern_types else "none"

    text_result = (
        f"✅ <b>TEACH RESULT — {name} (${symbol})</b>\n\n"
        f"📚 Ví cung cấp: <b>{len(result.taught_wallets)}</b>  |  "
        f"🔍 Phát hiện thêm: <b>{len(result.discovered_wallets)}</b>\n"
        f"💰 Funders: <b>{len(result.funders)}</b>  |  "
        f"📥 Collectors: <b>{len(result.collectors)}</b>\n"
        f"🔑 Patterns: {pattern_str}\n"
        f"Case ID: <b>#{result.case_id}</b>"
    )

    # Send result with inline keyboard for feedback — same chat where /teach was sent
    bot_url = f"{TELEGRAM_API}/bot{PUMP_BOT_TOKEN}"
    cid = result.case_id
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📋 Wallets", "callback_data": f"teach_view:{cid}:wallets"},
                {"text": "💰 Funders", "callback_data": f"teach_view:{cid}:funders"},
                {"text": "📥 Collectors", "callback_data": f"teach_view:{cid}:collectors"},
            ],
            [
                {"text": "💬 Thảo luận", "callback_data": f"teach_discuss:{cid}"},
                {"text": "✅ Hữu ích", "callback_data": f"teach_fb:{cid}:good"},
                {"text": "❌ Không chính xác", "callback_data": f"teach_fb:{cid}:bad"},
            ],
        ]
    }

    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"{bot_url}/sendMessage",
                json={
                    "chat_id": sender._chat_id,
                    "text": text_result,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": keyboard,
                },
            )
    except Exception:
        # Fallback: send without buttons
        await sender.send(text_result)


def _md_to_tg_html(md_text: str) -> str:
    """Convert Claude markdown output to Telegram HTML for readability."""
    import html as _html
    import re as _re

    lines = md_text.split("\n")
    result = []
    in_code_block = False

    for line in lines:
        # Code blocks ```
        if line.strip().startswith("```"):
            if in_code_block:
                result.append("</code></pre>")
                in_code_block = False
            else:
                result.append("<pre><code>")
                in_code_block = True
            continue

        if in_code_block:
            result.append(_html.escape(line))
            continue

        # Escape HTML first
        line = _html.escape(line)

        # Headers: ## → bold with emoji
        if line.startswith("### "):
            line = f"\n<b>▸ {line[4:]}</b>"
        elif line.startswith("## "):
            line = f"\n<b>◆ {line[3:]}</b>"
        elif line.startswith("# "):
            line = f"\n<b>━━ {line[2:]} ━━</b>"

        # Bold: **text** → <b>text</b>
        line = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', line)
        # Italic: *text* → <i>text</i>
        line = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', line)
        # Inline code: `text` → <code>text</code>
        line = _re.sub(r'`(.+?)`', r'<code>\1</code>', line)

        # Bullet points: - item → • item
        stripped = line.lstrip()
        if stripped.startswith("- "):
            indent = len(line) - len(stripped)
            spaces = "  " * (indent // 2)
            line = f"{spaces}• {stripped[2:]}"

        # Numbered lists with bold number
        m = _re.match(r'^(\d+)\.\s+(.+)', stripped)
        if m:
            line = f"<b>{m.group(1)}.</b> {m.group(2)}"

        # Horizontal rules
        if _re.match(r'^-{3,}$', stripped) or _re.match(r'^\*{3,}$', stripped):
            line = "─" * 20

        result.append(line)

    # Close unclosed code block
    if in_code_block:
        result.append("</code></pre>")

    return "\n".join(result)


def _split_html_chunks(text: str, max_len: int = 3800) -> list[str]:
    """Split text into chunks ≤ max_len, splitting at newlines."""
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            if current.strip():
                chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks or [text[:max_len]]


# Discuss sessions: chat_id → {"case_id": int, "session_id": str, "expires": float}
_discuss_sessions: dict[int, dict] = {}
_DISCUSS_TIMEOUT = 600  # 10 minutes


async def _handle_teach_discuss(
    cb_data: str,
    chat_id: int,
    sender: TelegramSender,
) -> None:
    """Run Claude Code -p with teach case context, send response to Telegram."""
    import subprocess
    import teach_store

    try:
        parts = cb_data.split(":")
        if len(parts) != 2:
            return
        case_id = int(parts[1])

        case = teach_store.get_case(case_id)
        if not case:
            await sender.send(f"❌ Case #{case_id} not found.")
            return

        name = case.token_name or case.token_symbol or "Unknown"
        symbol = case.token_symbol or "???"

        await sender.send(f"💬 Đang phân tích case #{case_id} — <b>{name}</b> (${symbol})...")

        # Build full context prompt (inline — no file reads needed)
        taught_list = "\n".join(f"  - {w}" for w in case.taught_wallets[:50])
        discovered_list = "\n".join(f"  - {w}" for w in case.discovered_wallets[:50])
        funder_list = "\n".join(f"  - {w}" for w in case.funders[:20])
        collector_list = "\n".join(f"  - {w}" for w in case.collectors[:20])

        # Query patterns from DB
        patterns_info = ""
        try:
            import teach_store as _ts
            patterns = _ts.get_patterns_for_case(case_id)
            if patterns:
                pat_lines = []
                for p in patterns:
                    pat_lines.append(
                        f"  - type={p.get('pattern_type','?')}, "
                        f"value={p.get('pattern_value','?')[:20]}..., "
                        f"confidence={p.get('confidence', 0):.2f}"
                    )
                patterns_info = f"\nPatterns ({len(patterns)}):\n" + "\n".join(pat_lines)
        except Exception:
            pass

        prompt = (
            f"Ban la chuyen gia phan tich on-chain Solana. "
            f"Phan tich teach case sau va tra loi bang tieng Viet.\n\n"
            f"=== CASE #{case_id} ===\n"
            f"Token: {name} (${symbol})\n"
            f"Mint: {case.mint}\n"
            f"Feedback: {case.feedback or 'chua co'}\n\n"
            f"Taught wallets ({len(case.taught_wallets)}):\n{taught_list}\n\n"
            f"Discovered wallets ({len(case.discovered_wallets)}):\n{discovered_list}\n\n"
            f"Funders ({len(case.funders)}):\n{funder_list}\n\n"
            f"Collectors ({len(case.collectors)}):\n{collector_list}\n"
            f"{patterns_info}\n\n"
            f"Hay phan tich:\n"
            f"1. Ai la controller chinh (funder/collector chung)?\n"
            f"2. Pattern nao noi bat (funder chung, collector chung, wallet reuse)?\n"
            f"3. Risk score (1-10) va ly do\n"
            f"4. Goi y hanh dong thuc chien:\n"
            f"   - Lam sao bat duoc keo SAU cua nhom nay? (theo doi vi nao, set alert gi)\n"
            f"   - Vi sàn (CEX deposit/withdraw) cua chung la gi? Da tim ra chua?\n"
            f"   - Thoi quen cua nhom: thoi gian deploy, kieu token, muc tien, thoi gian dump\n"
            f"   - Note quan trong can ghi nho de nhan dien nhom nay lan sau\n"
            f"Tra loi bang tieng Viet, ngan gon, ro rang, thuc chien."
        )

        scanner_dir = os.path.dirname(os.path.abspath(__file__))

        # Run claude -p with prompt directly (no tool use needed)
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["claude", "-p", "--max-turns", "3", prompt],
                cwd=scanner_dir,
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
            ),
        )

        output = (result.stdout or "").strip()
        if not output:
            output = (result.stderr or "No response from Claude.").strip()

        # Extract session ID for follow-up (from stderr)
        session_id = ""
        for line in (result.stderr or "").splitlines():
            if "session:" in line.lower() or "resume" in line.lower():
                # Try to extract UUID-like session ID
                import re as _re
                uuids = _re.findall(r'[0-9a-f]{8}-[0-9a-f-]{27,}', line)
                if uuids:
                    session_id = uuids[0]
                    break

        # Save discuss session for follow-up
        _discuss_sessions[chat_id] = {
            "case_id": case_id,
            "session_id": session_id,
            "name": name,
            "symbol": symbol,
            "expires": time.time() + _DISCUSS_TIMEOUT,
        }

        # Send response to Telegram — convert markdown → Telegram HTML
        import html as html_mod
        header = f"💬 <b>Case #{case_id} — {html_mod.escape(name)}</b>\n\n"
        formatted = _md_to_tg_html(output)
        full_text = header + formatted

        # Split into chunks ≤ 3800 chars
        chunks = _split_html_chunks(full_text, 3800)
        for chunk in chunks:
            await sender.send(chunk)

        if session_id:
            await sender.send(
                f"💬 Reply bất kỳ message nào để tiếp tục thảo luận.\n"
                f"Gõ <code>/enddiscuss</code> để kết thúc."
            )
        else:
            await sender.send(
                f"💬 Gõ <code>/discuss {case_id}</code> để hỏi thêm."
            )

    except subprocess.TimeoutExpired:
        await sender.send("⏳ Claude timeout (2 phút). Thử lại với case nhỏ hơn.")
    except Exception as e:
        logger.error(f"Teach discuss error: {e}")
        await sender.send(f"❌ Lỗi: {e}")


async def _handle_discuss_followup(
    chat_id: int,
    text: str,
    sender: TelegramSender,
    reply_to: int = 0,
) -> None:
    """Handle follow-up messages in discuss mode."""
    import subprocess
    import teach_store

    session = _discuss_sessions.get(chat_id)
    if not session:
        return

    session["expires"] = time.time() + _DISCUSS_TIMEOUT
    scanner_dir = os.path.dirname(os.path.abspath(__file__))

    await sender.send("💬 Đang xử lý...", reply_to=reply_to)

    try:
        # Build context inline so Claude doesn't need tools
        case_id = session["case_id"]
        case = teach_store.get_case(case_id)
        context = ""
        if case:
            taught_list = ", ".join(w[:8] + "..." for w in case.taught_wallets[:30])
            discovered_list = ", ".join(w[:8] + "..." for w in case.discovered_wallets[:30])
            funder_list = ", ".join(w[:8] + "..." for w in case.funders[:15])
            collector_list = ", ".join(w[:8] + "..." for w in case.collectors[:15])
            context = (
                f"Context: Case #{case_id}, Token: {case.token_name} (${case.token_symbol}), "
                f"Mint: {case.mint}\n"
                f"Taught wallets ({len(case.taught_wallets)}): {taught_list}\n"
                f"Discovered ({len(case.discovered_wallets)}): {discovered_list}\n"
                f"Funders ({len(case.funders)}): {funder_list}\n"
                f"Collectors ({len(case.collectors)}): {collector_list}\n"
                f"Full wallets:\n" + "\n".join(case.taught_wallets[:50]) + "\n"
                + "\n".join(case.discovered_wallets[:50]) + "\n\n"
            )

        prompt = (
            f"{context}"
            f"User hoi: {text}\n\n"
            f"Tra loi bang tieng Viet, ngan gon, ro rang."
        )
        cmd = ["claude", "-p", "--max-turns", "3", prompt]

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                cmd,
                cwd=scanner_dir,
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
            ),
        )

        output = (result.stdout or "").strip()
        if not output:
            output = (result.stderr or "No response.").strip()

        formatted = _md_to_tg_html(output)
        chunks = _split_html_chunks(formatted, 3800)
        for chunk in chunks:
            await sender.send(chunk)

    except subprocess.TimeoutExpired:
        await sender.send("⏳ Timeout. Thử câu hỏi ngắn hơn.")
    except Exception as e:
        logger.error(f"Discuss followup error: {e}")
        await sender.send(f"❌ Lỗi: {e}")


async def _handle_teach_view(
    cb_data: str,
    chat_id: int,
    sender: TelegramSender,
) -> None:
    """Handle teach_view:{case_id}:{wallets|funders|collectors} callback."""
    import teach_store

    try:
        parts = cb_data.split(":")
        if len(parts) != 3:
            return
        case_id = int(parts[1])
        view_type = parts[2]

        case = teach_store.get_case(case_id)
        if not case:
            await sender.send(f"❌ Case #{case_id} not found.")
            return

        name = case.token_name or case.token_symbol or _w(case.mint)

        if view_type == "wallets":
            all_w = list(dict.fromkeys(case.taught_wallets + case.discovered_wallets))
            if not all_w:
                await sender.send(f"Case #{case_id}: không có wallets.")
                return
            header = f"📋 <b>Wallets — {name}</b> (case #{case_id})\n"
            header += f"Taught: {len(case.taught_wallets)} | Discovered: {len(case.discovered_wallets)}\n\n"
            # Send in batches of 80
            for i in range(0, len(all_w), 80):
                batch = all_w[i:i + 80]
                part_label = f" (part {i // 80 + 1})" if len(all_w) > 80 else ""
                await sender.send(f"{header}<pre>{'\\n'.join(batch)}</pre>" if i == 0
                                  else f"📋 <b>Wallets{part_label}</b>\n<pre>{'\\n'.join(batch)}</pre>")

        elif view_type == "funders":
            if not case.funders:
                await sender.send(f"Case #{case_id}: không có funders.")
                return
            lines = [f"💰 <b>Funders — {name}</b> (case #{case_id})\n"]
            for i, f in enumerate(case.funders, 1):
                url = f"https://solscan.io/account/{f}"
                lines.append(f"{i}. <code>{f[:6]}...{f[-4:]}</code> <a href='{url}'>🔗</a>")
            await sender.send("\n".join(lines))

        elif view_type == "collectors":
            if not case.collectors:
                await sender.send(f"Case #{case_id}: không có collectors.")
                return
            lines = [f"📥 <b>Collectors — {name}</b> (case #{case_id})\n"]
            for i, c in enumerate(case.collectors, 1):
                url = f"https://solscan.io/account/{c}"
                lines.append(f"{i}. <code>{c[:6]}...{c[-4:]}</code> <a href='{url}'>🔗</a>")
            await sender.send("\n".join(lines))

    except Exception as e:
        logger.error(f"Teach view error: {e}")


async def _handle_teach_feedback(
    cb_data: str,
    chat_id: int,
    sender: TelegramSender,
    session: aiohttp.ClientSession,
    bot_url: str,
) -> None:
    """Handle teach_fb:{case_id}:{good|bad} callback."""
    import teach_store

    try:
        parts = cb_data.split(":")
        if len(parts) != 3:
            return

        case_id = int(parts[1])
        feedback = parts[2]  # "good" or "bad"

        if feedback not in ("good", "bad"):
            return

        # Update case feedback
        teach_store.update_feedback(case_id, feedback)

        # Adjust pattern confidence
        if feedback == "good":
            teach_store.adjust_pattern_confidence(case_id, delta=0.2)
            await sender.send(
                f"✅ Case #{case_id} đánh dấu <b>hữu ích</b>. "
                f"Confidence +0.2"
            )
        else:
            teach_store.adjust_pattern_confidence(case_id, delta=-0.3)
            await sender.send(
                f"❌ Case #{case_id} đánh dấu <b>không chính xác</b>. "
                f"Confidence -0.3"
            )

    except Exception as e:
        logger.error(f"Teach feedback error: {e}")


async def _cmd_teachlist(sender: TelegramSender, reply_to: int = 0) -> None:
    """Show recent teach cases."""
    import html as html_mod
    import teach_store

    cases = teach_store.list_cases(limit=10)
    if not cases:
        await sender.send("📚 Chưa có teach case nào.")
        return

    lines = ["📚 <b>Teach Cases</b>\n"]
    for c in cases:
        fb_icon = {"good": "✅", "bad": "❌", "pending": "⏳"}.get(
            c.feedback, "❓"
        )
        name = html_mod.escape(c.token_name or c.token_symbol or _w(c.mint))
        lines.append(
            f"#{c.id} {fb_icon} <b>{name}</b>\n"
            f"  Taught: {len(c.taught_wallets)} | "
            f"Discovered: {len(c.discovered_wallets)} | "
            f"Funders: {len(c.funders)}"
        )

    await sender.send("\n".join(lines), reply_to=reply_to)


async def _cmd_teachstats(sender: TelegramSender, reply_to: int = 0) -> None:
    """Show teach system statistics."""
    import teach_store

    stats = teach_store.get_teach_stats()
    pt = stats.get("pattern_types", {})
    pt_lines = "\n".join(
        f"  {k}: {v}" for k, v in pt.items()
    ) if pt else "  (none)"

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
    await sender.send(text, reply_to=reply_to)


async def _cmd_teachremove(case_id: int, sender: TelegramSender, reply_to: int = 0) -> None:
    """Remove a teach case."""
    import teach_store

    if teach_store.delete_case(case_id):
        await sender.send(f"🗑 Case #{case_id} đã xóa.", reply_to=reply_to)
    else:
        await sender.send(f"❌ Không tìm thấy case #{case_id}", reply_to=reply_to)


# ─── Main Entry Point ────────────────────────────────────────


async def main() -> None:
    """
    Main entry point — runs WS listener + scan loop concurrently.

    Architecture:
      Task 1: PumpPortal WS (real-time token discovery)
      Task 2: Scan loop (enrichment + analysis every 5 min)
    """

    if not HELIUS_API_KEYS:
        logger.error("No Helius API keys configured")
        sys.exit(1)

    if not PUMP_BOT_TOKEN:
        logger.error("PUMP_BOT_TOKEN not set in .env")
        sys.exit(1)

    has_moralis = bool(MORALIS_API_KEYS)

    # Load learned CEX wallets from DB
    load_dynamic_cex()

    logger.info("=" * 60)
    logger.info("Pump.fun Scanner starting (WS + Moralis dual-source)")
    logger.info(f"  PumpPortal WS: enabled (primary)")
    logger.info(f"  Moralis keys: {len(MORALIS_API_KEYS)} ({'fallback every 30min' if has_moralis else 'DISABLED'})")
    logger.info(f"  Helius keys: {len(HELIUS_API_KEYS)}")
    logger.info(f"  Alert group: {PUMP_ALERT_CHAT_ID}")
    logger.info(f"  Enrich interval: {ENRICH_INTERVAL_SECONDS}s")
    logger.info(f"  Moralis interval: {MORALIS_FALLBACK_INTERVAL}s")
    logger.info(f"  Max analysis/cycle: {MAX_ANALYSIS_PER_CYCLE}")
    logger.info("=" * 60)

    # Initialize clients
    token_client = TokenDataClient(moralis_api_keys=MORALIS_API_KEYS)
    helius = HeliusClient(api_keys=HELIUS_API_KEYS)

    # Initial key health check — remove dead keys from rotation
    logger.info("Checking Helius key health (removing dead keys)...")
    health = await helius.check_keys_health(force=True)
    logger.info(
        f"Key health: {health['active']} active, {health['no_credits']} dead, "
        f"{health.get('in_rotation', '?')} in rotation"
    )

    analyzer = PumpAnalyzer(helius=helius)
    sender = TelegramSender(
        bot_token=PUMP_BOT_TOKEN, chat_id=PUMP_ALERT_CHAT_ID
    )
    # Dev alert sender — sends devtrace results to group via laocongquetsan bot
    dev_sender = TelegramSender(
        bot_token=DEV_ALERT_BOT_TOKEN, chat_id=DEV_ALERT_CHAT_ID
    )
    state = ScannerState()

    # Initialize PumpPortal WS client
    portal = PumpPortalClient(
        on_new_token=lambda d: _on_ws_new_token(state, d),
        on_migration=lambda d: _on_ws_migration(state, d),
    )

    # Send startup message
    await sender.send(
        f"🔬 <b>Pump Scanner started</b>\n\n"
        f"Mode: WS primary + Moralis fallback\n"
        f"Moralis keys: {len(MORALIS_API_KEYS)}\n"
        f"Helius keys: {len(helius._keys)} active / {len(helius._dead_keys)} dead / {len(HELIUS_API_KEYS)} total\n"
        f"Enrich cycle: {ENRICH_INTERVAL_SECONDS // 60} min\n"
        f"Moralis fallback: {MORALIS_FALLBACK_INTERVAL // 60} min\n"
        f"Tracked tokens: {state.total_tokens}"
    )

    # Run WS + bot polling (autoscan runs in bot.py / laocongquetsan)
    ws_task = asyncio.create_task(portal.run_forever())
    bot_task = asyncio.create_task(
        _bot_polling(analyzer, sender, state, token_client, helius, portal,
                     dev_sender=dev_sender)
    )

    try:
        await asyncio.gather(ws_task, bot_task)
    except KeyboardInterrupt:
        logger.info("Scanner stopped by user")
    except asyncio.CancelledError:
        logger.info("Scanner tasks cancelled")
    finally:
        portal.stop()
        for task in (ws_task, scan_task, bot_task):
            task.cancel()

        # Wait for tasks to finish
        for task in (ws_task, scan_task, bot_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        await sender.send("🔴 <b>Pump Scanner stopped</b>")
        await token_client.close()
        await helius.close()
        await sender.close()
        state.save()
        logger.info("Cleanup complete")


if __name__ == "__main__":
    asyncio.run(main())
