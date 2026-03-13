"""
Degen Scanner - Clustering Engine
Groups wallets by CEX source and time window to detect coordinated activity.

Clustering conditions for a "box" (cluster):
1. Same CEX source (each exchange scanned separately)
2. Within configurable time window (10-20 min)
3. Amount patterns detected:
   - SAME: All wallets receive the same amount (e.g., 5 wallets x 1 SOL)
   - INCREASING: Amounts go up with time (e.g., 0.2 SOL @8:51, 0.3 @8:52, 0.5 @8:53)
   - SIMILAR: Amounts are close to each other (e.g., 2.1, 2.2, 2.15)
   - RAPID_FIRE: Different amounts withdrawn in rapid succession
"""

import json
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum

from parser import ParsedTransfer
from config import TIME_WINDOW_MINUTES, MIN_CLUSTER_SIZE

# File to persist alerted wallets across restarts
ALERTED_FILE = os.path.join(os.path.dirname(__file__), "data", "alerted_wallets.json")

# Max SOL spread between wallets in a cluster
# e.g., 1.0 means all amounts must be within 1 SOL of each other
MAX_AMOUNT_SPREAD = float(os.getenv("MAX_AMOUNT_SPREAD", "1.0"))

# CEX grouping — merge multiple hot wallets into one CEX for clustering
# Key: keyword to match (lowercase), Value: normalized display name
CEX_GROUPS = {
    "coinbase": "Coinbase 🐳",
}


def normalize_cex(cex_name: str) -> str:
    """
    Normalize CEX name for clustering.
    Groups related exchanges (e.g., all coinbase variants → 'Coinbase 🐳').
    """
    lower = cex_name.lower()
    for keyword, display in CEX_GROUPS.items():
        if keyword in lower:
            return display
    return cex_name

logger = logging.getLogger(__name__)


class AmountPattern(Enum):
    """Types of amount patterns detected in a cluster."""
    SAME = "same"           # All amounts are identical
    INCREASING = "increasing"  # Amounts go up sequentially
    SIMILAR = "similar"     # Amounts are close to each other (within 10%)
    RAPID_FIRE = "rapid_fire"  # Different amounts, rapid withdrawals

    @property
    def label(self) -> str:
        """Human-readable label with emoji."""
        labels = {
            "same": "🎯 Same Amount",
            "increasing": "📈 Increasing",
            "similar": "🔄 Similar Amount",
            "rapid_fire": "⚡ Rapid Fire",
        }
        return labels.get(self.value, self.value)


def detect_amount_pattern(transfers: List[ParsedTransfer]) -> AmountPattern:
    """
    Detect the amount pattern in a list of transfers.

    INCREASING checks amounts in chronological (timestamp) order:
    e.g., wallet1 at 8:51 → 0.2 SOL, wallet2 at 8:52 → 0.3 SOL

    Args:
        transfers: List of ParsedTransfer (with timestamp + amount)

    Returns:
        The detected AmountPattern type
    """
    if not transfers or len(transfers) < 2:
        return AmountPattern.RAPID_FIRE

    # Sort by timestamp → chronological order
    sorted_by_time = sorted(transfers, key=lambda t: t.timestamp)

    # Filter out zero amounts while keeping time order
    non_zero = [t for t in sorted_by_time if t.amount > 0]
    if len(non_zero) < 2:
        return AmountPattern.RAPID_FIRE

    amounts = [t.amount for t in non_zero]

    # Check SAME: all amounts are identical (with tiny float tolerance)
    if all(abs(a - amounts[0]) < 0.001 for a in amounts):
        return AmountPattern.SAME

    # Check INCREASING: amounts go UP with time (chronological order)
    # e.g., 0.2 SOL (8:51) → 0.3 SOL (8:52) → 0.5 SOL (8:53)
    if all(amounts[i] < amounts[i + 1] for i in range(len(amounts) - 1)):
        return AmountPattern.INCREASING

    # Check SIMILAR: all amounts within 10% of the average
    avg = sum(amounts) / len(amounts)
    if avg > 0:
        threshold = avg * 0.10  # 10% tolerance
        if all(abs(a - avg) <= threshold for a in amounts):
            return AmountPattern.SIMILAR

    # Default: RAPID_FIRE (different amounts, rapid withdrawal pattern)
    return AmountPattern.RAPID_FIRE


@dataclass
class Cluster:
    """A detected cluster of wallets from the same CEX."""
    cex: str
    wallets: List[ParsedTransfer]
    pattern: AmountPattern = AmountPattern.RAPID_FIRE
    detected_at: datetime = field(default_factory=datetime.now)

    @property
    def total_amount(self) -> float:
        return sum(w.amount for w in self.wallets)

    @property
    def wallet_count(self) -> int:
        return len(self.wallets)

    @property
    def time_range(self) -> tuple:
        """Return (earliest, latest) timestamps in the cluster."""
        times = [w.timestamp for w in self.wallets]
        return (min(times), max(times))

    def __repr__(self):
        start, end = self.time_range
        return (
            f"Cluster({self.cex} | {self.pattern.value} | {self.wallet_count} wallets | "
            f"{self.total_amount:.2f} SOL | {start:%H:%M}-{end:%H:%M})"
        )


