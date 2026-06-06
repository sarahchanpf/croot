"""Crustdata REST client.

DB-tier search only (full-fat once), identify cached 30 days, enrich cached and
batched. Network/HTTP failures raise CrustdataError EXCEPT for identify and
autocomplete, which fail soft (return None / []) — one company that won't
resolve or one industry that won't autocomplete should never sink a search, and
never produce a guessed enum value (the skill's hard rule).

Request shapes mirror v1 (which ran against the live endpoints):
  * search   POST /screener/persondb/search  {filters, limit}
  * identify POST /screener/identify/         {query_company_name, exact_match, count}
  * enrich   GET  /screener/person/enrich     ?linkedin_profile_url=...&fields=...
"""

from __future__ import annotations

import json
import time
from contextlib import closing

import requests

from .. import config
from ..db import db

_TIMEOUT = 30
_IDENTIFY_TIMEOUT = 10

# Fields requested on enrich. Personal contact info is included only when the
# caller opts in (it's the expensive part). business_email is intentionally
# omitted (skill rule: personal contact only).
_ENRICH_FIELDS_BASE = [
    "linkedin_profile_url", "linkedin_flagship_url", "name", "location",
    "headline", "summary", "num_of_connections", "skills", "profile_picture_url",
    "languages", "current_employers", "past_employers", "all_employers",
    "education_background", "certifications", "honors",
]
_ENRICH_FIELDS_CONTACT = [
    "personal_contact_info.personal_emails",
    "personal_contact_info.phone_numbers",
]


class CrustdataError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _post_headers() -> dict:
    return {
        "Authorization": f"Token {config.CRUSTDATA_API_KEY}",
        "Content-Type": "application/json",
    }


def _get_headers() -> dict:
    return {"Authorization": f"Token {config.CRUSTDATA_API_KEY}"}


def _autocomplete_headers() -> dict:
    # Autocomplete is on the NEW API: Bearer auth + version header (the legacy
    # /screener endpoints use Token auth instead).
    return {
        "Authorization": f"Bearer {config.CRUSTDATA_API_KEY}",
        "Content-Type": "application/json",
        "x-api-version": config.CRUSTDATA_API_VERSION,
    }


def normalize_linkedin_url(url: str) -> str:
    if not url:
        return ""
    return url.strip().split("?")[0].split("#")[0].rstrip("/").lower()


# ---------- search ----------

