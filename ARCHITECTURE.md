# Croot v2 — architecture

A web platform for recruiters and recruitment agencies to source candidates for
open roles. The recruiter describes who they're looking for in a **chat-like
experience** — typing requirements, pasting a job description, or pasting a JD
URL. Claude turns that into a structured **criteria** object, asks for the few
missing high-value fields (skippable), and the recruiter **reviews/edits before
submitting**. The platform then queries the **Crustdata API**, ranks the pool,
and returns the most viable candidates in-app.

> v1 (the rule-based Flask monolith in `app.py`) is preserved on the `legacy`
> branch. v2 is a clean rebuild on `main`.

## Design decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| Skill integration | **Skill as spec** — port `source-candidates` logic into backend code | Predictable cost/latency, testable, web-native. Claude is used only where it shines (intake). |
| Criteria gathering | **Claude-powered conversational agent** | Robust extraction from messy JDs/free text; can ask to fill gaps and accept "just search". |
| Output (this build) | **In-app ranked results**, behind a **pluggable export layer** | Ship the web product first; Google Sheets / Gem / ATS export drop in later without rework. |
| Stack | **Python / Flask on Vercel**, Claude API, Crustdata REST | Continues v1's stack; Fluid Compute supports streaming + 300s timeout. |

## What we reuse from the `source-candidates` skill

The skill is a Claude Code *agent* skill (MCP + `AskUserQuestion` + Google Sheets
/ Gem CLI). We do **not** run it; we treat its body as the authoritative
**playbook** and port the parts that aren't tied to Claude Code:

- **Criteria schema** — title (+ variants), seniority, **YoE with both a floor
  and a ceiling**, location (region, not HQ), must-have vs nice-to-have skills,
  excludes, `domain_signals`, `education_signals`, `career_path_signals`,
  `exclude_employers`, `title_excludes`, `tenure_floor_months` (default **6**),
  `hiring_company`.
- **Filter grammar** — the exact Crustdata operator set
  (`[.]` substring, `(.)` fuzzy, `=` `!=`, `in` `not_in`, `> < >= <=`,
  `geo_distance`; **no substring-negation operator exists**), the anchor `$or`
  strategy (`companies` / `industries` / `both` / `none`), and the hard rule:
  **always autocomplete `company_industry` and `institute_name` enum values
  before using them** (guessing silently matches nothing).
- **Search economics** — DB-tier search (`people_search_db`, ~3 cr/100) called
  **full-fat once** (`compact:false, truncate:false`), compressed locally, then
  reused for all rendering/ranking; contact enrichment is the expensive
  (~4 cr/profile) **opt-in** step over only the candidates being exported.
- **0–100 ranking rubric** — title 25 / must-have skills 25 / domain 20 /
  YoE-seniority 15 / location 10 / bonus 5, with hard-miss caps (contradicts
  excludes → ≤40; disqualifier → ≤60; data gap → ≤70). Deterministic, no LLM.
- **Relaxation** — one pass if pool < 8: drop skills → broaden title → widen geo
  50→100mi → drop education → drop anchor `$or` (last resort). Surface what was
  relaxed.
- **Same-employer dedup** — drop any candidate whose current employer is the
  hiring company (handles stale DB rows).

Concrete Crustdata field names / operator strings come from the skill body and
the v1 `SKILL.md` (both preserved — skill body via the Crustdata MCP, `SKILL.md`
on the `legacy` branch). The exact REST path for the autocomplete endpoint is
the one item to verify against Crustdata docs during implementation.

## Module layout

A package, not a monolith — the thing we're deliberately moving away from.

```
api/index.py              Vercel WSGI shim (kept): `from app import app`
app/
  __init__.py             Flask app factory; exposes `app`
  config.py               Env + named constants (credit caps, TTLs, rubric weights)
  db.py                   SQLite connection + schema (best-effort on /tmp)
  llm.py                  Claude client wrapper (prompt caching, structured tool-use)
  routes/
    chat.py               POST /api/chat        — conversational intake (SSE stream)
    search.py             POST /api/search, POST /api/preview
    profile.py            GET  /api/profile     — opt-in contact enrichment
    history.py            GET/POST /api/saved-searches, GET /api/history
    export.py             POST /api/export      — destination layer (CSV first)
  core/
    criteria.py           Criteria dataclass — the single contract all paths produce
    intake.py             Claude-driven extraction: text/JD → Criteria + follow-up
    jd_fetch.py           URL fetch + HTML strip (port v1's SSRF-guarded fetcher)
    filters.py            Criteria → Crustdata filter payload (the operator grammar)
    crustdata.py          Crustdata REST client: search_db, identify, enrich, autocomplete
    pool.py               Full-fat response → compressed candidate pool projection
    ranker.py             0–100 rubric + relaxation ladder
    export/
      base.py             Destination interface (write(candidates, meta) → result)
      csv_dest.py         CSV destination (first concrete impl)
templates/index.html      Chat SPA shell
static/app.js, styles.css Chat UI, criteria review card, results cards
tests/                    Unit tests: filters, ranker, criteria, jd_fetch, intake contract
```

