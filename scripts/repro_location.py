"""Reproduce intake extraction + filter build for the Stripe Chicago JD to see
why results came back outside Chicago (e.g. India)."""
from __future__ import annotations
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import intake, jd_fetch, cluster_finder
from app.core.filters import build_filters
from app.routes.search import _resolve_anchors

URL = "https://stripe.com/jobs/listing/solutions-architect-platforms-velocity/7396509"

jd_text = jd_fetch.fetch_from_url(URL)
print(f"JD chars: {len(jd_text)}")
print("JD mentions Chicago:", "chicago" in jd_text.lower(),
      "| mentions remote:", "remote" in jd_text.lower(),
      "| hybrid:", "hybrid" in jd_text.lower())

reply, criteria, ready = intake.run_turn(
    [{"role": "user", "content": "Find candidates for this role."}], jd_text=jd_text)

print("\n=== EXTRACTED CRITERIA ===")
print("title:", repr(criteria.title))
print("seniority:", repr(criteria.seniority), "| yoe:", criteria.yoe_min, "-", criteria.yoe_max)
print("location:", repr(criteria.location))
print("location_country:", repr(criteria.location_country))
print("remote_ok:", criteria.remote_ok)
print("anchor_strategy:", criteria.anchor_strategy)
print("anchor_companies:", criteria.anchor_companies)
print("cluster_hint:", repr(criteria.cluster_hint))
print("hiring_company:", repr(criteria.hiring_company))
print("ready_to_search:", ready)

# Mirror the chat route's cluster step.
if criteria.cluster_hint or (criteria.anchor_strategy in ("companies", "both") and not criteria.anchor_companies):
    comp = cluster_finder.find_cluster(criteria)
    if comp:
        criteria.anchor_companies += [c for c in comp if c.lower() not in {x.lower() for x in criteria.anchor_companies}]
        if criteria.anchor_strategy == "none":
            criteria.anchor_strategy = "companies"
    print("cluster built:", criteria.anchor_companies)

resolved = _resolve_anchors(criteria)
payload = build_filters(criteria, resolved)
print("\n=== FILTER PAYLOAD ===")
print(json.dumps(payload, indent=2))
has_geo = any(c.get("type") == "geo_distance" or c.get("column") in ("region", "location_country")
              for c in payload.get("conditions", []))
print("\n>>> HAS A LOCATION CLAUSE:", has_geo)

# Run the search live WITH relaxation (mirror the route) and inspect regions.
from app import config
from app.core import crustdata, pool, ranker, sort_picker

sorts = sort_picker.pick_sorts(criteria)
radius = config.GEO_RADIUS_DEFAULT_MILES
data = crustdata.search(payload, limit=config.SEARCH_LIMIT, sorts=sorts)
print(f"\nfirst pass: total_count={data.get('total_count')} returned={len(data.get('profiles') or [])}")
if (data.get("total_count") or 0) < config.BROAD_HEALTHY_TOTAL_COUNT:
    rc, nr, label = ranker.plan_relaxation(criteria, radius)
    if rc is not None:
        rr = _resolve_anchors(rc)
        rp = build_filters(rc, rr, geo_radius_miles=nr)
        rp_has_geo = any(c.get("type") == "geo_distance" for c in rp.get("conditions", []))
        print(f"RELAXED: {label} | relaxed filter still has geo: {rp_has_geo}")
        if rp["conditions"]:
            data = crustdata.search(rp, limit=config.SEARCH_LIMIT, sorts=sorts)
            print(f"after relax: total_count={data.get('total_count')} returned={len(data.get('profiles') or [])}")

profiles = data.get("profiles") or []
cands = pool.compress(profiles)
from collections import Counter
regions = [(c.get("region") or "?") for c in cands]
print(f"\n=== REGIONS of {len(cands)} returned candidates ===")
def is_chicago(r): return any(k in r.lower() for k in ("chicago", "illinois", " il"))
non_chi = [r for r in regions if not is_chicago(r)]
print("near Chicago:", sum(1 for r in regions if is_chicago(r)), "| NOT near Chicago:", len(non_chi))
for r, n in Counter(regions).most_common(20):
    print(f"  {n:>3}  {r}")
