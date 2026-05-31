# Design reference

Source prototype (published Canva site):
https://jeffsarah2026.my.canva.site/advanced-search-button-ui

The v2 frontend is built to match this prototype.

- `canva-prototype-main.png` — the prototype's main "Candidate Finder" screen
  (rendered from the published Canva site).
- `v2-advanced-modal.png` — our build's Advanced Search modal, for comparison.

## Design tokens (extracted / matched)

- **Font:** DM Sans (Arimo fallback), loaded from Google Fonts.
- **Primary (indigo):** `#5b5bf2` buttons / accents.
- **Background:** `#f6f7fb` (light gray-lavender); **cards:** white, soft border `#e8e8f1`, rounded.
- **Brand:** target/dartboard mark + "Candidate Finder" wordmark.

## Screens

1. **Main** — describe box + JD link + JD file + notes + "Search Candidates"; "Advanced Search" button top-right.
2. **Advanced Search Filters** modal — full filter grid with a live "Estimated candidates matching your criteria" count and "Apply Filters". See `docs/FILTER_BACKLOG.md` for which fields are wired vs. pending.
3. **Results** — "Top Matches" cards (score, rationale, skills, employers, LinkedIn, reveal-contact), CSV export.

> The modal and results visuals are our interpretation in the prototype's visual
> language — I couldn't capture those interactive states from the Canva site
> (its buttons don't respond to headless automation). Share screenshots of the
> Advanced modal and Results states from the live prototype to refine them.
