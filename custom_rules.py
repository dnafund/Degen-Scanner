"""
Degen Scanner - Custom Pattern Detection Engine
Matches transfers against user-defined pattern rules.

Pattern types:
- increasing:   amounts strictly increase over time
- decreasing:   amounts strictly decrease over time
- exact_amount: all amounts within tolerance_pct of median
- rapid_fire:   just needs min_wallets in time window

Supports max_interval_seconds: finds longest consecutive chain
where all gaps between adjacent transfers < limit.
"""

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Set, FrozenSet

from parser import ParsedTransfer
from storage import PatternRule, RuleStore

logger = logging.getLogger(__name__)


# ─── Data Classes ─────────────────────────────────────────────────

@dataclass
class CustomMatch:
    """A match detected by a custom pattern rule."""
    rule: PatternRule
    transfers: List[ParsedTransfer]
    detected_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def total_amount(self) -> float:
        return sum(t.amount for t in self.transfers)

    @property
    def wallet_count(self) -> int:
        return len(self.transfers)

    @property
    def cex_display(self) -> str:
        """Get the actual CEX name from matched transfers."""
        if self.transfers:
            return self.transfers[0].cex
        return self.rule.cex

    @property
    def time_range(self) -> tuple:
        """Return (earliest, latest) timestamps."""
        times = [t.timestamp for t in self.transfers]
        return (min(times), max(times))

    def __repr__(self):
        start, end = self.time_range
        return (
            f"CustomMatch(rule={self.rule.name} | {self.rule.pattern_type} | "
            f"{self.wallet_count} wallets | {self.total_amount:.2f} SOL | "
            f"{start:%H:%M}-{end:%H:%M})"
        )


# ─── Pattern Detection Helpers ────────────────────────────────────

def _is_increasing(amounts: List[float]) -> bool:
    """Check if amounts strictly increase."""
    return all(amounts[i] < amounts[i + 1] for i in range(len(amounts) - 1))


def _is_decreasing(amounts: List[float]) -> bool:
    """Check if amounts strictly decrease."""
    return all(amounts[i] > amounts[i + 1] for i in range(len(amounts) - 1))


