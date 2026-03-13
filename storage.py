"""
Degen Scanner - Custom Pattern Rule Storage
JSON-based CRUD for user-defined pattern rules + CEX auto-discovery registry.

Storage file: data/custom_rules.json
"""

import json
import logging
import os
import time
import secrets
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────

VALID_PATTERN_TYPES = {"increasing", "decreasing", "exact_amount", "rapid_fire"}
MIN_TIME_WINDOW = 1
MAX_TIME_WINDOW = 60
MIN_WALLETS = 2
MAX_WALLETS = 50
STORAGE_VERSION = 1


# ─── Data Classes ─────────────────────────────────────────────────

@dataclass
class PatternRule:
    """A user-defined custom detection rule."""
    id: str
    name: str
    cex: str                              # contains match (case-insensitive), "*" = any
    pattern_type: str                     # increasing / decreasing / exact_amount / rapid_fire
    time_window_minutes: int              # 1-60
    min_wallets: int                      # 2-50
    amount_min: Optional[float] = None    # SOL minimum per wallet
    amount_max: Optional[float] = None    # SOL maximum per wallet
    amount_tolerance_pct: Optional[float] = None  # for exact_amount: ±X%
    max_interval_seconds: Optional[int] = None    # max gap between consecutive wallets
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def matches_cex(self, cex_name: str) -> bool:
        """Check if a CEX name matches this rule's filter (case-insensitive contains)."""
        if self.cex == "*":
            return True
        return self.cex.lower() in cex_name.lower()

    def matches_amount(self, amount: float) -> bool:
        """Check if a SOL amount falls within this rule's range."""
        if self.amount_min is not None and amount < self.amount_min:
            return False
        if self.amount_max is not None and amount > self.amount_max:
            return False
        return True

    def to_dict(self) -> dict:
        """Serialize to dict for JSON storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PatternRule":
        """Deserialize from dict."""
        # Only pass known fields to avoid errors with extra keys
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


@dataclass
class CexEntry:
    """A tracked CEX exchange in the auto-discovery registry."""
    display: str        # Original display name (e.g., "MEXC 🧿")
    count: int = 0      # Total transfers seen
    last_seen: str = ""  # ISO timestamp of last activity

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CexEntry":
        return cls(
            display=data.get("display", ""),
            count=data.get("count", 0),
            last_seen=data.get("last_seen", ""),
        )


# ─── Rule Store ───────────────────────────────────────────────────

class RuleStore:
    """
    JSON-based storage for custom pattern rules + CEX registry.
    Thread-safe for single-process use (no concurrent writes).
    """

    def __init__(self, filepath: str):
        self._filepath = filepath
        self._rules: List[PatternRule] = []
        self._cex_registry: dict[str, CexEntry] = {}  # key = lowercase cex name
        self._load()

    # ─── File I/O ─────────────────────────────────────────────

    def _load(self) -> None:
        """Load rules and registry from JSON file."""
        if not os.path.exists(self._filepath):
            logger.info(f"No rules file found at {self._filepath}, starting fresh")
            return

        try:
            with open(self._filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Load rules
            for rule_data in data.get("rules", []):
                try:
                    rule = PatternRule.from_dict(rule_data)
                    self._rules.append(rule)
                except Exception as e:
                    logger.warning(f"Skipping invalid rule: {e}")

            # Load CEX registry
            for key, entry_data in data.get("cex_registry", {}).items():
                try:
                    self._cex_registry[key] = CexEntry.from_dict(entry_data)
                except Exception as e:
                    logger.warning(f"Skipping invalid CEX entry '{key}': {e}")

            logger.info(
                f"Loaded {len(self._rules)} rules, "
                f"{len(self._cex_registry)} CEX entries from {self._filepath}"
            )

        except json.JSONDecodeError as e:
            logger.error(f"Corrupted rules file: {e}")
        except Exception as e:
            logger.error(f"Error loading rules: {e}")

    def _save(self) -> None:
        """Persist rules and registry to JSON file."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self._filepath), exist_ok=True)

            data = {
                "version": STORAGE_VERSION,
                "rules": [r.to_dict() for r in self._rules],
                "cex_registry": {k: v.to_dict() for k, v in self._cex_registry.items()},
            }

            with open(self._filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.debug(f"Saved {len(self._rules)} rules to {self._filepath}")

        except Exception as e:
            logger.error(f"Error saving rules: {e}")

    # ─── Rule CRUD ────────────────────────────────────────────

    @staticmethod
    def generate_id() -> str:
        """Generate a unique rule ID: rule_{timestamp}_{hex4}."""
        ts = int(time.time())
        hex4 = secrets.token_hex(2)
        return f"rule_{ts}_{hex4}"

    @staticmethod
    def validate_rule(
        name: str,
        cex: str,
        pattern_type: str,
        time_window_minutes: int,
        min_wallets: int,
        amount_min: Optional[float] = None,
        amount_max: Optional[float] = None,
        amount_tolerance_pct: Optional[float] = None,
        max_interval_seconds: Optional[int] = None,
    ) -> Optional[str]:
        """
        Validate rule parameters. Returns error message or None if valid.
        """
        if not name or not name.strip():
            return "Name cannot be empty"

        if len(name) > 100:
            return "Name too long (max 100 chars)"

        if not cex or not cex.strip():
            return "CEX filter cannot be empty"

        if pattern_type not in VALID_PATTERN_TYPES:
            return f"Invalid pattern type. Must be one of: {', '.join(VALID_PATTERN_TYPES)}"

        if not (MIN_TIME_WINDOW <= time_window_minutes <= MAX_TIME_WINDOW):
            return f"Time window must be {MIN_TIME_WINDOW}-{MAX_TIME_WINDOW} minutes"

        if not (MIN_WALLETS <= min_wallets <= MAX_WALLETS):
            return f"Min wallets must be {MIN_WALLETS}-{MAX_WALLETS}"

        if amount_min is not None and amount_min < 0:
            return "Amount min cannot be negative"

        if amount_max is not None and amount_max < 0:
            return "Amount max cannot be negative"

        if amount_min is not None and amount_max is not None:
            if amount_min > amount_max:
                return "Amount min cannot be greater than amount max"

        if amount_tolerance_pct is not None:
            if amount_tolerance_pct < 0 or amount_tolerance_pct > 100:
                return "Tolerance must be 0-100%"

        if max_interval_seconds is not None:
            if max_interval_seconds < 1 or max_interval_seconds > 3600:
                return "Max interval must be 1-3600 seconds"

        return None

    def add_rule(
        self,
        name: str,
        cex: str,
        pattern_type: str,
        time_window_minutes: int,
        min_wallets: int,
        amount_min: Optional[float] = None,
        amount_max: Optional[float] = None,
        amount_tolerance_pct: Optional[float] = None,
        max_interval_seconds: Optional[int] = None,
    ) -> PatternRule:
        """
        Create and persist a new pattern rule.

        Returns:
            The created PatternRule

        Raises:
            ValueError: If validation fails
        """
        error = self.validate_rule(
            name, cex, pattern_type, time_window_minutes, min_wallets,
            amount_min, amount_max, amount_tolerance_pct, max_interval_seconds,
        )
        if error:
            raise ValueError(error)

        rule = PatternRule(
            id=self.generate_id(),
            name=name.strip(),
            cex=cex.strip(),
            pattern_type=pattern_type,
            time_window_minutes=time_window_minutes,
            min_wallets=min_wallets,
            amount_min=amount_min,
            amount_max=amount_max,
            amount_tolerance_pct=amount_tolerance_pct,
            max_interval_seconds=max_interval_seconds,
        )

        self._rules.append(rule)
        self._save()

        logger.info(f"Added rule: {rule.id} — {rule.name}")
        return rule

    def get_rule(self, rule_id: str) -> Optional[PatternRule]:
        """Get a rule by ID."""
        for rule in self._rules:
            if rule.id == rule_id:
                return rule
        return None

    def get_all_rules(self) -> List[PatternRule]:
        """Get all rules."""
        return list(self._rules)

    def get_enabled_rules(self) -> List[PatternRule]:
        """Get only enabled rules."""
        return [r for r in self._rules if r.enabled]

    def get_max_time_window(self) -> int:
        """Max time_window_minutes from all enabled rules. Returns 0 if no rules."""
        rules = self.get_enabled_rules()
        if not rules:
            return 0
        return max(r.time_window_minutes for r in rules)

    def delete_rule(self, rule_id: str) -> bool:
        """Delete a rule by ID. Returns True if found and deleted."""
        for i, rule in enumerate(self._rules):
            if rule.id == rule_id:
                removed = self._rules.pop(i)
                self._save()
                logger.info(f"Deleted rule: {removed.id} — {removed.name}")
                return True
        return False

    def toggle_rule(self, rule_id: str) -> Optional[bool]:
        """Toggle a rule's enabled state. Returns new state or None if not found."""
        rule = self.get_rule(rule_id)
        if not rule:
            return None

        rule.enabled = not rule.enabled
        self._save()
        logger.info(f"Toggled rule {rule.id}: enabled={rule.enabled}")
        return rule.enabled

    # ─── CEX Registry ─────────────────────────────────────────

    def update_cex(self, cex_name: str) -> None:
        """
        Track a CEX name in the auto-discovery registry.
        Called for every transfer processed — updates count and last_seen.
        """
        key = cex_name.lower().strip()
        if not key:
            return

        now_iso = datetime.utcnow().isoformat()

        if key in self._cex_registry:
            entry = self._cex_registry[key]
            entry.count += 1
            entry.last_seen = now_iso
        else:
            self._cex_registry[key] = CexEntry(
                display=cex_name.strip(),
                count=1,
                last_seen=now_iso,
            )
            logger.info(f"New CEX discovered: {cex_name.strip()}")

    def save_cex_registry(self) -> None:
        """Persist CEX registry to disk (call after batch processing)."""
        self._save()

    def get_cex_list(self) -> List[CexEntry]:
        """Get all tracked exchanges, sorted by count descending."""
        entries = list(self._cex_registry.values())
        entries.sort(key=lambda e: e.count, reverse=True)
        return entries

    def get_cex_names(self) -> List[str]:
        """Get display names of all known exchanges (for wizard autocomplete)."""
        entries = self.get_cex_list()
        return [e.display for e in entries]