def search(filters: dict, limit: int = config.SEARCH_LIMIT, sorts: list | None = None) -> dict:
    """people_search_db (DB tier). Returns {profiles: [...], total_count: N}.

    Full-fat call, matching the skill's Phase 2 Step 4: `compact=false` so each
    profile carries the nested fields the ranker scores on — skills, full
    work history (current + past employers with industry), and education.
    Without it the DB endpoint can return trimmed profiles (name / headline /
    region / current employer only), which would starve the ranker.
    (`truncate`/`format` are MCP-harness concepts — context-spillover cap and
    markdown-vs-json shaping — irrelevant to a programmatic REST consumer, which
    already receives uncapped JSON, so they're not sent.)

    `sorts` makes the fetched slice deterministic and on-axis; the DB default is
    `person_id asc`, i.e. an arbitrary slice of large pools (see sort_picker).
    """
    if not config.CRUSTDATA_API_KEY:
        raise CrustdataError("CRUSTDATA_API_KEY is not set.", 500)
    payload: dict = {"filters": filters, "limit": limit, "compact": False}
    if sorts:
        payload["sorts"] = sorts
    try:
        r = requests.post(config.CRUSTDATA_SEARCH_URL, headers=_post_headers(),
                          json=payload, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise CrustdataError(f"Upstream request failed: {exc}", 502)
    if r.status_code >= 400:
        raise CrustdataError(f"Crustdata returned {r.status_code}: {r.text[:500]}", r.status_code)
    return r.json()


# ---------- identify (cached, fail-soft) ----------

def identify(name: str) -> int | None:
    """Resolve a company name -> company_id. Cached 30 days (hits and misses).
    Returns None on miss or any error — never raises."""
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
        pass

    if not config.CRUSTDATA_API_KEY:
        return None

    company_id = None
    company_name = None
    try:
        r = requests.post(
            config.CRUSTDATA_IDENTIFY_URL, headers=_post_headers(),
            json={"query_company_name": name.strip(), "exact_match": False, "count": 3},
            timeout=_IDENTIFY_TIMEOUT,
        )
        if r.status_code < 400:
            data = r.json()
            if isinstance(data, list) and data:
                company_id = data[0].get("company_id")
                company_name = data[0].get("company_name")
    except requests.RequestException:
        return None

    try:
        with closing(db()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO company_id_cache "
                "(name_normalized, company_id, company_name, looked_up_at) VALUES (?, ?, ?, ?)",
                (key, company_id, company_name, now),
            )
    except Exception:
        pass
    return company_id


# ---------- autocomplete (fail-soft, never guesses) ----------

def autocomplete(field: str, query: str, limit: int = 10) -> list[str]:
    """Resolve canonical enum values (industries, schools) before they're used
    in legacy `in` clauses. Calls POST /person/search/autocomplete and returns
    the suggestion values. Fail-soft: returns [] on any error so callers emit no
    clause rather than a guessed value (the skill's hard rule).

    `field` is a NEW-API autocomplete field name (e.g.
    "experience.employment_details.current.company_industries",
    "education.schools.school") — distinct from the legacy search column the
    resulting values get filtered on. Values are verified compatible across the
    two APIs.
    """
    if not query or not query.strip() or not config.CRUSTDATA_API_KEY:
        return []
    try:
        r = requests.post(
            config.CRUSTDATA_AUTOCOMPLETE_URL, headers=_autocomplete_headers(),
            json={"field": field, "query": query.strip(), "limit": limit},
            timeout=_IDENTIFY_TIMEOUT,
        )
        if r.status_code >= 400:
            return []
        data = r.json()
    except (requests.RequestException, ValueError):
        return []

    out: list[str] = []
    for s in (data.get("suggestions") or []):
        val = s.get("value") if isinstance(s, dict) else s
        if val:  # skip the occasional blank value the API documents
            out.append(val)
    return out


# ---------- enrich (cached per-url, batched) ----------

def enrich(linkedin_urls: list[str], include_contact: bool = True) -> dict:
    """Enrich profiles by LinkedIn URL. Cache-first per URL (30-day TTL); only
    cache-missed URLs hit Crustdata, batched comma-separated. The expensive
    call — gated behind an explicit user action upstream.

    Returns {"profiles": [...]} preserving input order where data exists.
    """
    now = int(time.time())
    by_url: dict[str, dict] = {}
    misses: list[str] = []

    for url in linkedin_urls:
        key = normalize_linkedin_url(url)
        if not key:
            continue
        try:
            with closing(db()) as conn:
                row = conn.execute(
                    "SELECT payload, created_at FROM profile_cache WHERE linkedin_key = ?",
                    (key,),
                ).fetchone()
            if row and now - row["created_at"] < config.PROFILE_TTL_SECONDS:
                by_url[key] = json.loads(row["payload"])
                continue
        except Exception:
            pass
        misses.append(url)

    if misses:
        if not config.CRUSTDATA_API_KEY:
            raise CrustdataError("CRUSTDATA_API_KEY is not set.", 500)
        fields = list(_ENRICH_FIELDS_BASE)
        if include_contact:
            fields += _ENRICH_FIELDS_CONTACT
        try:
            r = requests.get(
                config.CRUSTDATA_ENRICH_URL, headers=_get_headers(),
                params={"linkedin_profile_url": ",".join(misses), "fields": ",".join(fields)},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise CrustdataError(f"Upstream request failed: {exc}", 502)
        if r.status_code >= 400:
            raise CrustdataError(f"Crustdata returned {r.status_code}: {r.text[:500]}", r.status_code)
        data = r.json()
        profiles = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        for prof in profiles:
            if not isinstance(prof, dict):
                continue
            purl = prof.get("linkedin_profile_url") or prof.get("linkedin_flagship_url") or ""
            key = normalize_linkedin_url(purl)
            if not key:
                continue
            by_url[key] = prof
            try:
                with closing(db()) as conn, conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO profile_cache (linkedin_key, payload, created_at) "
                        "VALUES (?, ?, ?)",
                        (key, json.dumps(prof), now),
                    )
            except Exception:
                pass

    ordered = []
    seen = set()
    for url in linkedin_urls:
        key = normalize_linkedin_url(url)
        if key in by_url and key not in seen:
            seen.add(key)
            ordered.append(by_url[key])
    return {"profiles": ordered}
