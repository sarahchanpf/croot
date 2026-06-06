"""Live parity check: run v2's pipeline end-to-end for a fixed query and dump
the filter payload, sorts, pool, and ranked list so it can be diffed against the
source-candidates skill running the same criteria.

Usage:  python scripts/parity_check.py
Makes real Crustdata + Anthropic calls (uses keys from .env).
"""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.core import crustdata, pool, ranker, sort_picker
from app.core.criteria import Criteria
from app.core.filters import build_filters
from app.routes.search import _resolve_anchors

CRITERIA = Criteria(
    title="Backend Engineer",
    seniority="senior",
    yoe_min=5,
    yoe_max=10,
    location="San Francisco",
    domain_signals=["fintech", "payments"],
    anchor_strategy="companies",
    anchor_companies=["Stripe", "Plaid", "Brex", "Ramp", "Mercury", "Modern Treasury"],
    tenure_floor_months=6,
)


def main():
    print("=== v2 PARITY RUN ===")
    print("ANTHROPIC key set:", bool(config.ANTHROPIC_API_KEY), "| RANK_MODEL:", config.RANK_MODEL)
    print("CRUSTDATA key set:", bool(config.CRUSTDATA_API_KEY))

    resolved = _resolve_anchors(CRITERIA)
    print("\n--- resolved anchors ---")
    print("anchor_company_ids:", resolved.anchor_company_ids)
    print("hiring_company_id:", resolved.hiring_company_id)

    radius = config.GEO_RADIUS_DEFAULT_MILES
    payload = build_filters(CRITERIA, resolved, geo_radius_miles=radius)
    sorts = sort_picker.pick_sorts(CRITERIA)
    print("\n--- filter payload ---")
    print(json.dumps(payload, indent=2))
    print("\n--- sorts ---")
    print(json.dumps(sorts))

    # Mirror the /api/search route: one search, then ONE relaxation if thin.
    relaxed = []
    data = crustdata.search(payload, limit=config.SEARCH_LIMIT, sorts=sorts)
    print("\n--- first-pass search result ---")
    print("total_count:", data.get("total_count"), "| returned:", len(data.get("profiles") or []))
    if (data.get("total_count") or 0) < config.BROAD_HEALTHY_TOTAL_COUNT:
        rc, new_radius, label = ranker.plan_relaxation(CRITERIA, radius)
        if rc is not None:
            rr = _resolve_anchors(rc)
            rp = build_filters(rc, rr, geo_radius_miles=new_radius)
            if rp["conditions"]:
                data = crustdata.search(rp, limit=config.SEARCH_LIMIT, sorts=sorts)
                relaxed.append(label)
                print(f"relaxed: {label} -> total_count:", data.get("total_count"),
                      "| returned:", len(data.get("profiles") or []))
    profiles = data.get("profiles") or []
    print("relaxed:", relaxed)

    # Verify compact:false actually delivered full profiles.
    if profiles:
        p0 = profiles[0]
        print("first profile has skills:", bool(p0.get("skills")),
              "| past_employers:", bool(p0.get("past_employers")),
              "| education:", bool(p0.get("education_background")))

    candidates = pool.compress(profiles)
    ranked = ranker.rank(candidates, CRITERIA, hiring_company_id=resolved.hiring_company_id)

    print("\n--- POOL (unranked, first 25, raw DB order) ---")
    for i, c in enumerate(candidates[:25]):
        print(f"{i:>2} | {(c.get('name') or '')[:24]:<24} | "
              f"{(c.get('current_title') or '')[:30]:<30} @ {(c.get('current_company') or '')[:18]:<18} | "
              f"yoe={c.get('yoe')} | {(c.get('region') or '')[:18]}")

    print(f"\n--- v2 RANKED (top 15 of {len(ranked)}) ---")
    for i, c in enumerate(ranked[:15]):
        print(f"{i+1:>2} | score={c.get('score'):>3} | {(c.get('name') or '')[:22]:<22} | "
              f"{(c.get('current_title') or '')[:28]:<28} @ {(c.get('current_company') or '')[:16]:<16}")
        print(f"     why: {(c.get('rationale') or '')[:110]}")

    # Dump machine-readable for later diffing.
    out = {
        "filter": payload,
        "sorts": sorts,
        "total_count": data.get("total_count"),
        "returned": len(profiles),
        "pool": [{"name": c.get("name"), "title": c.get("current_title"),
                  "company": c.get("current_company"), "linkedin": c.get("linkedin_url")}
                 for c in candidates],
        "ranked": [{"rank": i + 1, "score": c.get("score"), "name": c.get("name"),
                    "title": c.get("current_title"), "company": c.get("current_company"),
                    "linkedin": c.get("linkedin_url"), "rationale": c.get("rationale"),
                    "flags": c.get("flags")}
                   for i, c in enumerate(ranked)],
    }
    with open("/tmp/v2_parity.json", "w") as f:
        json.dump(out, f, indent=2)
    print("\nWrote /tmp/v2_parity.json")


if __name__ == "__main__":
    main()
