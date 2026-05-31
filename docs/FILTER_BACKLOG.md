# Advanced-filter backlog

The Canva prototype's Advanced Search modal shows more filters than the backend
currently maps to Crustdata. Per the "render all + flag for backend work"
decision, every field is rendered to match the design, but only the **wired**
ones drive a search today. The rest are shown with a "· not yet applied" hint
(`.adv-field.soon` in `static/styles.css`) and are inert.

## Wired now (map to the Criteria contract → `core/filters.py`)

| Modal field | Criteria field | Notes |
|---|---|---|
| Job title | `title` | |
| Seniority level | `seniority` | |
| Years of experience | `yoe_min` / `yoe_max` | range |
| Minimum tenure (years) | `tenure_floor_months` | years × 12 |
| Workplace type = Remote | `remote_ok` | only "Remote" maps; Hybrid/On-site inert |
| City / region / country | `location` | geo_distance |
| Company | `anchor_companies` (+ `anchor_strategy`) | resolved via identify |
| Skills used | `must_have_skills` | |
| Skills & assessments | `nice_to_have_skills` | scoring-only |
| Keywords (Boolean) | `domain_signals` | matched on industries + summary |
| Field of study | `education.majors` | |
| School | `education.schools` | autocompleted enum |

## Not yet wired — needs backend / Crustdata mapping work

Each needs a Criteria field, a `core/filters.py` clause (and possibly a
Crustdata field name to verify), plus ranker handling where relevant:

- **Job function** — Crustdata `current_employers.function_category` (v1 had this); add `function` to Criteria + a clause.
- **Employment type** — full-time/contract/etc. Confirm a Crustdata field exists.
- **Workplace type** (Hybrid / On-site) — only Remote maps today; the others need a Crustdata signal.
- **Postal/zip code radius** — geo by postal code; `geo_distance` currently uses city/region.
- **Network relationship degree** — 1st/2nd/3rd; needs a viewer context Crustdata may not expose for DB search.
- **Company size** — `*_employers.company_headcount_range` (v1 had size buckets).
- **Company type** — startup/public/etc.; map to a Crustdata field if available.
- **Tags / Project / Project status** — no obvious Crustdata equivalent; product decision needed.
- **Degree** — `education_background.degree_name` (verify).
- **Year of graduation** — `education_background.end_date` range (verify).
- **Spoken languages** — Crustdata `languages` (present on enrich; confirm searchability).

When adding any of these: extend `Criteria`, add the clause in `filters.py`,
remove `soon: true` from the field in `static/app.js`'s `ADV` list, and add a
test in `tests/test_filters.py`.
