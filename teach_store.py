"""
Degen Scanner - Teach Store (SQLite)

Persistent storage for the teaching/learning system.
Uses the same data/scanner.db as db.py.

Tables:
  learned_cases      — each /teach invocation = 1 case
  learned_patterns   — extracted patterns (funder, collector, mint_authority, wallet_reuse)
  known_dev_wallets  — flat lookup for fast matching during autoscan
"""

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "scanner.db")


def _get_conn() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode for concurrent reads."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


# ─── Schema ───────────────────────────────────────────────────


def init_teach_db() -> None:
    """Create teach tables if they don't exist."""
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS learned_cases (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                mint            TEXT    NOT NULL,
                token_name      TEXT    NOT NULL DEFAULT '',
                token_symbol    TEXT    NOT NULL DEFAULT '',
                taught_wallets  TEXT    NOT NULL DEFAULT '[]',
                discovered_wallets TEXT NOT NULL DEFAULT '[]',
                funders         TEXT    NOT NULL DEFAULT '[]',
                collectors      TEXT    NOT NULL DEFAULT '[]',
                feedback        TEXT    NOT NULL DEFAULT 'pending',
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS learned_patterns (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id         INTEGER NOT NULL,
                pattern_type    TEXT    NOT NULL,
                wallet          TEXT    NOT NULL DEFAULT '',
                related_wallet  TEXT    NOT NULL DEFAULT '',
                confidence      REAL    NOT NULL DEFAULT 0.5,
                metadata        TEXT    NOT NULL DEFAULT '{}',
                created_at      TEXT    NOT NULL,
                FOREIGN KEY (case_id) REFERENCES learned_cases(id)
            );

            CREATE TABLE IF NOT EXISTS known_dev_wallets (
                wallet          TEXT    NOT NULL,
                role            TEXT    NOT NULL DEFAULT 'buyer',
                case_id         INTEGER NOT NULL,
                confidence      REAL    NOT NULL DEFAULT 0.5,
                mint            TEXT    NOT NULL DEFAULT '',
                created_at      TEXT    NOT NULL,
                PRIMARY KEY (wallet, case_id),
                FOREIGN KEY (case_id) REFERENCES learned_cases(id)
            );

            CREATE INDEX IF NOT EXISTS idx_cases_mint
                ON learned_cases(mint);
            CREATE INDEX IF NOT EXISTS idx_patterns_case
                ON learned_patterns(case_id);
            CREATE INDEX IF NOT EXISTS idx_patterns_type
                ON learned_patterns(pattern_type);
            CREATE INDEX IF NOT EXISTS idx_patterns_wallet
                ON learned_patterns(wallet);
            CREATE INDEX IF NOT EXISTS idx_devwallets_wallet
                ON known_dev_wallets(wallet);
            CREATE INDEX IF NOT EXISTS idx_devwallets_case
                ON known_dev_wallets(case_id);
        """)
        conn.commit()
        logger.info("Teach DB tables initialized")
    finally:
        conn.close()


# ─── Dataclasses ──────────────────────────────────────────────


@dataclass
class LearnedCase:
    id: int
    mint: str
    token_name: str
    token_symbol: str
    taught_wallets: list[str]
    discovered_wallets: list[str]
    funders: list[str]
    collectors: list[str]
    feedback: str  # "pending", "good", "bad"
    created_at: str
    updated_at: str


@dataclass
class LearnedPattern:
    id: int
    case_id: int
    pattern_type: str  # "funder_wallet", "collector_wallet", "mint_authority", "wallet_reuse"
    wallet: str
    related_wallet: str
    confidence: float
    metadata: dict


@dataclass
class KnownDevWallet:
    wallet: str
    role: str  # "buyer", "funder", "collector", "taught"
    case_id: int
    confidence: float
    mint: str


# ─── CRUD: Cases ──────────────────────────────────────────────


def save_case(
    mint: str,
    token_name: str = "",
    token_symbol: str = "",
    taught_wallets: list[str] = None,
    discovered_wallets: list[str] = None,
    funders: list[str] = None,
    collectors: list[str] = None,
) -> int:
    """Insert a new learned case. Returns the case ID."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO learned_cases
               (mint, token_name, token_symbol, taught_wallets,
                discovered_wallets, funders, collectors, feedback,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                mint,
                token_name,
                token_symbol,
                json.dumps(taught_wallets or []),
                json.dumps(discovered_wallets or []),
                json.dumps(funders or []),
                json.dumps(collectors or []),
                now,
                now,
            ),
        )
        conn.commit()
        case_id = cursor.lastrowid
        logger.info(f"Saved teach case #{case_id} for {mint[:12]}")
        return case_id
    finally:
        conn.close()


