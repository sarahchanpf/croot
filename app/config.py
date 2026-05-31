"""Environment + named constants. No magic numbers scattered across modules."""

import os

from dotenv import load_dotenv

# Load .env for local dev. On Vercel, env vars are injected directly (and there
# is no .env), so this is a harmless no-op there.
load_dotenv()

# --- Credentials ---
CRUSTDATA_API_KEY = os.environ.get("CRUSTDATA_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # added later; intake guards on it

# --- Crustdata REST endpoints ---
CRUSTDATA_SEARCH_URL = "https://api.crustdata.com/screener/persondb/search"
CRUSTDATA_IDENTIFY_URL = "https://api.crustdata.com/screener/identify/"
CRUSTDATA_ENRICH_URL = "https://api.crustdata.com/screener/person/enrich"
# Autocomplete lives on Crustdata's NEW API (not the legacy /screener search):
# POST /person/search/autocomplete, Bearer auth + x-api-version header. Returns
# {"suggestions": [{"value": ...}]}. Values are compatible with the legacy
# search columns (verified: school + industry feed /screener/persondb/search).
CRUSTDATA_AUTOCOMPLETE_URL = "https://api.crustdata.com/person/search/autocomplete"
CRUSTDATA_API_VERSION = "2025-11-01"

# --- Claude (intake only) ---
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

# --- Persistence ---
if os.environ.get("VERCEL"):
    DB_PATH = "/tmp/croot.db"            # read-only fs except /tmp; best-effort cache
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "croot.db")

CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_HOURS", "72")) * 3600
COMPANY_ID_TTL_SECONDS = 30 * 24 * 3600
PROFILE_TTL_SECONDS = 30 * 24 * 3600

# --- Search economics ---
SEARCH_LIMIT = 100                       # full-fat once, then compress + reuse
GEO_RADIUS_DEFAULT_MILES = 50
GEO_RADIUS_BROAD_MILES = 100
BROAD_HEALTHY_TOTAL_COUNT = 8            # below this, run one relaxation pass
MAX_RELAXATION_PASSES = 1

# --- Ranking rubric (0-100; ported from skill Phase 6) ---
RUBRIC_WEIGHTS = {
    "title": 25,
    "skills": 25,
    "domain": 20,
    "yoe_seniority": 15,
    "location": 10,
    "bonus": 5,
}
CAP_CONTRADICTS_EXCLUDE = 40
CAP_DISQUALIFIER = 60
CAP_DATA_GAP = 70

# --- Uploads ---
MAX_UPLOAD_BYTES = 4 * 1024 * 1024       # under Vercel's 4.5 MB cap