class ClusterEngine:
    """
    Sliding window clustering engine.

    Maintains an in-memory window of recent transfers grouped by CEX.
    Detects when multiple wallets are funded from the same CEX within the time window.
    Each CEX is scanned independently - never mixed together.
    """

    def __init__(
        self,
        time_window_minutes: int = TIME_WINDOW_MINUTES,
        min_cluster_size: int = MIN_CLUSTER_SIZE,
    ):
        self.time_window = timedelta(minutes=time_window_minutes)
        self.min_cluster_size = min_cluster_size

        # Transfers grouped by CEX name (lowercase) for per-CEX scanning
        # Key: cex_name_lower, Value: deque of ParsedTransfer
        self.cex_windows: dict[str, deque[ParsedTransfer]] = defaultdict(deque)

        # Track wallets already alerted to avoid duplicate alerts
        # Key: (wallet_address, cex_name_lower)
        self.alerted: Set[tuple[str, str]] = set()

        # Persistent alerted wallets — survives restart
        # Key: wallet_address, Value: ISO timestamp when alerted
        self._persistent_alerted: dict[str, str] = {}
        self._load_alerted()

        # Cleanup counter
        self._cleanup_counter = 0
        self._cleanup_interval = 100

        logger.info(
            f"ClusterEngine initialized: window={time_window_minutes}min, "
            f"min_size={min_cluster_size}, "
            f"persisted={len(self._persistent_alerted)} wallets"
        )

    def _load_alerted(self) -> None:
        """Load persisted alerted wallets from JSON file."""
        try:
            if os.path.exists(ALERTED_FILE):
                with open(ALERTED_FILE, "r") as f:
                    data = json.load(f)
                # Clean entries older than 24 hours
                cutoff = datetime.utcnow() - timedelta(hours=24)
                cleaned = {}
                for wallet, ts_str in data.items():
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts > cutoff:
                            cleaned[wallet] = ts_str
                    except (ValueError, TypeError):
                        pass
                self._persistent_alerted = cleaned
                logger.info(f"📂 Loaded {len(cleaned)} persisted wallets (24h)")
        except Exception as e:
            logger.warning(f"Failed to load alerted wallets: {e}")

    def _save_alerted(self) -> None:
        """Save alerted wallets to JSON file."""
        try:
            os.makedirs(os.path.dirname(ALERTED_FILE), exist_ok=True)
            with open(ALERTED_FILE, "w") as f:
                json.dump(self._persistent_alerted, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save alerted wallets: {e}")

    def is_wallet_alerted(self, wallet: str) -> bool:
        """Check if wallet was already alerted (persistent, 24h)."""
        return wallet in self._persistent_alerted

    def mark_wallet_alerted(self, wallet: str) -> None:
        """Mark wallet as alerted (persistent)."""
        self._persistent_alerted[wallet] = datetime.utcnow().isoformat()

    def _cleanup_window(self, cex_key: str, now: datetime) -> None:
        """Remove entries older than the time window for a specific CEX."""
        cutoff = now - self.time_window
        window = self.cex_windows[cex_key]
        while window and window[0].timestamp < cutoff:
            old = window.popleft()
            # Also remove from alerted set if expired
            self.alerted.discard((old.wallet, cex_key))
            logger.debug(f"Removed expired: {old.wallet[:8]}... from {old.cex}")

        # Remove empty windows
        if not window:
            del self.cex_windows[cex_key]

    def _cleanup_all(self, now: datetime) -> None:
        """Periodically clean all CEX windows."""
        self._cleanup_counter += 1
        if self._cleanup_counter >= self._cleanup_interval:
            self._cleanup_counter = 0
            for cex_key in list(self.cex_windows.keys()):
                self._cleanup_window(cex_key, now)

    def _find_amount_group(self, transfers: List[ParsedTransfer]) -> Optional[List[ParsedTransfer]]:
        """
        Find the largest group of transfers with amounts within MAX_AMOUNT_SPREAD.

        Uses a sliding window on sorted amounts to find the biggest
        group where max - min <= MAX_AMOUNT_SPREAD.

        Returns:
            List of transfers in the best group, or None if too small.
        """
        if len(transfers) < self.min_cluster_size:
            return None

        # Sort by amount
        sorted_by_amount = sorted(transfers, key=lambda t: t.amount)
        amounts = [t.amount for t in sorted_by_amount]

        best_start = 0
        best_end = 0
        left = 0

        for right in range(len(amounts)):
            # Shrink window until spread is within limit
            while amounts[right] - amounts[left] > MAX_AMOUNT_SPREAD:
                left += 1

            # Track the largest valid window
            if (right - left) > (best_end - best_start):
                best_start = left
                best_end = right

        best_size = best_end - best_start + 1

        if best_size >= self.min_cluster_size:
            return sorted_by_amount[best_start:best_end + 1]

        return None

    def _find_cluster(self, cex_key: str) -> Optional[Cluster]:
        """
        Check if there's a cluster for the given CEX.
        Each CEX is scanned independently.

        Conditions:
        1. Same CEX (already grouped by cex_key)
        2. Within time window (managed by deque cleanup)
        3. At least min_cluster_size wallets
        4. Amount spread filter for ALL patterns

        SAME/SIMILAR → pattern itself limits spread (identical or ≤10%)
        INCREASING/RAPID_FIRE → need spread filter to remove outliers
          (e.g., 1.4→1.5→1.6→12.0 → filter keeps 1.4,1.5,1.6)

        Returns a Cluster with detected amount pattern, or None.
        """
        window = self.cex_windows.get(cex_key)
        if not window:
            return None

        # Filter out already-alerted wallets (in-memory + persistent)
        new_transfers = [
            t for t in window
            if (t.wallet, cex_key) not in self.alerted
            and not self.is_wallet_alerted(t.wallet)
        ]

        if len(new_transfers) < self.min_cluster_size:
            return None

        # Check pattern of ALL wallets first
        pattern = detect_amount_pattern(new_transfers)

        # SAME/SIMILAR → inherently tight spread, no extra filter needed
        if pattern in (AmountPattern.SAME, AmountPattern.SIMILAR):
            return Cluster(
                cex=normalize_cex(new_transfers[0].cex),
                wallets=new_transfers,
                pattern=pattern,
            )

        # INCREASING/RAPID_FIRE → apply spread filter to remove outliers
        # e.g., OKX 1.4→1.5→1.6→12.0 → filter keeps [1.4, 1.5, 1.6]
        amount_group = self._find_amount_group(new_transfers)

        if not amount_group:
            return None

        # Re-detect pattern within the filtered group
        filtered_pattern = detect_amount_pattern(amount_group)

        return Cluster(
            cex=normalize_cex(amount_group[0].cex),
            wallets=amount_group,
            pattern=filtered_pattern,
        )

    def add_transfer(self, transfer: ParsedTransfer) -> None:
        """
        Add a new transfer to the appropriate CEX window.
        Does NOT check for clusters - call detect_clusters() after adding all transfers.

        Args:
            transfer: Parsed transfer data
        """
        now = transfer.timestamp
        cex_key = normalize_cex(transfer.cex).lower()

        # Clean expired entries for this CEX
        self._cleanup_window(cex_key, now)
        self._cleanup_all(now)

        # Check for duplicate wallet in this CEX window
        for existing in self.cex_windows.get(cex_key, []):
            if existing.wallet == transfer.wallet:
                logger.debug(
                    f"Duplicate wallet {transfer.wallet[:8]}... from {transfer.cex}, skipping"
                )
                return

        # Add to the CEX-specific window
        self.cex_windows[cex_key].append(transfer)

        logger.debug(
            f"Added: {transfer.wallet[:8]}... from {transfer.cex} "
            f"(cex_window: {len(self.cex_windows[cex_key])})"
        )

    def detect_clusters(self) -> List[Cluster]:
        """
        Scan ALL CEX windows for clusters. Call after adding all transfers.

        Returns all wallets that match (not just min_cluster_size).
        Each CEX is scanned independently.

        Returns:
            List of detected Clusters
        """
        clusters = []

        for cex_key in list(self.cex_windows.keys()):
            cluster = self._find_cluster(cex_key)
            if cluster:
                logger.info(f"🚨 Cluster detected: {cluster}")

                # Mark all wallets in cluster as alerted (in-memory + persistent)
                for t in cluster.wallets:
                    self.alerted.add((t.wallet, cex_key))
                    self.mark_wallet_alerted(t.wallet)

                clusters.append(cluster)

        # Save to disk after detection
        if clusters:
            self._save_alerted()

        return clusters

    def get_stats(self) -> dict:
        """Get current engine statistics."""
        cex_counts = {
            cex: len(window) for cex, window in self.cex_windows.items()
        }

        return {
            "total_in_windows": sum(cex_counts.values()),
            "active_cex_count": len(self.cex_windows),
            "alerted_wallets": len(self.alerted),
            "cex_breakdown": cex_counts,
            "time_window_minutes": self.time_window.total_seconds() / 60,
            "min_cluster_size": self.min_cluster_size,
        }