def get_case(case_id: int) -> Optional[LearnedCase]:
    """Get a single case by ID."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM learned_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if not row:
            return None
        return _row_to_case(row)
    finally:
        conn.close()


def list_cases(limit: int = 20) -> list[LearnedCase]:
    """List recent cases, newest first."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM learned_cases ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_case(r) for r in rows]
    finally:
        conn.close()


def update_feedback(case_id: int, feedback: str) -> bool:
    """Update case feedback ('good' or 'bad'). Returns True if found."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "UPDATE learned_cases SET feedback = ?, updated_at = ? WHERE id = ?",
            (feedback, now, case_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_case(case_id: int) -> bool:
    """Delete a case and its patterns + wallets. Returns True if found."""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM known_dev_wallets WHERE case_id = ?", (case_id,))
        conn.execute("DELETE FROM learned_patterns WHERE case_id = ?", (case_id,))
        cursor = conn.execute("DELETE FROM learned_cases WHERE id = ?", (case_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def _row_to_case(row: sqlite3.Row) -> LearnedCase:
    return LearnedCase(
        id=row["id"],
        mint=row["mint"],
        token_name=row["token_name"],
        token_symbol=row["token_symbol"],
        taught_wallets=json.loads(row["taught_wallets"]),
        discovered_wallets=json.loads(row["discovered_wallets"]),
        funders=json.loads(row["funders"]),
        collectors=json.loads(row["collectors"]),
        feedback=row["feedback"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ─── CRUD: Patterns ──────────────────────────────────────────


def save_patterns(case_id: int, patterns: list[dict]) -> int:
    """
    Bulk insert patterns for a case.
    Each dict: {pattern_type, wallet, related_wallet, confidence, metadata}
    Returns count inserted.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        rows = [
            (
                case_id,
                p["pattern_type"],
                p.get("wallet", ""),
                p.get("related_wallet", ""),
                p.get("confidence", 0.5),
                json.dumps(p.get("metadata", {})),
                now,
            )
            for p in patterns
        ]
        conn.executemany(
            """INSERT INTO learned_patterns
               (case_id, pattern_type, wallet, related_wallet,
                confidence, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def get_patterns_for_case(case_id: int) -> list[LearnedPattern]:
    """Get all patterns for a case."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM learned_patterns WHERE case_id = ?", (case_id,)
        ).fetchall()
        return [
            LearnedPattern(
                id=r["id"],
                case_id=r["case_id"],
                pattern_type=r["pattern_type"],
                wallet=r["wallet"],
                related_wallet=r["related_wallet"],
                confidence=r["confidence"],
                metadata=json.loads(r["metadata"]),
            )
            for r in rows
        ]
    finally:
        conn.close()


