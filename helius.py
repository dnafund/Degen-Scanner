"""
Degen Scanner - Helius API Client
Fetches on-chain SOL transfer history for wallets using Helius Enhanced Transactions API.

Supports multiple API keys with round-robin rotation.
When a key gets 429 rate limited, auto-rotates to the next key.

Docs: https://docs.helius.dev/solana-apis/enhanced-transactions-api

Usage:
    client = HeliusClient(api_keys=["key1", "key2", ...])
    transfers = await client.get_outgoing_sol_transfers(wallet, after_timestamp)
    await client.close()
"""

import logging
import asyncio
import random
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional

import aiohttp
from solders.pubkey import Pubkey

from config import HELIUS_API_KEYS, FREE_RPC_ENDPOINTS

logger = logging.getLogger(__name__)

# Helius Enhanced Transactions API
HELIUS_BASE_URL = "https://api.helius.xyz/v0"

# Rate limiting
THROTTLE_SECONDS = 0.15  # 150ms between sequential requests
MAX_BACKOFF = 60         # Max backoff seconds on 429

# Minimum SOL per sub-wallet transfer to count as a real split
MIN_SPLIT_SOL = 0.15

# Only count transfers from these sources as real splits
# SYSTEM_PROGRAM = direct SOL transfer between wallets
# Everything else (PUMP_AMM, PUMP_FUN, RAYDIUM, JUPITER, etc.) = DEX swap
VALID_SPLIT_SOURCES = {"SYSTEM_PROGRAM"}

# Sources that indicate swap/DEX activity → remove from whale queue
SWAP_SOURCES = {
    "PUMP_AMM", "PUMP_FUN", "RAYDIUM", "RAYDIUM_AMM", "RAYDIUM_CPMM",
    "JUPITER", "ORCA", "ORCA_WHIRLPOOLS", "METEORA", "METEORA_DLMM",
    "PHOENIX", "LIFINITY", "ALDRIN", "SABER", "MARINADE",
    "MOONSHOT",
}

# Sources safe to ignore (wallet stays in queue)
# Wrap SOL, account creation, etc.
SAFE_SOURCES = {"SYSTEM_PROGRAM"}

def is_on_curve(address: str) -> bool:
    """
    Check if a Solana address is a regular wallet (on Ed25519 curve).

    Regular wallets (keypair accounts) are on-curve → True
    PDAs (Program Derived Addresses) are off-curve → False

    Uses solders (Rust-based Solana SDK) for accurate Ed25519 check.
    """
    try:
        return Pubkey.from_string(address).is_on_curve()
    except Exception:
        return False


@dataclass
class SolTransfer:
    """A single outgoing SOL transfer from a wallet."""
    from_wallet: str
    to_wallet: str
    amount_sol: float
    timestamp: datetime
    signature: str


