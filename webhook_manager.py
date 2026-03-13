"""
Degen Scanner - Webhook Manager
Manages Helius webhook lifecycle (create/update/delete) with persistence.

Helius Webhook API docs:
  https://docs.helius.dev/webhooks-and-websockets/webhooks

Each watcher gets its own webhook. Webhooks persist across restarts via data/webhooks.json.
First WEBHOOK_KEY_RESERVE API keys are reserved for webhooks, rest for polling.

Usage:
    manager = WebhookManager(api_keys=HELIUS_API_KEYS, webhook_url="https://xxx.ngrok.io")
    watcher = await manager.create_cex_watcher("ADDRESS", "Binance Hot")
    await manager.remove_watcher("ADDRESS")
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

import aiohttp

from config import HELIUS_API_KEYS, WEBHOOK_KEY_RESERVE, WEBHOOK_URL

logger = logging.getLogger(__name__)

HELIUS_WEBHOOK_API = "https://api.helius.xyz/v0/webhooks"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WEBHOOKS_FILE = os.path.join(DATA_DIR, "webhooks.json")


@dataclass
class WatcherConfig:
    """A single webhook watcher configuration."""
    id: str
    watcher_type: str  # "cex_wallet" or "token"
    address: str       # wallet address or token mint
    label: str
    webhook_id: str    # Helius webhook ID
    api_key_index: int  # which API key was used to create it

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "WatcherConfig":
        return WatcherConfig(
            id=d["id"],
            watcher_type=d["watcher_type"],
            address=d["address"],
            label=d["label"],
            webhook_id=d["webhook_id"],
            api_key_index=d["api_key_index"],
        )


class WebhookManager:
    """
    Manages Helius webhooks for real-time transaction monitoring.

    Partitions API keys:
    - First WEBHOOK_KEY_RESERVE keys → webhook creation/management
    - Remaining keys → polling (HeliusClient)
    """

    def __init__(
        self,
        api_keys: list[str] = HELIUS_API_KEYS,
        webhook_url: str = WEBHOOK_URL,
        key_reserve: int = WEBHOOK_KEY_RESERVE,
    ):
        self._all_keys = list(api_keys)
        self._webhook_url = webhook_url
        self._key_reserve = min(key_reserve, len(self._all_keys))

        # Round-robin index for webhook keys
        self._key_index = 0

        self._watchers: list[WatcherConfig] = []
        self._session: Optional[aiohttp.ClientSession] = None

        self._load()

        logger.info(
            f"WebhookManager initialized: "
            f"{len(self._watchers)} watchers, "
            f"{self._key_reserve} reserved keys, "
            f"URL={self._webhook_url or '(not set)'}"
        )

    @property
    def webhook_url(self) -> str:
        return self._webhook_url

    @property
    def webhook_keys(self) -> list[str]:
        """API keys reserved for webhook operations."""
        return self._all_keys[:self._key_reserve]

    @property
    def polling_keys(self) -> list[str]:
        """API keys available for polling (not reserved for webhooks)."""
        if self._key_reserve >= len(self._all_keys):
            return self._all_keys
        return self._all_keys[self._key_reserve:]

    def _next_webhook_key(self) -> tuple[str, int]:
        """Get next webhook API key (round-robin). Returns (key, index)."""
        keys = self.webhook_keys
        if not keys:
            raise RuntimeError("No API keys available for webhooks")
        idx = self._key_index % len(keys)
        self._key_index = (self._key_index + 1) % len(keys)
        return keys[idx], idx

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    # ─── Persistence ──────────────────────────────────────────

    def _load(self) -> None:
        """Load watchers from data/webhooks.json."""
        try:
            if os.path.exists(WEBHOOKS_FILE):
                with open(WEBHOOKS_FILE, "r") as f:
                    data = json.load(f)
                self._watchers = [WatcherConfig.from_dict(d) for d in data]
                logger.info(f"Loaded {len(self._watchers)} webhook watchers")
        except Exception as e:
            logger.warning(f"Failed to load webhooks: {e}")

    def _save(self) -> None:
        """Save watchers to data/webhooks.json."""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(WEBHOOKS_FILE, "w") as f:
                json.dump([w.to_dict() for w in self._watchers], f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save webhooks: {e}")

    # ─── Helius Webhook API ───────────────────────────────────

    async def _create_webhook(
        self,
        account_addresses: list[str],
        webhook_type: str,
        api_key: str,
    ) -> Optional[str]:
        """
        Create a Helius webhook via API.

        Args:
            account_addresses: Addresses to monitor
            webhook_type: "enhanced" for enhanced transactions
            api_key: Helius API key

        Returns:
            Webhook ID or None on failure
        """
        if not self._webhook_url:
            logger.error("WEBHOOK_URL not set — cannot create webhook")
            return None

        url = f"{HELIUS_WEBHOOK_API}?api-key={api_key}"
        payload = {
            "webhookURL": f"{self._webhook_url}/webhook/helius",
            "transactionTypes": ["Any"],
            "accountAddresses": account_addresses,
            "webhookType": webhook_type,
        }

        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    webhook_id = data.get("webhookID", "")
                    logger.info(f"Helius webhook created: {webhook_id}")
                    return webhook_id

                body = await resp.text()
                logger.error(f"Helius webhook create failed ({resp.status}): {body[:300]}")
                return None
        except Exception as e:
            logger.error(f"Helius webhook create error: {e}")
            return None

    async def _delete_webhook(self, webhook_id: str, api_key: str) -> bool:
        """Delete a Helius webhook."""
        url = f"{HELIUS_WEBHOOK_API}/{webhook_id}?api-key={api_key}"
        try:
            session = await self._get_session()
            async with session.delete(url) as resp:
                if resp.status in (200, 204):
                    logger.info(f"Helius webhook deleted: {webhook_id}")
                    return True
                body = await resp.text()
                logger.error(f"Helius webhook delete failed ({resp.status}): {body[:300]}")
                return False
        except Exception as e:
            logger.error(f"Helius webhook delete error: {e}")
            return False

    async def _update_webhook_url_single(
        self, webhook_id: str, new_url: str, api_key: str
    ) -> bool:
        """Update the webhook URL for a single webhook."""
        url = f"{HELIUS_WEBHOOK_API}/{webhook_id}?api-key={api_key}"
        payload = {
            "webhookURL": f"{new_url}/webhook/helius",
        }
        try:
            session = await self._get_session()
            async with session.put(url, json=payload) as resp:
                if resp.status in (200, 204):
                    return True
                body = await resp.text()
                logger.error(f"Webhook URL update failed ({resp.status}): {body[:200]}")
                return False
        except Exception as e:
            logger.error(f"Webhook URL update error: {e}")
            return False

    # ─── Public API ───────────────────────────────────────────

    async def create_cex_watcher(self, address: str, label: str) -> Optional[WatcherConfig]:
        """
        Create a watcher for a CEX hot wallet address.

        Returns:
            WatcherConfig on success, None on failure
        """
        # Check if already watching
        existing = self._find_watcher(address)
        if existing:
            logger.info(f"Already watching {address[:8]}... ({existing.label})")
            return existing

        api_key, key_idx = self._next_webhook_key()
        webhook_id = await self._create_webhook(
            account_addresses=[address],
            webhook_type="enhanced",
            api_key=api_key,
        )
        if not webhook_id:
            return None

        watcher = WatcherConfig(
            id=uuid.uuid4().hex[:8],
            watcher_type="cex_wallet",
            address=address,
            label=label,
            webhook_id=webhook_id,
            api_key_index=key_idx,
        )
        self._watchers.append(watcher)
        self._save()
        logger.info(f"CEX watcher created: {label} ({address[:8]}...)")
        return watcher

    async def create_token_watcher(self, mint: str, label: str) -> Optional[WatcherConfig]:
        """
        Create a watcher for an SPL token (by mint address).

        Returns:
            WatcherConfig on success, None on failure
        """
        existing = self._find_watcher(mint)
        if existing:
            logger.info(f"Already watching token {mint[:8]}... ({existing.label})")
            return existing

        api_key, key_idx = self._next_webhook_key()
        webhook_id = await self._create_webhook(
            account_addresses=[mint],
            webhook_type="enhanced",
            api_key=api_key,
        )
        if not webhook_id:
            return None

        watcher = WatcherConfig(
            id=uuid.uuid4().hex[:8],
            watcher_type="token",
            address=mint,
            label=label,
            webhook_id=webhook_id,
            api_key_index=key_idx,
        )
        self._watchers.append(watcher)
        self._save()
        logger.info(f"Token watcher created: {label} ({mint[:8]}...)")
        return watcher

    async def remove_watcher(self, id_or_address: str) -> Optional[WatcherConfig]:
        """
        Remove a watcher by ID, address, or address prefix.

        Returns:
            Removed WatcherConfig or None if not found
        """
        target = None
        for w in self._watchers:
            if (
                w.id == id_or_address
                or w.address == id_or_address
                or w.address.startswith(id_or_address)
            ):
                target = w
                break

        if not target:
            return None

        # Delete from Helius
        api_key = self.webhook_keys[target.api_key_index % len(self.webhook_keys)]
        await self._delete_webhook(target.webhook_id, api_key)

        self._watchers = [w for w in self._watchers if w.id != target.id]
        self._save()
        logger.info(f"Watcher removed: {target.label} ({target.address[:8]}...)")
        return target

    def get_watchers(self) -> list[WatcherConfig]:
        """Get all active watchers."""
        return list(self._watchers)

    def get_watcher_by_address(self, address: str) -> Optional[WatcherConfig]:
        """Find a watcher by monitored address."""
        return self._find_watcher(address)

    async def update_webhook_url(self, new_url: str) -> tuple[int, int]:
        """
        Update the webhook URL for ALL active webhooks.
        Called when tunnel URL changes.

        Returns:
            (success_count, fail_count)
        """
        self._webhook_url = new_url
        success = 0
        fail = 0

        for w in self._watchers:
            api_key = self.webhook_keys[w.api_key_index % len(self.webhook_keys)]
            ok = await self._update_webhook_url_single(w.webhook_id, new_url, api_key)
            if ok:
                success += 1
            else:
                fail += 1

        logger.info(f"Webhook URL updated: {success} ok, {fail} failed → {new_url}")
        return success, fail

    async def sync_webhooks(self) -> None:
        """
        Startup reconciliation — verify all stored webhooks still exist on Helius.
        Remove any that return 404.
        """
        if not self._watchers:
            return

        stale = []
        for w in self._watchers:
            api_key = self.webhook_keys[w.api_key_index % len(self.webhook_keys)]
            url = f"{HELIUS_WEBHOOK_API}/{w.webhook_id}?api-key={api_key}"
            try:
                session = await self._get_session()
                async with session.get(url) as resp:
                    if resp.status == 404:
                        stale.append(w.id)
                        logger.warning(f"Webhook {w.webhook_id} gone (404), removing watcher {w.label}")
            except Exception as e:
                logger.warning(f"Sync check failed for {w.label}: {e}")

        if stale:
            self._watchers = [w for w in self._watchers if w.id not in set(stale)]
            self._save()
            logger.info(f"Sync: removed {len(stale)} stale watcher(s)")

    def _find_watcher(self, address: str) -> Optional[WatcherConfig]:
        """Find a watcher by exact address match."""
        for w in self._watchers:
            if w.address == address:
                return w
        return None

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
