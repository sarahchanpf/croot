"""Tests for the canonical filter builder.

Pins the Crustdata payload structure clause-by-clause. No API key needed —
build_filters is a pure function; anything needing resolution is passed via
Resolved.

Run with:  python -m unittest tests.test_filters
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.criteria import Criteria
from app.core.filters import FIELD, Resolved, build_filters


def conditions_of(payload):
    return payload["conditions"]


def find(conds, column, type_=None):
    """First top-level condition matching column (+ optional type)."""
    for c in conds:
        if c.get("column") == column and (type_ is None or c.get("type") == type_):
            return c
    return None


class Envelope(unittest.TestCase):
    def test_top_level_is_and(self):
        payload = build_filters(Criteria(title="Engineer"))
        self.assertEqual(payload["op"], "and")
        self.assertIsInstance(payload["conditions"], list)

    def test_fully_empty_criteria_yields_no_conditions(self):
        # tenure_floor defaults to 6mo, so "truly empty" needs it cleared.
        # (The route's is_empty() guard rejects bare criteria before this anyway.)
        self.assertEqual(
            build_filters(Criteria(tenure_floor_months=None)),
            {"op": "and", "conditions": []},
        )


class Title(unittest.TestCase):
    def test_single_title_is_substring(self):
        c = conditions_of(build_filters(Criteria(title="Backend Engineer")))
        clause = find(c, FIELD.CURRENT_TITLE, "[.]")
        self.assertEqual(clause["value"], "Backend Engineer")

    def test_title_stays_full_phrase_under_company_anchor(self):
        # Skill Phase 2 Step 3: title is the full-phrase substring filter whether
        # or not the search is company-anchored. Broadening it is a relaxation
        # pass, not the default — the head-noun shortcut diverged from the skill.
        crit = Criteria(title="Backend Engineer", anchor_strategy="companies",
                        anchor_companies=["Stripe"])
        c = conditions_of(build_filters(crit, Resolved(anchor_company_ids=[101, 102])))
        clause = find(c, FIELD.CURRENT_TITLE, "[.]")
        self.assertEqual(clause["value"], "Backend Engineer")    # full phrase, not head noun
        self.assertTrue(any(x.get("op") == "or" for x in c))     # company anchor present

    def test_title_still_filters_when_only_industry_anchor(self):
        # Industry anchors are coarse, so title stays a hard filter there.
        crit = Criteria(title="Backend Engineer", anchor_strategy="industries")
        c = conditions_of(build_filters(crit, Resolved(anchor_industries=["Financial Services"])))
        self.assertIsNotNone(find(c, FIELD.CURRENT_TITLE, "[.]"))

    def test_multiple_variants_become_or(self):
        crit = Criteria(title="Backend Engineer", title_variants=["SWE", "backend engineer"])
        c = conditions_of(build_filters(crit))
        or_clause = next((x for x in c if x.get("op") == "or"), None)
        self.assertIsNotNone(or_clause)
        values = [d["value"] for d in or_clause["conditions"]]
        self.assertEqual(values, ["Backend Engineer", "SWE"])  # deduped case-insensitively


class Location(unittest.TestCase):
    def test_city_uses_geo_distance_with_radius(self):
        c = conditions_of(build_filters(Criteria(location="New York"), geo_radius_miles=100))
        clause = find(c, FIELD.REGION, "geo_distance")
        self.assertEqual(clause["value"], {"location": "New York", "distance": 100, "unit": "mi"})

    def test_country_uses_exact(self):
        c = conditions_of(build_filters(Criteria(location_country="Canada")))
        self.assertEqual(find(c, FIELD.COUNTRY, "=")["value"], "Canada")

    def test_remote_skips_geo(self):
        c = conditions_of(build_filters(Criteria(location="New York", remote_ok=True)))
        self.assertIsNone(find(c, FIELD.REGION))


class YoEAndTenure(unittest.TestCase):
    def test_yoe_band_emits_gte_and_lte(self):
        # Crustdata spells comparisons "=>" / "=<", not ">=" / "<=".
        c = conditions_of(build_filters(Criteria(yoe_min=5, yoe_max=10)))
        self.assertEqual(find(c, FIELD.YOE, "=>")["value"], 5)
        self.assertEqual(find(c, FIELD.YOE, "=<")["value"], 10)

    def test_default_tenure_floor_is_six_months(self):
        c = conditions_of(build_filters(Criteria(title="PM")))  # default 6 months
        self.assertAlmostEqual(find(c, FIELD.YEARS_AT_COMPANY, "=>")["value"], 0.5)

    def test_tenure_none_emits_no_clause(self):
        c = conditions_of(build_filters(Criteria(title="PM", tenure_floor_months=None)))
        self.assertIsNone(find(c, FIELD.YEARS_AT_COMPANY))


class Skills(unittest.TestCase):
    def test_skills_scoring_only_when_a_strong_narrower_present(self):
        # With a title to search on, skills are NOT a hard filter (sparse data).
        c = conditions_of(build_filters(Criteria(title="Engineer", must_have_skills=["Go"])))
        self.assertIsNone(find(c, FIELD.SKILLS))

    def test_skills_only_search_falls_back_to_filtering(self):
        # Nothing else to search on -> skills become the filter so we don't scan all.
        c = conditions_of(build_filters(Criteria(must_have_skills=["Go", "Kubernetes", "go"])))
        clause = find(c, FIELD.SKILLS, "in")
        self.assertEqual(clause["value"], ["Go", "Kubernetes"])

    def test_nice_to_haves_never_filter(self):
        c = conditions_of(build_filters(Criteria(nice_to_have_skills=["Rust"])))
        self.assertIsNone(find(c, FIELD.SKILLS))


class Anchors(unittest.TestCase):
    def test_companies_strategy_emits_current_and_past_or(self):
        crit = Criteria(anchor_strategy="companies", anchor_companies=["Stripe"])
        payload = build_filters(crit, Resolved(anchor_company_ids=[101, 102]))
        or_clause = next(x for x in payload["conditions"] if x.get("op") == "or")
        cols = {d["column"] for d in or_clause["conditions"]}
        self.assertEqual(cols, {FIELD.CURRENT_COMPANY_ID, FIELD.PAST_COMPANY_ID})
        self.assertEqual(or_clause["conditions"][0]["value"], [101, 102])

    def test_both_unifies_companies_and_industries_under_one_or(self):
        crit = Criteria(anchor_strategy="both", anchor_industries=["Fintech"])
        payload = build_filters(crit, Resolved(anchor_company_ids=[5], anchor_industries=["Fintech"]))
        or_clause = next(x for x in payload["conditions"] if x.get("op") == "or")
        cols = {d["column"] for d in or_clause["conditions"]}
        self.assertEqual(cols, {
            FIELD.CURRENT_COMPANY_ID, FIELD.PAST_COMPANY_ID,
            FIELD.CURRENT_INDUSTRY, FIELD.PAST_INDUSTRY,
        })

    def test_strategy_inferred_when_left_default(self):
        crit = Criteria(anchor_companies=["Stripe"])  # strategy defaults to "none"
        payload = build_filters(crit, Resolved(anchor_company_ids=[7]))
        self.assertTrue(any(x.get("op") == "or" for x in payload["conditions"]))

    def test_none_strategy_with_no_ids_emits_no_anchor(self):
        payload = build_filters(Criteria(anchor_strategy="none"))
        self.assertFalse(any(x.get("op") == "or" for x in payload["conditions"]))


class Exclusions(unittest.TestCase):
    def test_hiring_company_and_excludes_merge_into_one_not_in(self):
        payload = build_filters(
            Criteria(title="PM"),
            Resolved(hiring_company_id=900, exclude_company_ids=[800, 801]),
        )
        clause = find(payload["conditions"], FIELD.CURRENT_COMPANY_ID, "not_in")
        self.assertEqual(clause["value"], [800, 801, 900])


class Education(unittest.TestCase):
    def test_schools_use_in_majors_use_substring(self):
        crit = Criteria()
        crit.education.schools = ["Stanford University"]
        crit.education.majors = ["Computer Science"]
        c = conditions_of(build_filters(crit))
        self.assertEqual(find(c, FIELD.SCHOOL, "in")["value"], ["Stanford University"])
        self.assertEqual(find(c, FIELD.FIELD_OF_STUDY, "[.]")["value"], "Computer Science")


if __name__ == "__main__":
    unittest.main()
