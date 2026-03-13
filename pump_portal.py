"""
Degen Scanner - PumpPortal WebSocket Client

Real-time stream of ALL Pump.fun token events via PumpPortal's
free WebSocket API (no auth required).

Events:
  - subscribeNewToken: every new token creation on Pump.fun
  - subscribeMigration: tokens graduating to Raydium

Usage:
    portal = PumpPortalClient(on_new_token=callback, on_migration=callback)
    await portal.run_forever()  # reconnects automatically
"""

import asyncio
import logging
import time
from typing import Callable, Optional

import websockets

logger = logging.getLogger("pump_portal")

WS_URL = "wss://pumpportal.fun/api/data"
RECONNECT_BASE_DELAY = 5
RECONNECT_MAX_DELAY = 300


class PumpPortalClient:
    """
    WebSocket client for PumpPortal real-time token feed.

    Subscribes to new token creations and migration (graduation) events.
    Auto-reconnects with exponential backoff on disconnect.
    """

    def __init__(
        self,
        on_new_token: Optional[Callable] = None,
        on_migration: Optional[Callable] = None,
    ):
        self._on_new_token = on_new_token
        self._on_migration = on_migration
        self._running = False
        self._ws = None

        # Stats
        self._tokens_received = 0
        self._migrations_received = 0
        self._reconnects = 0
        self._connected_since: Optional[float] = None
        self._last_message_at: Optional[float] = None

    async def run_forever(self) -> None:
        """
        Main loop: connect → listen → on disconnect: backoff → reconnect.

        Runs until stop() is called. Backoff resets on successful connection.
        """
        self._running = True
        delay = RECONNECT_BASE_DELAY

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                logger.info("PumpPortal task cancelled")
                break
            except Exception as e:
                logger.error(f"PumpPortal connection error: {e}")

            if not self._running:
                break

            self._reconnects += 1
            logger.warning(
                f"PumpPortal disconnected, reconnecting in {delay}s "
                f"(attempt #{self._reconnects})"
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

        logger.info("PumpPortal client stopped")

    async def _connect_and_listen(self) -> None:
        """Connect to WebSocket and process messages until disconnect."""
        logger.info(f"Connecting to PumpPortal: {WS_URL}")

        async with websockets.connect(
            WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected_since = time.time()
            logger.info("PumpPortal connected")

            # Reset backoff on successful connect
            await self._subscribe(ws)

            async for raw_message in ws:
                if not self._running:
                    break
                try:
                    import json
                    data = json.loads(raw_message)
                    self._last_message_at = time.time()
                    await self._handle_message(data)
                except Exception as e:
                    logger.error(f"PumpPortal message error: {e}")

        self._ws = None
        self._connected_since = None

    async def _subscribe(self, ws) -> None:
        """Send subscription requests for new tokens and migrations."""
        import json

        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        logger.info("Subscribed to newToken events")

        await ws.send(json.dumps({"method": "subscribeMigration"}))
        logger.info("Subscribed to migration events")

    async def _handle_message(self, data: dict) -> None:
        """Route incoming message to the appropriate callback."""
        tx_type = data.get("txType", "")

        if tx_type == "create" and self._on_new_token:
            self._tokens_received += 1
            try:
                result = self._on_new_token(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"on_new_token callback error: {e}")

        elif tx_type == "migration" and self._on_migration:
            self._migrations_received += 1
            try:
                result = self._on_migration(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"on_migration callback error: {e}")

    def stop(self) -> None:
        """Signal the client to stop and disconnect."""
        self._running = False
        if self._ws:
            asyncio.ensure_future(self._ws.close())

    def get_stats(self) -> dict:
        """Return current stats for logging."""
        uptime = 0.0
        if self._connected_since:
            uptime = time.time() - self._connected_since

        return {
            "tokens_received": self._tokens_received,
            "migrations_received": self._migrations_received,
            "reconnects": self._reconnects,
            "connected": self._connected_since is not None,
            "uptime_seconds": round(uptime, 0),
            "last_message_ago": (
                round(time.time() - self._last_message_at, 1)
                if self._last_message_at
                else None
            ),
        }