def adjust_pattern_confidence(case_id: int, delta: float) -> int:
    """
    Adjust confidence of all patterns for a case by delta.
    Clamps to [0.0, 1.0]. Disables patterns below 0.1.
    Returns count updated.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, confidence FROM learned_patterns WHERE case_id = ?",
            (case_id,),
        ).fetchall()

        updated = 0
        for row in rows:
            new_conf = max(0.0, min(1.0, row["confidence"] + delta))
            conn.execute(
                "UPDATE learned_patterns SET confidence = ? WHERE id = ?",
                (new_conf, row["id"]),
            )
            updated += 1

        # Also adjust known_dev_wallets confidence
        wallet_rows = conn.execute(
            "SELECT wallet, confidence FROM known_dev_wallets WHERE case_id = ?",
            (case_id,),
        ).fetchall()
        for wr in wallet_rows:
            new_conf = max(0.0, min(1.0, wr["confidence"] + delta))
            conn.execute(
                "UPDATE known_dev_wallets SET confidence = ? WHERE wallet = ? AND case_id = ?",
                (new_conf, wr["wallet"], case_id),
            )

        conn.commit()
        return updated
    finally:
        conn.close()


# ─── CRUD: Known Dev Wallets ─────────────────────────────────


def save_dev_wallets(case_id: int, mint: str, wallets: list[dict]) -> int:
    """
    Bulk insert known dev wallets.
    Each dict: {wallet, role, confidence}
    Uses INSERT OR REPLACE to handle duplicates within same case.
    Returns count inserted.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        rows = [
            (
                w["wallet"],
                w.get("role", "buyer"),
                case_id,
                w.get("confidence", 0.5),
                mint,
                now,
            )
            for w in wallets
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO known_dev_wallets
               (wallet, role, case_id, confidence, mint, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def lookup_wallets(wallets: list[str], min_confidence: float = 0.1) -> list[KnownDevWallet]:
    """
    Lookup wallets in known_dev_wallets table.
    Returns matches with confidence >= min_confidence.
    """
    if not wallets:
        return []

    conn = _get_conn()
    try:
        placeholders = ",".join("?" for _ in wallets)
        rows = conn.execute(
            f"""SELECT * FROM known_dev_wallets
                WHERE wallet IN ({placeholders})
                AND confidence >= ?
                ORDER BY confidence DESC""",
            [*wallets, min_confidence],
        ).fetchall()
        return [
            KnownDevWallet(
                wallet=r["wallet"],
                role=r["role"],
                case_id=r["case_id"],
                confidence=r["confidence"],
                mint=r["mint"],
            )
            for r in rows
        ]
    finally:
        conn.close()


def get_all_patterns(min_confidence: float = 0.1) -> list[LearnedPattern]:
    """Get all active patterns across all cases."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT p.* FROM learned_patterns p
               JOIN learned_cases c ON p.case_id = c.id
               WHERE p.confidence >= ?
               AND c.feedback != 'bad'
               ORDER BY p.confidence DESC""",
            (min_confidence,),
        ).fetchall()
        return [
            LearnedPattern(
                id=r["id"],
                case_id=r["case_id"],
                pattern_type=r["pattern_type"],
                wallet=r["wallet"],
                related_wallet=r["related_wallet"],
                confidence=r["confidence"],
                metadata=json.loads(r["metadata"]),
            )
            for r in rows
        ]
    finally:
        conn.close()


def get_all_known_wallets(min_confidence: float = 0.1) -> dict[str, KnownDevWallet]:
    """Get all known dev wallets as {wallet: KnownDevWallet} (highest confidence wins)."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT dw.* FROM known_dev_wallets dw
               JOIN learned_cases c ON dw.case_id = c.id
               WHERE dw.confidence >= ?
               AND c.feedback != 'bad'
               ORDER BY dw.confidence DESC""",
            (min_confidence,),
        ).fetchall()
        result = {}
        for r in rows:
            w = r["wallet"]
            if w not in result:
                result[w] = KnownDevWallet(
                    wallet=w,
                    role=r["role"],
                    case_id=r["case_id"],
                    confidence=r["confidence"],
                    mint=r["mint"],
                )
        return result
    finally:
        conn.close()


# ─── Stats ────────────────────────────────────────────────────


def get_teach_stats() -> dict:
    """Get summary stats for /teachstats."""
    conn = _get_conn()
    try:
        case_count = conn.execute("SELECT COUNT(*) as c FROM learned_cases").fetchone()["c"]
        good_count = conn.execute(
            "SELECT COUNT(*) as c FROM learned_cases WHERE feedback = 'good'"
        ).fetchone()["c"]
        bad_count = conn.execute(
            "SELECT COUNT(*) as c FROM learned_cases WHERE feedback = 'bad'"
        ).fetchone()["c"]
        pending_count = conn.execute(
            "SELECT COUNT(*) as c FROM learned_cases WHERE feedback = 'pending'"
        ).fetchone()["c"]
        pattern_count = conn.execute(
            "SELECT COUNT(*) as c FROM learned_patterns WHERE confidence >= 0.1"
        ).fetchone()["c"]
        wallet_count = conn.execute(
            "SELECT COUNT(DISTINCT wallet) as c FROM known_dev_wallets WHERE confidence >= 0.1"
        ).fetchone()["c"]

        # Pattern type breakdown
        type_rows = conn.execute(
            """SELECT pattern_type, COUNT(*) as c
               FROM learned_patterns WHERE confidence >= 0.1
               GROUP BY pattern_type"""
        ).fetchall()
        pattern_types = {r["pattern_type"]: r["c"] for r in type_rows}

        return {
            "total_cases": case_count,
            "good": good_count,
            "bad": bad_count,
            "pending": pending_count,
            "active_patterns": pattern_count,
            "known_wallets": wallet_count,
            "pattern_types": pattern_types,
        }
    finally:
        conn.close()
