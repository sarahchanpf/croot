"""Full-fat search response -> compressed candidate pool.

A 100-profile full-fat response can be enormous; this is a pure local
projection down to the fields we render and score. Compressed pool is the
single source of truth for everything downstream (ranking, cards, export) —
no further Crustdata calls needed to display results.
"""

from __future__ import annotations


def compress(raw_profiles: list[dict]) -> list[dict]:
    """Project each raw profile to the compact candidate shape.

    TODO(impl): keep person_id, name, linkedin_url, current employer
    (name/title/company_id/years_at_company), region, top-20 skills,
    top-3 prior employers, YoE, education, headline; truncate long summaries.
    Tag rows missing person_id / employment / linkedin_url as data_gap.
    """
    raise NotImplementedError("pool.compress — see TODO")
