---
name: source-candidates
description: >
  Lightweight candidate sourcing skill. Triggered by `/source-candidates`, or natural
  language like "source candidates for this JD", "find candidates for this role",
  "build a candidate list from {link}". Takes a JD link plus optional keywords/criteria,
  runs a Crustdata DB search, deep-enriches one profile (person + every
  past employer) to show data depth, supports one round of live filter refinement,
  and ships a Google Sheet via `gws`.
model: claude-opus-4-7
argument-hint: <jd-url> [keywords/criteria] [calibration-sheet-url]
---

# source-candidates

A lightweight sourcing skill backed by the Crustdata MCP. Input (JD URL and/or free-text criteria — either is enough; optionally a Google Sheet URL of pre-scored candidates as external calibration data) → one full-fat `people_search_db` call compressed into a compact pool → 20-row table + one fully-enriched profile (with every employer in their timeline enriched too) → optional external-calibration ingestion (only if a calibration sheet was provided) → optional voting on 3–5 more candidates (light enrichment + score 1–4) which calibrates the displayed ranking → optional refine loop → Google Sheet.

Stateless by design: no scoring rubric, no memory layer. The base run (no calibration, no voting, no refines) uses fewer than 10 total Crustdata API calls; calibration ingestion adds 0 Crustdata calls (just `gws sheets` + local reasoning); voting and refines are operator-driven and add calls explicitly.

This file is the canonical reference. Read top-to-bottom on first invocation.

---

## 1. Trigger conditions

Invoke when the user types or implies any of:

- `/source-candidates {jd-url}` (with or without trailing keywords)
- "source candidates for this JD ..."
- "find candidates for this role ..."
- "build a candidate list from {link}"

If the invocation is ambiguous (just `/source-candidates` with no args), use `AskUserQuestion` once to ask for the JD URL + any criteria. Do not chain further questions before Phase 1.

**Optional**: an external **calibration sheet** Google Sheet URL — pre-scored candidates the operator wants used as ground truth. If provided in the initial invocation (or any time before Phase 4), the skill ingests it in Phase 4 and uses the scores as taste signal that informs the Phase 5 voting recommendations and the Phase 6 full-pool ranking weights. If not provided, Phase 4 is skipped and the rest of the skill runs as before.

---

## 2. Pre-flight (single check, fail-fast)

Only verify what blocks the run:

- [ ] `AskUserQuestion` schema loaded — `ToolSearch select:AskUserQuestion`
- [ ] CrustData MCP connected — `crustdata_credits_check` returns a balance
- [ ] `gws` CLI on path — `which gws` returns a binary
- [ ] **If** a calibration sheet URL was provided in the invocation, also test `gws sheets spreadsheets values get --params '{"spreadsheetId":"<id>","range":"A1:A1"}'` returns without an auth error. If `gws` can't read the sheet, surface the specific error and ask the operator whether to (a) re-share the sheet, or (b) drop calibration and continue without it.

If any check fails, surface the specific miss and stop. Do not silently degrade.

**Lazy pre-flight (Phase 9 — Gem upload only)**: `gem-integration` and `chrome-cdp` are required ONLY if the operator opts into Phase 9. **Do not pre-flight them up front** — many runs ship to Sheets only and never need Gem. If the operator opts into Phase 9 but the tools aren't available, surface a clear error and skip Phase 9, but still allow the run to complete through Phase 8 and ship to Sheets.

---

## 3. The 8-phase workflow (9 phases when an external calibration sheet is supplied; +1 optional Gem upload)

### User-facing phase labels (mandatory)

`Phase N` is for cross-referencing in this doc only. **Never expose `Phase N` to the operator.** All chat messages, AUQ `question` strings, AUQ option strings, and Sheet tab titles must use the user-facing label below.

| Internal | User-facing label |
|---|---|
| Phase 1 — Parse JD + criteria, confirm | **Criteria** |
| Phase 2 — Cluster resolution + DB search | **Sourcing** |
| Phase 3 — Rank-1 deep-dive | **Top-1 deep-dive** |
| Phase 4 — External calibration ingestion | **Calibration ingest** |
| Phase 5 — Voting | **Voting** |
| Phase 6 — Full-pool ranking | **Ranking** |
| Phase 7 — Post-result menu | **Next step** |
| Phase 8 — Google Sheet export (incl. Step 8.5 Gem CSV) | **Sheet export** |
| Phase 9 — Gem upload (optional) | **Gem upload** |

Examples:
- ❌ "Moving to Phase 7 now." → ✅ "What's next?"
- ❌ "Phase 8 complete." → ✅ "Sheet export complete."
- ❌ "Entering Phase 9." → ✅ "Pushing to Gem now."

Internal step labels (`Step 8.5`, `Step 9-FILTER`, `Step 9-CONTACT`, etc.) also stay out of user-facing text — refer to them by what they do (e.g. "writing the Gem CSV", "applying the score gate", "enriching contacts").

### Rendering rule (applies in every phase)

**Never embed a multi-line "profile card" inside an `AskUserQuestion` `question` field.** Claude Desktop strips newlines from AUQ question text, which collapses the card into an unreadable blob. CLI renders newlines fine, but we optimize for the worst case.

Instead, the convention for every phase that shows rich content + asks a question is:

1. Render the rich content as a **standalone chat message** (markdown is allowed; newlines, bullets, tables, and headings all render correctly because chat messages are not AUQ payload).
2. THEN call `AskUserQuestion` with a **short** `question` (one line, no newlines, no extended profile data) and clear options. Reference the candidate by name + current employer in the question, e.g., `"Vote on Eduardo Garcia (Robust.AI · Sr EE)?"`.
3. Inside option strings, also keep things to a single line — no embedded newlines.

This rule applies to Phase 3 (profile reveal — full profile-card variant), Phase 5 (voting — light profile-card variant), and Phase 7 (post-result menu). The canonical profile-card template lives in [`references/profile-card.md`](references/profile-card.md) — both phases render it as a standalone chat message before any AskUserQuestion fires. Phase 4 (external calibration ingestion) does NOT render profile cards — it just parses a sheet and writes a taste-signal object to disk.

### Phase 1 — Criteria (Parse JD + criteria, confirm) — user-facing label: **Criteria**

**Input**: a JD URL **and/or** free-text keywords/criteria from the user's prompt. At least one of the two must be present. This is a dialed-down skill — do NOT block to ask the user to also provide the other; just proceed with whatever was given. **Optionally**, a Google Sheet URL pointing to pre-scored calibration data — if present, hold the URL for Phase 4. Detect it as any standalone arg matching `https://docs.google.com/spreadsheets/...` (or a bare spreadsheet ID); never confuse it with the JD URL (JDs are careers/jobs/Greenhouse/Lever/Workable/LinkedIn-jobs domains, not `docs.google.com/spreadsheets`).

