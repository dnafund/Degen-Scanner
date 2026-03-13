"""
Degen Scanner - Token Data Client

Fetches Pump.fun token lists from Moralis API and enriches with
market data from DexScreener. Used by pump_scanner.py.

Moralis endpoints:
  - /token/mainnet/exchange/pumpfun/new      (new tokens)
  - /token/mainnet/exchange/pumpfun/graduated (graduated tokens)

DexScreener endpoint:
  - /tokens/v1/solana/{addr1},{addr2},...     (batch up to 30)
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ─── Moralis ──────────────────────────────────────────────────

MORALIS_BASE_URL = "https://solana-gateway.moralis.io"
MORALIS_PAGE_LIMIT = 100
MORALIS_MAX_PAGES = 300  # safety cap per endpoint (24h ~= 240 pages)

# ─── Pump.fun ────────────────────────────────────────────────

PUMPFUN_API_URL = "https://frontend-api-v3.pump.fun"
PUMPFUN_ATH_CONCURRENCY = 100  # concurrent ATH lookups

# ─── DexScreener ─────────────────────────────────────────────

DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
DEXSCREENER_BATCH_SIZE = 30  # max addresses per request
DEXSCREENER_RATE_DELAY = 0.25  # seconds between batches


class TokenDataClient:
    """
    Fetches token lists from Moralis and enriches with DexScreener data.

    Supports multiple Moralis API keys with round-robin rotation.
    Each key has its own CU quota — more keys = more daily capacity.

    Usage:
        client = TokenDataClient(moralis_api_keys=["key1", "key2"])
        tokens = await client.fetch_all_recent_tokens(max_age_hours=24)
        enriched = await client.enrich_tokens([t["tokenAddress"] for t in tokens])
        await client.close()
    """

    def __init__(self, moralis_api_keys: list[str] | str = ""):
        if isinstance(moralis_api_keys, str):
            self._keys = [moralis_api_keys] if moralis_api_keys else []
        else:
            self._keys = list(moralis_api_keys)
        self._key_index = 0
        self._dead_keys: dict[int, float] = {}  # index -> timestamp when marked dead
        self._dead_ttl = 3600  # revive dead keys after 1 hour
        self._session: Optional[aiohttp.ClientSession] = None

        if self._keys:
            logger.info(f"Moralis initialized with {len(self._keys)} API key(s)")

    def _revive_dead_keys(self) -> None:
        """Revive keys that have been dead longer than TTL."""
        import time
        now = time.time()
        revived = [
            idx for idx, ts in self._dead_keys.items()
            if now - ts >= self._dead_ttl
        ]
        for idx in revived:
            del self._dead_keys[idx]
            logger.info(f"Moralis key #{idx + 1} revived after {self._dead_ttl}s cooldown")

    @property
    def _moralis_key(self) -> str:
        """Current Moralis API key (skips dead keys, revives expired ones)."""
        if not self._keys:
            return ""
        self._revive_dead_keys()
        # Skip dead keys
        for _ in range(len(self._keys)):
            if self._key_index not in self._dead_keys:
                return self._keys[self._key_index]
            self._key_index = (self._key_index + 1) % len(self._keys)
        # All keys dead — try current anyway
        return self._keys[self._key_index]

    def _rotate_key(self, mark_dead: bool = False) -> None:
        """Rotate to next Moralis key. mark_dead=True for 401 (quota exhausted)."""
        import time
        if len(self._keys) <= 1:
            if mark_dead:
                self._dead_keys[self._key_index] = time.time()
                logger.warning(f"Moralis key #{self._key_index + 1} dead (will revive in {self._dead_ttl}s)")
            return
        if mark_dead:
            self._dead_keys[self._key_index] = time.time()
            alive = len(self._keys) - len(self._dead_keys)
            logger.warning(f"Moralis key #{self._key_index + 1} dead ({alive} alive, revive in {self._dead_ttl}s)")
        self._key_index = (self._key_index + 1) % len(self._keys)
        # Skip already-dead keys
        for _ in range(len(self._keys)):
            if self._key_index not in self._dead_keys:
                break
            self._key_index = (self._key_index + 1) % len(self._keys)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    # ─── Moralis: Fetch Token Lists ───────────────────────────

    async def _fetch_moralis_page(
        self,
        endpoint: str,
        cursor: str = "",
    ) -> tuple[list[dict], str]:
        """
        Fetch one page from a Moralis token endpoint.

        Returns:
            (tokens_list, next_cursor) — cursor is "" when no more pages.
        """
        url = f"{MORALIS_BASE_URL}{endpoint}"
        params = {"limit": MORALIS_PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor

        headers = {
            "accept": "application/json",
            "X-API-Key": self._moralis_key,
        }

        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 401:
                    self._rotate_key(mark_dead=True)
                    return [], cursor  # return same cursor to retry
                if resp.status == 429:
                    self._rotate_key()
                    await asyncio.sleep(0.5)
                    return [], cursor  # return same cursor to retry

                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        f"Moralis {resp.status} on {endpoint}: {body[:200]}"
                    )
                    return [], ""

                data = await resp.json()
                tokens = data.get("result", [])
                next_cursor = data.get("cursor", "")
                return tokens, next_cursor

        except asyncio.TimeoutError:
            logger.error(f"Moralis timeout on {endpoint}")
            return [], ""
        except Exception as e:
            logger.error(f"Moralis request error: {e}")
            return [], ""

    async def fetch_new_tokens(
        self, limit: int = MORALIS_PAGE_LIMIT, cursor: str = ""
    ) -> tuple[list[dict], str]:
        """Fetch one page of new Pump.fun tokens."""
        return await self._fetch_moralis_page(
            "/token/mainnet/exchange/pumpfun/new", cursor
        )

    async def fetch_graduated_tokens(
        self, limit: int = MORALIS_PAGE_LIMIT, cursor: str = ""
    ) -> tuple[list[dict], str]:
        """Fetch one page of graduated Pump.fun tokens."""
        return await self._fetch_moralis_page(
            "/token/mainnet/exchange/pumpfun/graduated", cursor
        )

    async def _paginate_endpoint(
        self,
        endpoint: str,
        max_age_hours: int = 24,
    ) -> list[dict]:
        """
        Paginate through all pages of a Moralis endpoint.

        Filters each token by createdAt (< max_age_hours = skip).
        Graduated tokens are sorted by graduation time, not creation time,
        so we can't break on first old token. Instead, stop after N
        consecutive pages with 0 qualifying tokens.
        """
        all_tokens: list[dict] = []
        cursor = ""
        cutoff = time.time() - (max_age_hours * 3600)
        retries = 0
        max_retries = len(self._keys)
        empty_streak = 0  # consecutive pages with 0 qualifying tokens

        for page in range(MORALIS_MAX_PAGES):
            tokens, next_cursor = await self._fetch_moralis_page(endpoint, cursor)

            if not tokens:
                if next_cursor == cursor and retries < max_retries:
                    retries += 1
                    continue
                break
            retries = 0

            # Filter: only keep tokens within max_age_hours
            # "new" endpoint has createdAt, "graduated" has graduatedAt
            page_added = 0
            for token in tokens:
                ts_str = (
                    token.get("createdAt")
                    or token.get("graduatedAt")
                    or ""
                )
                if not ts_str:
                    # No timestamp at all — skip (don't add unknown-age tokens)
                    continue
                try:
                    dt = datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")
                    )
                    if dt.timestamp() >= cutoff:
                        all_tokens.append(token)
                        page_added += 1
                except (ValueError, TypeError):
                    pass

            # Stop if consecutive pages with no qualifying tokens
            # Both endpoints are sorted newest-first, so empty pages
            # mean we've passed the cutoff
            if page_added == 0:
                empty_streak += 1
                if empty_streak >= 3:
                    logger.info(
                        f"Moralis {endpoint}: {empty_streak} empty pages, "
                        f"stopping at page {page + 1} ({len(all_tokens)} tokens)"
                    )
                    break
            else:
                empty_streak = 0

            if not next_cursor:
                break

            cursor = next_cursor
            self._rotate_key()
            await asyncio.sleep(0.15)

        logger.info(
            f"Moralis {endpoint}: {len(all_tokens)} tokens in {page + 1} pages"
        )
        return all_tokens

    async def fetch_all_recent_tokens(
        self, max_age_hours: int = 24
    ) -> list[dict]:
        """
        Fetch ALL recent Pump.fun tokens (new + graduated), deduplicated.

        Returns list of dicts with at least:
          tokenAddress, name, symbol, createdAt
          + optional: priceUsd, liquidity, fullyDilutedValuation
        """
        # Fetch both endpoints in parallel
        new_task = self._paginate_endpoint(
            "/token/mainnet/exchange/pumpfun/new", max_age_hours
        )
        grad_task = self._paginate_endpoint(
            "/token/mainnet/exchange/pumpfun/graduated", max_age_hours
        )

        new_tokens, grad_tokens = await asyncio.gather(new_task, grad_task)

        # Deduplicate by tokenAddress
        seen: set[str] = set()
        deduped: list[dict] = []

        # Graduated first (higher priority — already have liquidity)
        for token in grad_tokens:
            addr = token.get("tokenAddress", "")
            if addr and addr not in seen:
                seen.add(addr)
                token["_graduated"] = True
                deduped.append(token)

        for token in new_tokens:
            addr = token.get("tokenAddress", "")
            if addr and addr not in seen:
                seen.add(addr)
                token["_graduated"] = False
                deduped.append(token)

        logger.info(
            f"Moralis total: {len(new_tokens)} new + {len(grad_tokens)} graduated "
            f"= {len(deduped)} unique tokens"
        )
        return deduped

    # ─── DexScreener: Market Data Enrichment ──────────────────

    async def enrich_tokens(
        self, addresses: list[str]
    ) -> dict[str, dict]:
        """
        Batch-enrich token addresses with DexScreener market data.

        Args:
            addresses: List of Solana token mint addresses

        Returns:
            Dict mapping address → {priceUsd, volume24h, txns24h,
                                     liquidity, marketCap, pairAddress}
        """
        result: dict[str, dict] = {}

        # Split into batches of 30
        batches = [
            addresses[i:i + DEXSCREENER_BATCH_SIZE]
            for i in range(0, len(addresses), DEXSCREENER_BATCH_SIZE)
        ]

        logger.info(
            f"DexScreener: enriching {len(addresses)} tokens "
            f"in {len(batches)} batches"
        )

        for batch_idx, batch in enumerate(batches):
            addr_str = ",".join(batch)
            url = f"{DEXSCREENER_BASE_URL}/tokens/v1/solana/{addr_str}"

            try:
                session = await self._get_session()
                async with session.get(url) as resp:
                    if resp.status == 429:
                        logger.warning(
                            f"DexScreener 429 at batch {batch_idx}, "
                            f"waiting 5s"
                        )
                        await asyncio.sleep(5)
                        continue

                    if resp.status != 200:
                        logger.error(f"DexScreener {resp.status} at batch {batch_idx}")
                        continue

                    pairs = await resp.json()
                    if not isinstance(pairs, list):
                        continue

                    # DexScreener returns pairs — pick best pair per token
                    for pair in pairs:
                        base_token = pair.get("baseToken", {})
                        addr = base_token.get("address", "")
                        if not addr:
                            continue

                        # Keep the pair with highest liquidity
                        liq = pair.get("liquidity", {})
                        liq_usd = liq.get("usd", 0) if isinstance(liq, dict) else 0

                        existing = result.get(addr)
                        if existing and existing.get("liquidity", 0) >= liq_usd:
                            continue

                        txns = pair.get("txns", {})
                        txns_24h = txns.get("h24", {}) if isinstance(txns, dict) else {}
                        buys_24h = txns_24h.get("buys", 0) if isinstance(txns_24h, dict) else 0
                        sells_24h = txns_24h.get("sells", 0) if isinstance(txns_24h, dict) else 0

                        volume = pair.get("volume", {})
                        vol_24h = volume.get("h24", 0) if isinstance(volume, dict) else 0

                        result[addr] = {
                            "priceUsd": float(pair.get("priceUsd") or 0),
                            "volume24h": float(vol_24h or 0),
                            "txns24h": buys_24h + sells_24h,
                            "buys24h": buys_24h,
                            "sells24h": sells_24h,
                            "liquidity": float(liq_usd or 0),
                            "marketCap": float(pair.get("marketCap") or 0),
                            "pairAddress": pair.get("pairAddress", ""),
                            "dexId": pair.get("dexId", ""),
                        }

            except asyncio.TimeoutError:
                logger.error(f"DexScreener timeout at batch {batch_idx}")
            except Exception as e:
                logger.error(f"DexScreener error: {e}")

            # Rate limit between batches
            if batch_idx < len(batches) - 1:
                await asyncio.sleep(DEXSCREENER_RATE_DELAY)

        logger.info(f"DexScreener: enriched {len(result)}/{len(addresses)} tokens")
        return result

    # ─── Pump.fun: ATH Market Cap Lookup ─────────────────────

    async def lookup_ath_batch(
        self,
        mints: list[str],
        min_ath: float = 15000,
        on_progress: object = None,
    ) -> tuple[list[dict], dict[str, float]]:
        """
        Lookup ATH market cap for a list of mints via Pump.fun API.
        Returns (qualified_tokens, all_created_timestamps).

        Args:
            mints: list of token mint addresses
            min_ath: minimum ATH market cap in USD (default $15k)
            on_progress: optional async callback(done, total, found)

        Returns:
            List of dicts with keys: mint, name, symbol, ath_market_cap,
                                      usd_market_cap, created_timestamp
        """
        sem = asyncio.Semaphore(PUMPFUN_ATH_CONCURRENCY)
        results: list[dict] = []
        # created_timestamp for ALL tokens (not just qualified)
        all_created: dict[str, float] = {}
        done_count = 0
        session = await self._get_session()

        async def _lookup_one(mint: str) -> None:
            nonlocal done_count
            async with sem:
                try:
                    url = f"{PUMPFUN_API_URL}/coins/{mint}"
                    async with session.get(
                        url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            if text.strip().startswith("{"):
                                data = json.loads(text)
                                # Store created_timestamp for ALL tokens
                                ct = data.get("created_timestamp", 0)
                                if ct:
                                    all_created[mint] = ct / 1000 if ct > 1e12 else ct
                                ath = data.get("ath_market_cap", 0)
                                if isinstance(ath, (int, float)) and ath >= min_ath:
                                    results.append({
                                        "mint": mint,
                                        "name": data.get("name", ""),
                                        "symbol": data.get("symbol", ""),
                                        "ath_market_cap": ath,
                                        "usd_market_cap": data.get("usd_market_cap", 0),
                                        "created_timestamp": data.get("created_timestamp", 0),
                                        "complete": data.get("complete", False),
                                    })
                        elif resp.status == 429:
                            await asyncio.sleep(2)
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    pass
                except Exception as e:
                    logger.debug(f"Pump.fun ATH lookup error for {mint[:8]}: {e}")

                done_count += 1
                if on_progress and done_count % 1000 == 0:
                    try:
                        await on_progress(done_count, len(mints), len(results))
                    except Exception:
                        pass

        await asyncio.gather(*[_lookup_one(m) for m in mints])

        # Sort by ATH descending
        results.sort(key=lambda x: x.get("ath_market_cap", 0), reverse=True)

        logger.info(
            f"Pump.fun ATH: {len(results)}/{len(mints)} tokens with ATH >= ${min_ath:,.0f}"
        )
        return results, all_created

    # ─── Key Health Check (cached) ────────────────────────────

    _health_cache: Optional[dict] = None
    _health_cache_time: float = 0.0
    _HEALTH_CACHE_TTL = 3600  # 1 hour

    async def check_keys_health(self, force: bool = False) -> dict:
        """
        Check all Moralis API keys status. Cached for 1 hour.

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

        sem = asyncio.Semaphore(25)

        async def _check_one(key: str) -> str:
            async with sem:
                url = f"{MORALIS_BASE_URL}/token/mainnet/exchange/pumpfun/new"
                headers = {"X-API-Key": key, "accept": "application/json"}
                params = {"limit": 1}
                for attempt in range(2):
                    try:
                        if attempt > 0:
                            await asyncio.sleep(2)
                        session = await self._get_session()
                        async with session.get(
                            url, headers=headers, params=params,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status == 200:
                                return "active"
                            if resp.status == 429:
                                if attempt < 1:
                                    continue
                                return "no_credits"
                            if resp.status in (401, 403):
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

        statuses = await asyncio.gather(*[_check_one(k) for k in self._keys])

        result = {
            "active": sum(1 for s in statuses if s == "active"),
            "no_credits": sum(1 for s in statuses if s == "no_credits"),
            "errors": sum(1 for s in statuses if s == "error"),
            "total": len(self._keys),
        }
        self._health_cache = result
        self._health_cache_time = now
        logger.info(
            f"Moralis key health: {result['active']} active, "
            f"{result['no_credits']} no credits, {result['total']} total"
        )
        return {**result, "cached": False}

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
