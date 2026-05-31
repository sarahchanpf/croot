"""Crustdata REST client.

DB-tier search only (people_search_db equivalent), full-fat once. company
identify is cached 30 days; person enrich is the expensive opt-in step and is
also cached. All methods return parsed dicts or raise CrustdataError.
"""

from __future__ import annotations

import time
from contextlib import closing

import requests

from .. import config
from ..db import db


class CrustdataError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _headers() -> dict:
    return {
        "Authorization": f"Token {config.CRUSTDATA_API_KEY}",
        "Content-Type": "application/json",
    }


def search(filters: dict, limit: int = config.SEARCH_LIMIT, sorts: list | None = None) -> dict:
    """people_search_db, full-fat (compact=false, truncate=false).

    TODO(impl): POST CRUSTDATA_SEARCH_URL with
      {"filters": filters, "limit": limit, "sorts": sorts or [],
       "compact": False, "truncate": False, "format": "json"}.
    On large pools the raw payload can exceed context — capture, hand straight
    to pool.compress, never hold the whole thing in memory.
    """
    raise NotImplementedError("crustdata.search — see TODO")


def identify(name: str) -> int | None:
    """Resolve a company name -> company_id. Free-ish; cached 30 days (both
    hits and misses). Ported behaviour from v1's resolve_company_id."""
    if not name or not name.strip():
        return None
    key = name.strip().lower()
    now = int(time.time())
    try:
        with closing(db()) as conn:
            row = conn.execute(
                "SELECT company_id, looked_up_at FROM company_id_cache WHERE name_normalized = ?",
                (key,),
            ).fetchone()
        if row and now - row["looked_up_at"] < config.COMPANY_ID_TTL_SECONDS:
            return row["company_id"]
    except Exception:
        pass  # cache unavailable (read-only fs) — fall through to live lookup

    if not config.CRUSTDATA_API_KEY:
        return None
    # TODO(impl): POST CRUSTDATA_IDENTIFY_URL {query_company_name, exact_match:false, count:3},
    # take data[0].company_id, write through to company_id_cache, return it.
    raise NotImplementedError("crustdata.identify live lookup — see TODO")


def autocomplete(field: str, query: str) -> list[str]:
    """Resolve enum values for industry / school fields. Required before using
    `in` clauses on those columns. Not cached (query-bound, not entity-bound).

    TODO(impl): call CRUSTDATA_AUTOCOMPLETE_URL (verify path) and return the
    matched canonical values.
    """
    raise NotImplementedError("crustdata.autocomplete — see TODO")


def enrich(linkedin_urls: list[str], include_contact: bool = True) -> dict:
    """person enrich — the expensive (~4 cr/profile) opt-in step. Batches
    comma-separated URLs in one call; cache-first per URL.

    TODO(impl): cache lookup per url in profile_cache; for misses POST
    CRUSTDATA_ENRICH_URL; write through; merge cached + fresh.
    """
    raise NotImplementedError("crustdata.enrich — see TODO")