class HeliusClient:
    """
    Async Helius API client with multi-key rotation.

    Round-robin across API keys. Proactively rotates every
    CALLS_PER_KEY_ROTATE calls to spread load evenly and avoid 429s.
    Also rotates immediately on any 429.
    """

    # Proactively rotate key every N calls to avoid hitting per-key rate limit
    CALLS_PER_KEY_ROTATE = 80

    # Max concurrent requests — keep low to reduce IP footprint
    MAX_CONCURRENCY = 10

    # Dead key recheck interval
    _DEAD_KEY_RECHECK_SECONDS = 5 * 24 * 3600  # 5 days

    def __init__(self, api_keys: list[str] = HELIUS_API_KEYS):
        self._all_keys = list(api_keys)       # original full list
        self._keys = list(api_keys)            # active keys only
        self._dead_keys: dict[str, float] = {} # key -> timestamp when marked dead
        self._key_index = 0
        self._calls_on_current_key = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._daily_calls = 0
        self._last_reset: Optional[datetime] = None
        self._last_request_time = 0.0
        self._backoff = 0.0
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._last_dead_recheck: float = 0.0

        if self._keys:
            logger.info(f"Helius initialized with {len(self._keys)} API key(s)")

    def _mark_key_dead(self, key: str) -> None:
        """Remove a key from active rotation (no credits)."""
        import time as _time
        if key in self._dead_keys:
            return
        self._dead_keys[key] = _time.time()
        if key in self._keys:
            self._keys.remove(key)
            logger.warning(
                f"Helius key marked dead (no credits). "
                f"Active: {len(self._keys)}, Dead: {len(self._dead_keys)}"
            )

    async def _maybe_recheck_dead_keys(self) -> None:
        """Every 5 days, recheck dead keys and revive ones with credits."""
        import time as _time
        now = _time.time()
        if now - self._last_dead_recheck < self._DEAD_KEY_RECHECK_SECONDS:
            return
        if not self._dead_keys:
            self._last_dead_recheck = now
            return

        self._last_dead_recheck = now
        logger.info(f"Rechecking {len(self._dead_keys)} dead keys...")

        test_wallet = "So11111111111111111111111111111111111111112"
        sem = asyncio.Semaphore(25)
        dead_list = list(self._dead_keys.keys())
        revived = 0

        async def _check(key: str) -> bool:
            async with sem:
                url = f"{HELIUS_BASE_URL}/addresses/{test_wallet}/transactions"
                params = {"api-key": key, "limit": 1}
                try:
                    session = await self._get_session()
                    async with session.get(
                        url, params=params,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 200:
                            return True
                        if resp.status == 429:
                            body = await resp.text()
                            return "max usage reached" not in body.lower()
                        return resp.status != 402
                except Exception:
                    return False

        results = await asyncio.gather(*[_check(k) for k in dead_list])

        for key, alive in zip(dead_list, results):
            if alive:
                del self._dead_keys[key]
                if key not in self._keys:
                    self._keys.append(key)
                revived += 1

        logger.info(
            f"Dead key recheck done: {revived} revived, "
            f"{len(self._dead_keys)} still dead, {len(self._keys)} active"
        )

    @property
    def enabled(self) -> bool:
        """Check if Helius client is configured."""
        return len(self._keys) > 0

    @property
    def current_key(self) -> str:
        """Current API key in rotation."""
        if not self._keys:
            return ""
        return self._keys[self._key_index % len(self._keys)]

    @property
    def daily_calls(self) -> int:
        """Number of API calls made today."""
        self._maybe_reset_counter()
        return self._daily_calls

    def _rotate_key(self) -> str:
        """Rotate to the next API key. Returns new key."""
        if len(self._keys) <= 1:
            return self.current_key
        old_index = self._key_index
        self._key_index = (self._key_index + 1) % len(self._keys)
        logger.info(
            f"Helius key rotated: #{old_index + 1} → #{self._key_index + 1} "
            f"(of {len(self._keys)})"
        )
        return self.current_key

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    def _maybe_reset_counter(self) -> None:
        """Reset daily counter if a new UTC day has started."""
        now = datetime.now(timezone.utc)
        if self._last_reset is None or now.date() > self._last_reset.date():
            self._daily_calls = 0
            self._last_reset = now
            logger.info("Helius daily counter reset")

    async def _throttle(self) -> None:
        """Ensure minimum delay between requests."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < THROTTLE_SECONDS:
            await asyncio.sleep(THROTTLE_SECONDS - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def get_parsed_transactions(
        self,
        wallet: str,
        limit: int = 50,
        tx_type: str = "TRANSFER",
    ) -> Optional[List[dict]]:
        """
        Fetch parsed (enhanced) transactions for a wallet.

        Args:
            wallet: Solana wallet address
            limit: Max transactions to fetch (1-100)
            tx_type: Filter by transaction type (TRANSFER, SWAP, etc.)
                     Use "TRANSFER" to skip swaps/buys on DEX pools.
                     Use "" or None for all types.

        Returns:
            List of parsed transaction dicts, or None on error
        """
        self._maybe_reset_counter()
        await self._throttle()

        # Proactive key rotation to spread load evenly
        if (
            len(self._keys) > 1
            and self._calls_on_current_key >= self.CALLS_PER_KEY_ROTATE
        ):
            self._rotate_key()
            self._calls_on_current_key = 0

        url = f"{HELIUS_BASE_URL}/addresses/{wallet}/transactions"
        params = {
            "api-key": self.current_key,
            "limit": min(limit, 100),
        }
        if tx_type:
            params["type"] = tx_type

        # Apply backoff if we were rate limited
        if self._backoff > 0:
            logger.info(f"Helius backoff: waiting {self._backoff:.0f}s")
            await asyncio.sleep(self._backoff)

        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                self._daily_calls += 1
                self._calls_on_current_key += 1

                if resp.status == 429:
                    body = await resp.text()
                    used_key = self.current_key
                    if "max usage reached" in body.lower():
                        self._mark_key_dead(used_key)
                    # Rotate to next key
                    self._rotate_key()
                    self._calls_on_current_key = 0
                    self._backoff = min(
                        max(self._backoff * 2, 2.0), MAX_BACKOFF
                    )
                    logger.warning(
                        f"Helius 429 → rotated key, backoff: {self._backoff:.0f}s"
                    )
                    return None

                if resp.status == 402:
                    self._mark_key_dead(self.current_key)
                    self._rotate_key()
                    self._calls_on_current_key = 0
                    return None

                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        f"Helius API error {resp.status}: {body[:200]}"
                    )
                    return None

                # Success — reset backoff
                self._backoff = 0.0
                data = await resp.json()
                logger.debug(
                    f"Helius: {wallet[:8]}... → {len(data)} txns "
                    f"(key #{self._key_index + 1}, calls: {self._daily_calls})"
                )
                return data

        except asyncio.TimeoutError:
            logger.error(f"Helius timeout for {wallet[:8]}...")
            return None
        except Exception as e:
            logger.error(f"Helius request error: {e}")
            return None

    def _pick_key(self) -> str:
        """Pick a random key for parallel usage (avoids sequential exhaustion)."""
        if not self._keys:
            return ""
        return random.choice(self._keys)

    async def get_parsed_transactions_parallel(
        self,
        wallet: str,
        key: str,
        limit: int = 50,
        tx_type: str = "",
    ) -> Optional[List[dict]]:
        """
        Fetch parsed transactions using a specific API key.
        Designed for concurrent usage — no shared throttle state.
        """
        self._maybe_reset_counter()

        # Random jitter 0-500ms to avoid burst pattern
        await asyncio.sleep(random.uniform(0, 0.5))

        url = f"{HELIUS_BASE_URL}/addresses/{wallet}/transactions"
        params = {"api-key": key, "limit": min(limit, 100)}
        if tx_type:
            params["type"] = tx_type

        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                self._daily_calls += 1

                if resp.status == 429:
                    body = await resp.text()
                    if "max usage reached" in body.lower():
                        self._mark_key_dead(key)
                    await asyncio.sleep(random.uniform(1, 3))
                    return None

                if resp.status == 402:
                    self._mark_key_dead(key)
                    return None

                if resp.status != 200:
                    return None

                return await resp.json()

        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.error(f"Helius parallel request error: {e}")
            return None

    async def check_wallet_activity_parallel(
        self,
        wallet: str,
        key: str,
        after_timestamp: Optional[datetime] = None,
    ) -> str:
        """
        Same as check_wallet_activity but uses a specific key.
        For parallel batch processing.
        """
        txns = await self.get_parsed_transactions_parallel(wallet, key, limit=50, tx_type="")
        if txns is None:
            return "error"
        if not txns:
            return "idle"

        has_swap = False
        has_outgoing = False

        for tx in txns:
            tx_source = tx.get("source", "UNKNOWN")
            tx_timestamp = tx.get("timestamp")
            if not tx_timestamp:
                continue

            tx_time = datetime.fromtimestamp(tx_timestamp, tz=timezone.utc)

            if after_timestamp:
                after_ts_utc = after_timestamp
                if after_ts_utc.tzinfo is None:
                    after_ts_utc = after_ts_utc.replace(tzinfo=timezone.utc)
                if tx_time < after_ts_utc:
                    continue

            if tx_source in SWAP_SOURCES:
                has_swap = True
                continue

            if tx_source in VALID_SPLIT_SOURCES:
                for nt in tx.get("nativeTransfers", []):
                    from_addr = nt.get("fromUserAccount", "")
                    to_addr = nt.get("toUserAccount", "")
                    lamports = nt.get("amount", 0)
                    if from_addr != wallet or to_addr == wallet:
                        continue
                    if not is_on_curve(to_addr):
                        continue
                    if lamports / 1_000_000_000 > MIN_SPLIT_SOL:
                        has_outgoing = True
                        break
            if has_outgoing:
                break

        if has_outgoing:
            return "split"
        if has_swap:
            return "swap"
        return "idle"

    async def get_outgoing_sol_transfers(
        self,
        wallet: str,
        after_timestamp: Optional[datetime] = None,
    ) -> List[SolTransfer]:
        """
        Get outgoing SOL transfers FROM a wallet.

        Filters:
        - Only SYSTEM_PROGRAM source (real wallet-to-wallet transfers)
        - Only outgoing (from this wallet)
        - Only after the deposit timestamp
        - Only on-curve destinations (isOnCurve = True = regular user wallet)
        - Only amount > 0.15 SOL per sub-wallet
        - Skip self-transfers

        Args:
            wallet: Source wallet address
            after_timestamp: Only include transfers after this time

        Returns:
            List of SolTransfer objects
        """
        # Query ALL transaction types — local filters handle the rest
        # Don't use tx_type="TRANSFER" as it drops transactions containing
        # SYSTEM_PROGRAM nativeTransfers inside SWAP/other tx types
        txns = await self.get_parsed_transactions(wallet, limit=50, tx_type="")
        if not txns:
            return []

        transfers: List[SolTransfer] = []

        for tx in txns:
            # Skip non-SYSTEM_PROGRAM sources (DEX swaps, pool interactions, etc.)
            tx_source = tx.get("source", "UNKNOWN")
            if tx_source not in VALID_SPLIT_SOURCES:
                continue

            # Extract timestamp
            tx_timestamp = tx.get("timestamp")
            if not tx_timestamp:
                continue

            tx_time = datetime.fromtimestamp(tx_timestamp, tz=timezone.utc)

            # Filter by time — only after deposit
            if after_timestamp:
                after_ts_utc = after_timestamp
                if after_ts_utc.tzinfo is None:
                    after_ts_utc = after_ts_utc.replace(tzinfo=timezone.utc)
                if tx_time < after_ts_utc:
                    continue

            # Extract native SOL transfers
            native_transfers = tx.get("nativeTransfers", [])
            signature = tx.get("signature", "")

            for nt in native_transfers:
                from_addr = nt.get("fromUserAccount", "")
                to_addr = nt.get("toUserAccount", "")
                lamports = nt.get("amount", 0)

                # Only outgoing from this wallet
                if from_addr != wallet:
                    continue

                # Skip self-transfers
                if to_addr == wallet:
                    continue

                # Only take on-curve destinations (isOnCurve = True = regular wallet)
                # Off-curve = PDA/program account → skip
                if not is_on_curve(to_addr):
                    continue

                # Convert lamports → SOL
                amount_sol = lamports / 1_000_000_000

                # Skip transfers below minimum split amount
                if amount_sol <= MIN_SPLIT_SOL:
                    continue

                transfers.append(SolTransfer(
                    from_wallet=from_addr,
                    to_wallet=to_addr,
                    amount_sol=amount_sol,
                    timestamp=tx_time,
                    signature=signature,
                ))

        logger.debug(
            f"Outgoing transfers for {wallet[:8]}...: "
            f"{len(transfers)} found (after filter)"
        )
        return transfers

    async def check_wallet_activity(
        self,
        wallet: str,
        after_timestamp: Optional[datetime] = None,
    ) -> str:
        """
        Check what a wallet has been doing since deposit.

        Returns:
            "split" — outgoing SOL transfers to 2+ wallets
            "swap" — interacted with DEX/contract (remove from queue)
            "wrap" — only wrapped SOL (keep in queue)
            "idle" — no activity (keep in queue)
            "error" — API error (keep in queue, retry later)
        """
        # Fetch ALL transaction types (not just TRANSFER)
        txns = await self.get_parsed_transactions(wallet, limit=50, tx_type="")
        if txns is None:
            return "error"

        if not txns:
            return "idle"

        has_swap = False
        outgoing_transfers: List[SolTransfer] = []

        for tx in txns:
            tx_source = tx.get("source", "UNKNOWN")
            tx_timestamp = tx.get("timestamp")

            if not tx_timestamp:
                continue

            tx_time = datetime.fromtimestamp(tx_timestamp, tz=timezone.utc)

            # Filter by time
            if after_timestamp:
                after_ts_utc = after_timestamp
                if after_ts_utc.tzinfo is None:
                    after_ts_utc = after_ts_utc.replace(tzinfo=timezone.utc)
                if tx_time < after_ts_utc:
                    continue

            # Detect swap/DEX activity
            if tx_source in SWAP_SOURCES:
                has_swap = True
                continue

            # Detect outgoing SOL transfers (SYSTEM_PROGRAM)
            if tx_source in VALID_SPLIT_SOURCES:
                native_transfers = tx.get("nativeTransfers", [])
                signature = tx.get("signature", "")

                for nt in native_transfers:
                    from_addr = nt.get("fromUserAccount", "")
                    to_addr = nt.get("toUserAccount", "")
                    lamports = nt.get("amount", 0)

                    if from_addr != wallet:
                        continue
                    if to_addr == wallet:
                        continue
                    if not is_on_curve(to_addr):
                        continue

                    amount_sol = lamports / 1_000_000_000
                    if amount_sol <= MIN_SPLIT_SOL:
                        continue

                    outgoing_transfers.append(SolTransfer(
                        from_wallet=from_addr,
                        to_wallet=to_addr,
                        amount_sol=amount_sol,
                        timestamp=tx_time,
                        signature=signature,
                    ))

        # Decide result
        if outgoing_transfers:
            return "split"

        if has_swap:
            return "swap"

        return "idle"

    async def get_outgoing_for_wallet(
        self,
        wallet: str,
        after_timestamp: Optional[datetime] = None,
    ) -> List[SolTransfer]:
        """
        Get outgoing SOL transfers for alert display.
        Call this AFTER check_wallet_activity returns "split".
        Re-uses the same logic as get_outgoing_sol_transfers but fetches all types.
        """
        return await self.get_outgoing_sol_transfers(wallet, after_timestamp)

    # ─── Pump Analyzer Methods ────────────────────────────────

    async def get_das_asset(self, mint: str) -> Optional[dict]:
        """
        Fetch token metadata via DAS (Digital Asset Standard) API.

        Returns asset info dict with name, symbol, image, etc.
        """
        self._maybe_reset_counter()

        for attempt in range(3):
            key = self._pick_key()
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            payload = {
                "jsonrpc": "2.0",
                "id": "das-asset",
                "method": "getAsset",
                "params": {"id": mint},
            }

            try:
                session = await self._get_session()
                async with session.post(url, json=payload) as resp:
                    self._daily_calls += 1

                    if resp.status == 429:
                        body = await resp.text()
                        if "max usage reached" in body.lower():
                            self._mark_key_dead(key)
                        await asyncio.sleep(random.uniform(0.5, 2))
                        continue
                    if resp.status == 402:
                        self._mark_key_dead(key)
                        continue
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"DAS API error {resp.status}: {body[:200]}")
                        return None

                    data = await resp.json()
                    result = data.get("result")
                    if not result:
                        logger.warning(f"DAS: no result for mint {mint[:12]}...")
                    return result

            except Exception as e:
                logger.error(f"DAS API request error: {e}")
                return None

        logger.warning(f"DAS API 429 after 3 retries for {mint[:12]}...")
        return None

    async def get_token_transactions(
        self,
        address: str,
        source_filter: str = "PUMP_FUN",
        max_pages: int = 50,
    ) -> List[dict]:
        """
        Fetch paginated transaction history for an address.

        Paginates using `before` cursor (last signature of each page).
        Stops when source != source_filter (graduation) or max_pages reached.

        Args:
            address: Bonding curve PDA or any Solana address
            source_filter: Only include txs from this source (e.g. "PUMP_FUN")
            max_pages: Max pages to fetch (100 txs each)

        Returns:
            All matching transactions (up to max_pages * 100)
        """
        all_txns = []
        before_sig = None
        # More retries for page 0 (MUST succeed or we get 0 data)
        max_retries_page0 = 5
        max_retries_other = 2

        for page in range(max_pages):
            max_retries = max_retries_page0 if page == 0 else max_retries_other
            page_data = None

            for attempt in range(max_retries):
                key = self._pick_key()
                url = f"{HELIUS_BASE_URL}/addresses/{address}/transactions"
                params = {
                    "api-key": key,
                    "limit": 100,
                }
                if before_sig:
                    params["before"] = before_sig

                # Random jitter to avoid burst
                await asyncio.sleep(random.uniform(0.1, 0.4))

                try:
                    session = await self._get_session()
                    async with session.get(url, params=params) as resp:
                        self._daily_calls += 1

                        if resp.status == 429:
                            body = await resp.text()
                            if "max usage reached" in body.lower():
                                self._mark_key_dead(key)
                            logger.warning(
                                f"Token tx page {page} got 429 (attempt {attempt+1}/{max_retries}), "
                                f"trying different key..."
                            )
                            await asyncio.sleep(random.uniform(0.5, 1.5))
                            continue
                        if resp.status == 402:
                            self._mark_key_dead(key)
                            continue
                        if resp.status != 200:
                            logger.error(f"Token tx error {resp.status}")
                            break

                        page_data = await resp.json()
                        break

                except asyncio.TimeoutError:
                    logger.error(f"Token tx timeout at page {page}")
                    break
                except Exception as e:
                    logger.error(f"Token tx error: {e}")
                    break

            if page_data is None:
                if page == 0:
                    logger.error("Token tx: page 0 failed after all retries — 0 data")
                break

            if not page_data:
                break

            # Filter by source — skip non-matching txs
            # (graduated tokens have migration txs mixed in)
            page_matched = []
            for tx in page_data:
                tx_source = tx.get("source", "UNKNOWN")
                if not source_filter or tx_source == source_filter:
                    page_matched.append(tx)

            all_txns.extend(page_matched)

            # Set cursor for next page
            before_sig = page_data[-1].get("signature")
            if not before_sig:
                break

        logger.info(f"Token tx: fetched {len(all_txns)} txs in {page + 1} pages")
        return all_txns

    async def get_wallet_incoming_sol(
        self,
        wallet: str,
        key: str,
        before_timestamp: Optional[int] = None,
        lookback_hours: int = 48,
    ) -> Optional[dict]:
        """
        Find the largest incoming SOL transfer to a wallet.

        Looks for SYSTEM_PROGRAM transfers where wallet is the recipient.
        Used to trace funding source (CEX hot wallet).

        Args:
            wallet: Target wallet to check
            key: Helius API key to use
            before_timestamp: Unix timestamp — only look at txs before this
            lookback_hours: How far back to search

        Returns:
            Dict with {from_wallet, amount_sol, timestamp, source} or None
        """
        await asyncio.sleep(random.uniform(0, 0.5))

        url = f"{HELIUS_BASE_URL}/addresses/{wallet}/transactions"
        params = {"api-key": key, "limit": 50}

        txns = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    key = random.choice(self._keys) if self._keys else key
                    params["api-key"] = key

                session = await self._get_session()
                async with session.get(url, params=params) as resp:
                    self._daily_calls += 1

                    if resp.status == 429:
                        body = await resp.text()
                        if "max usage reached" in body.lower():
                            self._mark_key_dead(key)
                        await asyncio.sleep(random.uniform(0.5, 2))
                        continue
                    if resp.status == 402:
                        self._mark_key_dead(key)
                        continue
                    if resp.status != 200:
                        return None

                    txns = await resp.json()
                    break

            except Exception as e:
                logger.error(f"Wallet funding trace error: {e}")
                return None

        if not txns:
            return None

        # Find the largest incoming SOL > 0.05
        cutoff_ts = None
        if before_timestamp and lookback_hours:
            cutoff_ts = before_timestamp - (lookback_hours * 3600)

        best = None
        best_amount = 0.0

        for tx in txns:
            tx_source = tx.get("source", "UNKNOWN")
            tx_ts = tx.get("timestamp", 0)

            # Only look at txs before the buy timestamp
            if before_timestamp and tx_ts > before_timestamp:
                continue
            # Don't look too far back
            if cutoff_ts and tx_ts < cutoff_ts:
                continue

            # Check all nativeTransfers (not just SYSTEM_PROGRAM)
            for nt in tx.get("nativeTransfers", []):
                to_addr = nt.get("toUserAccount", "")
                from_addr = nt.get("fromUserAccount", "")
                lamports = nt.get("amount", 0)

                if to_addr != wallet:
                    continue
                if from_addr == wallet:
                    continue

                amount_sol = lamports / 1_000_000_000
                if amount_sol > 0.05 and amount_sol > best_amount:
                    best_amount = amount_sol
                    best = {
                        "from_wallet": from_addr,
                        "amount_sol": amount_sol,
                        "timestamp": tx_ts,
                        "source": tx_source,
                    }

        return best

    async def get_wallet_outgoing_sol(
        self,
        wallet: str,
        key: str,
        after_timestamp: Optional[int] = None,
    ) -> Optional[dict]:
        """
        Find the largest outgoing SOL transfer from a wallet.

        Looks for SYSTEM_PROGRAM transfers where wallet is the sender.
        Used to trace profit destination (collector wallet).

        Args:
            wallet: Source wallet to check
            key: Helius API key to use
            after_timestamp: Unix timestamp — only look at txs after this

        Returns:
            Dict with {to_wallet, amount_sol, timestamp, source} or None
        """
        await asyncio.sleep(random.uniform(0, 0.5))

        url = f"{HELIUS_BASE_URL}/addresses/{wallet}/transactions"
        params = {"api-key": key, "limit": 50}

        txns = None
        for attempt in range(3):
            try:
                # Retry with different key on 429
                if attempt > 0:
                    key = random.choice(self._keys) if self._keys else key
                    params["api-key"] = key

                session = await self._get_session()
                async with session.get(url, params=params) as resp:
                    self._daily_calls += 1

                    if resp.status == 429:
                        body = await resp.text()
                        if "max usage reached" in body.lower():
                            self._mark_key_dead(key)
                        await asyncio.sleep(random.uniform(0.5, 2))
                        continue
                    if resp.status == 402:
                        self._mark_key_dead(key)
                        continue
                    if resp.status != 200:
                        return None

                    txns = await resp.json()
                    break

            except Exception as e:
                logger.error(f"Wallet outgoing trace error: {e}")
                return None

        if not txns:
            return None

        best = None
        best_amount = 0.0

        for tx in txns:
            tx_source = tx.get("source", "UNKNOWN")
            tx_ts = tx.get("timestamp", 0)

            # Only look at txs after the buy timestamp
            if after_timestamp and tx_ts < after_timestamp:
                continue

            # Check all nativeTransfers (not just SYSTEM_PROGRAM)
            for nt in tx.get("nativeTransfers", []):
                from_addr = nt.get("fromUserAccount", "")
                to_addr = nt.get("toUserAccount", "")
                lamports = nt.get("amount", 0)

                if from_addr != wallet:
                    continue
                if to_addr == wallet:
                    continue
                if not is_on_curve(to_addr):
                    continue

                amount_sol = lamports / 1_000_000_000
                if amount_sol > 0.05 and amount_sol > best_amount:
                    best_amount = amount_sol
                    best = {
                        "to_wallet": to_addr,
                        "amount_sol": amount_sol,
                        "timestamp": tx_ts,
                        "source": tx_source,
                    }

        return best

    async def get_wallet_all_outgoing_sol(
        self,
        wallet: str,
        key: str,
        after_timestamp: Optional[int] = None,
        min_sol: float = 0.05,
    ) -> List[dict]:
        """
        Find ALL outgoing SOL transfers from a wallet (not just the largest).

        Returns list of {to_wallet, amount_sol, timestamp, source} dicts,
        sorted by amount descending.
        """
        await asyncio.sleep(random.uniform(0, 0.5))

        url = f"{HELIUS_BASE_URL}/addresses/{wallet}/transactions"
        params = {"api-key": key, "limit": 50}

        txns = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    key = random.choice(self._keys) if self._keys else key
                    params["api-key"] = key

                session = await self._get_session()
                async with session.get(url, params=params) as resp:
                    self._daily_calls += 1

                    if resp.status == 429:
                        body = await resp.text()
                        if "max usage reached" in body.lower():
                            self._mark_key_dead(key)
                        await asyncio.sleep(random.uniform(0.5, 2))
                        continue
                    if resp.status == 402:
                        self._mark_key_dead(key)
                        continue
                    if resp.status != 200:
                        return []

                    txns = await resp.json()
                    break

            except Exception as e:
                logger.error(f"Wallet all outgoing trace error: {e}")
                return []

        if not txns:
            return []

        results = []
        seen_to = set()

        for tx in txns:
            tx_source = tx.get("source", "UNKNOWN")
            tx_ts = tx.get("timestamp", 0)

            if after_timestamp and tx_ts < after_timestamp:
                continue

            for nt in tx.get("nativeTransfers", []):
                from_addr = nt.get("fromUserAccount", "")
                to_addr = nt.get("toUserAccount", "")
                lamports = nt.get("amount", 0)

                if from_addr != wallet:
                    continue
                if to_addr == wallet:
                    continue
                if not is_on_curve(to_addr):
                    continue

                amount_sol = lamports / 1_000_000_000
                if amount_sol >= min_sol and to_addr not in seen_to:
                    seen_to.add(to_addr)
                    results.append({
                        "to_wallet": to_addr,
                        "amount_sol": amount_sol,
                        "timestamp": tx_ts,
                        "source": tx_source,
                    })

        results.sort(key=lambda x: x["amount_sol"], reverse=True)
        return results

    async def get_wallet_trace_info(
        self,
        wallet: str,
        key: str,
        min_sol: float = 0.05,
    ) -> Optional[dict]:
        """
        Single API call for chain tracing: outgoing SOL transfers + swap detection.

        Returns {"outgoing": [...], "has_swap": bool} or None on API failure.
        - outgoing: list of {to_wallet, amount_sol} dicts, sorted by amount desc
        - has_swap: True if wallet has any DEX/swap transactions
        """
        await asyncio.sleep(random.uniform(0, 0.3))

        url = f"{HELIUS_BASE_URL}/addresses/{wallet}/transactions"
        params = {"api-key": key, "limit": 50}

        txns = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    key = random.choice(self._keys) if self._keys else key
                    params["api-key"] = key

                session = await self._get_session()
                async with session.get(url, params=params) as resp:
                    self._daily_calls += 1

                    if resp.status == 429:
                        body = await resp.text()
                        if "max usage reached" in body.lower():
                            self._mark_key_dead(key)
                        await asyncio.sleep(random.uniform(0.5, 2))
                        continue
                    if resp.status == 402:
                        self._mark_key_dead(key)
                        continue
                    if resp.status != 200:
                        return None

                    txns = await resp.json()
                    break

            except Exception as e:
                logger.error(f"Wallet trace info error: {e}")
                return None

        if txns is None:
            return None  # All attempts failed (dead keys / errors)

        if not txns:
            return {"outgoing": [], "has_swap": False}

        # Check for swap/DEX activity
        has_swap = any(tx.get("source", "") in SWAP_SOURCES for tx in txns)

        # Extract outgoing SOL transfers
        outgoing = []
        seen_to = set()
        for tx in txns:
            for nt in tx.get("nativeTransfers", []):
                from_addr = nt.get("fromUserAccount", "")
                to_addr = nt.get("toUserAccount", "")
                lamports = nt.get("amount", 0)

                if from_addr != wallet or to_addr == wallet:
                    continue
                if not is_on_curve(to_addr):
                    continue

                amount_sol = lamports / 1_000_000_000
                if amount_sol >= min_sol and to_addr not in seen_to:
                    seen_to.add(to_addr)
                    outgoing.append({
                        "to_wallet": to_addr,
                        "amount_sol": amount_sol,
                    })

        outgoing.sort(key=lambda x: x["amount_sol"], reverse=True)
        return {"outgoing": outgoing, "has_swap": has_swap}

    async def get_sol_balance(self, wallet: str, key: str) -> float:
        """Get SOL balance using free RPC providers first, Helius as fallback."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [wallet],
        }
        try:
            session = await self._get_session()

            # Try free RPC first
            if FREE_RPC_ENDPOINTS:
                url = random.choice(FREE_RPC_ENDPOINTS)
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            lamports = data.get("result", {}).get("value", 0)
                            if lamports is not None:
                                return lamports / 1_000_000_000
                except Exception:
                    pass

            # Fallback: Helius RPC
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    return 0.0
                data = await resp.json()
                lamports = data.get("result", {}).get("value", 0)
                return lamports / 1_000_000_000
        except Exception:
            return 0.0

    async def get_sol_balances(self, wallets: list[str], key: str) -> dict[str, float]:
        """Get SOL balances for multiple wallets in parallel."""
        tasks = [self.get_sol_balance(w, key) for w in wallets]
        results = await asyncio.gather(*tasks)
        return dict(zip(wallets, results))

    def get_stats(self) -> dict:
        """Get Helius client statistics."""
        self._maybe_reset_counter()
        return {
            "enabled": self.enabled,
            "total_keys": len(self._all_keys),
            "active_keys": len(self._keys),
            "dead_keys": len(self._dead_keys),
            "active_key": self._key_index + 1,
            "daily_calls": self._daily_calls,
        }

    # ─── Key Health Check (cached) ────────────────────────────

    _health_cache: Optional[dict] = None
    _health_cache_time: float = 0.0
    _HEALTH_CACHE_TTL = 3600  # 1 hour

    async def check_keys_health(self, force: bool = False) -> dict:
        """
        Check all API keys credit status. Cached for 1 hour.

        Returns:
            {"active": N, "no_credits": N, "errors": N, "total": N, "cached": bool}
        """
        import time as _time

        now = _time.time()
        if (
            not force
            and self._health_cache is not None
            and now - self._health_cache_time < self._HEALTH_CACHE_TTL
        ):
            return {**self._health_cache, "cached": True}

        if not self._keys:
            result = {"active": 0, "no_credits": 0, "errors": 0, "total": 0}
            self._health_cache = result
            self._health_cache_time = now
            return {**result, "cached": False}

        test_wallet = "So11111111111111111111111111111111111111112"
        sem = asyncio.Semaphore(25)

        async def _check_one(key: str) -> str:
            async with sem:
                url = f"{HELIUS_BASE_URL}/addresses/{test_wallet}/transactions"
                params = {"api-key": key, "limit": 1}
                for attempt in range(2):
                    try:
                        if attempt > 0:
                            await asyncio.sleep(2)
                        session = await self._get_session()
                        async with session.get(
                            url, params=params,
                            timeout=aiohttp.ClientTimeout(total=20),
                        ) as resp:
                            if resp.status == 200:
                                return "active"
                            if resp.status == 429:
                                body = await resp.text()
                                if "max usage reached" in body.lower():
                                    return "no_credits"
                                if attempt < 1:
                                    continue
                                return "active"  # temp rate limit = key is fine
                            if resp.status == 402:
                                return "no_credits"
                            if resp.status == 500 and attempt < 1:
                                await asyncio.sleep(1)
                                continue
                            return "error"
                    except Exception:
                        if attempt < 1:
                            await asyncio.sleep(1)
                            continue
                        return "error"
                return "error"

        # Check ALL keys (including already-dead ones for recheck)
        all_keys = self._all_keys
        statuses = await asyncio.gather(*[_check_one(k) for k in all_keys])

        # Mark dead keys and revive recovered ones
        for key, status in zip(all_keys, statuses):
            if status == "no_credits":
                self._mark_key_dead(key)
            elif status == "active" and key in self._dead_keys:
                # Key recovered — revive it
                del self._dead_keys[key]
                if key not in self._keys:
                    self._keys.append(key)

        result = {
            "active": sum(1 for s in statuses if s == "active"),
            "no_credits": sum(1 for s in statuses if s == "no_credits"),
            "errors": sum(1 for s in statuses if s == "error"),
            "total": len(all_keys),
            "in_rotation": len(self._keys),
            "dead_removed": len(self._dead_keys),
        }
        self._health_cache = result
        self._health_cache_time = now
        logger.info(
            f"Helius key health: {result['active']} active, "
            f"{result['no_credits']} no credits, {result['total']} total, "
            f"{result['in_rotation']} in rotation"
        )
        return {**result, "cached": False}

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