**Source-of-truth rules** (state explicitly in the confirmation card so the operator sees what's being used):

- **Both present** → JD is the structured base; user-typed criteria override on conflict.
- **JD URL only** → JD is god. Skip the keyword merge.
- **Criteria only** → criteria is god. Skip the `crustdata_web_fetch` call entirely. Output a 1-line note like `No JD URL provided — using your typed criteria as source of truth.`
- **Neither** → single `AskUserQuestion` asking the operator to type either a JD URL or criteria, then proceed.

**Action**:

1. If a JD URL is present, `crustdata_web_fetch` it to pull the verbatim posting (fall back to native `WebFetch` only on Crustdata error). If no JD URL, skip this step — saves 1 Crustdata API call.
2. Extract a structured criteria block: `title`, `seniority/YoE`, `location`, `must-have skills`, `nice-to-haves`, `excludes`, `hiring_company` (the company posting the JD; leave blank if criteria-only with no clear hiring company). Keep this short — 7–9 bullets total. Sourced from the JD when available, else entirely from the operator's typed criteria.
3. If both JD and typed criteria are present, merge typed criteria on top of JD-derived criteria — typed always WINS on conflict.
4. Render a 1-message confirmation card in chat showing the merged criteria + a "Source: JD / criteria / both (JD base + criteria override)" line. NOT a question yet — just visual. (Render the information in form of a markdown table for clarity, e.g., `| Criteria | Source |` with rows for each field.)
5. Single `AskUserQuestion` call:
   - question: "Use these criteria for the search?"
   - options:
     - "Yes — run search now (Recommended)"
     - "No — I'll type changes"
   - If "No", read the user's typed update and re-merge into the criteria block. Show one updated card. Do NOT loop a second time at this phase — proceed to Phase 2 after the second render.
6. **If a calibration sheet URL was passed**, surface the URL on the confirmation card with a `Calibration sheet: <url> — will be ingested in Phase 4` line so the operator sees it's been picked up. Do NOT fetch / parse the sheet here — that happens in Phase 4 once the candidate pool exists (so name-matching has something to match against). Just hold the URL on session state.

Cost: 0–3 credits (0 if criteria-only, 1–3 if JD URL fetched).

---

### Phase 2 — Sourcing (Cluster resolution + full-fat DB search → compressed pool) — user-facing label: **Sourcing**

Naive title + skills filters return noisy results. Anchor the search on a small set of relevant companies so the candidate pool is dense with the right backgrounds. The single search call in Step 4 is **full-fat** — it returns enough data per profile to render the rest of the skill (profile cards, voting, sheet export) **without any per-candidate enrichment**.

#### Step 1 — Derive 2–3 adjacent-company clusters

From the JD + the operator's confirmed criteria, pick **2–3 clusters** of companies whose engineers/operators are plausible fits for the role. Each cluster = a band of companies that share a similar profile (same industry, similar tech stack, similar stage). Use your own knowledge to identify these clusters and list of few company names in each. Examples:

- A robotics/UAV EE search → cluster A: defense-autonomy primes (`Anduril`, `Skydio`, `Shield AI`, etc); cluster B: aerospace + UAV (`Joby Aviation`, `Wisk Aero`, `Zipline`); cluster C: hardware-heavy robotics (`Boston Dynamics`, `Cobalt Robotics`, `Bear Robotics`).
- A founding-AE search → cluster A: top early-stage SaaS GTM brands (`Ramp`, `Linear`, `Vercel`); cluster B: AI-native startups with public revenue traction.
- A frontend/AI-tooling search → cluster A: developer-tools shops; cluster B: AI-infra startups.

Cap at **3-4 clusters with up to ~5 companies each (≤15 companies total)**. Pick clusters that are clearly supported by the JD or the user's criteria — do not invent names. More cluster companies = denser anchor for the people-search filter, but past ~15 you start hitting diminishing returns. Each cluster company costs one `crustdata_company_identify` call (the API resolves one company at a time), so the cluster size directly drives the call count in this step.

Surface the chosen clusters in chat as a 1-line summary (informational, NOT a question): `Clusters: A={names}; B={names}; C={names}`.

#### Step 2 — Resolve cluster company names → company_ids

`crustdata_company_identify` resolves **one company per call** — there is no batch mode. To stay fast, fan out **all** cluster company names in a SINGLE assistant turn so they run in parallel (one tool call per company, all dispatched together). N cluster companies = N parallel `company_identify` calls. Always call company identify with `exact: false` and count between 3-5 results per company name to maximize the chance of a match.

Capture the resolved `company_id` for each. If any names fail to resolve, drop them silently — do NOT retry. `crustdata_company_identify` is cheap (~0 credits each on most plans, fast latency), so the call-count is high but cost and wall-clock impact are low when fanned out in parallel.

**Also resolve `hiring_company` in the same parallel fan-out** — one extra `crustdata_company_identify` call. Capture its `company_id` as `<HIRING_COMPANY_ID>`. Used in Step 3 (filter) and Phases 3 / 5 (cross-check). If unresolved or blank, set null and skip the filter / cross-checks downstream.

#### Step 3 — Build the filter

Build the `crustdata_people_search_db` filter from the confirmed criteria + the resolved cluster company_ids:

```jsonc
{
  "current_employers.title":      { "$or": [/* title variants from JD/criteria */], "[.]": true },
  "current_employers.company_id": { "$nin": [/* <HIRING_COMPANY_ID> — exclude employees of the hiring company itself */] },
  "region":                       { "geo_distance": { "city": "{JD city}", "miles": 50 } },
  "years_of_experience_raw":      { "$gte": /* YoE floor */, "$lte": /* YoE ceiling */ },
  "skills":                       { "$any": [/* must-have skills */] },
  "$or": [
    { "current_employers.company_id": { "$in": [/* resolved cluster company_ids */] } },
    { "past_employers.company_id":    { "$in": [/* resolved cluster company_ids */] } }
  ]
}
```

(Field names illustrative — use exact names from the `crustdata_people_search_db` parameter doc. Skip filters whose values aren't confidently extracted; do NOT invent values. If `company_identify` returned zero IDs, drop the `$or` block and search on title + skills + geo + YoE only. If `<HIRING_COMPANY_ID>` is null, drop the `$nin` clause.)

#### Step 4 — Execute the full-fat search (raw response → file)

Call `crustdata_people_search_db` with **`compact: false`, `truncate: false`, `format: "json"`, `limit: 100`**.

- `compact: false` returns the full nested payload per profile — full work history, education, skills, certifications, employer headcount + industry. This is what removes the need for any follow-up `people_enrich` / `company_enrich` calls later in the skill.
- `truncate: false` bypasses the 75K-char response cap. The harness automatically **spills the raw payload to a file** instead of streaming it back through Claude's context — critical because a 100-profile full-fat response can easily exceed 400K tokens and would otherwise blow the context window.
- `format: "json"` is required so the spilled file is valid JSON for `compress_pool.py`.

**Do not read the spilled raw file into context.** The whole point of `truncate: false` is that the raw payload stays on disk. Capture the file path the tool returns and feed it directly to the projector in the next step. If the tool result includes inline content, ignore it — only the path is needed.

If the result count is < 8 (visible from the tool's metadata, not from reading the file), relax in this order (one step at a time, re-run with the same `compact:false, truncate:false, format:"json", limit:100` settings, max 1 relaxation): drop the skills filter → expand geo radius to 100mi → drop the cluster `$or` block. Stop after one relaxation pass.

#### Step 5 — Project the raw response into a compressed pool

Run `compress_pool.py` script against the spilled raw file. Script lives at:

> **Resume hint**: before re-running the search at the start of Phase 2, `ls "$POOL_DIR"/pool-*.jsonl 2>/dev/null` — if a pool from earlier in this session exists and the operator's confirmed criteria haven't changed, skip Steps 1–5 and jump straight to Step 6 (render) using the existing pool. Re-running the search wastes credits and produces a different pool because Crustdata's default ranking can drift between calls.

Invocation (Bash):

```bash
# Per-session working dir — sits inside the same project folder the harness uses for
# tool-result spillover, scoped to THIS session's UUID, with a `data/` subfolder for
# the skill's working files.
PROJECT_DIR="$HOME/.claude/projects/$(pwd | tr / -)"
SESSION_ID="$(ls -t "$PROJECT_DIR"/*.jsonl 2>/dev/null | head -1 | xargs -n1 basename | sed 's/\.jsonl$//')"
POOL_DIR="$PROJECT_DIR/$SESSION_ID/data"
mkdir -p "$POOL_DIR"

python3 scripts/compress_pool.py \
  --raw   "<path-to-spilled-raw-json>" \
  --output "$POOL_DIR/pool-{run-id}.jsonl"
```

`{run-id}` should be a short stamp (e.g. timestamp or first 8 chars of a UUID) to disambiguate the initial pull from any later refines in the same session. Output as `.jsonl` so per-row `Read` with offset/limit works for large pools and a sidecar `.meta.json` is written automatically.

**Why this path, not `/tmp`**:

- `~/.claude/projects/{cwd-slug}/` is the same project-scoped folder the Claude Code harness uses for tool-result spillover. The slug is the absolute working directory with `/` → `-` (e.g. `/home/bhavb/Desktop/cd` → `-home-bhavb-Desktop-cd`).
- The `<session-id>` subfolder is the UUID of the current Claude Code session — same UUID as the session's transcript `.jsonl` sitting one level up. Discovery: most recently modified `*.jsonl` in `$PROJECT_DIR` (the active session is the one currently being appended to).
- The `data/` subfolder under that session-id keeps the skill's working files separate from the harness's own per-session files (todo state, shell snapshots, etc.).

Net effect: files survive `/tmp` reboots, `/resume` on the same session picks up the existing `pool-{run-id}.jsonl` without re-running the search, and the whole working set sits next to the session transcript so it's trivially recoverable. Always derive `PROJECT_DIR` / `SESSION_ID` / `POOL_DIR` once at the top of Phase 2 Step 5 and reuse `$POOL_DIR` in every later phase that writes a derived file (Phase 4 calibration sidecar, Phase 6 ranked pool, Phase 7 refines).

`compress_pool.py` is a pure projection — it strips verbose fields (truncates summaries to 1500 chars, descriptions to 400 chars, keeps top-20 skills, top-3 honors/certs), keeps full work history within the last 10 years, and tags any row missing `person_id` / employment history / linkedin_url with a `data_gap` flag. No filtering, no scoring.

**This compressed pool is the single source of truth for everything downstream.** Rendering, voting, refining, and the final sheet all read from it. There are no further enrichment calls.

#### Step 6 — Render the result table

Read the compressed pool (`.jsonl` → one candidate per line). Render a compact in-chat table of the returned candidates: `# | Name | Current Title @ Company | YoE | Region | LinkedIn`. Informational — no question.

Cost: ~N credits for `company_identify` batch (N = cluster size) + ~3 credits per 100 results for the search (so ~3 credits for limit 100; +3 if relax pass needed). `compress_pool.py` is local — 0 credits.

---

### Phase 3 — Top-1 deep-dive (Deep-enrich rank-1 + every employer in their timeline) — user-facing label: **Top-1 deep-dive**

The point of this phase is to show data depth, NOT to score. Pick the top result from Phase 2 (rank 1).

Run **in parallel**:

- `crustdata_people_enrich` with `enrich_realtime=true, linkedin_profile_url={url}, include_business_email=true` — full timeline, education, skills, summary, contact info (do not add fields params, return everything)
- `crustdata_social_posts` with `person_linkedin_url={url}, limit=10` — recent activity.

**Cross-check current employer**: if the enriched `current_employers[0].company_id` matches `<HIRING_COMPANY_ID>` (stale DB), **remove the candidate's row from `$POOL_DIR/pool-{run-id}.jsonl`** (and any `*-ranked.jsonl` if Phase 6 has run) so they're excluded from voting, ranking, and the Phase 8 export. Then advance to the next rank and restart Phase 3 from the top. Skip if `<HIRING_COMPANY_ID>` is null.

Then, from the enriched profile's employer history, take **every distinct employer** (current + past) and run `crustdata_company_enrich` for each in a SINGLE batched flight (parallel tool calls in one assistant turn). Cap at 4 employers — if the timeline is longer, enrich the 4 most recent ones only (this keeps headroom under the 10-call total budget alongside Phase 2's cluster step). Use `fields: ["headcount", "funding_and_investment", "founders", "taxonomy", "all_office_addresses"]`.

Render the result as a **standalone chat message** in markdown (NOT inside an AskUserQuestion) using the canonical profile-card template — full variant — defined in **[`references/profile-card.md`](references/profile-card.md)**. The full variant includes the `_Employer profile_` sub-bullets under each timeline entry, fed by the per-employer `company_enrich` calls above.

The profile card MUST end with the **Rationale & recommended score** section from the reference (mandatory in every render). The recommendation is non-binding but always surfaced — it primes the operator with a 1–4 starting score derived from how the profile maps against the JD / recruiter's confirmed criteria.

Every employer in the timeline gets its own enriched profile inline. Do not skip any. Do not summarize away from the operator.

Cost: ~5 credits (live enrich) + ~varies (posts) + 4× company_enrich (~1–4 credits each) ≈ 10–25 credits total.

---

### Phase 4 — Calibration ingest (External calibration sheet, conditional — only fires if a sheet URL was provided in Phase 1) — user-facing label: **Calibration ingest**

**Skip this phase entirely if no calibration sheet URL was captured in Phase 1.** Jump straight from Phase 3 to the post-result menu (Phase 7), which then offers voting / ranking / refine / export as before. This is the only conditional phase in the skill.

**This phase exists because** an operator-supplied set of pre-scored candidates is the strongest taste signal in the entire run — much stronger than 3–5 votes the operator might cast in Phase 5 — so the skill ingests it once, up front, and threads the resulting taste signals into every downstream phase that does ranking or voting recommendations.

#### Step 1 — Fetch + parse the sheet via `gws sheets`

Read the sheet's full grid in a single `gws sheets spreadsheets values get` call:

```bash
gws sheets spreadsheets values get \
  --params '{"spreadsheetId": "<id>", "range": "A:Z"}'
```

Resolve `<id>` from the URL (`/spreadsheets/d/<id>/...`) or use the bare ID if the operator pasted that. Capture the full 2D array.

#### Step 2 — Extract `(name, linkedin_url, score, feedback)` rows

Identify the header row (case-insensitive match on at least one of `name`, `linkedin`, `score`, `rating`, `notes`, `feedback`, `comment`). From the header, lock columns by semantic role:

- `name` — required (any column matching `name` / `candidate` / `full name`)
- `linkedin_url` — preferred (any column matching `linkedin` / `profile url` / `url`); fallback: regex-scan any other column for `linkedin.com/in/`
- `score` — required (any column matching `score` / `rating` / `vote` / `tier`); coerce to a 1–4 integer where possible, mapping common patterns: `4/4` / `★★★★` / `Strong yes` → 4; `3/4` / `Maybe` → 3; `2/4` / `Skip` → 2; `1/4` / `No` → 1; ranges 0–100 → bucket (≥85 → 4, 70–84 → 3, 50–69 → 2, <50 → 1)
- `feedback` — optional (any column matching `feedback` / `notes` / `comment` / `why`); keep verbatim text, no truncation

Drop rows where `name` and `linkedin_url` are both blank, or where `score` cannot be coerced. If fewer than 3 valid rows remain, surface `"Calibration sheet had only N valid rows — too sparse to extract reliable signal. Continuing without calibration."` and skip the rest of this phase. If the sheet has more than 50 valid rows, keep all of them — calibration signal monotonically improves with N.

#### Step 3 — Match calibration rows against the Phase 2 pool

For each calibration row, try to find a corresponding candidate in `$POOL_DIR/pool-{run-id}.jsonl` (the compressed pool from Phase 2). Match order:

1. `linkedin_url` exact match (strip `?` query params, normalize trailing slashes, lowercase).
2. Full-name exact match within the same `current_employers.name` if the sheet carries a company column.
3. Full-name exact match anywhere in the pool — accept only if there is exactly one hit.

Tag each calibration row with `pool_match: {person_id}` if matched, else `pool_match: null`. Pool-matched rows do double duty later — they're treated as "already voted" so Phase 5 will not surface them again, and their compressed-pool fields (skills, employers, schools, YoE, region) are pulled in for the taste-signal extraction in Step 4.

For rows with `pool_match: null`, do NOT run any Crustdata enrichment to fetch the missing profile — calibration is a 0-Crustdata-call phase. Just keep what the sheet gave you (name + score + feedback) and use it as a weaker, name+feedback-only signal in Step 4.

#### Step 4 — Extract taste signals (single Opus reasoning pass)

Single Opus pass over: the Phase 1 confirmed criteria block, the calibration rows (with their `pool_match` payloads filled in where matched), and a 1-line summary of the overall pool. Emit a structured taste-signal object:

```jsonc
{
  "calibration_n":          /* int — total valid calibration rows */,
  "score_distribution":     /* {1: n, 2: n, 3: n, 4: n} */,
  "boost":                  [/* short labels — sub-specialties / employers / schools / skills that score 3–4 in calibration */],
  "penalize":               [/* short labels — patterns that score 1–2 */],
  "disqualifier_phrases":   [/* short phrases mined from the feedback column on score=1 rows */],
  "weight_adjustments":     {
    /* key = the suggested weight band; value = "+10" / "-5" / "neutral" */
    "title_role_match":           "neutral",
    "must_have_skills_overlap":   "neutral",
    "domain_industry_fit":        "neutral",
    "yoe_seniority_band":         "neutral",
    "location_match":             "neutral",
    "bonus_signal":               "neutral",
    /* etc. — whatever dimensions the Opus pass identifies as relevant to boost/penalize based on the calibration patterns. Max 10 bands */
  },
  "matched_pool_person_ids": [/* person_ids of pool rows that have a calibration score — Phase 5 will skip these in voting */],
  "rationale":              /* 1–2 sentences — the dominant pattern the calibration revealed, plain English */
}
```

Rules:

- **Don't invent signal that isn't in the data.** If only 3 rows scored ≥3 and they share no obvious pattern, leave `boost: []` and explain in `rationale`.
- **Mixed/contradictory signal** (e.g., two near-identical profiles, one scored 4 one scored 1, no feedback to disambiguate) → set `weight_adjustments` to all `"neutral"` and emit `"calibration signal mixed; using JD criteria as-is"` in `rationale`.
- **The weight adjustments are advisory caps**, not multiplicative, calibration shifts ≤ ±15 per band, never zeros out a band.

#### Step 5 — Persist + summarize

Write the taste-signal object to the pool sidecar:

```
$POOL_DIR/pool-{run-id}.meta.json    (existing sidecar — add a `calibration` key)
```

Render a 1-message summary in chat (informational, NOT an AskUserQuestion). Format:

```
Calibration ingested: {N} pre-scored candidates ({4-count}× 4-star, {3-count}× 3-star, {2-count}× 2-star, {1-count}× 1-star). {pool-matched-count} matched the current pool — they're now treated as already voted.
Boost patterns: {comma list, max 5}
Penalize patterns: {comma list, max 5}
Rationale: {1-sentence rationale}
```

If the calibration phase was skipped because the sheet was too sparse (Step 2) or because pool-matching + signal extraction collapsed to all-neutral (Step 4), surface that explicitly in the summary so the operator knows downstream phases are running on JD criteria alone.

**Tools used**: `gws sheets spreadsheets values get` (1 call). 0 Crustdata credits. Local Opus reasoning only.

**Cost**: 0 credits. Wall-clock dominated by the single `gws` call (~1–3s) + one Opus reasoning pass (~15–25s for sheets up to ~50 rows).

---

### Phase 5 — Voting (Voting + lightweight calibration; entered from the Next-step menu) — user-facing label: **Voting**

Stripped down version — no scoring rubric, no weight-tuning, no rejected/accepted memory. The point is: let the operator score a small batch of profiles so the skill can re-rank the remaining list with their actual taste signal.

**Phase 5 has no entry AUQ of its own.** It is reached only when the operator picks "Vote on top 3" or "Vote on top 5" from the **Phase 7 post-result menu** (described below). It is fully optional — skipping voting is the recommended default. The operator can also enter Phase 5 multiple times in the same run (e.g., vote on top 3 → see re-ranked list → vote on 2 more from the new top via "Let me pick a count" if added later). If Phase 4 ran (external calibration ingested), every voting profile-card's recommended-score rationale must factor the calibration signal alongside the JD criteria — be explicit about which calibration pattern is firing (e.g., `Recommended score: 3/4 — JD-aligned PCB+UAV but matches the "RF-only at Skydio" penalty pattern from calibration`).

#### Step 1 — Voting loop (one candidate per AskUserQuestion)

The voting set is the next N highest-ranked candidates from the **current** result-set (post any prior voting/refining), _excluding_ rank 1 (already shown in Phase 3) and any candidate already voted on earlier in this run. So with the original Phase 2 ranking:

- "Vote on top 3" → ranks 2, 3, 4
- "Vote on top 5" → ranks 2, 3, 4, 5, 6
- Already voted on rank 2 in a prior pass + "Vote on top 3" again → ranks 3, 4, 5
- Any candidate listed in `calibration.matched_pool_person_ids` (Phase 4) is treated as already voted & skip them in the voting set selection.

**Hard cap on total voted candidates per run: 5.** If the operator has already voted on 5, the Phase 7 menu should drop the Vote options entirely (only Export and Refine remain). Candidates pre-scored via calibration (Phase 4) do NOT count against the 5-vote cap - that cap is on AUQ-collected votes only.

For each candidate in voting order:

1. **Light enrichment** — run in parallel:
   - `crustdata_people_enrich` with `enrich_realtime=true, linkedin_profile_url={url}` (omit `include_business_email` and `include_personal_contact_info` here — voting doesn't need contact info; saves ~2 credits and avoids identifier-mutex issues. Also, do not add fields params, return everything).
   - `crustdata_social_posts` with `person_linkedin_url={url}, limit=10`.
   - **Skip per-employer `crustdata_company_enrich`** in this phase — that depth signal belongs to Phase 3. This keeps the per-vote API call count to 2.

   **Cross-check current employer**: if the enriched `current_employers[0].company_id` matches `<HIRING_COMPANY_ID>`, **remove the candidate's row from `$POOL_DIR/pool-{run-id}.jsonl`** (and any `*-ranked.jsonl`) so they're excluded from the Phase 8 export, do NOT render or count against the 5-vote cap, and advance to the next candidate in the voting order. Skip if `<HIRING_COMPANY_ID>` is null.

2. **Render the profile** as a standalone chat message using the canonical profile-card template — **light variant** — defined in **[`references/profile-card.md`](references/profile-card.md)**. The light variant drops the `_Employer profile_` sub-bullets and the `Business email` line. The card MUST end with the **Rationale & recommended score** section (mandatory) — that's the part that primes the operator with a 1–4 starting score before the AUQ fires. This is a regular chat message; newlines / bullets / headings render fine on Desktop and CLI.

3. **Then** call `AskUserQuestion` with a **short** question and the 4-option score:
   - question: `"Vote on {Name} ({Current Co} · {Current Title})?"` — single line, no embedded newlines.
   - options:
     - `"4 — Strong yes, send outreach"`
     - `"3 — Maybe, worth a deeper look"`
     - `"2 — Skip this one"`
     - `"1 — Not a fit"`
   - No "(Recommended)" tag on any option — the recommendation already lived in the profile-card rationale; the AUQ itself stays neutral so it doesn't double-recommend.

4. Capture the score + any "Other" override the operator types. Do NOT write to memory; this is in-conversation state only.

#### Step 2 — Calibrate the displayed list

After all the votes from this Phase 5 visit are collected, re-rank the unvoted candidates in the current result-set using a single Opus reasoning pass over: the votes (this visit + any prior visits in the same run), **the Phase 4 calibration scores if present** (treat each calibration row as an additional vote with the same 1–4 weight, but only when it carries a `pool_match` so it has full pool fields to reason against), the voted profiles, and the unvoted candidates' Phase 2 metadata. **No additional Crustdata API calls in this step** — pure reasoning.

The re-rank is a heuristic, not a scoring system:

- Candidates sharing prominent traits with the 4-voted candidates (top employers, top skills, school cluster, location pattern) float toward the top.
- Candidates matching 1- or 2-voted patterns sink toward the bottom.
- 3-voted candidates are neutral signal.
- Calibration `boost` patterns add weight toward the top; `penalize` patterns sink.
- If votes are mixed/contradictory (e.g., one 4 + one 1 with no clear common trait), surface a 1-line note: `"Calibration signal mixed; ranking unchanged."` and skip the re-rank.

Re-render the now-reordered 20-row table in chat with a `Vote` column showing the score for any candidates that have been voted on so far this run (blank for the rest). Then return to **Phase 7** to show the menu again.

#### Cost

- Per voted candidate: 1 `people_enrich` (realtime) + 1 `social_posts` ≈ 5–7 credits + posts cost.
- Voting 3 candidates ≈ 15–20 credits + Opus reasoning for the re-rank.
- Voting 5 candidates ≈ 25–35 credits + Opus reasoning for the re-rank.

#### Phase 5 API call accounting

Voting is excluded from the 10-call base-run cap. Each voted candidate adds exactly 2 Crustdata API calls (`people_enrich` + `social_posts`), so the maximum 5 voted candidates per run = +10 calls. The base run + max-5 voting still stays under ~20 total calls.

---

### Phase 6 — Ranking (Full-pool 0–100 ranking against JD/criteria; entered from the Next-step menu) — user-facing label: **Ranking**

Pure local reasoning over the **compressed pool file** from Phase 2 (and any subsequent refine in Phase 7). Score every candidate in the pool 0–100 on how well they fit the **confirmed criteria block from Phase 1** (title, seniority/YoE, location, must-have skills, nice-to-haves, excludes). No Crustdata API calls. No `people_enrich`, no `social_posts`, no `company_enrich`. Phase 6 is a thinking step, not a fetch step.

**Phase 6 has no entry AUQ of its own.** It is reached only when the operator picks `"Rank full pool by JD fit"` from the **Phase 7 post-result menu** (described below). It is fully optional — the default flow ships the Crustdata-default ranking. The operator can re-enter Phase 6 multiple times in the same run (e.g., rank → refine → rank again on the new pool).

#### Step 1 — Read the pool

Read the compressed pool file (`.jsonl` produced by `compress_pool.py`). For pools ≤ 30 rows, read the whole file; for larger pools, read in chunks via `Read` `offset`/`limit`. Do NOT re-read the spilled raw search response — the compressed pool already has everything Phase 6 needs (title, function, seniority, YoE, skills, employer history with industry/headcount, location, education, summary, honors). Re-reading the raw file would blow context. **Also read** the sidecar `.meta.json` — if a `calibration` key is present (Phase 4 ran), thread its `weight_adjustments` / `boost` / `penalize` / `disqualifier_phrases` into Step 2.

**Same-employer cross-check**: drop any pool row whose `current_employers[0].company_id` matches `<HIRING_COMPANY_ID>` (Phase 2 Step 2). Persist the filtered pool back to `$POOL_DIR/pool-{run-id}.jsonl` so the dropped rows don't leak into refinement, the ranked file, or the Phase 8 export. Skip if `<HIRING_COMPANY_ID>` is null.

#### Step 2 — Score each candidate 0–100

Single Opus reasoning pass. Score on **fit to the confirmed criteria block**, not on absolute prestige. Suggested weight bands (a guide, not a religion — bend them when a JD signal clearly dominates; if Phase 4 calibration produced `weight_adjustments`, apply those as ≤ ±15 shifts on top of the band — never zero a band out):

| Signal                                                                                                                         | ~Weight |
| ------------------------------------------------------------------------------------------------------------------------------ | ------- |
| Title / role match (current title vs. JD title + seniority)                                                                    | ~25     |
| Must-have skills overlap (`skills` ∩ JD must-haves)                                                                            | ~25     |
| Domain / industry fit (current + past employers in JD-relevant industries, with more importance to recency of required skills) | ~20     |
| YoE / seniority band match against JD floor                                                                                    | ~15     |
| Location match against JD geo (city / metro / region; remote-friendly = full credit if JD allows it)                           | ~10     |
| Bonus signal (recent role transition into JD-relevant work, JD-relevant education, certifications/honors)                      | ~5      |

**Penalize hard misses** — if a profile contradicts a JD `excludes` (wrong sub-specialty, wrong domain, wrong geo when JD is on-site only), drop the score sharply (cap at ~40) regardless of the other signals. **Don't double-count** — a JD-relevant skill and a JD-relevant employer that prove the same thing should not both stack. **Calibration disqualifier phrases** (Phase 4) act as soft excludes: any candidate whose profile clearly matches one drops a band (cap ~60) and gets a `fit_flag` like `"calibration:RF-only"` so the rationale is auditable.

For each candidate compute:

- `fit_score` (integer 0–100)
- `fit_rationale` (1–2 sentences, glanceable, citing the strongest signal driving the score — same `✓ / ⚠️ / ✗` shorthand used in the profile-card rationale section is fine)
- `fit_flags` (optional short list of risk/gap tags like `"sub-specialty mismatch"`, `"location mismatch"`, `"data_gap"` if the row is already flagged from compression)

Candidates already flagged with `data_gap: true` from `compress_pool.py` get a `data_gap` entry in `fit_flags` automatically AND a one-band penalty (cap their score at ~70 unless the surfaced fields independently support a higher score).

#### Step 3 — Persist the scored, re-ranked pool

Write the scored rows back as a new file (do NOT clobber the original pool — keep the Crustdata-default ordering recoverable). Convention — reuse the project-scoped `POOL_DIR` exported in Phase 2 Step 5:

```
$POOL_DIR/pool-{run-id}-ranked.jsonl
```

(That expands to `~/.claude/projects/{cwd-slug}/{session-id}/data/pool-{run-id}-ranked.jsonl`. Same `{run-id}` stamp as the compressed pool it was derived from, so the pair `pool-{run-id}.jsonl` ↔ `pool-{run-id}-ranked.jsonl` is obvious. Same rationale as the compressed pool's location — outlives `/tmp` reboots, picked up by `/resume` of the same session, colocated with the harness's tool-result spillover.)

One candidate per line, sorted by `fit_score` desc (tiebreak by original Crustdata rank — preserve a `crustdata_rank` field carrying the row's original position). Update the sidecar `.meta.json` with: `ranked: true`, `ranked_at: {ISO timestamp}`, `criteria_snapshot: {confirmed criteria block from Phase 1}`, `calibration_applied: {true|false — true if Phase 4 calibration was threaded into the score}`. Future Phase 7 menu rounds operate on this ranked file once it exists.

If a refine happens after ranking (Phase 7 → Refine), the ranked file is **invalidated** — Phase 6 must be re-run on the post-refine pool before its scores are used again. The Phase 7 menu surfaces this by showing `"Rank full pool by JD fit"` again as an available option after every refine. (Phase 4 calibration data is NOT invalidated by a refine — taste signals from external scores remain valid against the new pool.)

#### Step 4 — Render the re-ranked table

Render the now-reordered table in chat with new columns: `# | Score | Name | Current Title @ Company | YoE | Region | LinkedIn | User Vote | Why`. The `Score` column shows the 0–100 integer. The `Why` column shows the `fit_rationale` truncated to ~80 chars. The `Vote` column preserves any Phase 5 votes already cast in this run, plus calibration scores from Phase 4 (mark calibration-sourced votes with a `*` suffix so the operator can tell them apart from in-run votes).

For pools > 20, only render the top 20 in chat (the full ranked file is on disk for export). One-line note above the table if truncated: `_Showing top 20 of {N} ranked. Full ranked pool exported to sheet on operator request._`

Then return to the **Phase 7 menu**.

#### Cost

0 Crustdata credits. Local Opus reasoning pass only. Wall-clock cost grows with pool size — for the default 20-row pool, single-pass is fine; for refined pools that grew larger, batch the reasoning in groups of ~25 candidates if context pressure is real.

---

### Phase 7 — Next step (Post-result menu: export / rank / vote / refine) — user-facing label: **Next step**

Phase 7 is the **single navigation surface** between every result-set render and Phase 8 (export). It fires:

- After Phase 3 (the rank-1 deep-dive) — the first time the operator sees the list with one fully-enriched candidate visible.
- After every Phase 5 voting + re-rank pass.
- After every Phase 6 full-pool ranking pass.
- After every refine round (described below).
- **After every Phase 8 Sheet export completes** (regardless of destination — Sheet only, Both, or Push to Gem only) and after every Phase 9 Gem upload completes. Export is **not** a terminal state for the run — the same pool is still on disk, the operator may still want to refine / re-rank / re-vote / re-export, and the menu must come back so they can.

It loops **indefinitely**. There is **no hard cap** on the number of menu rounds — neither before nor after an export. Anything past the initial pull is the operator signaling the list could be better; forcing them to ship a list they don't like, or stranding them with no menu after the first export, defeats the point of the skill. The run ends only when the operator types `done` / `exit` / `stop` via the AUQ's Other input, or stops responding. Never auto-end after a successful export.

#### Menu structure

ONE `AskUserQuestion` per round. `AskUserQuestion` max = 4 options. Render slots 1-4 in order, skipping conditionals that don't apply:

1. **(always)** `"Export (Recommended)"` — fires the destination sub-AUQ below.
2. **(conditional)** `"Rank full pool by JD fit"` — only when no fresh ranked pool for the current pool. After a rank pass, suppress until pool is refined or new Phase 5 votes are cast. Refine always re-shows (refine invalidates the ranked file).
3. **(conditional)** `"Vote on top 3 to calibrate"` — only while AUQ-votes cast < 5. Top-5 lives in the chat tip via Other (saves a slot).
4. **(always)** `"Refine the search — I'll type filter changes"`.

Question (constant): `"What's next? Export, calibrate the ranking, or refine the search?"`

Tip line above the AUQ (every round; suppress in the 2-button Export+Refine state):

```
Tip: type "vote 5" to vote on the next 5 instead of 3, or "ship to gem" to jump straight to Gem upload. Type "done" via Other to end the run.
```

After a successful export (Phase 8 or Phase 9), the menu re-fires unchanged — same 4-slot logic, with a one-line preamble noting the prior export so the operator knows the pool state. Example: `Sheet exported (link above). Pool is still loaded — refine / re-rank / re-vote / re-export, or type "done" to finish.` The conditional slots reset normally (Rank suppressed only if a fresh ranked pool is still valid; Vote suppressed only if AUQ-vote count ≥ 5).

**Destination sub-AUQ** (fires when operator picks `"Export"`):

- question: `"Where to export?"`
- default options:
  - `"Both — Sheet + push to Gem (Recommended)"` → Phase 8 + Phase 9.
  - `"Sheet only"` → Phase 8, end run.
  - `"Push to Gem only (skip the Sheet)"` → Step 8.5 CSV directly, then Phase 9.
  - `"Cancel — back to menu"`.
- reshape rules:
  - Sheet already exists for this pool this session → replace `"Both"` with `"Push to Gem now (Recommended)"`, drop `"Sheet only"`.
  - `gem-integration` / `chrome-cdp` not installed → drop both Gem options; collapse to `[Sheet only (Recommended), Cancel]` + a one-line install hint above.

The `(Recommended)` tag is the only nudge. Don't embed candidate/vote counts in the question text.

#### Refine sub-step

If the operator picks "Refine the search":

1. Read their freeform update (e.g., `"drop the 50mi radius, add Skydio/Anduril to current_employers"`).
2. Merge it into the filter from Phase 2.
3. Re-run `crustdata_people_search_db` once with the new filter — same full-fat settings as Phase 2 (`compact: false, truncate: false, format: "json", limit: 50`), spilled to a new file, then `compress_pool.py` against it to produce a fresh compressed pool. Output to `$POOL_DIR/pool-{new-run-id}.jsonl` — reuse the same `$POOL_DIR` exported in Phase 2 Step 5 (it's session-scoped, not per-search), and bump `{new-run-id}` (e.g. append `-r1`, `-r2`, …) so each refine sits next to the prior pulls instead of overwriting. Do NOT chain extra `company_identify` or any other tool calls inside a refine — clusters are already resolved (unless explicitly required or company clusters need to be updated); only the people search is re-run.
4. **Invalidate any ranked pool file from a prior Phase 6** — leave the prior `pool-{old-run-id}-ranked.jsonl` on disk for traceability, but treat it as stale; the new pool needs a fresh rank if the operator wants one.
5. Render the updated 20-row table in chat (preserve any `Vote` column populated from prior Phase 5 visits — voted candidates still carry their score even after re-search, if they appear in the new result set; the `Score` column is dropped until Phase 6 is re-run). If Phase 4 calibration data is on disk, re-run the calibration's pool-matching step (Step 3 of Phase 4) against the new pool so `matched_pool_person_ids` is updated; the extracted taste signals themselves remain valid.
6. Return to the Phase 7 menu.

**Budget note**: each refine adds 1 `crustdata_people_search_db` call beyond the headline 10-call run budget. That's intentional — refines are operator-requested. Phase 4 calibration ingestion, Phase 5 voting, Phase 6 ranking, and Phase 7 refines are all explicitly operator-driven and outside the auto-path budget. Phase 4 calibration and Phase 6 ranking add 0 Crustdata calls (local reasoning + 1 `gws sheets` call for Phase 4).

Cost: ~1 credit per refine round.

---

### Phase 8 — Sheet export (Google Sheet via `gws` + Step 8.5 Gem-importable CSV) — user-facing label: **Sheet export**

Two-tab sheet: Tab 1 = candidates, Tab 2 = search context. Source rows from the **ranked pool file** if Phase 6 has been run since the last refine; otherwise from the latest compressed pool (in Crustdata-default order).

```
Spreadsheet title: "Source Candidates — {Role keyword} — {YYYY-MM-DD}"

Tab 1 - "Candidates"
  Columns: Rank | Score (0–100, drop if Phase 6 not run) | User Vote (Phase 5 score, drop if not voted; calibration-sourced votes from Phase 4 marked with a `*` suffix) |
           Name | Current Company | Current Title | YoE | Region |
           Top Skills (top 5) | Prior Employers (top 3) | LinkedIn URL (hyperlinked) |
           Headline | Rationale (freeform text regarding why this candidate is a fit, based on the JD/criteria and enriched signals — use the `fit_rationale` from Phase 6 if present, else write fresh) | Any risk flags or data gaps to note (use `fit_flags` + `data_gap_reasons` if present; include any `calibration:*` flags from Phase 6) | User Score | User Notes

Tab 2 - "About this search"
  2-column key/value layout. Rows (skip any with no value):
    Role                    | {JD title}
    JD link                 | =HYPERLINK("{jd_url}", "open JD")
    Hiring company          | {name} (if resolved in Phase 2)
    Search date             | {YYYY-MM-DD}
    Location                | {region + radius from filter}
    YoE                     | {floor}–{ceiling, if set}
    Must-have skills        | {comma-joined from confirmed criteria}
    Nice-to-haves           | {comma-joined}
    Excludes                | {comma-joined}
    Cluster anchors         | {company names used in Phase 2 search}
    Pool size               | {N rendered} of {total returned}
    Phase 6 ranking         | "applied" / "not run"
    Phase 4 calibration     | "applied — {N} pre-scored rows" / "none"
    Refines                 | {N rounds}, last filter delta: "{operator's freeform input}"
    Operator notes          | {free space — leave blank for the operator}
```
Note:
- Make sure to have the LinkedIn URL column properly hyperlinked and not just plain text — use the `=HYPERLINK(...)` formula in the cell values.
- The `User Score` and `User Notes` columns are blank for the operator to fill in after export; It can be populated with any existing Phase 5 votes or rationale if you want, but it's also fine to leave them blank since the operator can see that info in the sheet already.
- Drop the columns that aren't applicable based on which phases ran (e.g., no `Score` column if Phase 6 didn't run; no `User Vote` column if Phase 5 votes weren't cast).

Implementation:

1. `gws sheets spreadsheets create` with the title above + a `sheets` array of two sheet objects (`title: "Candidates"`, `title: "About this search"`). Capture `spreadsheetId`.
2. `gws sheets spreadsheets values batchUpdate` — one call writes both tabs (`Candidates!A1` header + 20 data rows; `About this search!A1` key/value pairs). Skip Tab 2 rows whose values aren't available rather than writing blanks.
3. Make the LinkedIn URL column (Tab 1) and the JD link (Tab 2) properly hyperlinked via `=HYPERLINK(...)`.
4. Run **Step 8.5** (Gem-importable CSV — see below). Always runs after the Sheet succeeds, regardless of whether Phase 9 is invoked.
5. Share Sheet URL in chat as a single informational message. Also surface the local path of `<RUN_DIR>/gem_import.csv` so an operator who picked `"Sheet only"` still has the CSV available for manual `--csv-path` use later.
6. **Do NOT fire any AUQ here** — destination (Sheet vs Gem vs Both) was already picked at the Phase 7 → Export sub-AUQ. If the operator picked `"Both"` or `"Push to Gem only"`, drop straight into Phase 9 now. If they picked `"Sheet only"`, **return to the Phase 7 menu** (with the post-export preamble — see Phase 7) so the operator can refine / re-rank / re-vote / re-export against the same pool. Do **not** end the run here.

If `gws` errors, surface the error and stop, and maybe allow the user to retry or option to fall back to local xlsx sheet.

Cost: 0 credits.

#### Step 8.5 — Generate the Gem-importable CSV (always runs after the Sheet)

Write `<RUN_DIR>/gem_import.csv` (where `<RUN_DIR>` is the same `$POOL_DIR` exported in Phase 2 Step 5). Source rows = the same set just exported to the Sheet (ranked pool if Phase 6 ran, else post-refine compressed pool).

Strict canonical headers in this **exact** order — `gem-integration`'s `upload_project_csv.py` rejects any unknown header:

```
First Name, Last Name, Primary Email, LinkedIn, Phone Number, Title, Company,
School, Location, Reason, All Emails, All Phone Numbers, Extra1, Extra2, Extra3, Last Note
```

Field mapping — sourced ONLY from data already on disk (compressed pool + any Phase 3 / Phase 5 enrichment artifacts captured earlier in the session). **Do NOT make extra Crustdata calls in Step 8.5** — contact info will be sparse here. That's fine; if the operator opts into Phase 9:

- **Step 9-FILTER** (conditional) drops candidates failing the score gate (`fit_score > 60` OR `vote > 2` OR `calibration_score > 2`) — but only when a Phase 6 ranked pool exists. With no ranking on disk, Step 9-FILTER skips and the full set goes through.
- **Step 9-CONTACT** batch-enriches personal + business contact info for the (possibly filtered) set and overwrites this CSV before handoff to `gem-integration`.

Step 8.5's CSV mirrors the Sheet (full 20 candidates, unfiltered) so an operator who declines Phase 9 still has a usable Gem-importable file. The Phase 9 CSV is the actual outreach payload — same set when no ranking exists, or a strict subset (the ones above the score threshold) with full contact info populated when a ranking exists.

| CSV header | Source | Notes |
|---|---|---|
| `First Name` / `Last Name` | split `name` on first whitespace | Skip empty if mononymous. |
| `Primary Email` | `business_email` from Phase 3 enrichment if available, else `personal_email`, else blank | At least one of `Primary Email` / `LinkedIn` must be non-empty per row. |
| `LinkedIn` | canonical `linkedin.com/in/<slug>` form | |
| `Phone Number` | `phone` if present, else blank | Don't backfill. |
| `Title` | `current_employers[0].title` | |
| `Company` | `current_employers[0].name` | |
| `School` | top education entry only | Gem `{{school}}` is single-valued. |
| `Location` | `region` | |
| `Reason` | ≤240-char one-line outreach hook | If Phase 6 ran, distil from `fit_rationale`; else compose from the JD-relevant skill / role intersection. Plain text — no HTML, no markdown, no literal `{{tokens}}`. |
| `All Emails` | comma-joined dedup of business + personal emails, drop empties | |
| `All Phone Numbers` | `phone` (just the one for now) | |
| `Extra1` | `fit_score` if Phase 6 ran (e.g. `87`), else `years_of_experience` | Surfaced in Gem UI under the contact's profile. |
| `Extra2` | first-line of `fit_rationale` or top-skill highlight (≤240 chars) | |
| `Extra3` | risk / data-gap flags (`fit_flags` joined with ` · `, ≤240 chars) | |
| `Last Note` | rationale + flags + concerns, multi-line OK, ≤1000 chars | Lands in Gem's Activity tab as a sourced-from note. |

**Quoting**: every cell containing a comma, quote, or newline MUST be RFC-4180 quoted. Use Python's `csv.writer` with `quoting=csv.QUOTE_MINIMAL`. Don't hand-roll CSV.

**Why these fields**: every column maps to a concrete Gem placeholder or surface — `Reason → {{reason}}`, `School → {{school}}`, `Title → {{title}}`, `Company → {{company}}`, `Extra1/2/3 → {{extra1/2/3}}`. The full mail-merge token allowlist lives at `~/.claude/skills/gem-integration/reference/_placeholders.md`.

---

### Phase 9 — Gem upload (Optional opt-in push to gem.com via `gem-integration`) — user-facing label: **Gem upload**

**Optional**, opt-in. Triggered when the operator picks `"Both — Sheet + push to Gem"` or `"Push to Gem only"` from the Phase 7 → Export destination sub-AUQ, or types `push to gem` / `ship to gem` mid-run. Pure orchestration of the standalone `gem-integration` skill — no Crustdata calls, no scoring, no memory writes.

**At a glance**:
1. Pre-flight (`gem-integration` + `chrome-cdp` installed; logged-in `gem.com` tab open; `<RUN_DIR>/gem_import.csv` exists).
2. **Score gate** (Step 9-FILTER, conditional): if a Phase 6 ranked pool (`pool-{run-id}-ranked.jsonl`) exists for the current run, drop any candidate whose only evidence of fit is failing — keep iff `fit_score > 60` OR `vote > 2` (Phase 5 vote) OR `calibration_score > 2` (Phase 4 pool-matched row). **Strict greater-than** (60/100 and 2/4 themselves are middling, not "good"). Surface `Score gate: kept N / dropped M (≤60/100 and ≤2/4)`. If 0 remain, fire one AUQ asking the operator whether to (a) abort, (b) lower threshold to `≥50/100 OR ≥2/4`, or (c) push the unfiltered set anyway. **If Phase 6 has NOT run** (no ranked pool on disk), **skip the score gate entirely** — do NOT auto-rank, do NOT block, just push the full export set. Surface a one-line note: `No Phase 6 ranking on disk — pushing the full {N}-candidate set without score-gating. Run "Rank full pool by JD fit" from the Phase 7 menu first if you want filtering.` Filtered runs rewrite `<RUN_DIR>/gem_import.csv` to the kept rows; the Sheet (Phase 8 output) stays full.
3. **Contact enrichment** (Step 9-CONTACT): batch-call `crustdata_people_enrich` with `enrich_realtime=true, include_business_email=true, include_personal_contact_info=true` (do not add fields params, return everything) for every candidate **in the export set** who doesn't already have business + personal contact info on disk. Then **update `<RUN_DIR>/gem_import.csv`** so `Primary Email`, `Phone Number`, `All Emails`, `All Phone Numbers` are populated. Without this step, Gem can upload candidates but has no email to send to. Cost: ~5–7 credits per kept candidate. Filtered set of 8–14 → **~50–100 credits**; unfiltered (no Phase 6) set of 20 → **~100–130 credits**.
4. AUQ to confirm the per-run project name (Step 9A) — default `<Role keyword> outreach <YYYY-MM-DD>`.
5. AUQ to pick the sequence target (draft new copy / paste a template id / skip sequencing / cancel).
6. **If "Claude drafts the copy" was chosen, run the 2-question authoring AUQ** (Step 9C-Q) — sequence shape (stages + cadence as one preset) + voice/tone — then draft a sequence object using sensible defaults for everything else (immediate send, 12:00 noon local, JD-derived subject), then surface a preview AUQ. The operator can still redirect any default via "Edit the copy first" at the preview.
7. Hand off to `gem-integration` with: `csv_path` (the contact-enriched CSV), `project_name`, `target`, `sequence_source` (existing id OR authored object), `filter_to_linkedin_set` (lowercased LinkedIn URLs from the CSV), `output_dir`. That skill owns the script chain (`extract_cookies.py` → `ensure_project.py` → `upload_project_csv.py` → `list_project_people.py` → `add_to_sequence.py` / `create_full_sequence.py` → `cleanup_cookies.py`), validation, and DRAFT discipline.
8. Read `gem_upload.json` + `gem_sequence.json`, surface upload counts + errors verbatim in chat.
9. Surface project name + sequence URL — operator starts manually from Gem UI. **Sequence is always left as DRAFT.**
10. **Return to the Phase 7 menu** (with the post-export preamble — see Phase 7) so the operator can iterate further on the same pool (refine / re-rank / re-vote / re-export). Do **not** end the run here.

**→ Full reference: [`references/gem-upload.md`](references/gem-upload.md)** (pre-flight steps, contact enrichment, project-name AUQ, sequence-target AUQ, the 2-question authoring AUQ, sequence-object schema, handoff inputs to `gem-integration`, error-reporting recipes, anti-patterns). Read it on every Phase 9 run.

**Also read** `~/.claude/skills/gem-integration/SKILL.md` for the gem-integration skill's contract. Division of labor: this skill authors the CSV (Step 8.5) and the email copy (Step 9C); `gem-integration` validates, sends, and reports errors.

**Tools used**: `Bash` (orchestrating `gem-integration` scripts) + `AskUserQuestion` (project-name + sequence-target + 2-question authoring + preview confirm).

Cost: 0 Crustdata credits (Gem GraphQL is free).

---

## 4. Tool tier (kept tight — only what each phase needs)

| Phase   | Tool                                                    | Notes                                                                                                   |
| ------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 1       | `crustdata_web_fetch`                                   | JD pull (skipped if criteria-only). Native `WebFetch` only on error.                                    |
| 1, 5, 7 | `AskUserQuestion`                                       | Phase 1 confirm; Phase 5 per-vote score; Phase 7 export/rank/vote/refine.                               |
| 2       | `crustdata_company_identify`                            | One call per cluster company. Fan out in parallel — up to ~15 calls.                                    |
| 2, 7    | `crustdata_people_search_db`                            | DB only. Phase 7 re-runs once per refine round (same full-fat settings as Phase 2).                     |
| 3       | `crustdata_people_enrich` (realtime, full)              | Rank-1 candidate, full live enrichment incl. business email.                                            |
| 5       | `crustdata_people_enrich` (realtime, light)             | Per voted candidate (no contact-info flags). Skipped if operator chooses Skip.                          |
| 3, 5    | `crustdata_social_posts`                                | Last 10 posts. Rank-1 in Phase 3, plus each voted candidate in Phase 5.                                 |
| 3       | `crustdata_company_enrich`                              | Per-employer, batched parallel, cap 4. NOT re-run for voted candidates. Does not take `truncate` field. |
| 4       | `gws sheets spreadsheets values get`                    | Single call to fetch the calibration sheet (only fires if URL was provided in Phase 1). 0 credits.      |
| 4, 6    | _(none — local Opus reasoning)_                         | Phase 4 extracts taste signals; Phase 6 reads pool file, writes ranked pool file. Zero Crustdata calls. |
| 8       | `gws sheets spreadsheets create` / `values batchUpdate` | Output.                                                                                                 |
| 8.5     | local Python (`csv.writer`)                             | Always runs after Phase 8. 0 credits. Writes `<RUN_DIR>/gem_import.csv` from data already on disk.      |
| 9       | local CSV rewrite (no tool call)                        | **Step 9-FILTER** (conditional). Reads the Phase 6 ranked pool if present, drops rows failing `fit_score > 60` OR `vote > 2` OR `calibration_score > 2`, rewrites `<RUN_DIR>/gem_import.csv`. Skipped entirely when no ranked pool exists (no auto-rank). 0 credits. |
| 9       | `crustdata_people_enrich` (realtime, full contact)      | **Step 9-CONTACT** only. Parallel batch (waves of ≤6 in-flight) over the (possibly filtered) export set with `enrich_realtime=true, include_business_email=true, include_personal_contact_info=true`. ~5–7 credits per kept candidate. Filtered → ~50–100 credits. Unfiltered (full 20) → ~100–130 credits. Without this step, Gem has no email to send to. |
| 9       | `Bash` (orchestrating `gem-integration` scripts)        | Optional. 0 Crustdata credits in this part (Gem GraphQL is free). Calls `extract_cookies.py` → `ensure_project.py` → `upload_project_csv.py` → `list_project_people.py` → `add_to_sequence.py` / `create_full_sequence.py` → `cleanup_cookies.py`. |
| 9       | `AskUserQuestion`                                       | Step 9A (project-name confirm), Step 9B (sequence target), Step 9C-Q (2-question authoring — sequence shape + voice — when drafting copy), Step 9C preview confirm. ~5 fires when drafting; 2 when reusing a template; 2 when skipping sequence. |

**Total Crustdata API call budget** (base run, no calibration, no voting, no refines): ~10 "expensive" calls + N parallel `company_identify` calls where N = cluster size (≤15). The `company_identify` calls are intentionally not counted against the headline budget — they're cheap on credits, fast in parallel, and Crustdata exposes them as one-company-per-call by design.

Typical breakdown — Phase 1: 0–1 (`web_fetch`, skipped if criteria-only) · Phase 2: N + 1–2 (N parallel `company_identify` for cluster size N + 1 `people_search_db`, +1 if relax) · Phase 3: 2 + up to 4 (1 `people_enrich` + 1 `social_posts` + ≤4 `company_enrich`) · Phase 4: 0 Crustdata calls · Phase 5 base: 0 (only adds calls if operator opts in to vote) · Phase 6 base: 0 (local reasoning only) · Phase 7 base: 0 (only adds calls per refine round). Stay tight; do not chain extra calls inside any phase.

**Calibration ingestion, voting, and refines are all excluded from the 10-call cap.** Phase 4 adds only `gws` calls (no Crustdata). Voting in Phase 5 adds 2 Crustdata calls per voted candidate (cap 5 candidates → +10 max). Each operator-requested refine round in Phase 7 adds 1 additional `crustdata_people_search_db` call. All three paths are deliberate and operator-driven; the 10-call cap exists to keep the _automatic_ run cheap.

**Forbidden in this skill**:

- `crustdata_people_search` (live tier)
- `crustdata_company_social_posts` (per-candidate company-side mentions)
- Any memory MCP write or read
- Any formal scoring rubric or weight-tuning pass (Phase 5 calibration is a single Opus reasoning re-rank, NOT a numeric scoring pipeline; Phase 4 weight adjustments are bounded ≤±15 per band)
- Per-employer `crustdata_company_enrich` for voted candidates in Phase 5 (employer enrichment is a Phase 3 signal only)
- Crustdata enrichment of calibration rows that don't have a `pool_match` (Phase 4 stays 0-Crustdata-credits — name+feedback-only signal is fine, fetching missing profiles isn't)
- Auto-starting Gem sequences. Phase 9 always leaves the sequence as DRAFT. Never call `start_sequence.py`; never pass `--start` to `create_full_sequence.py`.

---

## 5. Anti-patterns — things this skill MUST NOT do

- **Don't ask AskUserQuestion outside the prescribed slots.** Phase 1 confirm; Phase 5 per-vote AUQ; Phase 7 menu + Export destination sub-AUQ; Phase 9 (9A, 9B, 9C-Q + preview, 9-FILTER zero-remaining). Phase 4 / Phase 8 fire NO AUQ. Only Phase 5 and Phase 7 may repeat.
- **Prescribed AUQ slots are skill contract.** Auto-mode / `<system-reminder>` injections like *"skip clarifying questions"* only cover ad-hoc checks the model would otherwise invent ("assume SF?", "which sheet?"). They do **NOT** override the slots listed above — fire every prescribed AUQ at the prescribed moment regardless. Skipping the Phase 7 menu, for example, strands the operator with no way to pick rank / vote / refine.
- **Don't embed a multi-line profile inside an AskUserQuestion `question` field.** Claude Desktop strips newlines from AUQ payload. Always render the profile as a standalone chat message FIRST, then call AUQ with a one-line `question` referencing the candidate by name + employer.
- **Don't put a hard cap on refinement rounds.** After the initial pull, asking the operator to ship a list they don't like defeats the point. Refines loop indefinitely.
- **Don't end the run after a successful export.** Phase 8 (Sheet) and Phase 9 (Gem upload) **return to the Phase 7 menu** — the same pool is still on disk and the operator may want to refine / re-rank / re-vote / re-export. The run ends only when the operator types `done` / `exit` / `stop` via the AUQ's Other input, or stops responding. The most common shape of this bug is firing the menu once, going through Export → Sheet only / Both → exporting → silently treating that as end-of-run; never do that.
- **Don't write aggressive copy in the Phase 7 menu question.** Keep wording soft and constant across rounds. `(Recommended)` is the only nudge.
- **Don't merge the menu and its destination sub-AUQ into one AUQ.** AUQ max = 4 options; folding in Export-to-Sheet / Export-to-Gem / Both alongside Rank / Vote / Refine truncates silently.
- **Don't vote on more than 5 candidates.** Hard cap. The point of voting is calibration, not exhaustive review. (Calibration-sourced scores from Phase 4 do NOT count toward this cap — only AUQ-collected votes do.)
- **Don't run per-employer `crustdata_company_enrich` for voted candidates.** That depth signal belongs to Phase 3 only. Voting profiles get `people_enrich` + `social_posts`, nothing more.
- **Don't enrich more than one candidate live in Phase 3.** Phase 3 is the one deep-dive. Voting in Phase 5 is the place for additional candidate enrichments.
- **Don't skip cluster resolution in Phase 2.** A naive title+skills filter returns noise. Always derive 2–3 clusters and resolve them via a batched `crustdata_company_identify` before searching, so the people search filter can anchor on `current_employers.company_id` / `past_employers.company_id`.
- **Don't skip company enrichment in Phase 3.** Every distinct employer (up to 4) gets `crustdata_company_enrich`. That's the depth signal.
- **Don't run Phase 4 if no calibration sheet URL was provided.** It's strictly conditional. The default flow has no Phase 4 — surface no fake "no calibration data" message, just go straight from Phase 3 to the Phase 7 menu.
- **Don't fetch a candidate profile via Crustdata in Phase 4 to fill in a missing pool match.** Calibration is a 0-Crustdata-call phase by design. Name+score+feedback alone is enough signal for the taste extraction; the pool-matched rows do the heavy lifting.
- **Don't exceed ~10 expensive Crustdata API calls in the BASE run** (calibration + voting + refines + parallel `company_identify` excluded; `company_identify` is cheap and one-call-per-company by API design). Cap clusters at 3 with **≤15 cluster companies total** (= ≤15 parallel `company_identify` calls in one turn), Phase 3 `company_enrich` at 4, and never chain extra `web_search` / `people_search_db` calls beyond what each phase prescribes. Phase 5 voting adds 2 calls per voted candidate; Phase 7 refines add 1 per round; both are expected.
- **Don't run `crustdata_company_identify` calls sequentially.** It accepts one company per call, so fan ALL cluster names out in a single assistant turn (parallel tool calls). Sequential dispatch would multiply wall-clock time by N.
- **Cap parallel in-flight at 6 for realtime enrichment endpoints** (`crustdata_people_enrich` with `enrich_realtime=true`, `crustdata_company_enrich`, `crustdata_social_posts`). Crustdata rate-limits aggressively past ~6 concurrent realtime calls; surplus calls queue server-side and stall or 429. When a phase needs more than 6 of these (e.g. Step 9-CONTACT enriching 20 candidates), batch them in **waves of ≤6**: dispatch up to 6 parallel calls in a single assistant turn, await all results, then dispatch the next wave. This rule does NOT apply to `crustdata_company_identify` (cheap, non-realtime — fan all ~15 in one turn) or `crustdata_people_search_db` (single call, no parallelism needed).
- **Don't formalize the Phase 5 vote-driven calibration into a scoring rubric.** It's a single Opus reasoning pass over votes + Phase 2 metadata + (optional) Phase 4 taste signals. No weights, no composite, no tiers.
- **Don't write to memory.** No `~/.claude/memory/` writes, no Memory MCP entities. Stateless by design. The Phase 4 taste-signal object lives in the pool sidecar `.meta.json` (session-scoped), not in long-term memory.
- **Don't auto-rank.** The default display order is Crustdata's default ranking (or, after a refine, the new search's default order). Phase 6 (full-pool 0–100 scoring) only runs when the operator explicitly picks `"Rank full pool by JD fit"` from the Phase 7 menu. Never invoke Phase 6 silently or as part of any other phase's flow.
- **Don't re-rank the unvoted pool with a 0–100 numeric score in Phase 5.** Phase 5's calibration is a single Opus reasoning re-rank using votes as taste signal — no numeric scoring, no weights. Numeric 0–100 scoring against the JD lives in Phase 6 only, and uses the criteria block from Phase 1 — not vote signal.
- **Don't bundle questions or use inline-text questions.** Always go through `AskUserQuestion`.
- **Don't invent filter field names or values.** Only use exact names from `crustdata_people_search_db` docs; only use values confidently extracted from the JD or operator input.
- **Only person_db_search has `compact: false, truncate: false` options.** Don't try to apply those options to any other tool.
- **Don't auto-start a Gem sequence.** Phase 9 stops at `add_to_sequence.py` / `create_full_sequence.py` and leaves `SequenceStatus.DRAFT`. The operator starts manually after reviewing in the Gem UI. Concretely: don't call `start_sequence.py`, and don't pass `--start` to `create_full_sequence.py` (DRAFT-stop is its default; `--start` is the explicit opt-in for `startSequence`, which is the persistent save in Gem).
- **Don't pre-flight Chrome / `gem-integration` until Phase 9 actually fires.** Many runs ship to Sheets only. Pre-flight § (§2) defers those checks to Phase 9's first step.
- **Don't pass non-canonical CSV headers to `upload_project_csv.py`.** Step 8.5's schema is strict (16 headers in a specific order). Renaming `Title` → `Job Title` breaks the upload. The mapping table at `~/.claude/skills/gem-integration/reference/_csv_mapping.md` is the source of truth.
- **Don't put HTML, markdown, or `{{token}}` placeholders into the Step 8.5 CSV cells.** Mail-merge resolution happens server-side at send-time inside Gem's composer — the CSV holds plain values. Putting `{{first_name}}` into a cell mails the literal string.
- **Don't use placeholders outside the gem-integration allowlist** (`{{first_name}} {{last_name}} {{school}} {{company}} {{title}} {{nickname}} {{reason}} {{day_of_week}} {{extra1}} {{extra2}} {{extra3}} {{recruiter_name}}`). Anything else aborts inside `_placeholders.validate_sequence_data` and Phase 9 stops.
- **Don't sequence the project unfiltered.** Always pass `filter_to_linkedin_set` to `gem-integration` (Step 9D in `references/gem-upload.md`).
- **Don't make extra Crustdata calls in Step 8.5.** Step 8.5 runs on data already on disk. (Phase 9 has one explicit Crustdata batch — Step 9-CONTACT — to populate personal/business contact info before Gem handoff.)
- **If GitHub profile is asked to be included by user, don't fetch it from Crustdata web APIs** - To fetch github profile id, use `github_profiles` in the fields parameter of `crustdata_people_enrich` API call (do a separate call with `enrich_realtime=false, linkedin_profile_url={url}, fields=[github_profiles]`). Don't try to fetch it via `crustdata_web_fetch` or any other means.

---

## 6. Cost model (per run)

| Phase                                                   | Credits            |
| ------------------------------------------------------- | ------------------ |
| 1 — JD fetch (skipped if criteria-only)                 | 0–3                |
| 2 — Cluster identify + DB search (limit 20)             | ~2–4               |
| 3 — Live enrich + posts + 4× company_enrich             | 10–25              |
| 4 — Calibration ingestion (only if sheet URL provided)  | 0 |
| 5 — Voting (per voted candidate, opt-in)                | ~5–7 each          |
| 6 — Full-pool ranking (opt-in, local Opus only)         | 0                  |
| 7 — Refine (per round, repeatable)                      | ~1 each            |
| 8 — Sheet (incl. Step 8.5 Gem CSV)                      | 0                  |
| 9 — Gem upload (opt-in; incl. Step 9-FILTER + Step 9-CONTACT) | **Filtered** (Phase 6 ranked pool exists): ~50–100 credits — Step 9-FILTER drops candidates failing the score gate, Step 9-CONTACT enriches only the kept set. **Unfiltered** (no Phase 6 on disk): ~100–130 credits — gate is skipped, contact enrichment runs on all 20. Both add 0 credits for the Gem GraphQL chain. |
| **Base total (no calibration, no voting, no refines)**  | **~12–32 credits** |
| **+ Phase 4 calibration (if sheet provided)**           | **+0 credits**     |
| **+ each voted candidate (Phase 5)**                    | **+~5–7 credits**  |
| **+ each refine round (Phase 7)**                       | **+~1 credit**     |

---

## 7. End-to-end walkthrough (canonical example, no calibration sheet)

**Operator**: `/source-candidates https://jobs.matternet.us/ee-senior  must have PCB + UAV background`

**Phase 1**: `crustdata_web_fetch` JD → extract `title=Sr Electrical Engineer, YoE>=7, Mountain View on-site, skills=Altium/Eagle/I2C/SPI/CAN, robotics/UAV preferred`. Merge operator's "must have PCB + UAV" → upgraded must-haves. Render card. AskUserQuestion: "Use these?" → "Yes". No calibration sheet provided → Phase 4 will be skipped.

**Phase 2**: Derive 3 clusters — A: defense-autonomy primes (`Anduril`, `Skydio`, `Shield AI`, `Saronic`, `Epirus`); B: aerospace + UAV (`Joby Aviation`, `Wisk Aero`, `Zipline`, `Archer Aviation`, `Reliable Robotics`); C: hardware-heavy robotics (`Boston Dynamics`, `Cobalt Robotics`, `Bear Robotics`, `Nimble Robotics`, `Agility Robotics`). 15 names total → 15 parallel `crustdata_company_identify` calls dispatched in one assistant turn (one per company; the API has no batch mode) → resolve ~14 `company_id`s. Build `crustdata_people_search_db` filter with title variants + 50mi geo + YoE>=7 + skills=[Altium, PCB layout, ...] + `$or` over `current_employers.company_id` / `past_employers.company_id` ∈ resolved IDs. limit=20. Render 20-row table.

**Phase 3**: Take rank 1 → `crustdata_people_enrich` (realtime) + `crustdata_social_posts` (limit 10) in parallel. Take up to 4 most-recent distinct employers from the timeline → `crustdata_company_enrich` for each in one parallel flight. Render the canonical profile card (full variant from `references/profile-card.md`) as a standalone chat message — including the mandatory Rationale & recommended-score section at the bottom (e.g., `Recommended score: 4/4 — 8 yrs PCB+robotics with verbatim Altium/Eagle evidence and direct UAV exposure at Skydio`).

**Phase 4 (skipped — no calibration sheet was provided in Phase 1).**

**Phase 7 menu — round 1**: 4 buttons `[Export, Rank, Vote-3, Refine]` → operator picks "Vote on top 3 to calibrate" → Phase 5 with N=3.

**Phase 5 (entered from menu)**: For each of ranks 2, 3, 4: parallel `crustdata_people_enrich` (light) + `crustdata_social_posts` (limit 10), render the light profile card from `references/profile-card.md` (no `_Employer profile_` sub-bullets, no business email line) — including the Rationale & recommended-score section. Then a short AUQ `"Vote on {Name} ({Co} · {Title})?"` with options 4/3/2/1. Operator votes 4, 3, 2 across the three (mostly agreeing with the recommendations, overriding rank 4 from a 3 down to 2). Single Opus reasoning re-rank pass over the remaining 17 unvoted candidates — candidates sharing top employers / skills with the 4-voted profile float to top. Re-render the 20-row table with a `Vote` column populated for ranks that have been voted on this run.

**Phase 7 menu — round 2**: 4 buttons → operator picks "Refine" and types `"drop the geo filter, add Anduril to current_employers"`. Refine sub-step re-runs `people_search_db` and renders the new 20 (votes preserved).

**Phase 7 menu — round 3**: 4 buttons (Rank back; refine invalidated any prior ranked file) → operator picks "Export" → destination sub-AUQ → "Both" → Phase 8 → Phase 9. No Phase 6 ranking on disk → Step 9-FILTER skipped; Step 9-CONTACT enriches contacts; AUQs for project name + sequence target; gem-integration handoff.

**Phase 8**: `gws sheets spreadsheets create` + `values batchUpdate` for the 20 candidates with hyperlinked LinkedIn URLs (and a `Vote` column populated for any candidates that got a Phase 5 vote). Share URL.

**Phase 7 menu — round 4 (post-export re-fire)**: Phase 9 finishes (sequence URL surfaced as DRAFT). Menu re-fires with the post-export preamble (`Sheet exported · Gem upload complete. Pool still loaded — iterate or type "done".`). 4 buttons → operator types `done` via Other → run ends.

### 7b. Walkthrough variant — with an external calibration sheet

**Operator**: `/source-candidates https://jobs.matternet.us/ee-senior  must have PCB + UAV background  https://docs.google.com/spreadsheets/d/1AbC_calibration_sheet/edit`

**Phase 1**: Same as above; the third arg is detected as a Google Sheets URL (not a JD URL) and held for Phase 4. Confirmation card adds a `Calibration sheet: <url> — will be ingested in Phase 4` line. Operator confirms.

**Phases 2 & 3**: Identical to the canonical walkthrough.

**Phase 4 (calibration ingestion)**: `gws sheets spreadsheets values get` pulls the sheet (1 call, 0 Crustdata credits). Detect 18 valid `(name, linkedin_url, score, feedback)` rows. Match against the Phase 2 pool: 4 of the 18 calibration candidates are also in the current pool (matched by linkedin_url). Single Opus reasoning pass extracts taste signals: `boost = ["PCB + flight controller experience", "Altium evidence in last 24mo", "ex-Skydio HW"]`, `penalize = ["RF-only at defense primes", "FPGA-heavy without board layout"]`, `disqualifier_phrases = ["test engineer not designer"]`, `weight_adjustments = {must_have_skills_overlap: "+10", domain_industry_fit: "+5", title_role_match: "neutral", ...}`, `matched_pool_person_ids = [4 ids]`. Persist to `pool-{run-id}.meta.json`. Surface a 1-line "Calibration ingested: 18 rows (5× 4-star, 6× 3-star, 5× 2-star, 2× 1-star). 4 matched the pool — already voted. Boost: PCB+flight-controller, ex-Skydio HW. Penalize: RF-only, FPGA without layout."

**Phase 7 menu — round 1**: same 4 buttons as the canonical walkthrough → operator picks "Rank full pool by JD fit" → enter Phase 6 (full-pool ranking).

**Phase 6 (entered from menu)**: Read pool + sidecar `.meta.json` → calibration weights are applied (`must_have_skills_overlap` band shifted +10, etc.). Score 16 unmatched candidates (4 are skipped because they're calibration-pre-scored; their calibration scores are surfaced in the rendered table with a `*` suffix). Disqualifier-phrase matches drop two candidates a band; their `fit_flags` get `"calibration:RF-only"`. Persist `pool-{run-id}-ranked.jsonl` with `calibration_applied: true` in the meta. Render the re-ranked 20-row table.

**Phase 7 menu — round 2**: 3 buttons `[Export, Vote-3, Refine]` (Rank suppressed — fresh ranked pool from round 1 still valid) → operator picks "Vote on top 3" → Phase 5 votes (calibration-pre-scored candidates excluded). Re-rank uses Phase 5 votes + Phase 4 signals. Back to menu.

**Phase 7 menu — round 3**: operator picks "Export" → "Both" → Phase 8 + Phase 9. Score gate fires (fresh ranked pool from round 1); drops rows failing `fit_score > 60` OR `vote > 2` OR `calibration_score > 2`; Step 9-CONTACT enriches the kept set; gem-integration handoff.

**Phase 8**: Sheet exported. Top frozen row carries `Calibration applied: 18 rows; boosted PCB+flight-controller; penalized RF-only`. `User Vote` column has both AUQ-collected votes and calibration scores (calibration ones marked `4*`, `3*`, etc.). Share URL.

**Phase 7 menu — round 4 (post-export re-fire)**: Phase 9 wraps up. Menu re-fires with the post-export preamble. Operator decides the calibration-aware list shipped is good and types `done` via Other → run ends. (Alternative: operator could pick "Refine" to broaden the pool and ship a second sheet against the same session.)

---

## 8. Glossary

- **JD** — Job Description (URL passed in by the operator).
- **Cluster** — a small band of companies (3–4 names) that share a profile relevant to the role (industry, stage, tech stack). Used in Phase 2 to anchor the people search on `current_employers.company_id` / `past_employers.company_id`.
- **Per-employer enrichment** — running `crustdata_company_enrich` on every employer in the chosen candidate's timeline. The signature depth-signal of this skill (Phase 3 only).
- **External calibration sheet** — an optional Google Sheet URL passed in the initial invocation pointing to a list of pre-scored candidates (`name`, `linkedin_url`, `score`, `feedback`). Loaded by Phase 4 once the candidate pool exists, parsed via `gws sheets`, name-matched against the pool, and condensed into a taste-signal object (`boost` / `penalize` / `disqualifier_phrases` / `weight_adjustments` / `matched_pool_person_ids` / `rationale`) stored in the pool's `.meta.json` sidecar.
- **Calibration ingestion (Phase 4)** — the conditional phase that processes the external calibration sheet. No Crustdata enrichment, single Opus reasoning pass to extract signals. Phase 4 is the strongest taste signal in the run (operator-curated, multi-row); Phase 5 votes are a weaker single-session signal that sit on top.
- **Voting / vote-driven calibration** — Phase 5 opt-in flow where the operator scores 3–5 additional candidates 1–4 (after their profiles render as standalone chat messages). The skill then re-ranks the remaining list using the votes (and any Phase 4 calibration data) as taste signal, in a single Opus reasoning pass — no formal scoring rubric, no memory writes.
- **Full-pool ranking** — Phase 6 opt-in flow (entered only from the Phase 7 menu) where every candidate in the compressed pool is scored 0–100 against the Phase 1 confirmed criteria via a single Opus reasoning pass. Pure local; 0 Crustdata calls. If Phase 4 ran, calibration `weight_adjustments` shift the suggested weight bands by ≤±15 each (and may introduce new bands beyond the base set, capped at 10 total bands), calibration `disqualifier_phrases` act as soft excludes (cap ~60), and calibration-pre-scored candidates surface their scores instead of being re-scored. Output is a new ranked pool file (`*-ranked.jsonl`) sorted by `fit_score` desc, with `fit_rationale` and `fit_flags` per row. Invalidated by any subsequent refine.
- **Profile card** — the canonical markdown rendering of a candidate (heading + career timeline + education + skills + posts + contact + **Rationale & recommended score**). Two variants — full (Phase 3) and light (Phase 5) — both defined in [`references/profile-card.md`](references/profile-card.md). Always rendered as a standalone chat message; never inside an `AskUserQuestion` payload.
- **Gem upload (Phase 9)** — optional, opt-in phase that pushes the current result set into Gem (gem.com) by orchestrating the standalone `gem-integration` skill. Creates a per-run Gem project, uploads `<RUN_DIR>/gem_import.csv`, and (optionally) adds the candidates to a sequence. Sequences are always left as `SequenceStatus.DRAFT` — the operator starts them manually from the Gem UI. Stateless: writes no memory. Triggered when the operator picks `"Both — Sheet + push to Gem"` or `"Push to Gem only"` from the Phase 7 → Export destination sub-AUQ, or via natural-language `push to gem` / `ship to gem`. Full reference: [`references/gem-upload.md`](references/gem-upload.md).
- **Gem-import CSV (Step 8.5)** — `<RUN_DIR>/gem_import.csv`, written automatically at the end of Phase 8 alongside the Sheet. 16 strict canonical headers. Plain text only — no HTML, no markdown, no literal `{{tokens}}` (mail-merge happens server-side at send-time inside Gem).
- **Sequence object** — JSON shape passed to `gem-integration`'s `create_full_sequence.py` when Phase 9 authors a fresh sequence. Carries `name`, `subject`, `body_html`, an array of `stages` (each with `delay_days`, `subject`, `message`), and a `schedule` (`type`, `start`, `interval_days`). Authored in Step 9C from the 2-question AUQ (sequence shape + voice) + sensible defaults + Phase 1 confirmed criteria. Placeholders are restricted to the gem-integration allowlist.
- **DRAFT discipline** — Phase 9's non-negotiable rule that every Gem sequence created by this skill is left as `SequenceStatus.DRAFT`. Never call `start_sequence.py`; never pass `--start` to `create_full_sequence.py`. The operator reviews the sequence in the Gem UI and starts it themselves.
- **Rationale & recommended score** — mandatory final section of every profile card. Maps the profile against the JD / recruiter's confirmed criteria using `✓ / ⚠️ / ✗` and emits a non-binding 1–4 recommendation. Primes the operator before voting in Phase 5; serves as a quality-check signal in Phase 3 even though no vote is collected there. If Phase 4 calibration ran, the rationale must also call out which calibration `boost` / `penalize` patterns are firing on this profile.
