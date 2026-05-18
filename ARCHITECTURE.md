# Croot — architecture (post-refactor)

A small recruiting tool that turns free-text recruiter intent into Crustdata
people-search calls, then scores and ranks the results. Flask + SQLite on the
backend, plain HTML/CSS/JS on the frontend, deployed on Vercel.

## Files

| File | Purpose |
|---|---|
| `app.py` | The whole backend — Flask app, Crustdata client, JD parser, filter builder, scorer, all routes. ~2,400 lines, organised top-to-bottom: config → DB → operator helpers + FIELD constants → natural-language parser → JD cleaner + parser → tier definitions → filter builder → HTTP client → broad-mode ladder → scorer → routes. |
| `api/index.py` | Vercel WSGI shim — just re-exports `app` from `app.py`. |
| `vercel.json` | Rewrites everything to `/api/index` so Flask sees every URL. |
| `templates/index.html` | Single-page UI: Brief paste, Describe-in-words, Build form with employer rows + chip inputs (skills, project keywords, came-from companies) + advanced details, mode toggle, results list, profile slide-out, waitlist modal. |
| `static/styles.css` | Vanilla CSS with the palette tokens at the top (Vanilla bg, Columbia Blue cards, Cal Poly Green ink, Terracotta accent). |
| `static/app.js` | All UI behaviour. One IIFE; per-section comment banners (chip inputs, criteria collection, submit, results render, semantic auto-fill, context upload, preview, waitlist gate, profile panel). |
| `tests/test_jd_extraction.py` | 23 unit tests pinning the universal JD-extraction rules, the must-have/nice-to-have split, and the zero-results diagnostics. |
| `SKILL.md` | Reference doc for the Crustdata API conventions (operators, field names, cluster anchoring). Not loaded at runtime — kept as a design reference. |

## Canonical data flow

```
recruiter input  →  criteria dict  →  build_filters()  →  Crustdata
   (3 entry                              (single                 ↓
    points)                              canonical            response
                                         builder)                ↓
                                                          score_and_sort
                                                                 ↓
                                                          response JSON
```

### Three input → criteria entry points

1. **Brief paste / file upload** (`/api/extract`) — runs `parse_jd(text)`. JD
   parser pipeline: clean → split into sections → extract per-field from the
   correct section. Returns `(criteria, sources)` where sources says which JD
   section each field came from.
2. **Describe in words** (`/api/parse`) — runs `parse_query(text)`. Lighter
   regex-based extraction over a single sentence; returns just `criteria`.
3. **Build form** — frontend collects the structured fields directly and
   sends them to `/api/search` or `/api/preview`. No parsing step.

All three produce the same criteria-dict contract.

### Single canonical filter builder

`build_filters(criteria, mode)` is the only place that produces a Crustdata
filter payload. It runs `normalize_criteria()` once (folds legacy `skills`
into `must_have_skills`), then composes six per-stanza helpers:

| Helper | Produces |
|---|---|
| `_filter_identity` | Title, location, school, seniority, YoE band |
| `_filter_skills_and_keywords` | Must-have skills (strict AND), project keywords on summary |
| `_filter_from` | "They came from…" tier pills + freeform companies (OR group) |
| `_filter_advanced` | Recently-changed, overly-senior cap, function, industry, lines of defense, career arc |
| `_filter_exclusions` | Exclude titles / companies / seniorities |
| `_filter_employers` | "They worked at X between Y and Z" — date overlap on past employers |

Operator helpers (`fuzzy`, `exact`, `gte`, `lte`, `token`, `geo`, `op_in`,
`op_not_in`, `op_neg`, `op_neq`, `op_or`, `op_and`) take a `FIELD.X` constant
plus a value; every Crustdata field name lives on the `FIELD` namespace so
the rest of the code never spells one out.

Three modes — `exact`, `similar`, `broad` — change only the text operator
(`token` vs `fuzzy`) and the geo radius (`GEO_RADIUS_DEFAULT_MILES = 50`
vs `GEO_RADIUS_BROAD_MILES = 100`). Filter relaxation is a higher-level
concern: see below.

### Nice-to-have skills are scoring-only

`nice_to_have_skills` are never sent to Crustdata. They're consumed only by
`_score_one_profile`, which adds `NICE_TO_HAVE_WEIGHT = 0.25` to both the
numerator and denominator on a match (lifting the percentage) and adds
nothing on a miss (so a missing bonus never drags a candidate down).

### Broad-mode relaxation

`_search_broad` walks `_BROAD_RELAXATION_LADDER` — an ordered list of
`(label, mutator)` pairs that progressively drop or loosen criteria fields.
Each step rebuilds the filter and re-calls Crustdata until `total_count`
crosses `BROAD_HEALTHY_TOTAL_COUNT = 10` or the budget
(`BROAD_MAX_EXTRA_CALLS = 3`) runs out. The dropped labels are surfaced to
the UI so the recruiter sees what was relaxed.

### Match scoring

`_score_one_profile` walks every requested criterion as a "slot" and tallies
matched/missed. Score is `100 * matched/total`; the label is `Strong Match`
above `MATCH_STRONG_MIN = 85`, `Good Match` above `MATCH_GOOD_MIN = 60`,
else `Partial Match`. `score_and_sort_profiles` runs `normalize_criteria`
once at the top so the scorer doesn't need to think about legacy shapes.

## Routes (public API)

| Route | Purpose |
|---|---|
| `GET /` | Renders the SPA. |
| `POST /api/parse` | Free-text → criteria. Local-only, no Crustdata call. |
| `POST /api/extract` | JD paste OR `.pdf` / `.docx` / `.txt` upload → criteria + section attribution. |
| `POST /api/preview` | Returns `total_count` for the current filter (limit=1 search). If `mode=exact` returns zero, auto-retries in `similar` and surfaces `fallback_count`. |
| `POST /api/search` | The big one. Builds filter, optionally auto-switches Exact→Similar past `AUTO_SWITCH_FILTER_THRESHOLD = 5` active criteria, calls Crustdata (broad mode walks the relaxation ladder), scores+sorts, caches, returns. |
| `GET /api/history` | Last 25 cached searches by summary. |
| `POST /api/waitlist` | Email gate after the free-search limit. Writes to SQLite always, then best-effort forwards to a Google Sheet via `GSHEETS_WAITLIST_URL` if set. |
| `GET /api/profile` | Slide-out enrichment: takes a `linkedin_url`, calls `/screener/person/enrich`, caches for 30 days. |

## Persistence

SQLite at `/tmp/croot.db` on Vercel (read-only fs everywhere else, so cache
is best-effort), or `./croot.db` locally. Five tables:

- `search_cache` — keyed by hash of the full filter payload + mode.
- `search_history` — append-only list of summaries for the history panel.
- `company_id_cache` — caches `/screener/identify` lookups for 30 days.
- `waitlist` — name + email + UA + IP audit trail.
- `profile_cache` — `/screener/person/enrich` responses for 30 days.

## Configuration

Env vars: `CRUSTDATA_API_KEY` (required), `GSHEETS_WAITLIST_URL` (optional),
`CACHE_TTL_HOURS` (default 72). Named constants at the top of the relevant
sections in `app.py`: `SEARCH_MODES`, `DEFAULT_MODE`, `GEO_RADIUS_*`,
`BROAD_*`, `MATCH_*`, `NICE_TO_HAVE_WEIGHT`, `AUTO_SWITCH_FILTER_THRESHOLD`,
`MAX_UPLOAD_BYTES`.
