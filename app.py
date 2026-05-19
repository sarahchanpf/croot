import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

CRUSTDATA_API_KEY = os.environ.get("CRUSTDATA_API_KEY", "")
CRUSTDATA_URL = "https://api.crustdata.com/screener/persondb/search"
CRUSTDATA_IDENTIFY_URL = "https://api.crustdata.com/screener/identify/"
CRUSTDATA_ENRICH_URL = "https://api.crustdata.com/screener/person/enrich"
# Optional Google Apps Script Web App URL. When set, every waitlist signup is
# forwarded here so it lands in a Google Sheet. SQLite remains the local audit
# log so a sheet failure doesn't lose the signup.
GSHEETS_WAITLIST_URL = os.environ.get("GSHEETS_WAITLIST_URL", "")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_HOURS", "72")) * 3600
COMPANY_ID_TTL_SECONDS = 30 * 24 * 3600
PROFILE_TTL_SECONDS = 30 * 24 * 3600

# On Vercel the function filesystem is read-only except for /tmp, and /tmp is
# wiped between cold starts. The SQLite cache is therefore best-effort: it
# survives warm invocations, gets recreated on cold starts.
if os.environ.get("VERCEL"):
    DB_PATH = "/tmp/croot.db"
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "croot.db")

app = Flask(__name__)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                response TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS company_id_cache (
                name_normalized TEXT PRIMARY KEY,
                company_id INTEGER,
                company_name TEXT,
                looked_up_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS waitlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                user_agent TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_cache (
                linkedin_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                criteria TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'similar',
                created_at INTEGER NOT NULL,
                last_run_at INTEGER
            )
            """
        )


def resolve_company_id(name: str):
    """Resolve a company name to Crustdata's numeric company_id.

    Uses /screener/identify (free, no credits per docs). Caches both positive
    hits and negative misses for COMPANY_ID_TTL_SECONDS so the same names in
    repeat searches are free.
    """
    if not name:
        return None
    key = name.strip().lower()
    if not key:
        return None

    now = int(time.time())
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT company_id, looked_up_at FROM company_id_cache WHERE name_normalized = ?",
            (key,),
        ).fetchone()
    if row and now - row["looked_up_at"] < COMPANY_ID_TTL_SECONDS:
        return row["company_id"]

    if not CRUSTDATA_API_KEY:
        return None

    try:
        r = requests.post(
            CRUSTDATA_IDENTIFY_URL,
            headers={
                "Authorization": f"Token {CRUSTDATA_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"query_company_name": name.strip(), "exact_match": False, "count": 3},
            timeout=10,
        )
    except requests.RequestException:
        return None

    company_id = None
    company_name = None
    if r.status_code < 400:
        data = r.json()
        if isinstance(data, list) and data:
            first = data[0]
            company_id = first.get("company_id")
            company_name = first.get("company_name")

    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO company_id_cache "
            "(name_normalized, company_id, company_name, looked_up_at) VALUES (?, ?, ?, ?)",
            (key, company_id, company_name, now),
        )
    return company_id


def cache_key_for(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_cached(key: str):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT response, created_at FROM search_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    if time.time() - row["created_at"] > CACHE_TTL_SECONDS:
        return None
    return json.loads(row["response"])


def put_cached(key: str, payload: dict, response: dict, summary: str):
    now = int(time.time())
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO search_cache (cache_key, payload, response, created_at) VALUES (?, ?, ?, ?)",
            (key, json.dumps(payload), json.dumps(response), now),
        )
        conn.execute(
            "INSERT INTO search_history (cache_key, summary, created_at) VALUES (?, ?, ?)",
            (key, summary, now),
        )


# ---------- Filter construction ----------
#
# Crustdata filter primitives. Every condition in a /screener/persondb/search
# call is `{"filter_type": <field>, "type": <operator>, "value": <value>}`.
# The helpers below assemble that shape; the FIELD constants name each column
# we touch so we don't sprinkle magic strings across build_filters and the
# scorer.

class FIELD:
    # Current role
    CURRENT_TITLE = "current_employers.title"
    CURRENT_SENIORITY = "current_employers.seniority_level"
    CURRENT_FUNCTION = "current_employers.function_category"
    CURRENT_INDUSTRIES = "current_employers.company_industries"
    CURRENT_COMPANY_ID = "current_employers.company_id"
    CURRENT_NAME = "current_employers.name"
    # Career history (current + past, merged)
    ALL_EMPLOYERS_ID = "all_employers.company_id"
    ALL_EMPLOYERS_NAME = "all_employers.name"
    ALL_EMPLOYERS_SENIORITY = "all_employers.seniority_level"
    ALL_EMPLOYERS_HEADCOUNT_RANGE = "all_employers.company_headcount_range"
    CURRENT_HEADCOUNT_RANGE = "current_employers.company_headcount_range"
    PAST_HEADCOUNT_RANGE = "past_employers.company_headcount_range"
    # Past roles (used for date-bounded tenure overlap)
    PAST_COMPANY_ID = "past_employers.company_id"
    PAST_NAME = "past_employers.name"
    PAST_START_DATE = "past_employers.start_date"
    PAST_END_DATE = "past_employers.end_date"
    # Profile-level
    REGION = "region"
    SKILLS = "skills"
    SUMMARY = "summary"
    SCHOOL = "education_background.institute_name"
    YEARS_EXPERIENCE = "years_of_experience_raw"
    RECENTLY_CHANGED = "recently_changed_jobs"


def fuzzy(field: str, value: str):
    return {"filter_type": field, "type": "(.)", "value": value}


def exact(field: str, value):
    return {"filter_type": field, "type": "=", "value": value}


def gte(field: str, value):
    return {"filter_type": field, "type": "=>", "value": value}


def lte(field: str, value):
    return {"filter_type": field, "type": "=<", "value": value}


def op_in(field: str, values):
    return {"filter_type": field, "type": "in", "value": list(values)}


def op_not_in(field: str, values):
    return {"filter_type": field, "type": "not_in", "value": list(values)}


def op_neg(field: str, value: str):
    """Fuzzy negation — exclude profiles whose field fuzzy-matches `value`."""
    return {"filter_type": field, "type": "(!)", "value": value}


def op_neq(field: str, value):
    return {"filter_type": field, "type": "!=", "value": value}


def op_or(conditions: list) -> dict:
    return {"op": "or", "conditions": list(conditions)}


def op_and(conditions: list) -> dict:
    return {"op": "and", "conditions": list(conditions)}


def year_to_start(year: str) -> str:
    return f"{int(year):04d}-01-01"


def year_to_end(year: str) -> str:
    return f"{int(year):04d}-12-31"


def normalize_criteria(criteria: dict) -> dict:
    """Return a shallow copy of `criteria` with one-off legacy / shape
    normalisations applied. Specifically, fold the pre-split `skills` field
    into `must_have_skills` so downstream consumers only need to look in one
    place. This is the single source-of-truth shim for the criteria contract.
    """
    if not isinstance(criteria, dict):
        return {}
    c = dict(criteria)
    legacy_skills = c.pop("skills", None)
    if legacy_skills and not c.get("must_have_skills"):
        c["must_have_skills"] = list(legacy_skills)
    c.setdefault("title_cluster", [])
    c.setdefault("came_from_orgs", [])
    c.setdefault("came_from_companies", [])
    return c


def _resolve_companies(names) -> tuple[list[int], list[str]]:
    """Resolve a list of company name strings into (ids, unresolved_names).

    `names` may be any iterable; empty/whitespace entries are skipped.
    Used by both the 'They came from…' OR group and the exclude-companies
    list, which share the same id-first-then-name-fallback semantics.
    """
    ids: list[int] = []
    unresolved: list[str] = []
    if not names:
        return ids, unresolved
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        cid = resolve_company_id(name)
        if cid is not None:
            ids.append(cid)
        else:
            unresolved.append(name)
    return ids, unresolved


SENIORITY_MAP = {
    "entry": "Entry",
    "mid": "Senior",
    "senior": "Senior",
    "lead": "Senior",
    "manager": "Manager",
    "director": "Director",
    "vp": "Vice President",
    "executive": "CXO",
}


# ---------- Natural-language parsing ----------
#
# Rule-based extraction of structured criteria from free-text recruiter queries
# like "fintech product managers in New York" or "ex-Apple laser engineers".
# Intentionally deterministic and fast — no LLM call, no extra API key. Swap
# parse_query() for an LLM call later if more nuance is needed; the response
# contract (a dict with the same keys the frontend already sends to /api/search)
# stays the same.

# Locations the parser knows about. Longest-first match so "san francisco bay
# area" wins over "san francisco". Output is sent through to Crustdata's
# geo_distance operator, which prefers plain city names.
LOCATION_PATTERNS = [
    "san francisco bay area", "san francisco", "new york city", "new york",
    "los angeles", "bay area", "silicon valley", "south bay",
    "seattle", "boston", "austin", "chicago", "denver", "atlanta",
    "miami", "philadelphia", "washington dc", "washington",
    "toronto", "vancouver", "montreal", "ottawa",
    "london", "berlin", "paris", "amsterdam", "dublin", "stockholm",
    "tel aviv", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad",
    "singapore", "tokyo", "hong kong", "sydney", "melbourne",
    "nyc", "sf", "la",
]

# Map short forms to canonical city names that Crustdata geocodes well.
LOCATION_CANONICAL = {
    "nyc": "New York City",
    "sf": "San Francisco",
    "la": "Los Angeles",
    "bengaluru": "Bangalore",
}

# Title variants → canonical Title Case form. Longest first so "solutions
# engineer" wins over "engineer".
TITLE_VARIANTS = [
    ("sales development representative", "Sales Development Representative"),
    ("business development representative", "Business Development Representative"),
    ("customer success manager", "Customer Success Manager"),
    ("machine learning engineer", "Machine Learning Engineer"),
    ("solutions engineer", "Solutions Engineer"),
    ("software engineer", "Software Engineer"),
    ("data scientist", "Data Scientist"),
    ("data engineer", "Data Engineer"),
    ("data analyst", "Data Analyst"),
    ("product manager", "Product Manager"),
    ("product designer", "Product Designer"),
    ("ux designer", "UX Designer"),
    ("ui designer", "UI Designer"),
    ("graphic designer", "Graphic Designer"),
    ("account executive", "Account Executive"),
    ("sales engineer", "Sales Engineer"),
    ("operations manager", "Operations Manager"),
    ("project manager", "Project Manager"),
    ("program manager", "Program Manager"),
    ("marketing manager", "Marketing Manager"),
    ("growth marketer", "Growth Marketer"),
    ("content marketer", "Content Marketer"),
    ("brand manager", "Brand Manager"),
    ("ml engineer", "Machine Learning Engineer"),
    ("ai engineer", "AI Engineer"),
    ("recruiter", "Recruiter"),
    ("developer", "Software Engineer"),
    ("engineer", "Engineer"),
    ("designer", "Designer"),
    ("founder", "Founder"),
    ("ceo", "CEO"),
    ("cto", "CTO"),
    ("cfo", "CFO"),
    ("coo", "COO"),
    ("vp", "Vice President"),
    ("director", "Director"),
    ("manager", "Manager"),
    ("analyst", "Analyst"),
    ("pm", "Product Manager"),
    ("sdr", "Sales Development Representative"),
    ("bdr", "Business Development Representative"),
    ("ae", "Account Executive"),
    ("swe", "Software Engineer"),
]

# Industry / project-domain keywords that recruiters drop into queries.
# Hits go to project_keywords (searched in the LinkedIn About summary).
KEYWORDS = [
    "fintech", "healthtech", "edtech", "biotech", "deeptech", "cleantech",
    "proptech", "insurtech", "climate", "crypto", "blockchain", "web3",
    "defense", "aerospace", "robotics", "autonomy", "self-driving", "drones",
    "uav", "satellite", "space",
    "machine learning", "ai", "ml", "nlp", "computer vision",
    "saas", "b2b", "b2c", "marketplace", "ecommerce", "e-commerce",
    "gaming", "hardware", "iot", "consumer", "enterprise",
    "ar", "vr", "xr", "lidar", "laser", "optics", "rf",
    "pcb", "embedded", "firmware", "infrastructure", "devops",
    "security", "cybersecurity", "privacy",
    "model-based systems engineering",
    "optical coherence tomography",
    "guidance navigation control",
    "photonic integrated circuit",
    "electro-optical system",
    "high frequency trading",
    "quantum communication",
    "logistics optimization",
    "algorithmic trading",
    "battery technology",
    "climate technology",
    "free-space optics",
    "surgical robotics",
    "autonomous vehicles",
    "autonomous driving",
    "threat intelligence",
    "privacy preserving",
    "penetration testing",
    "augmented reality",
    "federated learning",
    "quantum computing",
    "quantum sensing",
    "medical imaging",
    "drug discovery",
    "energy storage",
    "fleet management",
    "edge computing",
    "mixed reality",
    "virtual reality",
    "directed energy",
    "electromagnetics",
    "plasma physics",
    "supply chain",
    "hypersonics",
    "photonics",
    "acoustics",
    "regtech",
    "nuclear",
]

# Schools the parser will pick up by name (otherwise the user can write
# "at <Company>" / "from <Company>" to anchor on a known employer).
SCHOOLS = [
    "stanford", "harvard", "mit", "yale", "princeton", "columbia", "cornell",
    "uc berkeley", "berkeley", "ucla", "usc", "nyu", "carnegie mellon", "cmu",
    "uchicago", "northwestern", "duke", "brown",
    "university of toronto", "uwaterloo", "waterloo",
    "cambridge", "oxford", "imperial college", "lse",
    "iit", "iim", "isb",
]


def _word_re(value: str) -> str:
    """Build a regex that matches `value` only on word boundaries."""
    return rf"(?<!\w){re.escape(value)}(?!\w)"


def _title_re(value: str) -> str:
    """Word-boundary regex for titles that tolerates a trailing plural 's'.
    Lets "product manager" match "product managers" without forcing every
    plural form into the TITLE_VARIANTS table."""
    return rf"(?<!\w){re.escape(value)}s?(?!\w)"


def parse_query(text: str) -> dict:
    """Best-effort parse of a natural-language recruiter query into the criteria
    dict shape the frontend already posts to /api/search. Unknown content falls
    through unused — the recruiter can refine the structured fields manually."""
    text = (text or "").strip()
    if not text:
        return {}

    out: dict = {}
    project_keywords: list[str] = []
    employers: list[dict] = []
    lower = text.lower()

    # Location — longest match first; substitute out so it doesn't pollute
    # later passes (e.g., "boston" matching as a skill keyword).
    for loc in sorted(LOCATION_PATTERNS, key=len, reverse=True):
        if re.search(_word_re(loc), lower):
            out["location"] = LOCATION_CANONICAL.get(loc, loc.title())
            lower = re.sub(_word_re(loc), " ", lower)
            break

    # Title — first/most-specific match wins. _title_re tolerates a trailing
    # plural 's' so "product managers" still hits the "product manager" variant.
    for variant, canonical in TITLE_VARIANTS:
        if re.search(_title_re(variant), lower):
            out["current_title"] = canonical
            lower = re.sub(_title_re(variant), " ", lower)
            break

    # School — explicit known names, longest-first.
    for school in sorted(SCHOOLS, key=len, reverse=True):
        if re.search(_word_re(school), lower):
            # Crustdata's education_background.institute_name expects the
            # canonical form, so title-case the raw token.
            out["school"] = school.title()
            lower = re.sub(_word_re(school), " ", lower)
            break

    # Employer anchor — "ex-X", "at X", "from X", case-sensitive on the X so
    # we only grab capitalised proper nouns rather than every preposition.
    seen_employers = set()
    employer_patterns = [
        r"\bex[- ]([A-Z][\w\.&'\-]+(?:\s+[A-Z][\w\.&'\-]+){0,2})",
        r"\bat ([A-Z][\w\.&'\-]+(?:\s+[A-Z][\w\.&'\-]+){0,2})",
        r"\bfrom ([A-Z][\w\.&'\-]+(?:\s+[A-Z][\w\.&'\-]+){0,2})",
    ]
    for pat in employer_patterns:
        for m in re.finditer(pat, text):
            company = m.group(1).strip().rstrip(",.")
            key = company.lower()
            if key in seen_employers:
                continue
            seen_employers.add(key)
            employers.append({"company": company, "start_year": "", "end_year": ""})

    # Years of experience — "5-10 years", "5+ years", or implicit "senior"/"jr".
    yr_range = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*(?:years|yrs|yoe|yo)", lower)
    if yr_range:
        out["years_experience_min"] = int(yr_range.group(1))
        out["years_experience_max"] = int(yr_range.group(2))
    else:
        yr_plus = re.search(r"(\d+)\+?\s*(?:years|yrs|yoe|yo)", lower)
        if yr_plus:
            out["years_experience_min"] = int(yr_plus.group(1))

    if "senior" in lower or "sr." in lower:
        out["seniority"] = "senior"
    elif "junior" in lower or "jr." in lower:
        out["seniority"] = "entry"

    # Industries / domains → project_keywords (searched in summary).
    for kw in sorted(KEYWORDS, key=len, reverse=True):
        if re.search(_word_re(kw), lower):
            project_keywords.append(kw)
            lower = re.sub(_word_re(kw), " ", lower)

    if project_keywords:
        out["project_keywords"] = project_keywords
    if employers:
        out["employers"] = employers

    return out


# ---------- JD parsing (section-aware) ----------
#
# Distinct from parse_query() (which handles short recruiter inputs). For full
# JDs we need to:
#   - Pull the specific title from the top of the posting
#   - Pull skills from Responsibilities + Qualifications only — never from
#     mission / company description / perks
#   - Never produce employers (a JD never tells you where a candidate worked)
#   - Tell the caller which section each field came from

JD_SECTION_HEADERS: dict[str, list[str]] = {
    "responsibilities": [
        "responsibilities", "what you'll do", "what you will do",
        "what you'll be doing", "the role", "your role", "key duties",
        "duties", "day to day", "key responsibilities",
        "primary responsibilities", "core responsibilities",
    ],
    "qualifications": [
        "qualifications", "requirements", "required qualifications",
        "minimum qualifications", "minimum requirements",
        "must have", "must haves", "must-have", "must-haves",
        "what we're looking for", "what were looking for",
        "what we are looking for",
        "what you bring", "what you'll need", "what you will need",
        "you have", "you bring", "skills and experience",
        "experience", "required skills", "key requirements",
        "candidate requirements", "the ideal candidate",
    ],
    "preferred": [
        "preferred qualifications", "nice to have", "nice to haves",
        "bonus", "bonus points", "preferred", "pluses", "preferred skills",
        "additional qualifications", "would be a plus", "extra credit",
    ],
    "company": [
        "about us", "about the company", "about acme", "company description",
        "our company", "our mission", "the mission", "who we are",
        "why this job matters", "why this matters", "why join us",
        "why we exist", "the opportunity", "our story", "company overview",
        "why this role matters",
    ],
    "benefits": [
        "benefits", "what we offer", "perks", "compensation",
        "perks and benefits", "comp and benefits", "salary range",
        "equity", "what's in it for you",
        "equal opportunity", "equal employment opportunity",
        "diversity statement", "eeo statement",
    ],
}

JD_SKIP_SECTIONS = {"company", "benefits"}


# ---------- JD text cleanup (LinkedIn / job-board chrome) ----------

# Lines that are pure UI chrome on LinkedIn / Indeed / etc. Drop them whole.
JD_CHROME_LINE_PATTERNS = [
    r"^easy apply\s*\.?\s*$",
    r"^apply now\s*$",
    r"^apply\s*$",
    r"^save\s*$",
    r"^save\s+.+\s+at\s+\S+\s*$",   # "Save Robotics Engineer at LuminX"
    r"^report this job\s*$",
    r"^promoted(?:\s+by\s+\S+)?\s*$",
    r"^promoted by hirer\s*$",
    r"^over\s+\d+(?:K|k)?\+?\s+applicants?\s*$",
    r"^\d+(?:K|k)?\+?\s+applicants?\s*$",
    r"^\d+\s*(?:second|minute|hour|day|week|month|year)s?\s+ago\s*$",
    r"^posted\s+.*\s+ago\s*$",
    r"^posted\s+(?:on\s+)?\w+\s+\d+,?\s*\d{0,4}\s*$",
    r"^actively reviewing(?:\s+applicants?)?\s*$",
    r"^your profile is missing.*",
    r"^show match details\s*$",
    r"^tailor my resume\s*$",
    r"^help me stand out\s*$",
    r"^create cover letter\s*$",
    r"^beta\s*$",
    r"^is this information helpful\??\s*$",
    r"^see more\s*$",
    r"^show (?:more|less|details)\s*$",
    r"^view all\s+\d*\+?\s*jobs.*",
    r"^see all\s+\d*\+?\s*jobs.*",
    r"^by signing in.*",
    r"^sign in to.*",
    r"^join now to.*",
    r"^show all comments\s*$",
    r"^like\s+comment\s+share\s*$",
    r"^about the job\s*$",          # LinkedIn "About the job" label
    r"^promoted by hirer\b.*",       # "Promoted by hirer · Actively reviewing applicants"
    # Workplace / job-type pills shown as standalone metadata, e.g.
    #   "On-site"
    #   "Hybrid · Full-time"
    #   "Remote • Contract"
    # We only drop these when they're the ENTIRE line — combined with other
    # text they may be legitimate body copy.
    r"^(?:on[-\s]?site|hybrid|remote)\s*$",
    r"^(?:full|part)[-\s]?time\s*$",
    r"^(?:contract|internship|temporary|freelance)\s*$",
    r"^(?:on[-\s]?site|hybrid|remote)\s*[·•\-–|]\s*(?:full|part)[-\s]?time\s*$",
    r"^(?:full|part)[-\s]?time\s*[·•\-–|]\s*(?:on[-\s]?site|hybrid|remote)\s*$",
    r"^\d+\s*-\s*\d+\s+employees?\s*$",
    r"^\$[\d,]+(?:\.\d+)?\s*-\s*\$[\d,]+(?:\.\d+)?(?:\s*/\s*\w+)?\s*$",
]

_JD_CHROME_RE = [re.compile(p, re.IGNORECASE) for p in JD_CHROME_LINE_PATTERNS]


def clean_jd_text(text: str) -> str:
    """Strip LinkedIn / job-board UI chrome, URLs, markdown noise, and
    invisible unicode so the downstream parser sees clean JD body text."""
    if not text:
        return text

    # Invisible unicode (zero-width spaces, BOMs, line/paragraph separators).
    text = re.sub(r"[​-‏  ﻿]", "", text)
    # Normalise non-breaking spaces.
    text = text.replace(" ", " ")

    # URLs with any tracking tail.
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"www\.[^\s]+", "", text)

    # Markdown decoration — strip the syntax, keep the inner text.
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)       # # heading
    text = re.sub(r"^\s*[-=_]{3,}\s*$", "", text, flags=re.MULTILINE)  # --- ___ rule
    # Bullet glyphs → ASCII hyphen so section detection doesn't trip.
    text = re.sub(r"^\s*[•·●◦▪►–—]\s+", "- ", text, flags=re.MULTILINE)

    # Drop chrome lines.
    out_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue
        if any(p.match(stripped) for p in _JD_CHROME_RE):
            continue
        out_lines.append(line)

    # Strip inline chrome fragments — e.g. a location line like
    # "San Francisco Bay Area · 5 days ago · Over 100 applicants" keeps
    # the location and drops the metadata pills.
    inline_chrome_patterns = [
        r"\s*[·•|]\s*(?:over\s+)?\d+(?:K|k)?\+?\s+applicants?\b",
        r"\s*[·•|]\s*\d+\s*(?:second|minute|hour|day|week|month|year)s?\s+ago\b",
        r"\s*[·•|]\s*posted\s+.+?(?=\s*[·•|]|$)",
        r"\s*[·•|]\s*easy\s+apply\b",
        r"\s*[·•|]\s*promoted\s+by\s+hirer\b",
        r"\s*[·•|]\s*actively\s+reviewing(?:\s+applicants?)?",
        r"\s*[·•|]\s*(?:on[-\s]?site|hybrid|remote)\b",
        r"\s*[·•|]\s*(?:full|part)[-\s]?time\b",
    ]
    scrubbed: list[str] = []
    for line in out_lines:
        for pat in inline_chrome_patterns:
            line = re.sub(pat, "", line, flags=re.IGNORECASE)
        scrubbed.append(line.rstrip(" ·•|–—-"))
    out_lines = scrubbed

    # Collapse runs of blank lines to a single blank.
    cleaned = "\n".join(out_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = "\n".join(ln.rstrip() for ln in cleaned.split("\n"))
    return cleaned.strip()


# Curated tech vocabulary — caught case-insensitively as whole tokens. Ordered
# longest-first inside extract_jd_skills so multi-word phrases win over their
# single-word substrings.
JD_TECH_VOCAB: list[str] = [
    # Languages / frameworks
    "Python", "JavaScript", "TypeScript", "Java", "C++", "C#", "Go", "Rust",
    "Ruby", "PHP", "Swift", "Kotlin", "Scala", "Haskell", "Elixir",
    "SQL", "MATLAB", "Verilog", "VHDL",
    "React", "Vue", "Angular", "Next.js", "Django", "Flask", "Rails",
    "Spring", "Node.js", "FastAPI",
    # Data / ML
    "TensorFlow", "PyTorch", "Keras", "scikit-learn", "Pandas", "NumPy",
    "Spark", "Hadoop", "Airflow", "Kafka", "Snowflake", "Databricks",
    "Tableau", "PowerBI",
    # Databases
    "PostgreSQL", "Postgres", "MySQL", "MongoDB", "Redis", "Cassandra",
    "DynamoDB", "Elasticsearch", "Neo4j", "BigQuery", "Redshift", "dbt",
    # Design tools
    "Figma", "Sketch", "Adobe XD", "InVision", "Framer", "prototyping",
    # Cloud / infra
    "AWS", "Azure", "GCP", "Kubernetes", "Docker", "Terraform", "Ansible",
    "Jenkins", "GitHub Actions",
    # ML inference / robotics edge
    "TensorRT", "ONNX", "CUDA", "OpenVINO", "OpenCV",
    "NVIDIA Jetson", "Jetson", "RK3588",
    "V4L2", "GStreamer", "FFmpeg", "RTSP",
    "ROS2", "ROS",
    "VLMs", "VLM", "LLMs", "LLM",
    "MIPI", "MIPI/CSI", "CSI", "USB3", "GigE",
    "OTA updates", "OTA",
    "quantization",
    # EDA / mech CAD
    "Altium Designer", "Altium", "Cadence", "KiCad", "Eagle", "OrCAD",
    "SolidWorks", "AutoCAD", "Fusion 360", "Simulink",
    # Bus / protocols
    "EtherCAT", "Modbus", "CANopen", "CAN bus", "CAN",
    "PROFINET", "PROFIBUS", "DeviceNet", "OPC UA",
    "I2C", "SPI", "UART", "Ethernet",
    "MQTT", "ROS", "REST", "GraphQL", "gRPC",
    # Hardware terms
    "PCB design", "PCB layout", "PCB",
    "FPGA", "ASIC", "microcontroller", "MCU", "DSP", "RTOS",
    "embedded systems", "firmware",
    # Test equipment
    "oscilloscopes", "oscilloscope",
    "logic analyzers", "logic analyzer",
    "multimeter", "spectrum analyzer",
    # Components / actuators / sensors
    "servo drives", "servo drive",
    "linear motors", "linear motor",
    "stepper motor",
    "encoders", "encoder",
    "solenoids", "solenoid",
    "RTDs", "RTD",
    "thermistor", "load cell",
    # Mechanical / electrical
    "wiring harnesses", "wiring harness",
    "control panels", "control panel",
    "PLC", "HMI",
    "schematic capture",
    # Hardware / Optics / Photonics
    "photonic integrated circuit",
    "optical coherence tomography",
    "semiconductor fabrication",
    "vector network analyzer",
    "free-space optical",
    "optical alignment",
    "wavefront sensing",
    "optical simulation",
    "optical metrology",
    "signal processing",
    "image processing",
    "optical testing",
    "optical design",
    "laser systems",
    "fiber optics",
    "diode laser",
    "pulsed laser",
    "electro-optics",
    "acousto-optics",
    "embedded C",
    "beam steering",
    "beam optics",
    "CW laser",
    "thin film",
    "cleanroom",
    "interferometry",
    "spectroscopy",
    "photonics",
    "waveguide",
    "resonator",
    "LightTools",
    "LabVIEW",
    "NX CAD",
    "CATIA",
    "Zemax",
    "CODE V",
    "FRED",
    "Oslo",
    "LiDAR", "LIDAR",
    "laser",
    "MEMS", "MOEMS",
    "ISO 9001", "IPC-610", "MIL-SPEC", "DO-254", "DO-178",
    # Defense / Aerospace
    "verification and validation",
    "requirements management",
    "model-based systems engineering",
    "guidance navigation",
    "inertial navigation",
    "trajectory analysis",
    "security clearance",
    "secret clearance",
    "antenna design",
    "systems engineering",
    "systems integration",
    "thermal imaging",
    "flight dynamics",
    "export control",
    "electro-optical",
    "RF systems",
    "MBSE", "DOORS",
    "infrared", "radar", "sonar",
    "EO/IR", "TS/SCI", "MIL-STD", "V&V", "GPS",
    # Biotech / Medtech
    "high throughput screening",
    "regulatory affairs",
    "fluorescence imaging",
    "quality systems",
    "assay development",
    "clinical trials",
    "drug discovery",
    "flow cytometry",
    "bioinformatics",
    "cell culture",
    "microscopy",
    "confocal",
    "sequencing",
    "genomics",
    "proteomics",
    "GMP", "GLP",
    "FDA", "PMA", "CLIA", "CAP",
    "ISO 13485", "IEC 62304",
    "510k", "PCR", "ELISA",
    # Finance / Quant
    "high frequency trading",
    "portfolio optimization",
    "quantitative research",
    "quantitative analysis",
    "market microstructure",
    "execution algorithms",
    "time series analysis",
    "stochastic calculus",
    "algorithmic trading",
    "signal generation",
    "options pricing",
    "risk management",
    "alpha research",
    "factor models",
    "fixed income",
    "FIX protocol",
    "derivatives",
    "backtesting",
    "order book",
    "Bloomberg", "Reuters",
    "HFT",
    # General technical
    "model-based systems engineering",
    "reinforcement learning",
    "consensus algorithms",
    "distributed systems",
    "feature engineering",
    "computer vision",
    "object detection",
    "model deployment",
    "vector database",
    "motion planning",
    "data engineering",
    "data pipelines",
    "WebAssembly",
    "embeddings",
    "WebGPU", "WebGL", "WASM",
    "robotics",
    "simulation",
    "MLOps",
    "SLAM", "RAG", "RL",
    "ETL", "ELT",
]

# Words that can act as the head noun of a job title.
JD_TITLE_HEAD_NOUNS = (
    "engineer", "engineering", "manager", "scientist", "designer",
    "architect", "developer", "analyst", "director", "officer",
    "consultant", "specialist", "lead", "head", "founder", "researcher",
    "advisor", "partner", "principal", "executive", "associate", "intern",
    "recruiter", "ceo", "cto", "cfo", "coo", "vp",
)

# Acronyms we want preserved in title case (don't convert to "Ceo").
JD_TITLE_ACRONYMS = {
    "CEO", "CTO", "CFO", "COO", "VP", "AI", "ML", "AE", "SDR", "BDR",
    "PM", "QA", "UX", "UI", "PCB", "PMM", "RF",
    "LIDAR", "LiDAR", "EO", "IR", "FPGA",
    "MEMS", "DSP", "ASIC", "SoC", "GPU", "CPU",
    "GNC", "GN&C", "UAV", "UAS", "ISR",
    "NLP", "CV", "MLOps", "DevOps", "SecOps",
    "CPO", "CRO", "GM", "DRI",
}


def _detect_jd_section(line: str) -> str | None:
    """Return the canonical section key for a header line, or None if the
    line isn't a section header."""
    # Headers tend to be short. A line > 80 chars is almost certainly body.
    if len(line) > 80:
        return None
    normalized = re.sub(r"[^\w\s'/-]", " ", line.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        return None
    for canon, patterns in JD_SECTION_HEADERS.items():
        for p in patterns:
            # Match if the normalized line equals the pattern, or starts with
            # the pattern followed by a colon/dash/space (so "Qualifications:"
            # and "Qualifications - the must-haves" both hit).
            if normalized == p or normalized.startswith(p + " ") or normalized.startswith(p + ":"):
                return canon
    # Generic "About <CompanyName>" — a company-section header where the
    # name varies per JD (About LuminX, About Stripe, etc.). Heuristic: the
    # raw line starts with "About " followed by a Proper-Noun-looking token.
    m = re.match(r"^About\s+([A-Z][\w&.\-]+)\s*$", line.strip())
    if m:
        rest = m.group(1).lower()
        if rest not in {"us", "the", "our", "this", "what"}:
            return "company"
    return None


def split_jd_sections(text: str) -> dict[str, str]:
    """Split a JD into canonical sections. Returns `{section_key: text}` where
    section_key is one of the keys in JD_SECTION_HEADERS or `_intro` for the
    top-of-doc text before any recognised header."""
    sections: dict[str, list[str]] = {"_intro": []}
    current = "_intro"
    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            sections.setdefault(current, []).append("")
            continue
        section = _detect_jd_section(stripped)
        if section:
            current = section
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if v}


_TITLE_LOWERCASE_CONNECTORS = {
    "of", "to", "in", "on", "for", "the", "and", "or", "at", "as", "by",
}


def _normalize_title_case(s: str) -> str:
    """Title-case a job title while preserving common acronyms.
    Exact-case matches in JD_TITLE_ACRONYMS (e.g. "LiDAR") win over uppercase
    canonicalisation so mixed-case brand acronyms survive. Small connector
    words ("of", "and", "the", …) stay lowercase in non-initial position so
    "VP of Engineering" doesn't become "VP Of Engineering"."""
    words = s.split()
    out = []
    for idx, w in enumerate(words):
        stripped = w.strip(".,;:-—()/")
        upper = stripped.upper()
        if stripped in JD_TITLE_ACRONYMS:
            out.append(stripped)
        elif upper in JD_TITLE_ACRONYMS:
            out.append(upper)
        elif stripped.isupper() and 2 <= len(stripped) <= 4:
            out.append(stripped)
        elif idx > 0 and stripped.lower() in _TITLE_LOWERCASE_CONNECTORS:
            out.append(stripped.lower())
        else:
            out.append(stripped.capitalize() if stripped else w)
    return " ".join(out)


# ---------- Title backward-walk (used by extract_jd_title) ----------
#
# When we find a head noun in a line (engineer, manager, …), we walk
# BACKWARDS through preceding tokens to collect up to 3 modifier words so
# "Senior Laser Systems Engineer" stays intact instead of collapsing to
# "Engineer". The walk halts at sentence-glue words (articles, common
# prepositions, conjunctions, JD-prose verbs), at punctuation used as a
# separator (comma / pipe / dash / em-dash), at marketing fluff that should
# never appear in a real title, or at any word matched by _JD_TITLE_NOISE_RE.
_TITLE_BACKWARD_STOPS = {
    # Articles
    "a", "an", "the",
    # Prepositions (compound "X of Y" titles handled separately below)
    "for", "in", "on", "at", "with", "by", "from", "as",
    # Conjunctions
    "and", "or", "but",
    # JD-prose verbs that aren't legitimate title modifiers
    "is", "are", "be", "seek", "seeking", "hiring", "looking", "looks",
    "join", "joining", "need", "needs", "want", "wants", "build", "design",
    "drive", "work", "works", "wanted",
    # Marketing fluff that doesn't belong in a real title
    "ninja", "rockstar", "guru", "wizard", "passionate",
    "amazing", "awesome",
}

# Prepositions that DON'T halt the walk when immediately preceded by another
# head noun — that's the "VP of Engineering" / "Director of Operations" /
# "Head of Product" pattern, which is a legitimate compound title.
_TITLE_LOOKBACK_PREPS = {"of", "to"}

_TITLE_SEPARATORS = {",", "|", "-", "–", "—"}


def _walk_back_title(
    tokens: list[str], head_idx: int, head_set: set[str],
) -> list[str]:
    """Walk backwards from a head-noun token to collect up to 3 modifiers.
    Returns the modifier-prefixed phrase as a list of original-case tokens."""
    out = [tokens[head_idx]]
    count = 0
    i = head_idx - 1
    while i >= 0 and count < 3:
        word = tokens[i]
        wl = word.lower()
        if word in _TITLE_SEPARATORS:
            break
        if _JD_TITLE_NOISE_RE.search(word):
            break
        if wl in _TITLE_LOOKBACK_PREPS:
            # "VP of Engineering" — only include the preposition + one more
            # token if that token is itself a head noun.
            if i > 0 and tokens[i - 1].lower() in head_set and count + 2 <= 3:
                out.insert(0, tokens[i])
                out.insert(0, tokens[i - 1])
                i -= 2
                count += 2
                continue
            break
        if wl in _TITLE_BACKWARD_STOPS:
            break
        out.insert(0, word)
        i -= 1
        count += 1
    return out


_JD_TITLE_HEAD_RE = "|".join(JD_TITLE_HEAD_NOUNS)
_JD_TITLE_LINE_RE = re.compile(
    rf"""(?:^|\b)
    (
        (?:[A-Za-z][A-Za-z\-/&]*\s+){{0,4}}     # 0–4 leading words (e.g. "Senior Electrical ")
        (?:{_JD_TITLE_HEAD_RE})                  # head noun
        (?:[\s\-/][A-Za-z0-9]+){{0,2}}           # optional suffix (II, Lead, etc.)
    )
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Lines that contain any of these are metadata / chrome, not job titles —
# never extract a title from them.
_JD_TITLE_NOISE_RE = re.compile(
    r"\bago\b"
    r"|\bapplicants?\b"
    r"|\bpromoted\b"
    r"|\bactively\b"
    r"|\beasy\s+apply\b"
    r"|\bover\s+\d+\b"
    r"|^\s*save\b"                     # "Save Robotics Engineer at LuminX"
    r"|\b\d+\s*(?:second|minute|hour|day|week|month|year)s?\s+ago\b"
    r"|[·•]"                            # standalone bullet separators on metadata lines
    r"|\bshow\s+(?:more|less|match)\b"
    r"|^about\b",                       # "About the job", "About <company>"
    re.IGNORECASE | re.MULTILINE,
)


def _trim_title_suffix(phrase: str) -> str:
    """Drop " - Company", " | Company", " at Company", " @ Company" tails."""
    phrase = re.sub(r"\s*[-–|@]\s*[A-Z].*$", "", phrase)
    phrase = re.sub(r"\s+at\s+[A-Z]\w.*$", "", phrase, flags=re.IGNORECASE)
    phrase = re.sub(r"\s*\([^)]*\)\s*$", "", phrase)
    return phrase.strip(" .,;:-—()/")


def extract_jd_title(intro: str) -> str | None:
    """Find the job title at the top of a JD.

    Always returns something when a head-noun (engineer / manager / etc.) is
    present in the first ~12 intro lines. Walks top-down, skips lines that
    contain noisy metadata (dates, applicants count, "Save …", "About …"),
    and returns the FIRST clean 2–4-word title-like phrase. Falls back to
    a looser pass if the first pass finds nothing.

    Title construction strategy: tokenise the candidate line, find each
    head-noun token (engineer / manager / scientist / …), and walk
    BACKWARDS from each to collect up to 3 modifier words. The longest
    resulting phrase wins, capped at 4 total words. This preserves
    multi-word modifiers like "Senior Laser Systems" that the previous
    regex-only path could drop.
    """
    if not intro:
        return None
    head_set = {h.lower() for h in JD_TITLE_HEAD_NOUNS}
    # Long unbroken prose (no newlines) is common when a JD is pasted from a
    # single paragraph. Split such lines on sentence boundaries so a title
    # buried in "We are looking for a Senior Laser Systems Engineer to …"
    # still becomes its own candidate line.
    raw_lines = [l.strip() for l in intro.split("\n") if l.strip()]
    lines: list[str] = []
    for rl in raw_lines:
        if len(rl) > 120:
            parts = re.split(r"(?<=[.!?])\s+", rl)
            for p in parts:
                p = p.strip()
                if p:
                    lines.append(p)
        else:
            lines.append(rl)

    def _candidate(line: str) -> str | None:
        cleaned = re.sub(
            r"^(?:job\s*title|title|position|role)\s*[:\-–]\s*",
            "", line, flags=re.IGNORECASE,
        ).strip()
        cleaned = _trim_title_suffix(cleaned)
        if not cleaned:
            return None
        # Tokenise: words (including internal hyphens / slashes / ampersands)
        # as one token each; separator punctuation as its own token so the
        # backward walk can halt on it.
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-/&]*|[,|–—-]", cleaned)
        if not tokens:
            return None
        head_positions = [
            i for i, t in enumerate(tokens) if t.lower() in head_set
        ]
        if not head_positions:
            return None
        best: list[str] | None = None
        for hi in head_positions:
            chunk = _walk_back_title(tokens, hi, head_set)
            if not chunk:
                continue
            # Cap at 4 words per the rule "first 2-4 words that form the role name".
            if len(chunk) > 4:
                chunk = chunk[-4:]
            if best is None or len(chunk) > len(best):
                best = chunk
        if not best:
            return None
        return _normalize_title_case(" ".join(best))

    # Pass 1 — first clean line wins.
    for line in lines[:12]:
        if len(line) > 120 or len(line) < 3:
            continue
        if _JD_TITLE_NOISE_RE.search(line):
            continue
        candidate = _candidate(line)
        if candidate and len(candidate.split()) >= 2:
            return candidate

    # Pass 2 — same scan but accepting single-word candidates from short lines.
    for line in lines[:12]:
        if len(line) > 60 or len(line) < 3:
            continue
        if _JD_TITLE_NOISE_RE.search(line):
            continue
        candidate = _candidate(line)
        if candidate:
            return candidate

    # Pass 3 — loosen noise filter as a last resort so the field is never blank
    # when a head-noun exists somewhere near the top.
    for line in lines[:15]:
        if len(line) > 120 or len(line) < 3:
            continue
        candidate = _candidate(line)
        if candidate:
            return candidate

    return None


def _infer_seniority(title: str | None, yoe_min) -> tuple[str | None, str | None]:
    """Infer seniority from title prefix first, then YoE band:
        Senior / Sr / Lead / Staff / Principal in title → senior
        Junior / Jr / Entry / Intern / Associate in title → entry
        else by YoE: 0–2 → entry, 3–5 → mid, 6+ → senior
    Returns (value, source_description) or (None, None) if nothing applies."""
    title_lower = (title or "").lower()
    if re.search(r"\b(senior|sr\.?|lead|staff|principal)\b", title_lower):
        return "senior", "inferred from title"
    if re.search(r"\b(junior|jr\.?|entry|intern|associate)\b", title_lower):
        return "entry", "inferred from title"
    if isinstance(yoe_min, int):
        if yoe_min <= 2:
            return "entry", "inferred from years of experience"
        if yoe_min >= 6:
            return "senior", "inferred from years of experience"
        return "mid", "inferred from years of experience"
    return None, None


def _root_forms(s: str) -> list[str]:
    """Possible root forms for plural-aware dedup."""
    forms = [s]
    if s.endswith("ses") and len(s) > 4:
        forms.append(s[:-2])     # harnesses -> harness
    elif s.endswith("es") and len(s) > 3 and not s.endswith("oes"):
        forms.append(s[:-2])     # encoders no, but covers analyses/processes
        forms.append(s[:-1])     # encoders -> encoder
    elif s.endswith("s") and not s.endswith("ss") and not s.endswith("us") and len(s) > 2:
        forms.append(s[:-1])     # encoders -> encoder
    return forms


def _dedupe_skills(skills: list[str]) -> list[str]:
    """Two-pass cleanup so the surfaced skill list reads cleanly:

      1. Collapse singular/plural pairs — first match wins.
      2. Drop single-word terms that are already covered by a multi-word
         match that contains them (e.g. drop "PCB" when "PCB design" is in).
    """
    if not skills:
        return skills

    seen_roots: set[str] = set()
    after_plural: list[str] = []
    for s in skills:
        sl = s.lower()
        forms = _root_forms(sl)
        if any(f in seen_roots for f in forms):
            continue
        for f in forms:
            seen_roots.add(f)
        after_plural.append(s)

    multi_tokens: set[str] = set()
    for s in after_plural:
        if " " in s:
            for w in s.lower().split():
                multi_tokens.add(w)
    return [s for s in after_plural if " " in s or s.lower() not in multi_tokens]


def extract_jd_skills(text: str) -> list[str]:
    """Pull known tech terms out of the given text. Longest-first match so
    multi-word phrases anchor first; then dedupe singular/plural and
    subset matches so the final list reads cleanly."""
    if not text:
        return []
    seen_lower = set()
    found: list[str] = []
    occupied: list[tuple[int, int]] = []  # [(start, end), ...]

    def _overlaps(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in occupied)

    for term in sorted(JD_TECH_VOCAB, key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        for m in pattern.finditer(text):
            if _overlaps(m.start(), m.end()):
                continue
            occupied.append((m.start(), m.end()))
            key = term.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            found.append(term)
    return _dedupe_skills(found)


# Cue words that signal a skill is REQUIRED vs OPTIONAL even inside the
# wrong section. Lines containing these phrases override the section default.
_MUST_HAVE_CUES = re.compile(
    r"\b("
    r"required|must\s+have|must-have|essential|proficien(?:cy|t)\s+in|"
    r"expertise\s+in|deep\s+experience|strong\s+experience|"
    r"hands[-\s]?on\s+experience|extensive\s+experience|"
    r"proven\s+experience|"
    r"minimum\s+\d+\+?\s+(?:years?|yrs)|"
    r"\d+\+\s*(?:years?|yrs)\s+of"
    r")\b",
    re.IGNORECASE,
)
_NICE_TO_HAVE_CUES = re.compile(
    r"\b("
    r"bonus|nice\s+to\s+have|preferred|plus|familiarity\s+with|"
    r"ideally|bonus\s+points|would\s+be\s+(?:a\s+)?plus|"
    r"exposure\s+to|appreciated|helpful"
    r")\b",
    re.IGNORECASE,
)
# Note: "experience with" is deliberately NOT a nice-to-have cue — it's the
# most common phrasing in must-have requirement bullets ("Experience with
# Python", "Hands-on experience with ROS"), so demoting on it would
# misclassify the majority of real-world requirement lines.


def split_jd_skills_by_strictness(text: str, default: str) -> tuple[list[str], list[str]]:
    """Pull tech-vocab skills from `text` and split into (must, nice) lists.

    `default` is the section's default classification — "must" for a
    Qualifications/Requirements section, "nice" for a Preferred/Bonus
    section. Per-line cue words override the default.
    """
    must: list[str] = []
    nice: list[str] = []
    if not text:
        return must, nice
    for line in text.split("\n"):
        if not line.strip():
            continue
        if _MUST_HAVE_CUES.search(line):
            line_class = "must"
        elif _NICE_TO_HAVE_CUES.search(line):
            line_class = "nice"
        else:
            line_class = default
        line_skills = extract_jd_skills(line)
        if line_class == "must":
            must.extend(line_skills)
        else:
            nice.extend(line_skills)
    return _dedupe_skills(must), _dedupe_skills(nice)


# ---------- Per-field extractors for parse_jd ----------
#
# Each helper returns (value, source_label) — or (None, None) when nothing
# was found. They never write to the criteria dict directly; parse_jd is the
# single place that knows about the dict's shape.

def _jd_find_location(intro: str, quals_block: str) -> tuple[str | None, str | None]:
    """Pick the first known location in the intro, then the requirements
    zone. Never reads from company/benefits text."""
    for block, origin in ((intro, "top of posting"), (quals_block, "Qualifications")):
        if not block:
            continue
        lower = block.lower()
        for loc in sorted(LOCATION_PATTERNS, key=len, reverse=True):
            if re.search(_word_re(loc), lower):
                return LOCATION_CANONICAL.get(loc, loc.title()), origin
    return None, None


_JD_YOE_RANGE_RE = re.compile(
    r"(\d+)\s*[-–]\s*(\d+)\s*\+?\s*(?:years?|yrs|yoe|yo)\b",
    re.IGNORECASE,
)
_JD_YOE_PLUS_RE = re.compile(
    r"(\d+)\+?\s*(?:years?|yrs|yoe|yo)\b",
    re.IGNORECASE,
)


def _jd_find_yoe(quals_block: str) -> tuple[int | None, int | None]:
    """Return (min, max) years of experience, either of which may be None."""
    if not quals_block.strip():
        return None, None
    text = quals_block.lower()
    m = _JD_YOE_RANGE_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _JD_YOE_PLUS_RE.search(text)
    if m:
        return int(m.group(1)), None
    return None, None


def _jd_find_school(quals_block: str) -> str | None:
    """Pick the first known school in the requirements zone."""
    if not quals_block:
        return None
    lower = quals_block.lower()
    for school in sorted(SCHOOLS, key=len, reverse=True):
        if re.search(_word_re(school), lower):
            return school.title()
    return None


def _jd_find_keywords(quals_block: str) -> list[str]:
    """Industry / project keywords present in the requirements zone."""
    if not quals_block:
        return []
    lower = quals_block.lower()
    return [
        kw for kw in sorted(KEYWORDS, key=len, reverse=True)
        if re.search(_word_re(kw), lower)
    ]


def _jd_split_skills(
    qualifications: str,
    preferred: str,
    has_quals_section: bool,
    quals_block: str,
) -> tuple[list[str], list[str]]:
    """Pull skills out of the JD and split them into (must, nice).

    Rule of thumb:
      * Skills under Qualifications/Requirements → must_have
      * Skills under Preferred/Bonus            → nice_to_have
      * Per-line cue word (bonus / nice / preferred / ideally /
        familiarity with) inside Qualifications demotes that line to
        nice; inversely required / must have / essential / proficiency in
        inside Preferred promotes it to must.
      * Unstructured JD (no Qualifications section detected) → default
        all skills to nice_to_have so the search isn't over-restricted.
      * A skill that lands in both buckets is kept as must-have only.
    """
    must_q, nice_q = split_jd_skills_by_strictness(qualifications, default="must")
    must_p, nice_p = split_jd_skills_by_strictness(preferred, default="nice")
    must = _dedupe_skills(must_q + must_p)
    nice = _dedupe_skills(nice_q + nice_p)
    if not has_quals_section:
        nice = _dedupe_skills(nice + extract_jd_skills(quals_block))
        must = []
    must_lower = {m.lower() for m in must}
    nice = [s for s in nice if s.lower() not in must_lower]
    return must, nice


def parse_jd(text: str) -> tuple[dict, dict]:
    """Universal JD parser. Returns (criteria, sources).

    Pipeline:
      1. Clean — strip LinkedIn/job-board chrome, URLs, markdown, invisible
         unicode.
      2. Split into canonical sections (intro / responsibilities /
         qualifications / preferred / company / benefits).
      3. Build the "requirements zone" — qualifications + preferred when
         either is present, otherwise everything except company/benefits.
      4. Title (intro only) → location (intro then requirements) → YoE,
         school, skills, project keywords (requirements only) → seniority
         (inferred from title + YoE, never scraped).
      5. Employers are NEVER auto-filled from a JD — the field is set to
         [] so the frontend wipes any stale selection.
      6. title_cluster — adjacent / related titles inferred from the
         extracted title via a local lookup table. Used by the frontend to
         seed "Good to have" chips.
      7. came_from_orgs — types of organisations the JD signals would be a
         good background (Defense, FAANG, Top Startup, …). Inferred by
         scanning the full cleaned JD text against a signal map.
    """
    # Local lookup tables — kept inside parse_jd because they are only used
    # by the inference passes below and aren't part of the public surface.
    TITLE_CLUSTER_MAP = [
        ("software engineer", [
            "Tech Lead", "Staff Engineer", "Senior Software Engineer",
            "Software Developer", "Senior Backend Engineer",
        ]),
        ("frontend engineer", [
            "UI Engineer", "React Developer", "Web Engineer",
            "Full Stack Engineer",
        ]),
        ("backend engineer", [
            "Software Engineer", "Platform Engineer",
            "Infrastructure Engineer", "Full Stack Engineer",
        ]),
        ("full stack", [
            "Software Engineer", "Web Engineer",
            "Frontend Engineer", "Backend Engineer",
        ]),
        ("data engineer", [
            "Analytics Engineer", "Data Platform Engineer",
            "Backend Engineer", "ML Engineer",
        ]),
        ("machine learning engineer", [
            "ML Engineer", "AI Engineer", "Research Engineer",
            "Data Scientist", "MLOps Engineer",
        ]),
        ("data scientist", [
            "ML Engineer", "Research Scientist",
            "Quantitative Analyst", "Applied Scientist",
        ]),
        ("product manager", [
            "Technical Program Manager", "Group Product Manager",
            "Senior Product Manager", "Product Lead",
        ]),
        ("engineering manager", [
            "Tech Lead Manager", "Director of Engineering",
            "Senior Engineering Manager", "Staff Engineer",
        ]),
        ("devops", [
            "Site Reliability Engineer", "Platform Engineer",
            "Infrastructure Engineer", "Cloud Engineer",
        ]),
        ("security engineer", [
            "AppSec Engineer", "Cloud Security Engineer",
            "Security Architect", "Penetration Tester",
        ]),
        ("optical engineer", [
            "Photonics Engineer", "Laser Engineer",
            "Systems Engineer", "Electro-Optical Engineer",
        ]),
        ("laser engineer", [
            "Optical Engineer", "Photonics Engineer",
            "Electro-Optical Engineer", "Systems Engineer",
        ]),
        ("systems engineer", [
            "Hardware Engineer", "Embedded Engineer",
            "Integration Engineer", "Aerospace Engineer",
        ]),
        ("hardware engineer", [
            "Electrical Engineer", "Embedded Engineer",
            "Systems Engineer", "PCB Engineer",
        ]),
        ("electrical engineer", [
            "Hardware Engineer", "Embedded Engineer",
            "Power Electronics Engineer", "RF Engineer",
        ]),
        ("mechanical engineer", [
            "Systems Engineer", "Structural Engineer",
            "Design Engineer", "Manufacturing Engineer",
        ]),
        ("research scientist", [
            "Applied Scientist", "ML Research Engineer",
            "Senior Researcher", "Staff Research Scientist",
        ]),
        ("quantitative", [
            "Quantitative Researcher", "Quantitative Developer",
            "Algorithmic Trader", "Data Scientist",
        ]),
        ("recruiter", [
            "Talent Acquisition", "Technical Recruiter",
            "Sourcer", "People Operations",
        ]),
    ]

    ORG_SIGNALS = [
        ("Defense & Government", [
            "defense", "government", "dod", "department of defense",
            "itar", "security clearance", "classified", "mil-spec",
            "military", "federal", "intelligence community",
        ]),
        ("FAANG / Big Tech", [
            "google", "meta", "amazon", "apple", "microsoft",
            "netflix", "faang", "big tech", "top tech",
        ]),
        ("Top Startup", [
            "fast-paced startup", "early stage", "series a",
            "series b", "seed stage", "startup experience",
            "hypergrowth", "scale-up",
        ]),
        ("Big Pharma / Healthcare", [
            "pharmaceutical", "biotech", "medtech", "medical device",
            "fda", "clinical", "healthcare", "life sciences", "gmp",
        ]),
        ("Consulting", [
            "consulting", "consultancy", "mckinsey", "bcg", "bain",
            "deloitte", "accenture", "pwc", "kpmg", "ey ",
        ]),
        ("Big Bank", [
            "investment bank", "goldman", "morgan stanley",
            "jp morgan", "jpmorgan", "citadel", "two sigma",
            "hedge fund", "asset management", "financial services",
        ]),
        ("Top University", [
            "phd required", "phd preferred", "doctorate",
            "research university", "academic background",
            "postdoc", "national lab",
        ]),
    ]

    if not text or not text.strip():
        return {}, {}

    text = clean_jd_text(text)
    if not text:
        return {}, {}

    sections = split_jd_sections(text)
    intro = sections.get("_intro", "")
    qualifications = sections.get("qualifications", "")
    preferred = sections.get("preferred", "")

    quals_block = "\n".join(s for s in (qualifications, preferred) if s)
    has_quals_section = bool(quals_block.strip())
    if not has_quals_section:
        # Unstructured JD — treat everything outside company/benefits as
        # the requirements zone so we still get useful signal.
        quals_block = "\n".join(
            s for k, s in sections.items() if k not in JD_SKIP_SECTIONS
        )

    criteria: dict = {"employers": []}  # JD never anchors employers
    sources: dict = {}

    title = extract_jd_title(intro)
    if title:
        criteria["current_title"] = title
        sources["current_title"] = "top of posting"

    loc, loc_src = _jd_find_location(intro, quals_block)
    if loc:
        criteria["location"] = loc
        sources["location"] = loc_src

    yoe_min, yoe_max = _jd_find_yoe(quals_block)
    if yoe_min is not None:
        criteria["years_experience_min"] = yoe_min
        sources["years_experience_min"] = "Qualifications"
    if yoe_max is not None:
        criteria["years_experience_max"] = yoe_max
        sources["years_experience_max"] = "Qualifications"

    sen_value, sen_source = _infer_seniority(
        criteria.get("current_title"),
        criteria.get("years_experience_min"),
    )
    if sen_value:
        criteria["seniority"] = sen_value
        sources["seniority"] = sen_source

    school = _jd_find_school(quals_block)
    if school:
        criteria["school"] = school
        sources["school"] = "Qualifications"

    must_have, nice_to_have = _jd_split_skills(
        qualifications, preferred, has_quals_section, quals_block,
    )
    if must_have:
        criteria["must_have_skills"] = must_have
        sources["must_have_skills"] = "Qualifications"
    if nice_to_have:
        criteria["nice_to_have_skills"] = nice_to_have
        sources["nice_to_have_skills"] = "Preferred" if preferred else "Qualifications"

    project_keywords = _jd_find_keywords(quals_block)
    if project_keywords:
        criteria["project_keywords"] = project_keywords
        sources["project_keywords"] = "Qualifications"

    # Infer title_cluster from the extracted title via the local map.
    # First substring match wins. The primary title itself is filtered out
    # of the cluster so the chip list doesn't duplicate the role line.
    title_cluster: list[str] = []
    extracted_title = criteria.get("current_title") or ""
    if extracted_title:
        title_lower = extracted_title.lower()
        for needle, cluster in TITLE_CLUSTER_MAP:
            if needle in title_lower:
                title_cluster = [
                    t for t in cluster if t.lower() != title_lower
                ]
                break
    criteria["title_cluster"] = title_cluster

    # Infer came_from_orgs from full-JD-text signals. Any signal hit anywhere
    # in the cleaned JD adds that org type. Multiple matches are allowed.
    text_lower = text.lower()
    came_from_orgs: list[str] = []
    for org_type, signals in ORG_SIGNALS:
        if any(sig in text_lower for sig in signals):
            came_from_orgs.append(org_type)
    criteria["came_from_orgs"] = came_from_orgs

    return criteria, sources


# ---------- Filter construction ----------

# ---------- Curated "They came from..." tiers ----------
#
# Each tier maps to a list of well-known entities. Selecting a tier means
# "anyone who has worked at any of these orgs (or attended any of these
# schools) at any point in their career."

TIERS: dict[str, dict] = {
    "big_bank": {
        "label": "Big Bank",
        "type": "employer",
        "items": [
            "Goldman Sachs", "JPMorgan Chase", "Morgan Stanley",
            "Bank of America", "Citigroup", "Barclays", "Credit Suisse",
            "UBS", "Deutsche Bank", "HSBC", "Wells Fargo",
        ],
    },
    "big_law": {
        "label": "Big Law",
        "type": "employer",
        "items": [
            "Skadden, Arps, Slate, Meagher & Flom",
            "Cravath, Swaine & Moore", "Sullivan & Cromwell",
            "Wachtell, Lipton, Rosen & Katz", "Davis Polk & Wardwell",
            "Latham & Watkins", "Kirkland & Ellis",
            "Paul, Weiss, Rifkind, Wharton & Garrison",
            "Simpson Thacher & Bartlett", "Gibson Dunn",
        ],
    },
    "faang": {
        "label": "FAANG / Big Tech",
        "type": "employer",
        "items": ["Meta", "Apple", "Amazon", "Netflix", "Google", "Microsoft"],
    },
    "top_startup": {
        "label": "Top Startup",
        "type": "employer",
        "items": [
            "Stripe", "Airbnb", "Uber", "Coinbase", "DoorDash", "Plaid",
            "Notion", "Linear", "Figma", "OpenAI", "Anthropic", "Scale AI",
            "Databricks", "Snowflake", "Vercel", "Discord", "Canva",
            "Instacart", "Brex", "Ramp",
        ],
    },
    "top_uni": {
        "label": "Top University",
        "type": "school",
        "items": [
            "Stanford University",
            "Massachusetts Institute of Technology",
            "Harvard University", "Yale University", "Princeton University",
            "Columbia University", "University of Pennsylvania",
            "Brown University", "Cornell University", "Dartmouth College",
            "University of Oxford", "University of Cambridge",
            "California Institute of Technology",
        ],
    },
    "consulting": {
        "label": "Consulting",
        "type": "employer",
        "items": [
            "McKinsey & Company", "Boston Consulting Group", "Bain & Company",
            "Deloitte", "PwC", "EY", "KPMG", "Accenture",
            "Oliver Wyman", "Roland Berger",
        ],
    },
    "big_pharma": {
        "label": "Big Pharma / Healthcare",
        "type": "employer",
        "items": [
            "Pfizer", "Johnson & Johnson", "Merck", "Novartis", "Roche",
            "AstraZeneca", "AbbVie", "Eli Lilly", "GSK",
            "Bristol-Myers Squibb", "Sanofi",
        ],
    },
    "defense_gov": {
        "label": "Defense & Government",
        "type": "employer",
        "items": [
            "Lockheed Martin", "Raytheon", "Northrop Grumman", "Boeing",
            "General Dynamics", "Booz Allen Hamilton", "BAE Systems",
            "L3Harris",
        ],
    },
}


def build_from_conditions(tier_keys, custom_companies) -> dict | None:
    """Combine tier selections + freeform company names into one OR group:

        (worked at any tier company)
        OR (worked at any freeform company)
        OR (attended any tier school)

    Employer entries get resolved to Crustdata company_ids via the cached
    /screener/identify endpoint; anything that fails to resolve falls back
    to a fuzzy name match. Schools always fuzzy-match on the school field.
    Returns None when no tier or company is selected.
    """
    tier_keys = tier_keys if isinstance(tier_keys, list) else []
    custom_companies = custom_companies if isinstance(custom_companies, list) else []
    if not tier_keys and not custom_companies:
        return None

    tier_company_names: list[str] = []
    school_names: list[str] = []
    for key in tier_keys:
        tier = TIERS.get(key)
        if not tier:
            continue
        if tier["type"] == "employer":
            tier_company_names.extend(tier["items"])
        elif tier["type"] == "school":
            school_names.extend(tier["items"])

    employer_ids, unresolved_names = _resolve_companies(
        tier_company_names + list(custom_companies)
    )

    or_conditions: list = []
    if employer_ids:
        # Dedupe — tier + custom may list the same company.
        or_conditions.append(op_in(FIELD.ALL_EMPLOYERS_ID, sorted(set(employer_ids))))
    for name in unresolved_names:
        or_conditions.append(fuzzy(FIELD.ALL_EMPLOYERS_NAME, name))
    for school in school_names:
        or_conditions.append(fuzzy(FIELD.SCHOOL, school))

    if not or_conditions:
        return None
    if len(or_conditions) == 1:
        return or_conditions[0]
    return op_or(or_conditions)


def token(field: str, value):
    """Substring/token match (no typos). The skill's preferred operator for
    structured fields like titles and skills, per Phase 2 Step 3."""
    return {"filter_type": field, "type": "[.]", "value": value}


def geo(field: str, location: str, miles: int = 50):
    return {
        "filter_type": field,
        "type": "geo_distance",
        "value": {"location": location, "distance": miles, "unit": "mi"},
    }


SEARCH_MODES = ("exact", "similar", "broad")
DEFAULT_MODE = "similar"

# Geo radius for the `region` geo_distance condition. Broad opens it up so the
# search degrades gracefully when Exact/Similar over-constrain.
GEO_RADIUS_DEFAULT_MILES = 50
GEO_RADIUS_BROAD_MILES = 100

# Broad-mode auto-relaxation budget — at most this many extra Crustdata calls
# beyond the initial one, and stop relaxing once total_count crosses the
# health threshold.
BROAD_HEALTHY_TOTAL_COUNT = 10
BROAD_MAX_EXTRA_CALLS = 3

# Match-band score cutoffs (out of 100). Anything below GOOD lands in Partial.
MATCH_STRONG_MIN = 85
MATCH_GOOD_MIN = 60

# Nice-to-have skill weight — a matched bonus skill adds this much to both the
# numerator and denominator of the match score, so the percentage lifts but a
# miss never penalises the candidate.
NICE_TO_HAVE_WEIGHT = 0.25


def _normalize_mode(value) -> str:
    """Coerce a possibly-user-supplied mode string to one of the valid modes."""
    if not value:
        return DEFAULT_MODE
    v = str(value).lower().strip()
    return v if v in SEARCH_MODES else DEFAULT_MODE


def _filter_identity(c: dict, text_op, radius_miles: int) -> list:
    """Title / location / school / seniority / YoE — the always-on stanza."""
    out: list = []
    title = (c.get("current_title") or "").strip()
    if title:
        out.append(text_op(FIELD.CURRENT_TITLE, title))

    location = (c.get("location") or "").strip()
    if location:
        # Broad mode auto-widens to GEO_RADIUS_BROAD_MILES — the user can
        # widen further but never below that floor (otherwise "Broad" stops
        # being broad). In Exact / Similar modes the user-supplied value wins
        # outright so a tight 10-mile search behaves as asked.
        # work_preference is collected client-side but has no Crustdata field
        # yet — store on the criteria, but don't emit. TODO: surface in scorer
        # when Crustdata exposes the workplace_type field.
        raw_radius = c.get("location_radius_miles")
        try:
            user_radius = int(raw_radius) if raw_radius not in (None, "") else None
        except (TypeError, ValueError):
            user_radius = None
        if user_radius is None:
            effective = radius_miles
        elif radius_miles == GEO_RADIUS_BROAD_MILES:
            effective = max(user_radius, radius_miles)
        else:
            effective = user_radius
        out.append(geo(FIELD.REGION, location, miles=effective))

    school = (c.get("school") or "").strip()
    if school:
        out.append(fuzzy(FIELD.SCHOOL, school))

    seniority = (c.get("seniority") or "").strip()
    if seniority:
        value = SENIORITY_MAP.get(seniority.lower(), seniority)
        out.append(exact(FIELD.CURRENT_SENIORITY, value))

    yoe_min = c.get("years_experience_min")
    if yoe_min not in (None, "", 0, "0"):
        try:
            out.append(gte(FIELD.YEARS_EXPERIENCE, int(yoe_min)))
        except (TypeError, ValueError):
            pass

    yoe_max = c.get("years_experience_max")
    if yoe_max not in (None, ""):
        try:
            out.append(lte(FIELD.YEARS_EXPERIENCE, int(yoe_max)))
        except (TypeError, ValueError):
            pass
    return out


def _filter_skills_and_keywords(c: dict, text_op) -> list:
    """Must-have skills as strict AND conditions; project keywords as fuzzy
    summary matches. Nice-to-have skills never reach Crustdata — they only
    affect the local match score in _score_one_profile."""
    out: list = []
    for skill in (c.get("must_have_skills") or []):
        skill = (skill or "").strip()
        if skill:
            out.append(text_op(FIELD.SKILLS, skill))
    for keyword in (c.get("project_keywords") or []):
        keyword = (keyword or "").strip()
        if keyword:
            out.append(fuzzy(FIELD.SUMMARY, keyword))
    return out


# Maps the recruiter-friendly size buckets shown in the UI to the
# `company_headcount_range` strings Crustdata's persondb returns. The
# Crustdata range labels are the same ones LinkedIn surfaces, so a single
# UI bucket may cover more than one underlying range.
SIZE_BUCKET_MAP: dict[str, list[str]] = {
    "1-50":      ["1-10", "11-50"],
    "51-200":    ["51-200"],
    "201-1000":  ["201-500", "501-1000"],
    "1001-5000": ["1001-5000"],
    "5000+":     ["5001-10000", "10001+"],
}


def _size_buckets(label) -> list[str]:
    """Resolve a UI size label to the underlying Crustdata range strings.
    Returns [] for empty / "any" / unknown labels so callers can short-circuit."""
    s = (label or "").strip().lower()
    if not s or s == "any":
        return []
    return SIZE_BUCKET_MAP.get(s, [])


def _filter_from(c: dict) -> list:
    """'They came from…' — tier pills + freeform companies, plus an optional
    company-size constraint that applies to the candidate's full career
    history (not just the current role)."""
    out: list = []
    cond = build_from_conditions(c.get("from_tiers"), c.get("from_companies"))
    if cond:
        out.append(cond)
    size_buckets = _size_buckets(c.get("came_from_size"))
    if size_buckets:
        out.append(op_in(FIELD.ALL_EMPLOYERS_HEADCOUNT_RANGE, size_buckets))
    return out


def _filter_advanced(c: dict) -> list:
    """Recently changed jobs, overly-senior cap, function/industry,
    lines-of-defense, and career-arc. Each block is independent."""
    out: list = []

    if c.get("signal_recently_changed") or c.get("recently_changed_jobs"):
        out.append(exact(FIELD.RECENTLY_CHANGED, True))
    # signal_likely_mobile has no direct Crustdata field — handled in
    # _score_one_profile by checking tenure length on the current role.

    # Exclude overly senior — drop the CXO bucket AND any current title
    # containing "Chief".
    if c.get("exclude_overly_senior"):
        out.append(op_neq(FIELD.CURRENT_SENIORITY, "CXO"))
        out.append(op_neg(FIELD.CURRENT_TITLE, "Chief"))

    function_areas = c.get("function_areas") or []
    if isinstance(function_areas, list) and function_areas:
        out.append(op_in(FIELD.CURRENT_FUNCTION, function_areas))

    industries = c.get("industries") or []
    if isinstance(industries, list) and industries:
        out.append(op_in(FIELD.CURRENT_INDUSTRIES, industries))

    lines_of_defense = c.get("lines_of_defense") or []
    if isinstance(lines_of_defense, list) and lines_of_defense:
        line_or = [fuzzy(FIELD.SUMMARY, line) for line in lines_of_defense if line]
        if len(line_or) == 1:
            out.append(line_or[0])
        elif line_or:
            out.append(op_or(line_or))

    # Career arc — each step is an OR (employer-name OR summary); steps AND.
    for raw_step in (c.get("career_arc") or []):
        step = (raw_step or "").strip()
        if not step:
            continue
        out.append(op_or([
            fuzzy(FIELD.ALL_EMPLOYERS_NAME, step),
            fuzzy(FIELD.SUMMARY, step),
        ]))
    return out


def _filter_exclusions(c: dict) -> list:
    """Exclude-title (fuzzy negation), exclude-company (id-first),
    exclude-seniority list, and exclude-skills (fuzzy negation per skill)."""
    out: list = []
    for raw in (c.get("exclude_titles") or []):
        title = (raw or "").strip()
        if title:
            out.append(op_neg(FIELD.CURRENT_TITLE, title))

    excl_ids, excl_unresolved = _resolve_companies(c.get("exclude_companies"))
    if excl_ids:
        out.append(op_not_in(FIELD.ALL_EMPLOYERS_ID, sorted(set(excl_ids))))
    for name in excl_unresolved:
        out.append(op_neg(FIELD.ALL_EMPLOYERS_NAME, name))

    exclude_seniority = c.get("exclude_seniority") or []
    if isinstance(exclude_seniority, list) and exclude_seniority:
        out.append(op_not_in(FIELD.ALL_EMPLOYERS_SENIORITY, exclude_seniority))

    for raw in (c.get("exclude_skills") or []):
        skill = (raw or "").strip()
        if skill:
            out.append(op_neg(FIELD.SKILLS, skill))
    return out


def _filter_employers(c: dict) -> list:
    """'They worked at X between Y and Z' employer stops. With dates we use a
    nested AND (tenure overlaps the range); without dates the stop matches
    current OR past employment of that company.

    Each employer row carries an optional `tenure`:
      * "either" (default) — current OR past — preserves the legacy behaviour
      * "current"          — only people currently at that company
      * "past"             — only people who left that company
    """
    out: list = []
    for employer in (c.get("employers") or []):
        company = (employer.get("company") or "").strip()
        start_year = (employer.get("start_year") or "").strip()
        end_year = (employer.get("end_year") or "").strip()
        tenure = (employer.get("tenure") or "either").strip().lower()
        if tenure not in ("current", "past", "either"):
            tenure = "either"
        size_buckets = _size_buckets(employer.get("company_size"))
        if not (company or start_year or end_year):
            continue

        company_id = resolve_company_id(company) if company else None
        has_dates = bool(start_year or end_year)

        # Dates only make sense against past tenure (Crustdata exposes
        # start/end on past_employers). If the user asked for "current" with
        # dates, drop the dates and fall through to the no-date branch — they
        # want anyone currently at the company, the years are informational.
        if has_dates and tenure == "current":
            has_dates = False

        # Size constraint binds to the same scope as the row's tenure so it
        # narrows the same employer record rather than matching any employer
        # of that size in the candidate's history.
        if tenure == "current":
            size_field = FIELD.CURRENT_HEADCOUNT_RANGE
        elif tenure == "past":
            size_field = FIELD.PAST_HEADCOUNT_RANGE
        else:
            size_field = FIELD.ALL_EMPLOYERS_HEADCOUNT_RANGE

        if has_dates:
            # Overlap semantics: tenure overlaps [start, end] iff
            #   start_date <= end_of_range AND end_date >= start_of_range.
            # One nested AND keeps the operands bound to the same
            # past_employers element per Crustdata's nested semantics.
            employer_conds: list = []
            if company_id:
                employer_conds.append(exact(FIELD.PAST_COMPANY_ID, company_id))
            elif company:
                employer_conds.append(fuzzy(FIELD.PAST_NAME, company))
            if end_year:
                employer_conds.append(lte(FIELD.PAST_START_DATE, year_to_end(end_year)))
            if start_year:
                employer_conds.append(gte(FIELD.PAST_END_DATE, year_to_start(start_year)))
            if size_buckets:
                employer_conds.append(op_in(FIELD.PAST_HEADCOUNT_RANGE, size_buckets))
            out.append(op_and(employer_conds))
        elif company_id:
            if tenure == "current":
                base = exact(FIELD.CURRENT_COMPANY_ID, company_id)
                if size_buckets:
                    out.append(op_and([base, op_in(size_field, size_buckets)]))
                else:
                    out.append(base)
            elif tenure == "past":
                base = exact(FIELD.PAST_COMPANY_ID, company_id)
                if size_buckets:
                    out.append(op_and([base, op_in(size_field, size_buckets)]))
                else:
                    out.append(base)
            else:
                # "either" — match current OR past (Phase 2 Step 3 cluster anchor).
                # With a size constraint we bind the company_id and the size to
                # the same employer record via the merged all_employers fields,
                # otherwise a candidate with company X in the past AND any
                # unrelated current employer of the requested size would match.
                if size_buckets:
                    out.append(op_and([
                        exact(FIELD.ALL_EMPLOYERS_ID, company_id),
                        op_in(FIELD.ALL_EMPLOYERS_HEADCOUNT_RANGE, size_buckets),
                    ]))
                else:
                    out.append(op_or([
                        exact(FIELD.CURRENT_COMPANY_ID, company_id),
                        exact(FIELD.PAST_COMPANY_ID, company_id),
                    ]))
        elif company:
            if tenure == "current":
                base = fuzzy(FIELD.CURRENT_NAME, company)
            elif tenure == "past":
                base = fuzzy(FIELD.PAST_NAME, company)
            else:
                base = fuzzy(FIELD.ALL_EMPLOYERS_NAME, company)
            if size_buckets:
                out.append(op_and([base, op_in(size_field, size_buckets)]))
            else:
                out.append(base)
    return out


def build_filters(criteria: dict, mode: str = DEFAULT_MODE) -> dict:
    """Single canonical filter builder. Turns the structured criteria dict
    into the {"op": "and", "conditions": [...]} payload Crustdata expects.

    Three modes share the same shape but differ in operator + radius:
      * exact   — `[.]` strict substring on titles/skills, default geo
      * similar — `(.)` fuzzy on titles/skills, default geo
      * broad   — same as similar but with the wider geo radius. Filter
                  *relaxation* happens later in /api/search; here, broad
                  only changes the radius.

    The criteria dict is normalised once (legacy `skills` → `must_have_skills`)
    and then each stanza adds independently. The recipe is:

        identity → skills+keywords → "from" → advanced → exclusions → employers

    Note: hands_on_leader / team_size_led / stakeholder_range / technical_depth /
    weight_recent_experience are captured client-side but don't yet map to
    Crustdata filters — they survive on the criteria dict for future use
    without producing brittle conditions.
    """
    mode = _normalize_mode(mode)
    c = normalize_criteria(criteria)
    text_op = token if mode == "exact" else fuzzy
    radius_miles = GEO_RADIUS_BROAD_MILES if mode == "broad" else GEO_RADIUS_DEFAULT_MILES

    conditions: list = []
    conditions.extend(_filter_identity(c, text_op, radius_miles))
    conditions.extend(_filter_skills_and_keywords(c, text_op))
    conditions.extend(_filter_from(c))
    conditions.extend(_filter_advanced(c))
    conditions.extend(_filter_exclusions(c))
    conditions.extend(_filter_employers(c))
    return op_and(conditions)


def summarize(criteria: dict) -> str:
    """One-line human summary of the criteria — used as the search-history
    label. Reads from the normalised criteria so legacy `skills` folds in."""
    c = normalize_criteria(criteria)
    parts: list[str] = []
    if c.get("current_title"):
        parts.append(c["current_title"])
    employers = [e.get("company") for e in (c.get("employers") or []) if e.get("company")]
    if employers:
        parts.append("ex-" + ", ".join(employers))
    if c.get("location"):
        parts.append("in " + c["location"])
    must_have = [s for s in (c.get("must_have_skills") or []) if s]
    if must_have:
        parts.append("must: " + ", ".join(must_have))
    nice_to_have = [s for s in (c.get("nice_to_have_skills") or []) if s]
    if nice_to_have:
        parts.append("nice: " + ", ".join(nice_to_have))
    return " · ".join(parts) or "Untitled search"


# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html")


def _call_crustdata_search(payload: dict):
    """Call /screener/persondb/search. Returns either the parsed response dict
    or {"_error": ..., "_status": int} so the route can short-circuit."""
    try:
        r = requests.post(
            CRUSTDATA_URL,
            headers={
                "Authorization": f"Token {CRUSTDATA_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        return {"_error": {"error": f"Upstream request failed: {exc}"}, "_status": 502}
    if r.status_code >= 400:
        return {
            "_error": {
                "error": f"Crustdata returned {r.status_code}",
                "details": r.text[:2000],
            },
            "_status": r.status_code,
        }
    return r.json()


# Broad-mode relaxation ladder — applied in order to the criteria dict until
# the search returns enough matches or the ladder runs out. Each step is
# (user-facing label, mutator). The mutator drops or loosens fields on the
# `crit` dict in place; an early step that drops nothing is silently skipped
# by the caller (so the ladder works on partial criteria).
_BROAD_RELAXATION_LADDER: list[tuple[str, "callable[[dict], None]"]] = [
    ("must-have skills", lambda c: (
        c.pop("must_have_skills", None),
        c.pop("skills", None),
    )),
    ("project keywords", lambda c: c.pop("project_keywords", None)),
    ("seniority", lambda c: c.pop("seniority", None)),
    ("years of experience", lambda c: (
        c.pop("years_experience_min", None),
        c.pop("years_experience_max", None),
    )),
    ("school", lambda c: c.pop("school", None)),
    ("employer dates", lambda c: [
        e.update({"start_year": "", "end_year": ""})
        for e in (c.get("employers") or [])
    ]),
    ("location", lambda c: c.pop("location", None)),
    ("employers", lambda c: c.pop("employers", None)),
]


def _search_broad(criteria: dict, limit: int):
    """Run a broad-mode search: progressively drop filters until total_count
    is healthy or we run out of relaxations. Capped at 3 extra calls so credit
    spend stays bounded."""
    relaxed: list[str] = []

    working = json.loads(json.dumps(criteria))  # deep copy
    filters = build_filters(working, mode="broad")
    data = _call_crustdata_search({"filters": filters, "limit": limit})
    if "_error" in data:
        return data, filters, relaxed
    if (data.get("total_count") or 0) >= BROAD_HEALTHY_TOTAL_COUNT:
        return data, filters, relaxed

    extra = 0
    for label, drop in _BROAD_RELAXATION_LADDER:
        before = json.dumps(working, sort_keys=True)
        try:
            drop(working)
        except Exception:
            continue
        after = json.dumps(working, sort_keys=True)
        if before == after:
            continue  # nothing to drop at this step
        relaxed.append(label)
        new_filters = build_filters(working, mode="broad")
        if not new_filters["conditions"]:
            break
        extra += 1
        data = _call_crustdata_search({"filters": new_filters, "limit": limit})
        if "_error" in data:
            return data, new_filters, relaxed
        filters = new_filters
        if (data.get("total_count") or 0) >= BROAD_HEALTHY_TOTAL_COUNT:
            break
        if extra >= BROAD_MAX_EXTRA_CALLS:
            break

    return data, filters, relaxed


# ---------- Match scoring ----------

def _str_in(needle: str, haystack: str) -> bool:
    """Case-insensitive substring check that tolerates None / empty values."""
    if not needle or not haystack:
        return False
    return needle.lower() in haystack.lower()


def _profile_employers(p: dict) -> list[dict]:
    """All employer records on a profile (current + past + the merged `all_`
    list, de-duplicated by position_id where present)."""
    seen = set()
    out = []
    for key in ("current_employers", "past_employers", "all_employers"):
        for e in (p.get(key) or []):
            ident = e.get("position_id") or (
                f"{e.get('name') or e.get('employer_name') or ''}|{e.get('start_date') or ''}"
            )
            if ident in seen:
                continue
            seen.add(ident)
            out.append(e)
    return out


def _score_one_profile(profile: dict, criteria: dict) -> dict:
    """Score a single profile against the user's criteria. Each criterion that
    was actually requested counts as one slot; matched slots are summed.

    Returns {"score": 0..100, "label": "Strong"|"Good"|"Partial", "matched": [...], "missed": [...]}.
    """
    matched_slots = 0
    total_slots = 0
    matched_labels: list[str] = []
    missed_labels: list[str] = []

    employers = _profile_employers(profile)
    profile_titles = [e.get("title") or e.get("employee_title") or "" for e in employers]
    profile_employer_names = [
        (e.get("name") or e.get("employer_name") or "") for e in employers
    ]
    profile_employer_ids = {e.get("company_id") for e in employers if e.get("company_id")}
    profile_region = (profile.get("region") or profile.get("location") or "")
    profile_skills_lower = [s.lower() for s in (profile.get("skills") or []) if isinstance(s, str)]
    profile_summary = (profile.get("summary") or "")
    profile_schools = [
        ed.get("institute_name") or ed.get("school") or ""
        for ed in (profile.get("education_background") or [])
    ]
    profile_yoe = profile.get("years_of_experience_raw")
    try:
        profile_yoe_int = int(profile_yoe) if profile_yoe is not None else None
    except (TypeError, ValueError):
        profile_yoe_int = None

    # Title
    title = (criteria.get("current_title") or "").strip()
    if title:
        total_slots += 1
        if any(_str_in(title, t) for t in profile_titles):
            matched_slots += 1
            matched_labels.append("title")
        else:
            missed_labels.append("title")

    # Location
    location = (criteria.get("location") or "").strip()
    if location:
        total_slots += 1
        # Profile region is a free-text string; fuzzy substring check is
        # fine here — geo_distance already cut the pool to nearby cities.
        if _str_in(location.split(",")[0].strip(), profile_region):
            matched_slots += 1
            matched_labels.append("location")
        else:
            # Treat geo_distance hit as a match if Crustdata returned them
            # (they passed the radius). Soft-score 0.7 of the slot.
            matched_slots += 0.7
            matched_labels.append("location (nearby)")

    # School
    school = (criteria.get("school") or "").strip()
    if school:
        total_slots += 1
        if any(_str_in(school, s) for s in profile_schools):
            matched_slots += 1
            matched_labels.append("school")
        else:
            missed_labels.append("school")

    # Seniority
    seniority = (criteria.get("seniority") or "").strip()
    if seniority:
        total_slots += 1
        target = SENIORITY_MAP.get(seniority.lower(), seniority).lower()
        seniority_values = [
            (e.get("seniority_level") or "").lower() for e in employers if e.get("seniority_level")
        ]
        if any(target in sv or sv in target for sv in seniority_values):
            matched_slots += 1
            matched_labels.append("seniority")
        else:
            missed_labels.append("seniority")

    # YoE band
    yoe_min = criteria.get("years_experience_min")
    yoe_max = criteria.get("years_experience_max")
    if yoe_min not in (None, "", 0, "0") or yoe_max not in (None, ""):
        total_slots += 1
        try:
            ymin = int(yoe_min) if yoe_min not in (None, "") else None
            ymax = int(yoe_max) if yoe_max not in (None, "") else None
        except (TypeError, ValueError):
            ymin = ymax = None
        if profile_yoe_int is not None:
            within = True
            if ymin is not None and profile_yoe_int < ymin:
                within = False
            if ymax is not None and profile_yoe_int > ymax:
                within = False
            if within:
                matched_slots += 1
                matched_labels.append("years")
            else:
                missed_labels.append("years")
        else:
            missed_labels.append("years")

    # Must-have skills — each is its own scoring slot. (Legacy `skills` was
    # already folded into must_have_skills by normalize_criteria upstream.)
    must_have = list(criteria.get("must_have_skills") or [])
    for skill in must_have:
        s = (skill or "").strip()
        if not s:
            continue
        total_slots += 1
        if any(s.lower() in sk for sk in profile_skills_lower):
            matched_slots += 1
            matched_labels.append(f"skill:{s}")
        else:
            missed_labels.append(f"skill:{s}")

    # Nice-to-have skills — bonus only. A match adds a fractional slot to
    # both numerator and denominator (so it lifts the percentage), but a
    # miss adds NOTHING to either side — missing a nice-to-have can never
    # drag a candidate down.
    nice_to_have = list(criteria.get("nice_to_have_skills") or [])
    for skill in nice_to_have:
        s = (skill or "").strip()
        if not s:
            continue
        if any(s.lower() in sk for sk in profile_skills_lower):
            total_slots += NICE_TO_HAVE_WEIGHT
            matched_slots += NICE_TO_HAVE_WEIGHT
            matched_labels.append(f"bonus:{s}")

    # Project keywords — search the summary
    for kw in (criteria.get("project_keywords") or []):
        k = (kw or "").strip()
        if not k:
            continue
        total_slots += 1
        if _str_in(k, profile_summary):
            matched_slots += 1
            matched_labels.append(f"keyword:{k}")
        else:
            missed_labels.append(f"keyword:{k}")

    # "They came from..." tiers — each selected tier is its own slot. A
    # profile hits the slot if any employer name in the tier appears in
    # their employer history (or any school for the Top University tier).
    profile_employer_names_lower = [n.lower() for n in profile_employer_names]
    profile_schools_lower = [(s or "").lower() for s in profile_schools]
    for tier_key in (criteria.get("from_tiers") or []):
        tier = TIERS.get(tier_key)
        if not tier:
            continue
        total_slots += 1
        items_lower = [(item or "").lower() for item in tier.get("items", [])]
        if tier.get("type") == "school":
            hit = any(
                any(item in sch for sch in profile_schools_lower)
                for item in items_lower
            )
        else:
            hit = any(
                any(item in name for name in profile_employer_names_lower)
                for item in items_lower
            )
        if hit:
            matched_slots += 1
            matched_labels.append(f"tier:{tier.get('label', tier_key)}")
        else:
            missed_labels.append(f"tier:{tier.get('label', tier_key)}")

    # Freeform companies typed under the tier pills.
    for raw in (criteria.get("from_companies") or []):
        company = (raw or "").strip()
        if not company:
            continue
        total_slots += 1
        if any(company.lower() in name for name in profile_employer_names_lower):
            matched_slots += 1
            matched_labels.append(f"from:{company}")
        else:
            missed_labels.append(f"from:{company}")

    # Likely-mobile signal — recruiter wants people short in their current
    # role (under 18 months). Crustdata doesn't expose this as a filterable
    # field, so we score against the current employer's start_date when it's
    # available. Profiles missing a start_date skip this slot entirely (no
    # penalty), keeping the score comparable against existing matches.
    if criteria.get("signal_likely_mobile"):
        current_arr = profile.get("current_employers") or []
        start = ""
        if current_arr:
            first = current_arr[0] or {}
            start = first.get("start_date") or ""
        months_in_role = None
        if isinstance(start, str) and len(start) >= 7:
            try:
                sy = int(start[0:4])
                sm = int(start[5:7])
                now = datetime.utcnow()
                months_in_role = (now.year - sy) * 12 + (now.month - sm)
            except (ValueError, TypeError):
                months_in_role = None
        if months_in_role is not None:
            total_slots += 1
            if 0 <= months_in_role < 18:
                matched_slots += 1
                matched_labels.append("likely-mobile")
            else:
                missed_labels.append("likely-mobile")

    # Employers — each requested employer is its own slot
    for emp in (criteria.get("employers") or []):
        company = (emp.get("company") or "").strip()
        if not company:
            continue
        total_slots += 1
        target_id = None  # we don't re-resolve here; rely on name match
        hit = any(_str_in(company, name) for name in profile_employer_names)
        if hit or (target_id and target_id in profile_employer_ids):
            matched_slots += 1
            matched_labels.append(f"employer:{company}")
        else:
            missed_labels.append(f"employer:{company}")

    if total_slots == 0:
        score = 0
        label = "Partial Match"
    else:
        score = int(round((matched_slots / total_slots) * 100))
        if score >= MATCH_STRONG_MIN:
            label = "Strong Match"
        elif score >= MATCH_GOOD_MIN:
            label = "Good Match"
        else:
            label = "Partial Match"

    return {
        "score": score,
        "label": label,
        "matched": matched_labels,
        "missed": missed_labels,
    }


def count_active_criteria(criteria: dict) -> int:
    """Count how many distinct filters are active. Used to decide whether
    Exact mode is going to be too tight. Nice-to-have skills are deliberately
    excluded — they don't restrict, so they shouldn't trip the threshold."""
    c = normalize_criteria(criteria)
    count = 0
    for f in ("current_title", "location", "school", "seniority"):
        if c.get(f):
            count += 1
    if c.get("years_experience_min") or c.get("years_experience_max"):
        count += 1
    for arr_field in (
        "must_have_skills", "project_keywords", "employers",
        "from_tiers", "from_companies",
        "function_areas", "lines_of_defense", "industries",
        "exclude_titles", "exclude_companies", "exclude_seniority",
        "exclude_skills",
        "career_arc",
    ):
        v = c.get(arr_field) or []
        if isinstance(v, list):
            count += len(v)
    return count


def diagnose_zero_results(criteria: dict, mode: str) -> list[str]:
    """Return human-readable suggestions for relaxing the search, ordered by
    likely impact. The top 2-3 are surfaced to the user. Never empty — the
    final fallback is always to try Broad mode."""
    c = normalize_criteria(criteria)
    suggestions: list[str] = []

    if mode == "exact":
        suggestions.append("switching from Exact to Similar to widen matching")

    # Nice-to-haves never restrict the search, so only must-haves appear here.
    skills = list(c.get("must_have_skills") or [])
    if skills:
        most_specific = max(skills, key=len)
        suggestions.append(f"removing [Must-have: {most_specific}]")

    project_keywords = c.get("project_keywords") or []
    if project_keywords:
        most_specific = max(project_keywords, key=len)
        suggestions.append(f"removing [Project keyword: {most_specific}]")

    for emp in (c.get("employers") or []):
        if emp.get("start_year") or emp.get("end_year"):
            comp = emp.get("company") or "that career stop"
            suggestions.append(f"loosening the dates on [Worked at: {comp}]")
            break

    if c.get("school"):
        suggestions.append(f"removing [School: {c['school']}]")

    yoe_min = c.get("years_experience_min")
    yoe_max = c.get("years_experience_max")
    try:
        if yoe_min and yoe_max and int(yoe_max) - int(yoe_min) < 3:
            suggestions.append("widening the years-of-experience band")
    except (TypeError, ValueError):
        pass

    tiers = c.get("from_tiers") or []
    if len(tiers) > 1:
        suggestions.append(f"removing one of the {len(tiers)} ‘They came from’ tiers")

    if mode != "broad":
        suggestions.append("switching to Broad mode — it auto-relaxes filters")

    if not suggestions:
        suggestions.append("loosening the title or location")
    return suggestions[:3]


def score_and_sort_profiles(profiles: list, criteria: dict) -> list:
    """Attach a `_match` block to every profile and return the list sorted by
    score descending (stable so the original Crustdata order is the
    tiebreaker). Normalises the criteria dict once so every per-profile call
    sees the canonical shape."""
    c = normalize_criteria(criteria)
    scored = []
    for idx, p in enumerate(profiles or []):
        match = _score_one_profile(p, c)
        out = dict(p)
        out["_match"] = match
        out["_orig_rank"] = idx
        scored.append(out)
    scored.sort(key=lambda x: (-x["_match"]["score"], x["_orig_rank"]))
    return scored


# ---------- /api/search ----------

AUTO_SWITCH_FILTER_THRESHOLD = 5  # at or above this, Exact becomes Similar


@app.route("/api/search", methods=["POST"])
def search():
    criteria = request.get_json(force=True, silent=True) or {}
    requested_mode = _normalize_mode(criteria.get("mode"))

    # Auto-switch from Exact to Similar when the recruiter has piled on
    # enough filters that strict matching is almost guaranteed to zero out.
    auto_mode_switch_from: str | None = None
    if requested_mode == "exact" and count_active_criteria(criteria) >= AUTO_SWITCH_FILTER_THRESHOLD:
        mode = "similar"
        auto_mode_switch_from = "exact"
    else:
        mode = requested_mode

    filters = build_filters(criteria, mode)

    if not filters["conditions"]:
        return jsonify({"error": "Add at least one criterion."}), 400

    limit = int(criteria.get("limit", 100))

    # Cache key includes mode so swapping modes doesn't serve stale results.
    cache_payload = {"filters": filters, "limit": limit, "mode": mode}
    print(
        f"croot.search mode={mode} filters={json.dumps(filters)}",
        file=sys.stderr, flush=True,
    )

    key = cache_key_for(cache_payload)
    cached = get_cached(key)
    if cached is not None:
        return jsonify({
            "from_cache": True,
            "criteria": criteria,
            "filters": filters,
            "mode": mode,
            "relaxed": (cached or {}).get("_relaxed", []),
            "results": cached,
        })

    if not CRUSTDATA_API_KEY:
        return jsonify({
            "error": "CRUSTDATA_API_KEY is not set. Add it to .env and restart.",
            "filters": filters,
        }), 500

    relaxed: list[str] = []
    if mode == "broad":
        data, filters, relaxed = _search_broad(criteria, limit)
    else:
        data = _call_crustdata_search({"filters": filters, "limit": limit})

    if isinstance(data, dict) and "_error" in data:
        return jsonify(data["_error"]), data["_status"]

    profiles = (data or {}).get("profiles") or []
    scored = score_and_sort_profiles(profiles, criteria)
    data["profiles"] = scored
    data["_mode"] = mode
    data["_relaxed"] = relaxed

    # If we got zero results, generate suggestions so the UI never shows a
    # dead-end "no candidates" state.
    suggestions: list[str] = []
    if (data.get("total_count") or 0) == 0:
        suggestions = diagnose_zero_results(criteria, mode)
    data["_suggestions"] = suggestions

    print(
        f"croot.search profile_count={len(scored)} total={data.get('total_count')} relaxed={relaxed} mode={mode}",
        file=sys.stderr, flush=True,
    )
    put_cached(key, cache_payload, data, summarize(criteria))

    return jsonify({
        "from_cache": False,
        "criteria": criteria,
        "filters": filters,
        "mode": mode,
        "auto_mode_switch_from": auto_mode_switch_from,
        "relaxed": relaxed,
        "suggestions": suggestions,
        "results": data,
    })


@app.route("/api/parse", methods=["POST"])
def parse():
    """Turn a free-text recruiter query into the structured criteria the form
    uses. Pure local parsing — no Crustdata call, no credit spend."""
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text", "")
    criteria = parse_query(text)
    return jsonify({"criteria": criteria})


@app.route("/api/title-variants")
def title_variants():
    """Surface which other titles the search will match — pure local lookup
    against TITLE_VARIANTS so the recruiter understands what "Similar" / "Broad"
    actually does to their typed title. No Crustdata call."""
    raw = (request.args.get("title") or "").strip()
    mode = _normalize_mode(request.args.get("mode"))
    if not raw:
        return jsonify({"canonical": "", "variants": [], "mode": mode})

    lower = raw.lower()
    canonical = None
    # TITLE_VARIANTS is longest-first, so the first hit wins — matches
    # parse_query's selection rule.
    for variant, canon in TITLE_VARIANTS:
        v = variant.lower()
        if v == lower or v in lower or lower in v:
            canonical = canon
            break
    if not canonical:
        # Unknown title: echo it back as canonical with no variants. The
        # frontend hides the disclosure line in that case.
        return jsonify({"canonical": raw, "variants": [], "mode": mode})

    if mode == "exact":
        variants: list = []
    elif mode == "similar":
        seen = {canonical.lower()}
        variants = []
        for v, c in TITLE_VARIANTS:
            if c != canonical:
                continue
            if v.lower() in seen:
                continue
            seen.add(v.lower())
            variants.append(v)
    else:  # broad — share the head noun
        head = canonical.split()[-1].lower() if canonical else ""
        seen = {canonical.lower()}
        variants = []
        for _, c in TITLE_VARIANTS:
            if not c or c.lower() in seen:
                continue
            if c.split()[-1].lower() == head:
                seen.add(c.lower())
                variants.append(c)

    return jsonify({"canonical": canonical, "variants": variants, "mode": mode})


# ---------- Document upload → criteria extraction ----------

MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # ~4 MB — safely under Vercel's 4.5 MB cap


def _extract_pdf_text(data: bytes) -> str:
    """Pull text from a PDF byte stream. Returns "" on any error so the route
    can degrade gracefully instead of 500ing on malformed uploads."""
    try:
        from pypdf import PdfReader  # imported lazily so unrelated routes pay nothing
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)
    except Exception as exc:
        print(f"croot.extract pdf_error={exc}", file=sys.stderr, flush=True)
        return ""


def _extract_docx_text(data: bytes) -> str:
    """Pull text from a .docx byte stream."""
    try:
        from docx import Document  # python-docx, imported lazily
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text)
    except Exception as exc:
        print(f"croot.extract docx_error={exc}", file=sys.stderr, flush=True)
        return ""


def _read_uploaded_text(file_storage) -> tuple[str, str]:
    """Return (extracted_text, source_label) for a Werkzeug FileStorage.

    Dispatches by filename extension first (cheap, deterministic), falls back
    to mimetype only if the extension is missing. Anything else is treated as
    plain text decoded UTF-8 with errors ignored."""
    name = (file_storage.filename or "").lower()
    data = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("File too large (max 4 MB).")

    if name.endswith(".pdf"):
        return _extract_pdf_text(data), "pdf"
    if name.endswith(".docx"):
        return _extract_docx_text(data), "docx"
    if name.endswith(".doc"):
        # Legacy .doc isn't supported by python-docx. Tell the user.
        raise ValueError("Legacy .doc isn't supported — save as .docx or paste the text.")
    # .txt or anything else: decode as text.
    try:
        return data.decode("utf-8", errors="ignore"), "text"
    except Exception:
        return data.decode("latin-1", errors="ignore"), "text"


@app.route("/api/extract", methods=["POST"])
def extract():
    """Extract structured criteria from a longer document (JD, client notes,
    meeting transcript). Accepts either a JSON `{text: ...}` payload (paste
    flow) or a multipart upload with a `file` field (.txt/.pdf/.docx).

    The output shape matches /api/parse so the frontend can reuse the
    auto-fill code path."""
    text = ""
    source = "text"

    if request.files and "file" in request.files:
        try:
            text, source = _read_uploaded_text(request.files["file"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        body = request.get_json(force=True, silent=True) or {}
        text = (body.get("text") or "").strip()

    text = (text or "").strip()
    if not text:
        return jsonify({"error": "No text or file content found."}), 400

    # Cap parser input so the regex sweep stays predictable. Job descriptions
    # rarely exceed a few thousand chars; transcripts can balloon, and the
    # parser doesn't care about the tail.
    parsed_text = text[:20000]
    criteria, sources = parse_jd(parsed_text)

    return jsonify({
        "criteria": criteria,
        "sources": sources,
        "source": source,
        "char_count": len(text),
    })


@app.route("/api/preview", methods=["POST"])
def preview_count():
    """Cheap candidate-count for the current filter set, used to show
    "~N candidates match" before the recruiter commits to a full search.

    Strategy: hit /screener/persondb/search with limit=1 and read
    `total_count` from the response. Crustdata's `preview=true` mode used
    to return 200 + total_count=0 even when the full search had matches,
    so we don't use it. Limit=1 still returns the unconstrained total_count
    and costs only the minimum per-search credit fee.

    If the requested mode is `exact` and returns zero, we auto-retry the
    same filter set in `similar` mode and surface that count as
    `fallback_count` so the UI can offer a one-click loosening.
    """
    criteria = request.get_json(force=True, silent=True) or {}
    requested_mode = _normalize_mode(criteria.get("mode"))
    filters = build_filters(criteria, requested_mode)
    if not filters["conditions"]:
        return jsonify({"total_count": 0, "mode": "empty"})

    if not CRUSTDATA_API_KEY:
        return jsonify({"error": "CRUSTDATA_API_KEY is not set."}), 500

    def count_for(payload_filters) -> tuple[int | None, dict | None]:
        data = _call_crustdata_search({"filters": payload_filters, "limit": 1})
        if "_error" in data:
            return None, data
        total = (data or {}).get("total_count")
        return int(total or 0), None

    total, err = count_for(filters)
    if err is not None:
        return jsonify(err["_error"]), err["_status"]

    body = {"total_count": total, "mode": requested_mode}

    # Auto-relax if Exact mode returned zero — surface the Similar count so
    # the frontend can show "0 strict / ~N similar" instead of dead-ending.
    if total == 0 and requested_mode == "exact":
        similar_filters = build_filters(criteria, "similar")
        if similar_filters["conditions"]:
            fallback, _ = count_for(similar_filters)
            if fallback and fallback > 0:
                body["fallback_count"] = fallback
                body["fallback_mode"] = "similar"

    return jsonify(body)


@app.route("/api/history")
def history():
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT cache_key, summary, created_at FROM search_history ORDER BY id DESC LIMIT 25"
        ).fetchall()
    return jsonify([
        {
            "cache_key": row["cache_key"],
            "summary": row["summary"],
            "created_at": datetime.utcfromtimestamp(row["created_at"]).isoformat() + "Z",
        }
        for row in rows
    ])


# ---------- Saved searches ----------

@app.route("/api/saved-searches", methods=["GET", "POST"])
def saved_searches():
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        name = (body.get("name") or "").strip()
        criteria = body.get("criteria")
        mode = _normalize_mode(body.get("mode"))
        if not name or len(name) > 200:
            return jsonify({"error": "Name is required (max 200 chars)."}), 400
        if not isinstance(criteria, dict):
            return jsonify({"error": "criteria must be an object."}), 400
        now = int(time.time())
        with closing(db()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO saved_searches (name, criteria, mode, created_at) VALUES (?, ?, ?, ?)",
                (name, json.dumps(criteria), mode, now),
            )
            inserted_id = cur.lastrowid
        return jsonify({"id": inserted_id, "name": name})

    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, name, criteria, mode, created_at, last_run_at "
            "FROM saved_searches ORDER BY created_at DESC"
        ).fetchall()
    out = []
    for row in rows:
        try:
            criteria_obj = json.loads(row["criteria"])
        except (TypeError, ValueError):
            criteria_obj = {}
        out.append({
            "id": row["id"],
            "name": row["name"],
            "criteria": criteria_obj,
            "mode": row["mode"] or DEFAULT_MODE,
            "created_at": datetime.utcfromtimestamp(row["created_at"]).isoformat() + "Z",
            "last_run_at": (
                datetime.utcfromtimestamp(row["last_run_at"]).isoformat() + "Z"
                if row["last_run_at"] else None
            ),
        })
    return jsonify(out)


@app.route("/api/saved-searches/<int:search_id>", methods=["DELETE"])
def delete_saved_search(search_id: int):
    with closing(db()) as conn, conn:
        cur = conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        if cur.rowcount == 0:
            return jsonify({"error": "Not found."}), 404
    return jsonify({"deleted": True})


# ---------- Waitlist ----------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _forward_to_sheet(name: str, email: str, user_agent: str, ip: str) -> None:
    """Best-effort POST to the Google Apps Script Web App. Failures are logged
    to stderr and swallowed — the SQLite write is the authoritative record, so
    a sheet outage never blocks a signup."""
    if not GSHEETS_WAITLIST_URL:
        return
    try:
        r = requests.post(
            GSHEETS_WAITLIST_URL,
            json={"name": name, "email": email, "user_agent": user_agent, "ip": ip},
            timeout=8,
            # Apps Script's /exec URL 302-redirects to the actual script
            # runtime on script.googleusercontent.com — follow it.
            allow_redirects=True,
        )
        if r.status_code >= 400:
            print(
                f"croot.waitlist sheet_forward http={r.status_code} body={r.text[:200]}",
                file=sys.stderr,
                flush=True,
            )
    except requests.RequestException as exc:
        print(f"croot.waitlist sheet_forward error={exc}", file=sys.stderr, flush=True)


@app.route("/api/waitlist", methods=["POST"])
def waitlist_signup():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    if not name or len(name) > 200:
        return jsonify({"error": "Name is required."}), 400
    if not email or not _EMAIL_RE.match(email) or len(email) > 320:
        return jsonify({"error": "A valid work email is required."}), 400

    user_agent = (request.headers.get("User-Agent") or "")[:500]
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    now = int(time.time())
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO waitlist (name, email, user_agent, created_at) VALUES (?, ?, ?, ?)",
            (name, email, user_agent, now),
        )
    _forward_to_sheet(name, email, user_agent, ip)
    return jsonify({"ok": True})


# ---------- Profile enrichment ----------

# Fields we ask Crustdata for when enriching a profile for the slide-out panel.
# Kept compact so the response stays digestible; certifications and honors are
# behind an access flag on some plans (per docs) — we request them anyway and
# render whatever comes back, gracefully degrading when fields are absent.
_ENRICH_FIELDS = ",".join([
    "linkedin_profile_url",
    "linkedin_flagship_url",
    "name",
    "location",
    "headline",
    "summary",
    "num_of_connections",
    "skills",
    "profile_picture_url",
    "languages",
    "current_employers",
    "past_employers",
    "all_employers",
    "education_background",
    "certifications",
    "honors",
    "business_email",
])


def _normalize_linkedin_url(url: str) -> str:
    """Cache key normalization — strip query, fragment, trailing slash, lowercase."""
    if not url:
        return ""
    cleaned = url.strip().split("?")[0].split("#")[0].rstrip("/").lower()
    return cleaned


@app.route("/api/profile")
def profile():
    linkedin_url = (request.args.get("linkedin_url") or "").strip()
    if not linkedin_url:
        return jsonify({"error": "linkedin_url is required"}), 400

    key = _normalize_linkedin_url(linkedin_url)
    now = int(time.time())

    with closing(db()) as conn:
        row = conn.execute(
            "SELECT payload, created_at FROM profile_cache WHERE linkedin_key = ?",
            (key,),
        ).fetchone()
    if row and now - row["created_at"] < PROFILE_TTL_SECONDS:
        return jsonify({"profile": json.loads(row["payload"]), "from_cache": True})

    if not CRUSTDATA_API_KEY:
        return jsonify({"error": "CRUSTDATA_API_KEY is not set."}), 500

    try:
        r = requests.get(
            CRUSTDATA_ENRICH_URL,
            headers={"Authorization": f"Token {CRUSTDATA_API_KEY}"},
            params={"linkedin_profile_url": linkedin_url, "fields": _ENRICH_FIELDS},
            timeout=30,
        )
    except requests.RequestException as exc:
        return jsonify({"error": f"Upstream request failed: {exc}"}), 502

    if r.status_code >= 400:
        return jsonify({
            "error": f"Crustdata returned {r.status_code}",
            "details": r.text[:500],
        }), r.status_code

    data = r.json()
    # The enrich endpoint returns an array of profiles (since it accepts
    # comma-separated URLs). For our single-URL request we want the first.
    if isinstance(data, list):
        profile_data = data[0] if data else None
    elif isinstance(data, dict):
        profile_data = data
    else:
        profile_data = None

    if not profile_data:
        return jsonify({"error": "Profile not found."}), 404

    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO profile_cache (linkedin_key, payload, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(profile_data), now),
        )

    return jsonify({"profile": profile_data, "from_cache": False})


# Idempotent — runs once at import time so Vercel cold starts and `python app.py`
# both get a usable DB. Uses CREATE TABLE IF NOT EXISTS, so re-runs are no-ops.
init_db()


if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
