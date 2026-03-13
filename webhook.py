"""
Degen Scanner - Webhook Server
HTTP server that receives real-time transaction events from Helius webhooks.

Endpoints:
  POST /webhook/helius  — receive enhanced transaction data
  GET  /health          — health check

Dedup: signature-based cache (10 min TTL)
Rate limit: max 1 alert per address per 5 seconds
Auto-queue whale if amount >= MIN_WHALE_SOL

Usage:
    server = WebhookServer(port=8080, alert_callback=my_callback)
    await server.start()
    ...
    await server.stop()
"""

import asyncio
import logging
import time
from typing import Callable, Awaitable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# Dedup cache TTL (seconds)
DEDUP_TTL = 600  # 10 minutes

# Rate limit: 1 alert per address per N seconds
RATE_LIMIT_SECONDS = 5


class WebhookServer:
    """
    Async HTTP server receiving Helius webhook events.

    Args:
        port: Port to listen on
        alert_callback: async function(event_type, tx_data, watcher_info) → called for each valid event
    """

    def __init__(
        self,
        port: int = 8080,
        alert_callback: Optional[Callable[..., Awaitable]] = None,
    ):
        self._port = port
        self._alert_callback = alert_callback
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Dedup: signature → timestamp
        self._seen_sigs: dict[str, float] = {}

        # Rate limit: address → last_alert_time
        self._rate_limit: dict[str, float] = {}

    async def start(self) -> None:
        """Start the HTTP server."""
        self._app = web.Application()
        self._app.router.add_get("/", self._handle_root)
        self._app.router.add_post("/webhook/helius", self._handle_helius)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await self._site.start()
        logger.info(f"Webhook server started on port {self._port}")

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Webhook server stopped")

    def set_alert_callback(self, callback: Callable[..., Awaitable]) -> None:
        """Set or update the alert callback."""
        self._alert_callback = callback

    # ─── Handlers ─────────────────────────────────────────────

    async def _handle_root(self, request: web.Request) -> web.Response:
        return web.json_response({
            "name": "Degen Scanner Webhook",
            "status": "running",
            "endpoints": ["/health", "/webhook/helius"],
        })

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "dedup_cache_size": len(self._seen_sigs),
        })

    async def _handle_helius(self, request: web.Request) -> web.Response:
        """
        Handle incoming Helius webhook POST.
        Payload is a list of enhanced transaction objects.
        """
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON")

        # Helius sends array of transactions
        if not isinstance(body, list):
            body = [body]

        now = time.monotonic()
        self._cleanup_dedup(now)

        processed = 0
        for tx in body:
            sig = tx.get("signature", "")
            if not sig:
                continue

            # Dedup by signature
            if sig in self._seen_sigs:
                continue
            self._seen_sigs[sig] = now

            # Process transaction
            await self._process_transaction(tx, now)
            processed += 1

        logger.debug(f"Webhook received {len(body)} tx(s), processed {processed}")
        return web.Response(status=200, text="OK")

    async def _process_transaction(self, tx: dict, now: float) -> None:
        """Classify and route a single transaction."""
        if not self._alert_callback:
            return

        sig = tx.get("signature", "")
        tx_type = tx.get("type", "UNKNOWN")
        source = tx.get("source", "UNKNOWN")
        timestamp = tx.get("timestamp", 0)

        # Extract involved accounts for rate limiting
        account_keys = set()
        for nt in tx.get("nativeTransfers", []):
            account_keys.add(nt.get("fromUserAccount", ""))
            account_keys.add(nt.get("toUserAccount", ""))
        for tt in tx.get("tokenTransfers", []):
            account_keys.add(tt.get("fromUserAccount", ""))
            account_keys.add(tt.get("toUserAccount", ""))
            account_keys.add(tt.get("mint", ""))
        account_keys.discard("")

        # Rate limit check: skip if any involved address was alerted recently
        rate_limited = False
        for addr in account_keys:
            last_alert = self._rate_limit.get(addr, 0)
            if (now - last_alert) < RATE_LIMIT_SECONDS:
                rate_limited = True
                break

        if rate_limited:
            return

        # Update rate limit for all involved addresses
        for addr in account_keys:
            self._rate_limit[addr] = now

        # Classify: check nativeTransfers for SOL movements
        native_transfers = tx.get("nativeTransfers", [])
        token_transfers = tx.get("tokenTransfers", [])

        # Build event info
        event = {
            "signature": sig,
            "type": tx_type,
            "source": source,
            "timestamp": timestamp,
            "native_transfers": native_transfers,
            "token_transfers": token_transfers,
            "description": tx.get("description", ""),
            "fee": tx.get("fee", 0),
            "fee_payer": tx.get("feePayer", ""),
            "account_keys": list(account_keys),
            "raw": tx,
        }

        try:
            await self._alert_callback(event)
        except Exception as e:
            logger.error(f"Alert callback error for {sig[:16]}...: {e}")

    def _cleanup_dedup(self, now: float) -> None:
        """Remove expired entries from dedup cache."""
        expired = [
            sig for sig, ts in self._seen_sigs.items()
            if (now - ts) > DEDUP_TTL
        ]
        for sig in expired:
            del self._seen_sigs[sig]

        # Also clean rate limit entries older than 60s
        expired_rl = [
            addr for addr, ts in self._rate_limit.items()
            if (now - ts) > 60
        ]
        for addr in expired_rl:
            del self._rate_limit[addr]

    def get_stats(self) -> dict:
        """Get webhook server statistics."""
        return {
            "port": self._port,
            "dedup_cache_size": len(self._seen_sigs),
            "rate_limit_entries": len(self._rate_limit),
        }
