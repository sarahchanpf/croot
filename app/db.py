"""SQLite access. Best-effort on Vercel (/tmp is wiped between cold starts)."""

import sqlite3
from contextlib import closing

from .config import DB_PATH


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
