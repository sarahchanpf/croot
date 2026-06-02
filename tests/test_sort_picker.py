"""Tests for the Crustdata sort-axis picker (ported from sort-recipes.md)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.criteria import Criteria
from app.core.sort_picker import pick_sorts


def cols(sorts):
    return [s["column"] for s in sorts] if sorts else None


class SortPicker(unittest.TestCase):
    def test_seniority_sorts_by_experience(self):
        s = pick_sorts(Criteria(title="Backend Engineer", seniority="Senior"))
        self.assertEqual(cols(s), ["years_of_experience_raw", "current_employers.years_at_company_raw"])

    def test_gtm_sorts_by_connections(self):
        s = pick_sorts(Criteria(title="Account Executive"))
        self.assertEqual(s[0]["column"], "num_of_connections")

    def test_gtm_beats_seniority(self):
        # "Senior Account Executive" → network axis, not raw YoE.
        s = pick_sorts(Criteria(title="Account Executive", seniority="Senior"))
        self.assertEqual(s[0]["column"], "num_of_connections")

    def test_junior_with_ceiling_sorts_ascending(self):
        s = pick_sorts(Criteria(title="Engineer", yoe_max=3))
        self.assertEqual(s[0], {"column": "years_of_experience_raw", "order": "asc"})

    def test_no_axis_returns_none(self):
        # Plain role, no seniority lock, no YoE ceiling.
        self.assertIsNone(pick_sorts(Criteria(title="Backend Engineer")))

    def test_yoe_floor_alone_is_not_a_seniority_axis(self):
        self.assertIsNone(pick_sorts(Criteria(title="Engineer", yoe_min=5)))


if __name__ == "__main__":
    unittest.main()