def _is_exact_amount(amounts: List[float], tolerance_pct: float) -> bool:
    """Check if all amounts are within tolerance_pct of the median."""
    if not amounts:
        return False

    sorted_amounts = sorted(amounts)
    median = sorted_amounts[len(sorted_amounts) // 2]

    if median <= 0:
        return False

    threshold = median * (tolerance_pct / 100.0)
    return all(abs(a - median) <= threshold for a in amounts)


def _find_longest_chain(
    transfers: List[ParsedTransfer],
    max_gap_seconds: int,
) -> List[ParsedTransfer]:
    """
    Find the longest consecutive chain where all gaps < max_gap_seconds.

    Example:
        Transfers: T+0, T+30, T+45, T+200, T+230
        max_gap = 60s
        Gap T+45→T+200 = 155s > 60s
        Chain 1: [T+0, T+30, T+45] = 3 items ← longest
        Chain 2: [T+200, T+230] = 2 items

    Returns the longest chain.
    """
    if len(transfers) <= 1:
        return transfers

    # Sort by timestamp
    sorted_t = sorted(transfers, key=lambda t: t.timestamp)

    best_chain: List[ParsedTransfer] = []
    current_chain: List[ParsedTransfer] = [sorted_t[0]]

    for i in range(1, len(sorted_t)):
        gap = (sorted_t[i].timestamp - sorted_t[i - 1].timestamp).total_seconds()

        if gap <= max_gap_seconds:
            current_chain.append(sorted_t[i])
        else:
            # Gap too large — save best and start new chain
            if len(current_chain) > len(best_chain):
                best_chain = current_chain
            current_chain = [sorted_t[i]]

    # Check final chain
    if len(current_chain) > len(best_chain):
        best_chain = current_chain

    return best_chain


# ─── Custom Pattern Engine ────────────────────────────────────────

class CustomPatternEngine:
    """
    Matches transfers against user-defined pattern rules.

    Mirrors ClusterEngine architecture:
    - Per-rule sliding windows
    - Dedup via alerted set
    - add_transfer() → filter + buffer
    - detect_matches() → scan all rules → return matches
    """

    def __init__(self, store: RuleStore):
        self._store = store

        # Per-rule sliding windows: rule_id → deque[ParsedTransfer]
        self._windows: dict[str, deque[ParsedTransfer]] = defaultdict(deque)

        # Dedup: (rule_id, frozenset of wallet addresses) → already alerted
        self._alerted: Set[tuple[str, FrozenSet[str]]] = set()

        logger.info("CustomPatternEngine initialized")

    def add_transfer(self, transfer: ParsedTransfer) -> None:
        """
        Add a transfer to all matching rule windows.
        Filters by CEX keyword + amount range before buffering.
        """
        rules = self._store.get_enabled_rules()
        if not rules:
            return

        for rule in rules:
            # Filter by CEX
            if not rule.matches_cex(transfer.cex):
                continue

            # Filter by amount range
            if not rule.matches_amount(transfer.amount):
                continue

            window = self._windows[rule.id]

            # Check for duplicate wallet in this rule's window
            if any(t.wallet == transfer.wallet for t in window):
                continue

            window.append(transfer)

    def _cleanup_window(self, rule: PatternRule) -> None:
        """Remove transfers outside the rule's time window."""
        window = self._windows.get(rule.id)
        if not window:
            return

        cutoff = datetime.utcnow() - timedelta(minutes=rule.time_window_minutes)

        while window and window[0].timestamp < cutoff:
            window.popleft()

        if not window:
            del self._windows[rule.id]

    def _check_pattern(
        self,
        rule: PatternRule,
        transfers: List[ParsedTransfer],
    ) -> bool:
        """Check if transfers match the rule's pattern type."""
        # Sort by timestamp for pattern analysis
        sorted_t = sorted(transfers, key=lambda t: t.timestamp)
        amounts = [t.amount for t in sorted_t]

        if rule.pattern_type == "increasing":
            return _is_increasing(amounts)

        if rule.pattern_type == "decreasing":
            return _is_decreasing(amounts)

        if rule.pattern_type == "exact_amount":
            tolerance = rule.amount_tolerance_pct if rule.amount_tolerance_pct is not None else 5.0
            return _is_exact_amount(amounts, tolerance)

        if rule.pattern_type == "rapid_fire":
            # rapid_fire just needs min_wallets in the window — always matches
            return True

        return False

    def _detect_for_rule(self, rule: PatternRule) -> Optional[CustomMatch]:
        """
        Detect a match for a single rule.

        Steps:
        1. Clean expired transfers
        2. Apply max_interval_seconds chain filter (if set)
        3. Check min_wallets
        4. Check pattern type
        5. Dedup check
        """
        self._cleanup_window(rule)

        window = self._windows.get(rule.id)
        if not window:
            return None

        candidates = list(window)

        # Step 2: Apply max_interval_seconds if set
        if rule.max_interval_seconds is not None:
            candidates = _find_longest_chain(candidates, rule.max_interval_seconds)

        # Step 3: Check min_wallets
        if len(candidates) < rule.min_wallets:
            return None

        # Step 4: Check pattern type
        if not self._check_pattern(rule, candidates):
            return None

        # Step 5: Dedup — check if we already alerted this exact set
        wallet_set = frozenset(t.wallet for t in candidates)
        dedup_key = (rule.id, wallet_set)

        if dedup_key in self._alerted:
            return None

        # Match found!
        self._alerted.add(dedup_key)

        return CustomMatch(
            rule=rule,
            transfers=sorted(candidates, key=lambda t: t.timestamp),
        )

    def detect_matches(self) -> List[CustomMatch]:
        """
        Scan all enabled rules for pattern matches.

        Returns:
            List of CustomMatch objects
        """
        matches = []

        for rule in self._store.get_enabled_rules():
            match = self._detect_for_rule(rule)
            if match:
                logger.info(f"Custom match: {match}")
                matches.append(match)

        return matches

    def get_stats(self) -> dict:
        """Get engine statistics."""
        return {
            "rules_enabled": len(self._store.get_enabled_rules()),
            "rules_total": len(self._store.get_all_rules()),
            "windows_active": len(self._windows),
            "total_buffered": sum(len(w) for w in self._windows.values()),
            "alerted_count": len(self._alerted),
        }
