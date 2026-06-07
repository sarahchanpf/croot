"""Environment + named constants. No magic numbers scattered across modules."""

import os

from dotenv import load_dotenv

# Load .env for local dev. On Vercel, env vars are injected directly (and there
# is no .env), so this is a harmless no-op there.
load_dotenv()

# --- Credentials ---
CRUSTDATA_API_KEY = os.environ.get("CRUSTDATA_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # added later; intake guards on it
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "reCroot1")
SESSION_SECRET = os.environ.get("SESSION_SECRET", ACCESS_PASSWORD)

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
# Sonnet is the default: intake is a short extraction task, so it's far cheaper
# than Opus with no meaningful quality loss. Override with CLAUDE_MODEL.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
# The dedicated cluster builder (cluster_finder) uses a stronger model — picking
# the right peer companies benefits from Opus's broader, more precise knowledge.
CLUSTER_MODEL = os.environ.get("CLUSTER_MODEL", "claude-opus-4-8")
# Ranking is the skill's Phase 6 — a single Opus reasoning pass scoring each
# candidate 0-100. Opus matches the skill; override with RANK_MODEL. When no
# ANTHROPIC_API_KEY is set the ranker falls back to a deterministic rubric.
RANK_MODEL = os.environ.get("RANK_MODEL", "claude-opus-4-8")
RANK_MAX_TOKENS = 4096                    # enough for a ~25-candidate batch

# --- Persistence ---
if os.environ.get("VERCEL"):
    DB_PATH = "/tmp/croot.db"            # read-only fs except /tmp; best-effort cache
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "croot.db")

CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_HOURS", "72")) * 3600
COMPANY_ID_TTL_SECONDS = 30 * 24 * 3600
PROFILE_TTL_SECONDS = 30 * 24 * 3600
FREE_SEARCH_LIMIT = 5

# Durable signup capture. Each access/waitlist signup is POSTed here so the
# names/emails survive Vercel's ephemeral /tmp (which doesn't persist). Point it
# at a Google Apps Script web app that appends a row to a Sheet. Empty = disabled.
SIGNUP_WEBHOOK_URL = os.environ.get("SIGNUP_WEBHOOK_URL", "")

# --- Search economics ---
# One full-fat, sorted DB search; one relaxation pass only if the pool is thin
# (skill Phase 2 Step 4). No multi-pass merging — that diluted the pool.
SEARCH_LIMIT = 100                       # full-fat once, then compress + reuse
SEARCH_ALGO_VERSION = "skill-parity-v1"
GEO_RADIUS_DEFAULT_MILES = 50
GEO_RADIUS_BROAD_MILES = 100
BROAD_HEALTHY_TOTAL_COUNT = 8            # below this, run one relaxation pass
MAX_RELAXATION_PASSES = 1

# --- Ranking rubric (0-100; skill Phase 6 weight bands) ---
# Used verbatim by the LLM ranker's system prompt and by the deterministic
# fallback rubric. No cluster-pedigree slot — the skill's Phase 6 has none;
# cluster relevance is enforced by the anchor filter and rewarded via `domain`.
RUBRIC_WEIGHTS = {
    "title": 25,
    "skills": 25,
    "domain": 20,
    "yoe_seniority": 15,
    "location": 10,
    "bonus": 5,
}
CAP_DATA_GAP = 70                        # incomplete profile caps the fit score

# --- Uploads ---
MAX_UPLOAD_BYTES = 4 * 1024 * 1024       # under Vercel's 4.5 MB cap
