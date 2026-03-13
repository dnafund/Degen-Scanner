"""
Degen Scanner - Whale Splitter v2

Monitors wallets that receive large CEX withdrawals (≥12 SOL, new wallets with *)
and checks on-chain if they split funds to sub-wallets.

Flow:
  1. scan_messages detects transfer ≥12 SOL with * → add to whale queue
  2. Every 5 min scan: check all queued wallets via Helius API
  3. Split detected (SYSTEM_PROGRAM transfers) → ALERT + remove
  4. Swap/DEX detected → remove from queue
  5. Idle (no activity) → keep checking (up to 30 days, then auto-purge)
  6. Wrap SOL → keep checking

Persistence:
  - whale_queue.json: wallets waiting to be checked (survives restart)
  - whale_alerted.json: wallets already alerted (24h retention)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Optional, Set

from helius import HeliusClient, SolTransfer, is_on_curve
from parser import ParsedTransfer
from cluster import normalize_cex

logger = logging.getLogger(__name__)

# Minimum SOL to qualify as whale (can be overridden via .env or /whalethreshold)
MIN_WHALE_SOL = float(os.getenv("MIN_WHALE_SOL", "12.0"))

# Minimum sub-wallets to count as a split
MIN_SPLIT_DESTINATIONS = 1

# Max wallets to check per scan cycle (0 = unlimited, check all)
BATCH_SIZE = 0

# Max concurrent Helius API calls (auto-set to key count at runtime)
MAX_CONCURRENT = 127  # fallback, overridden in SplitterEngine.__init__

# Max age before auto-purging from queue (30 days)
MAX_QUEUE_AGE = timedelta(days=30)

# Persistence files
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
QUEUE_FILE = os.path.join(DATA_DIR, "whale_queue.json")
ALERTED_FILE = os.path.join(DATA_DIR, "whale_alerted.json")


@dataclass
class WhaleSplit:
    """A detected whale fund-splitting event."""
    cex: str
    deposit_wallet: str
    deposit_amount: float
    deposit_timestamp: datetime
    deposit_tx_link: Optional[str]
    sub_transfers: List[SolTransfer]

    @property
    def sub_wallet_count(self) -> int:
        return len(set(t.to_wallet for t in self.sub_transfers))

    @property
    def total_split_amount(self) -> float:
        return sum(t.amount_sol for t in self.sub_transfers)

    def __repr__(self):
        return (
            f"WhaleSplit({self.cex} → {self.deposit_wallet[:8]}... | "
            f"{self.deposit_amount:.2f} SOL → {self.sub_wallet_count} sub-wallets)"
        )


@dataclass
class QueuedWallet:
    """A whale wallet in the check queue."""
    wallet: str
    cex: str
    amount: float
    timestamp: datetime  # when CEX withdrawal happened
    tx_link: Optional[str] = None
    added_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "cex": self.cex,
            "amount": self.amount,
            "timestamp": self.timestamp.isoformat(),
            "tx_link": self.tx_link,
            "added_at": self.added_at.isoformat(),
        }

    @staticmethod
    def from_dict(d: dict) -> "QueuedWallet":
        return QueuedWallet(
            wallet=d["wallet"],
            cex=d["cex"],
            amount=d["amount"],
            timestamp=datetime.fromisoformat(d["timestamp"]),
            tx_link=d.get("tx_link"),
            added_at=datetime.fromisoformat(d.get("added_at", datetime.utcnow().isoformat())),
        )


class SplitterEngine:
    """
    Whale splitter v2 — persistent queue, no retry limit.

    Wallets stay in queue until:
    - Split/transfer detected → alert + remove + save to alerted (24h)
    - Swap/contract detected → remove
    - Idle for 30+ days → auto-purged
    """

    def __init__(self, helius: HeliusClient):
        self.helius = helius
        # Cap concurrency at 30 to avoid IP-level detection
        # Keys rotate round-robin so all keys get used over time
        self.max_concurrent = min(len(helius._keys), 30)

        # Queue: wallets waiting to be checked
        self._queue: List[QueuedWallet] = []

        # Alerted wallets (24h retention)
        self._alerted: dict[str, str] = {}  # wallet → ISO timestamp

        # Already-checked wallets (skip duplicates in add_whale)
        self._checked: set[str] = set()

        # Stats
        self._total_queued = 0
        self._total_checked = 0
        self._total_splits = 0
        self._total_removed_swap = 0

        # Scan cycle counter for priority scheduling
        self._cycle_count = 0

        # Load persisted data
        self._load_queue()
        self._load_alerted()

        logger.info(
            f"SplitterEngine v2 initialized: "
            f"queue={len(self._queue)}, alerted={len(self._alerted)}"
        )

    @property
    def enabled(self) -> bool:
        return self.helius.enabled

    # ─── Persistence ──────────────────────────────────────────

    def _load_queue(self) -> None:
        """Load whale queue from JSON file."""
        try:
            if os.path.exists(QUEUE_FILE):
                with open(QUEUE_FILE, "r") as f:
                    data = json.load(f)
                self._queue = [QueuedWallet.from_dict(d) for d in data]
                logger.info(f"📂 Loaded {len(self._queue)} whales from queue")
        except Exception as e:
            logger.warning(f"Failed to load whale queue: {e}")

    def _save_queue(self) -> None:
        """Save whale queue to JSON file."""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(QUEUE_FILE, "w") as f:
                json.dump([w.to_dict() for w in self._queue], f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save whale queue: {e}")

    def _load_alerted(self) -> None:
        """Load alerted wallets from JSON (24h retention)."""
        try:
            if os.path.exists(ALERTED_FILE):
                with open(ALERTED_FILE, "r") as f:
                    data = json.load(f)
                cutoff = datetime.utcnow() - timedelta(hours=24)
                self._alerted = {
                    w: ts for w, ts in data.items()
                    if datetime.fromisoformat(ts) > cutoff
                }
                logger.info(f"📂 Loaded {len(self._alerted)} alerted whales (24h)")
        except Exception as e:
            logger.warning(f"Failed to load whale alerted: {e}")

    def _save_alerted(self) -> None:
        """Save alerted wallets to JSON."""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(ALERTED_FILE, "w") as f:
                json.dump(self._alerted, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save whale alerted: {e}")

    # ─── Queue Management ─────────────────────────────────────

    def is_alerted(self, wallet: str) -> bool:
        """Check if wallet was already alerted (24h)."""
        return wallet in self._alerted

    def is_in_queue(self, wallet: str) -> bool:
        """Check if wallet is already in queue."""
        return any(w.wallet == wallet for w in self._queue)

    def add_whale(self, transfer: ParsedTransfer) -> bool:
        """
        Add a whale wallet to the queue.

        Conditions:
        - Amount ≥ MIN_WHALE_SOL
        - Not already in queue
        - Not already alerted (24h)

        Args:
            transfer: Parsed CEX withdrawal

        Returns:
            True if added, False if skipped
        """
        # Skip PDA/program addresses — only check real user wallets
        if not is_on_curve(transfer.wallet):
            logger.debug(f"Whale {transfer.wallet[:8]}... is off-curve (PDA), skipping")
            return False

        # Skip if already checked or already in queue
        if transfer.wallet in self._checked:
            logger.debug(f"Whale {transfer.wallet[:8]}... already checked, skipping")
            return False

        if self.is_in_queue(transfer.wallet):
            return False

        if self.is_alerted(transfer.wallet):
            return False

        queued = QueuedWallet(
            wallet=transfer.wallet,
            cex=normalize_cex(transfer.cex),
            amount=transfer.amount,
            timestamp=transfer.timestamp,
            tx_link=transfer.tx_link,
        )

        self._queue.append(queued)
        self._total_queued += 1
        self._save_queue()

        logger.info(
            f"🐋 Whale queued: {transfer.wallet[:8]}... | "
            f"{transfer.cex} | {transfer.amount:.2f} SOL"
        )
        return True

    # ─── Check Pending ────────────────────────────────────────

    async def check_pending(self) -> List[WhaleSplit]:
        """
        Check all queued wallets for activity via Helius.

        Returns:
            List of detected WhaleSplit events
        """
        if not self._queue:
            return []

        if not self.helius.enabled:
            logger.warning("Helius not configured, skipping whale checks")
            return []

        # Auto-purge wallets older than 30 days
        now_purge = datetime.utcnow()
        old_count = len(self._queue)
        self._queue = [
            w for w in self._queue
            if (now_purge - w.added_at) < MAX_QUEUE_AGE
        ]
        purged = old_count - len(self._queue)
        if purged:
            logger.info(f"🗑️ Purged {purged} whale wallet(s) older than 30 days")
            self._save_queue()

        if not self._queue:
            return []

        splits: List[WhaleSplit] = []
        to_remove: List[str] = []  # wallet addresses to remove

        # Priority-based scheduling (parallel, no batch limit):
        #   < 24h old  → every cycle (5 min)
        #   1-3 days   → every 3rd cycle (15 min)
        #   3+ days    → every 10th cycle (50 min)
        self._cycle_count += 1
        now = datetime.utcnow()

        check_list: List[QueuedWallet] = []
        for qw in self._queue:
            age = now - qw.added_at
            if age < timedelta(hours=24):
                check_list.append(qw)
            elif age < timedelta(days=3):
                if self._cycle_count % 3 == 0:
                    check_list.append(qw)
            else:
                if self._cycle_count % 10 == 0:
                    check_list.append(qw)

        logger.info(
            f"🔍 Checking {len(check_list)}/{len(self._queue)} whale wallets "
            f"(cycle #{self._cycle_count}, concurrency={self.max_concurrent})"
        )

        # Parallel checking — cap concurrency, assign unique keys
        semaphore = asyncio.Semaphore(self.max_concurrent)
        error_count = 0
        num_keys = len(self.helius._keys)

        async def _check_one(idx: int, qw: QueuedWallet) -> tuple:
            """Check a single wallet. Returns (qw, activity)."""
            nonlocal error_count
            if error_count >= 10:
                return (qw, "skip")

            # Each task gets a unique key via index mod
            key = self.helius._keys[idx % num_keys]
            async with semaphore:
                activity = await self.helius.check_wallet_activity_parallel(
                    wallet=qw.wallet,
                    key=key,
                    after_timestamp=qw.timestamp,
                )
            if activity == "error":
                error_count += 1
            return (qw, activity)

        # Fire all checks in parallel
        results = await asyncio.gather(
            *[_check_one(i, qw) for i, qw in enumerate(check_list)],
            return_exceptions=True,
        )

        # Process results
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Parallel check exception: {result}")
                continue

            qw, activity = result
            self._total_checked += 1

            if activity in ("error", "skip"):
                continue

            if activity == "split":
                # Get transfer details (sequential — only for splits)
                outgoing = await self.helius.get_outgoing_sol_transfers(
                    wallet=qw.wallet,
                    after_timestamp=qw.timestamp,
                )

                if outgoing:
                    split = WhaleSplit(
                        cex=qw.cex,
                        deposit_wallet=qw.wallet,
                        deposit_amount=qw.amount,
                        deposit_timestamp=qw.timestamp,
                        deposit_tx_link=qw.tx_link,
                        sub_transfers=outgoing,
                    )
                    splits.append(split)
                    self._total_splits += 1
                    logger.info(f"🚨 Split detected: {split}")

                # Mark as alerted + remove from queue
                self._alerted[qw.wallet] = datetime.utcnow().isoformat()
                to_remove.append(qw.wallet)

            elif activity == "swap":
                logger.info(f"💱 Whale {qw.wallet[:8]}... swapped, removing from queue")
                self._total_removed_swap += 1
                to_remove.append(qw.wallet)

            # "idle" and "wrap" → keep in queue, do nothing

        # Remove processed wallets
        remove_set = set(to_remove)
        self._checked.update(remove_set)
        if remove_set:
            self._queue = [w for w in self._queue if w.wallet not in remove_set]
            self._save_queue()
            self._save_alerted()
        else:
            # Rotate: move checked batch to end so next scan checks different wallets
            checked_count = len(check_list)
            if checked_count < len(self._queue):
                self._queue = self._queue[checked_count:] + self._queue[:checked_count]

        if splits:
            logger.info(f"🐋 Found {len(splits)} whale splits!")

        return splits

    def get_queue(self) -> List[QueuedWallet]:
        """Get a copy of the current queue (sorted by amount desc)."""
        return sorted(self._queue, key=lambda w: w.amount, reverse=True)

    def remove_wallet(self, wallet: str) -> bool:
        """
        Remove a wallet from the queue by full address or prefix match.

        Returns:
            True if found and removed, False otherwise
        """
        match = None
        for qw in self._queue:
            if qw.wallet == wallet or qw.wallet.startswith(wallet):
                match = qw
                break

        if not match:
            return False

        self._queue = [w for w in self._queue if w.wallet != match.wallet]
        self._save_queue()
        logger.info(f"🗑️ Removed whale {match.wallet[:8]}... from queue")
        return True

    def clear_queue(self, max_age_days: Optional[int] = None) -> int:
        """
        Clear wallets from the queue.

        Args:
            max_age_days: If set, only remove wallets older than N days.
                          If None, clear ALL wallets.

        Returns:
            Number of wallets removed
        """
        old_count = len(self._queue)

        if max_age_days is not None:
            cutoff = datetime.utcnow() - timedelta(days=max_age_days)
            self._queue = [w for w in self._queue if w.added_at > cutoff]
        else:
            self._queue = []

        removed = old_count - len(self._queue)
        if removed:
            self._save_queue()
            logger.info(f"🗑️ Cleared {removed} whale(s) from queue")
        return removed

    def get_min_sol(self) -> float:
        """Get current MIN_WHALE_SOL threshold."""
        global MIN_WHALE_SOL
        return MIN_WHALE_SOL

    def set_min_sol(self, value: float) -> None:
        """Update MIN_WHALE_SOL threshold at runtime."""
        global MIN_WHALE_SOL
        MIN_WHALE_SOL = value
        logger.info(f"🐋 MIN_WHALE_SOL updated to {value}")

    def get_stats(self) -> dict:
        """Get splitter engine statistics."""
        helius_stats = self.helius.get_stats()
        return {
            "enabled": self.enabled,
            "queue_size": len(self._queue),
            "total_queued": self._total_queued,
            "total_checked": self._total_checked,
            "total_splits": self._total_splits,
            "total_removed_swap": self._total_removed_swap,
            "alerted_wallets": len(self._alerted),
            "helius": helius_stats,
        }
