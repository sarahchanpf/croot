"""End-to-end test on a real London JD: intake -> cluster -> filter -> search ->
rank, reporting the extracted location, the geo clause, and where the returned
candidates actually are."""
from __future__ import annotations
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import Counter

from app import config
from app.core import intake, jd_fetch, cluster_finder, crustdata, pool, ranker, sort_picker
from app.core.filters import build_filters
from app.routes.search import _resolve_anchors

URL = sys.argv[1] if len(sys.argv) > 1 else "https://boards.greenhouse.io/monzo/jobs/3837925"

jd_text = jd_fetch.fetch_from_url(URL)
print(f"JD chars: {len(jd_text)} | mentions London: {'london' in jd_text.lower()}")

reply, c, ready = intake.run_turn([{"role": "user", "content": "Find candidates for this role."}], jd_text=jd_text)
print("\n=== EXTRACTED ===")
print(f"title={c.title!r} seniority={c.seniority!r} yoe={c.yoe_min}-{c.yoe_max}")
print(f"location={c.location!r} country={c.location_country!r} region={c.location_region!r} remote_ok={c.remote_ok}")
print(f"hiring_company={c.hiring_company!r} cluster_hint={c.cluster_hint!r}")

# Mirror the chat route's cluster step.
if c.cluster_hint or (c.anchor_strategy in ("companies", "both") and not c.anchor_companies):
    comp = cluster_finder.find_cluster(c)
    if comp:
        c.anchor_companies += [x for x in comp if x.lower() not in {a.lower() for a in c.anchor_companies}]
        if c.anchor_strategy == "none":
            c.anchor_strategy = "companies"
print(f"cluster: {c.anchor_companies}")

resolved = _resolve_anchors(c)
sorts = sort_picker.pick_sorts(c)
payload = build_filters(c, resolved, geo_radius_miles=config.GEO_RADIUS_DEFAULT_MILES)
geo = [x for x in payload["conditions"] if x.get("type") == "geo_distance" or x.get("column") in ("region", "location_country")]
print(f"\n=== GEO CLAUSE === {json.dumps(geo)}")

# Mirror the route: search -> adaptive broaden -> thin relaxation.
data = crustdata.search(payload, limit=config.SEARCH_LIMIT, sorts=sorts)
total = data.get("total_count") or 0
relaxed = []
from app.routes.search import _without_anchor
if resolved.anchor_company_ids and total < config.AUTO_BROADEN_BELOW:
    broad = _without_anchor(c)
    bp = build_filters(broad, _resolve_anchors(broad), geo_radius_miles=config.GEO_RADIUS_DEFAULT_MILES)
    if bp["conditions"]:
        bd = crustdata.search(bp, limit=config.SEARCH_LIMIT, sorts=sorts)
        if (bd.get("total_count") or 0) >= config.AUTO_BROADEN_BELOW:
            data, total = bd, bd.get("total_count") or 0
            relaxed.append(f"broadened beyond the company cluster (anchored pool was {payload and ''}thin)")
if total < config.BROAD_HEALTHY_TOTAL_COUNT:
    rc, nr, label = ranker.plan_relaxation(c, config.GEO_RADIUS_DEFAULT_MILES)
    if rc is not None:
        rp = build_filters(rc, _resolve_anchors(rc), geo_radius_miles=nr)
        if rp["conditions"]:
            data = crustdata.search(rp, limit=config.SEARCH_LIMIT, sorts=sorts)
            relaxed.append(label)
print(f"total_count={data.get('total_count')} returned={len(data.get('profiles') or [])} relaxed={relaxed}")

cands = pool.compress(data.get("profiles") or [])
ranked = ranker.rank(cands, c, hiring_company_id=resolved.hiring_company_id,
                     anchor_company_ids=resolved.anchor_company_ids)

def country(r): return (r or "?").split(",")[-1].strip() or "?"
print("\n=== where are the returned candidates? ===")
for ctry, n in Counter(country(x.get("region")) for x in cands).most_common(12):
    print(f"   {n:>3}  {ctry}")

print(f"\n=== RANKED top 10 of {len(ranked)} ===")
for i, x in enumerate(ranked[:10]):
    print(f"{i+1:>2} | {x.get('score')} | {(x.get('name') or '')[:22]:<22} | "
          f"{(x.get('current_title') or '')[:26]:<26} @ {(x.get('current_company') or '')[:16]:<16} | {x.get('region')}")
