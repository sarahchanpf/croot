"""SQLite access. Best-effort on Vercel (/tmp is wiped between cold starts)."""

import hashlib
import json
import time
import sqlite3
from contextlib import closing

from .config import CACHE_TTL_SECONDS, DB_PATH


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key  TEXT PRIMARY KEY,
                payload    TEXT NOT NULL,
                response   TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key  TEXT NOT NULL,
                summary    TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS company_id_cache (
                name_normalized TEXT PRIMARY KEY,
                company_id      INTEGER,
                company_name    TEXT,
                looked_up_at    INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_cache (
                linkedin_key TEXT PRIMARY KEY,
                payload      TEXT NOT NULL,
                created_at   INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_searches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                criteria    TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                last_run_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                email      TEXT NOT NULL,
                user_agent TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_usage (
                email        TEXT PRIMARY KEY,
                search_count INTEGER NOT NULL DEFAULT 0,
                updated_at   INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS waitlist_signups (
                email      TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )


def get_search_count(email: str) -> int:
    """Return persisted usage, falling back to zero when storage is unavailable."""
    try:
        with closing(db()) as conn:
            row = conn.execute(
                "SELECT search_count FROM search_usage WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
    except Exception:
        return 0
    return int(row["search_count"]) if row else 0


def increment_search_count(email: str, minimum_count: int = 0) -> int:
    """Atomically increment usage without losing a higher cookie-backed count."""
    normalized = email.strip().lower()
    now = int(time.time())
    with closing(db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT search_count FROM search_usage WHERE email = ?",
            (normalized,),
        ).fetchone()
        current = max(int(row["search_count"]) if row else 0, int(minimum_count))
        updated = current + 1
        conn.execute(
            "INSERT OR REPLACE INTO search_usage (email, search_count, updated_at) "
            "VALUES (?, ?, ?)",
            (normalized, updated, now),
        )
        conn.commit()
    return updated


def add_to_waitlist(name: str, email: str) -> None:
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO waitlist_signups (email, name, created_at) "
            "VALUES (?, ?, ?)",
            (email.strip().lower(), name.strip(), int(time.time())),
        )


def is_waitlisted(email: str) -> bool:
    try:
        with closing(db()) as conn:
            row = conn.execute(
                "SELECT 1 FROM waitlist_signups WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
    except Exception:
        return False
    return row is not None


# ---------- search cache (best-effort) ----------

def cache_key_for(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_cached(key: str):
    try:
        with closing(db()) as conn:
            row = conn.execute(
                "SELECT response, created_at FROM search_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
    except Exception:
        return None
    if not row or time.time() - row["created_at"] > CACHE_TTL_SECONDS:
        return None
    return json.loads(row["response"])


def put_cached(key: str, payload: dict, response: dict, summary: str) -> None:
    now = int(time.time())
    try:
        with closing(db()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO search_cache (cache_key, payload, response, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key, json.dumps(payload), json.dumps(response), now),
            )
            conn.execute(
                "INSERT INTO search_history (cache_key, summary, created_at) VALUES (?, ?, ?)",
                (key, summary, now),
            )
    except Exception:
        pass  # read-only fs — cache simply unavailable
