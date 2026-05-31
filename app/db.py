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
