"""
Degen Scanner - SQLite Persistence

Stores stats and activity_log in data/scanner.db so data survives restarts.

Tables:
  stats          - key/value pairs for counters (scans, transfers, etc.)
  activity_log   - timestamped events for /summary reporting
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "scanner.db")

# Auto-purge activity older than this
ACTIVITY_MAX_AGE = timedelta(days=7)


def _get_conn() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode for concurrent reads."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stats (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activity_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT    NOT NULL,
                ts         TEXT    NOT NULL,
                data       TEXT    NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_activity_ts
                ON activity_log(ts);
            CREATE INDEX IF NOT EXISTS idx_activity_type
                ON activity_log(event_type);

            CREATE TABLE IF NOT EXISTS cex_wallets (
                address TEXT PRIMARY KEY,
                name    TEXT NOT NULL,
                seen    INTEGER NOT NULL DEFAULT 1,
                updated TEXT NOT NULL
            );
        """)
        conn.commit()
        logger.info(f"SQLite DB initialized at {DB_PATH}")

        # Initialize teach tables
        _init_teach_tables(conn)
    finally:
        conn.close()


def _init_teach_tables(conn: sqlite3.Connection) -> None:
    """Create teach/learn tables if they don't exist."""
    try:
        from teach_store import init_teach_db
        init_teach_db()
    except Exception as e:
        logger.warning(f"Teach DB init skipped: {e}")


# ─── Stats ────────────────────────────────────────────────────────

_STAT_KEYS = [
    "messages_received",
    "transfers_parsed",
    "clusters_detected",
    "whale_queued",
    "whale_splits",
    "scans",
    "started_at",
]


def load_stats() -> dict:
    """Load all stats from DB. Returns dict with defaults for missing keys."""
    result = {
        "messages_received": 0,
        "transfers_parsed": 0,
        "clusters_detected": 0,
        "whale_queued": 0,
        "whale_splits": 0,
        "scans": 0,
        "started_at": None,
    }
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM stats").fetchall()
        for row in rows:
            key, value = row["key"], row["value"]
            if key == "started_at":
                result[key] = datetime.fromisoformat(value) if value != "null" else None
            elif key in result:
                result[key] = int(value)
        return result
    finally:
        conn.close()


def save_stat(key: str, value) -> None:
    """Upsert a single stat."""
    str_value = value.isoformat() if isinstance(value, datetime) else str(value)
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO stats(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str_value),
        )
        conn.commit()
    finally:
        conn.close()


def increment_stat(key: str, amount: int = 1) -> int:
    """Atomically increment a counter stat. Returns new value."""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
        current = int(row["value"]) if row else 0
        new_val = current + amount
        conn.execute(
            "INSERT INTO stats(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(new_val)),
        )
        conn.commit()
        return new_val
    finally:
        conn.close()


# ─── CEX Wallets (auto-learned from source group) ────────────────

def save_cex_wallet(address: str, name: str) -> None:
    """Save or update a CEX wallet address. Increments seen count."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO cex_wallets(address, name, seen, updated) VALUES(?, ?, 1, ?) "
            "ON CONFLICT(address) DO UPDATE SET seen = seen + 1, updated = excluded.updated",
            (address, name, now),
        )
        conn.commit()
    finally:
        conn.close()


def load_cex_wallets() -> dict[str, str]:
    """Load all CEX wallets from DB. Returns {address: name}."""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT address, name FROM cex_wallets").fetchall()
        return {row["address"]: row["name"] for row in rows}
    finally:
        conn.close()


# ─── Activity Log ─────────────────────────────────────────────────

def log_activity(event_type: str, **kwargs) -> None:
    """Insert an activity event and purge old entries."""
    now = datetime.now(timezone.utc)
    data = json.dumps(kwargs, default=str)
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO activity_log(event_type, ts, data) VALUES(?, ?, ?)",
            (event_type, now.isoformat(), data),
        )
        # Purge old entries
        cutoff = (now - ACTIVITY_MAX_AGE).isoformat()
        conn.execute("DELETE FROM activity_log WHERE ts < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def query_activity(hours: int) -> list[dict]:
    """
    Get activity events from the last N hours.
    Returns list of dicts: {"type": str, "ts": datetime, ...extra data}
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT event_type, ts, data FROM activity_log WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
        result = []
        for row in rows:
            entry = {
                "type": row["event_type"],
                "ts": datetime.fromisoformat(row["ts"]),
                **json.loads(row["data"]),
            }
            result.append(entry)
        return result
    finally:
        conn.close()


def get_activity_count(event_type: str, hours: int) -> int:
    """Fast count of events by type within time window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM activity_log WHERE event_type = ? AND ts >= ?",
            (event_type, cutoff),
        ).fetchone()
        return row["cnt"]
    finally:
        conn.close()