`api/index.py` does `from app import app`; `app/__init__.py` defines
`app = create_app()`, so the existing Vercel shim keeps working unchanged.

## Data flow

```
            ┌─ free text ─┐
recruiter ──┤  paste JD   ├─► /api/chat ──► intake.py ──► Criteria (partial)
            └─ JD URL ────┘    (Claude)      │                 │
                                             │ asks for missing │ review/edit
                                             │ high-value fields │ (skippable)
                                             ▼                 ▼
                                        confirmed Criteria ──► /api/search
                                                                 │
   filters.py ──► crustdata.identify (cached) ──► people_search_db (full-fat once)
       │                                                 │
       └────────────────────────────────────────► pool.compress ──► ranker.rank
                                                                         │
                                            relaxation pass if pool < 8  │
                                                                         ▼
                                                        ranked candidates → UI
                                                                         │
                                          opt-in: /api/profile (enrich), /api/export
```

### 1. Conversational intake (`/api/chat`, streamed)

The frontend holds the message history and resends it each turn, so the backend
stays stateless (Vercel-friendly). Each turn:

1. If the message contains a URL → `jd_fetch` retrieves and strips it to text.
2. `intake.py` calls Claude with the conversation + any JD text. Claude returns
   **two things via tool-use**: a natural-language reply, and a structured
   `set_criteria` tool call whose schema *is* the `Criteria` contract — so
   extraction is reliable, not regex-scraped from prose.
3. Claude proposes the criteria card and asks for the few missing high-value
   fields (e.g. YoE range, must-have skills). The user can answer or say
   "just search" — partial criteria are allowed (the skill is "dialed-down":
   it never blocks on missing input).
4. When the user confirms, the criteria card is the input to `/api/search`.

Response is **streamed via SSE** (Fluid Compute supports streaming) so the chat
feels live. Claude system prompt is **prompt-cached** to keep per-turn cost low.

### 2. Build the filter (`filters.py`)

`build_filters(criteria)` is the single canonical builder (the one lesson we
keep from v1). It emits the skill's `{op:"and", conditions:[...]}` envelope:
identity (title variants, geo, YoE band, tenure floor, same-employer `not_in`)
+ skills (`skills in [...]`, dropped if nice-to-have-only) + the anchor `$or`
(company_ids and/or industries) + education + excludes. Enum values
(industries, schools) are resolved through `crustdata.autocomplete` first.

### 3. Search + rank (`/api/search`)

- Resolve company anchors via `identify` (30-day cached, ~free).
- Call `people_search_db` **once, full-fat**, `limit:100`, with a `sorts` axis.
- `pool.compress` projects the large payload down to the fields we render/score
  (we never load the raw full-fat payload into memory wholesale).
- `ranker.rank` scores every candidate 0–100, drops same-employer matches,
  sorts desc. If the pool is < 8, run **one** relaxation pass and re-search.
- Cache the result keyed by a hash of the filter payload.
- `/api/preview` runs a `limit:1` search to show `total_count` **before** the
  recruiter spends credits on a full search.

### 4. Results, enrichment, export

- UI renders ranked cards: score + rationale (deterministic, from matched/missed
  slots), title, employer, YoE, region, top skills, prior employers, LinkedIn.
- **Contact enrichment is opt-in** (`/api/profile` → `people_enrich`), per
  candidate or top-N, cached 30 days — this is the expensive call, gated behind
  an explicit click.
- **Export** goes through `core/export/base.py`'s `Destination` interface.
  `csv_dest.py` is the first implementation; a future `sheets_dest.py` /
  `gem_dest.py` / ATS destination drops in without touching the rest.

## Persistence (SQLite, best-effort)

`/tmp/croot.db` on Vercel (wiped on cold start, survives warm), `./croot.db`
locally. Tables:

- `search_cache` — keyed by hash of the filter payload.
- `company_id_cache` — `identify` results, 30-day TTL.
- `profile_cache` — `enrich` results, 30-day TTL.
- `saved_searches` — named criteria a recruiter can re-run.
- `search_history` — recent search summaries.

Conversation state is **not** persisted server-side (held client-side and
resent), keeping `/api/chat` stateless.

## Cost controls

**Claude (intake only):** prompt-cached system prompt; one short call per chat
turn; ranking and filtering are code, not LLM.

**Crustdata credits:** `identify` cached 30d; **full-fat search once** then
reuse the compressed pool for everything; `/api/preview` shows count before the
real spend; contact `enrich` is opt-in and cached 30d.

## Configuration

Env: `CRUSTDATA_API_KEY` (required), `ANTHROPIC_API_KEY` (required for intake),
`CACHE_TTL_HOURS` (default 72). Named constants in `app/config.py`: rubric
weights, `BROAD_HEALTHY_TOTAL_COUNT`, geo radii, `tenure_floor_months` default,
credit caps, `MAX_UPLOAD_BYTES`.

## Out of scope for this build (architected for, not built)

Google Sheets / Gem / ATS export (export layer is pluggable), user accounts &
auth, the Phase 5 voting / Phase 4 calibration loops, and team collaboration.
None require schema changes that would force a later rewrite.
```

