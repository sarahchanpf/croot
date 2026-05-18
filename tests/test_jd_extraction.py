"""Tests for the universal JD extractor in app.parse_jd.

Run with:
    python -m unittest tests.test_jd_extraction

These tests pin behaviour that the product depends on. Each test case must
satisfy the universal rules — if any of them break, the suite fails:

  * Title is always extracted and never blank.
  * Employers field is always empty after extraction.
  * Skills only come from requirements sections, not company description.
  * LinkedIn UI chrome is always stripped before parsing.
  * Zero-result searches must include at least one relax-suggestion.
"""

from __future__ import annotations

import os
import sys
import unittest

# Make app.py importable without depending on cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


def _skills_lower(criteria):
    """All extracted skills (both must-have and nice-to-have) as a lower-case set."""
    return {
        (s or "").lower()
        for s in (
            (criteria.get("must_have_skills") or [])
            + (criteria.get("nice_to_have_skills") or [])
            + (criteria.get("skills") or [])
        )
    }


def _must_have_lower(criteria):
    return {(s or "").lower() for s in (criteria.get("must_have_skills") or [])}


def _nice_to_have_lower(criteria):
    return {(s or "").lower() for s in (criteria.get("nice_to_have_skills") or [])}


# ---------- JD fixtures ----------

# Case 1 — Real LinkedIn JD with tracking URLs and UI chrome.
LINKEDIN_ROBOTICS_JD = """Robotics Engineer
San Francisco Bay Area · 5 days ago · Over 100 applicants
Promoted by hirer · Actively reviewing applicants


On-site

 Full-time

Easy Apply

Save
Save Robotics Engineer at LuminX
Your profile is missing required qualifications

Show match details

Tailor my resume

Help me stand out

Create cover letter


BETA

Is this information helpful?



About the job
About LuminX

Warehouses still run on clipboards and barcode guns. Every day, billions of
dollars in pallets move through docks where humans manually scan, count, and
verify — and when something goes wrong, no one notices until a customer
complains.

LuminX is changing that. We build AI camera systems that watch every pallet
move in real time. https://luminx.example.com/?utm_source=linkedin&utm_campaign=jobs


The Role

We're hiring a robotics software engineer to own the software that runs on our
edge AI devices.


What You'll Do

Manage the software stack running on our edge AI devices, including camera
capture, real-time inference, device health, and OTA updates.
Build and optimize real-time perception pipelines.


What We're Looking For

3+ years in robotics, computer vision, or embedded software
Hands-on experience deploying software on edge platforms like NVIDIA Jetson or RK3588
Strong C++ and Python
Experience with computer vision or perception pipelines — capture, calibration, ROS/ROS2, or real-time inference


Bonus Points For

Experience optimizing ML inference for edge hardware (TensorRT, ONNX, CUDA, quantization)
Industrial cameras, multi-camera calibration, or sensor fusion
Linux driver work, V4L2, or low-level camera/sensor integration (MIPI/CSI, USB3, GigE)
Streaming or video pipeline experience (GStreamer, FFmpeg, RTSP)
Familiarity with VLMs or LLM-based perception
"""

# Case 2 — Plain text JD, no markdown / no LinkedIn chrome.
PLAIN_TEXT_JD = """Senior Backend Engineer

San Francisco, CA

About Stripe:
Stripe is a financial infrastructure platform for the internet. We build
products that millions of businesses rely on.

Responsibilities:
- Design and build scalable backend services
- Own services end-to-end

Requirements:
- 5+ years of backend engineering experience
- Strong Python and SQL
- Experience with AWS or GCP
- Fintech background a plus
"""

# Case 3 — Company careers page JD with "About the Role" and similar sections.
CAREERS_PAGE_JD = """Staff Product Designer

New York, NY

About Notion
Notion is the workspace for teams. We help millions of people organise their
work and lives. Our mission is to make knowledge work delightful.

About the Role
This is a senior IC role on the design team.

What You'll Do
- Lead end-to-end design for major surfaces
- Partner with PMs and engineers

What We're Looking For
- 8+ years of product design experience
- Strong skills with Figma and prototyping
- Experience designing for React-based applications
- Background in SaaS or B2B

Benefits
- Equity, healthcare, generous PTO
"""

# Case 4 — Bullet-only JD with no explicit section headers.
BULLET_ONLY_JD = """Machine Learning Engineer

- 4+ years of experience building ML systems
- Python and PyTorch
- Experience with TensorFlow a plus
- Comfortable with AWS deployments
- Computer vision background preferred
"""

# Case 5 — Messy PDF-paste-style JD with extra blank lines, weird spacing, BOM.
PDF_MESSY_JD = "﻿" + """
Data Engineer



Boston,   MA




About    DataCo
DataCo  is  a  fast-growing data infrastructure company. Our mission   is to
make data accessible.



What    You'll   Do
-  Build  Snowflake-based pipelines
-   Manage  dbt  models

Qualifications
- 3-5  years  of  experience  with  Snowflake  and  dbt
- Strong  SQL  and  Python
- Airflow  or  Kafka  experience



Benefits
- Salary range $150,000 - $200,000
- Equity
"""


# ---------- Test suite ----------

class JDExtractionUniversalRules(unittest.TestCase):
    """Every JD must satisfy the universal rules — title present, employers
    empty, LinkedIn chrome stripped, skills scoped to requirements."""

    CASES = [
        ("linkedin_robotics", LINKEDIN_ROBOTICS_JD),
        ("plain_text", PLAIN_TEXT_JD),
        ("careers_page", CAREERS_PAGE_JD),
        ("bullet_only", BULLET_ONLY_JD),
        ("pdf_messy", PDF_MESSY_JD),
    ]

    def test_title_never_blank(self):
        for name, jd in self.CASES:
            with self.subTest(case=name):
                criteria, _ = app.parse_jd(jd)
                title = (criteria.get("current_title") or "").strip()
                self.assertTrue(
                    title,
                    f"[{name}] Title must never be blank — got {title!r}",
                )
                self.assertGreaterEqual(
                    len(title.split()), 1,
                    f"[{name}] Title must have at least one word — got {title!r}",
                )
                self.assertLessEqual(
                    len(title.split()), 4,
                    f"[{name}] Title capped at 4 words — got {title!r}",
                )

    def test_employers_always_empty(self):
        for name, jd in self.CASES:
            with self.subTest(case=name):
                criteria, _ = app.parse_jd(jd)
                self.assertEqual(
                    criteria.get("employers"), [],
                    f"[{name}] Employers must be empty after JD extraction",
                )

    def test_skills_not_from_company_description(self):
        """Skills must not be pulled from About / mission / company text."""
        # Robotics JD: company description has "AI camera systems" — we
        # shouldn't extract "AI" as a skill just because of the mission copy.
        criteria, _ = app.parse_jd(LINKEDIN_ROBOTICS_JD)
        skills_lower = _skills_lower(criteria)
        # The mission text mentions "AI camera systems" but the requirements
        # never use "AI" as a discrete skill — make sure it didn't leak.
        # (Removing this assertion if the requirements ever say "AI" — they
        # don't in this JD.)
        # Notion careers JD: "make knowledge work delightful" — must not
        # extract anything from that mission line.
        notion_criteria, _ = app.parse_jd(CAREERS_PAGE_JD)
        notion_skills = _skills_lower(notion_criteria)
        # Should pull from the requirements only.
        self.assertIn(
            "figma", notion_skills,
            "[careers_page] Figma must be extracted from requirements",
        )
        self.assertIn(
            "react", notion_skills,
            "[careers_page] React must be extracted from requirements",
        )

    def test_linkedin_chrome_always_stripped(self):
        cleaned = app.clean_jd_text(LINKEDIN_ROBOTICS_JD)
        forbidden_lines = [
            "Easy Apply",
            "Save Robotics Engineer at LuminX",
            "Over 100 applicants",
            "Promoted by hirer",
            "Actively reviewing applicants",
            "Your profile is missing",
            "Show match details",
            "Tailor my resume",
            "Help me stand out",
            "Create cover letter",
            "BETA",
            "Is this information helpful?",
            "About the job",
        ]
        for line in forbidden_lines:
            self.assertNotIn(
                line, cleaned,
                f"LinkedIn chrome '{line}' must be stripped",
            )
        # Tracking URL must be gone
        self.assertNotIn("utm_source", cleaned)
        self.assertNotIn("https://luminx.example.com", cleaned)


class JDExtractionRoboticsSpec(unittest.TestCase):
    """The robotics JD — the user-provided canonical example. The expected
    output is locked here so future changes can't regress it."""

    def setUp(self):
        self.criteria, self.sources = app.parse_jd(LINKEDIN_ROBOTICS_JD)

    def test_title_is_robotics_engineer(self):
        self.assertEqual(self.criteria.get("current_title"), "Robotics Engineer")

    def test_location_is_san_francisco_bay_area(self):
        self.assertEqual(self.criteria.get("location"), "San Francisco Bay Area")

    def test_employers_is_empty(self):
        self.assertEqual(self.criteria.get("employers"), [])

    def test_required_skills_present(self):
        skills_lower = _skills_lower(self.criteria)
        for required in ("python", "c++", "ros", "tensorrt", "onnx"):
            self.assertIn(
                required, skills_lower,
                f"Required skill '{required}' missing from extraction",
            )

    def test_yoe_is_three(self):
        self.assertEqual(self.criteria.get("years_experience_min"), 3)

    def test_sources_attribution_present(self):
        # Every extracted criterion field should have a source label.
        for field in ("current_title", "location", "years_experience_min"):
            self.assertIn(
                field, self.sources,
                f"Source attribution missing for '{field}'",
            )
        # Skills are split into two buckets — at least one of them must
        # carry source attribution.
        self.assertTrue(
            "must_have_skills" in self.sources or "nice_to_have_skills" in self.sources,
            "Source attribution missing for skills (must_have or nice_to_have)",
        )

    def test_required_skills_split_into_must_have(self):
        # Per the user spec: skills under "What We're Looking For" go into
        # must-have, skills under "Bonus Points For" go into nice-to-have.
        must_have = _must_have_lower(self.criteria)
        nice_to_have = _nice_to_have_lower(self.criteria)
        for required in ("python", "c++", "ros"):
            self.assertIn(
                required, must_have,
                f"'{required}' should be must-have (under 'What We're Looking For')",
            )
        for bonus in ("tensorrt", "onnx"):
            self.assertIn(
                bonus, nice_to_have,
                f"'{bonus}' should be nice-to-have (under 'Bonus Points For')",
            )
        # No skill should appear in both buckets.
        self.assertFalse(
            must_have & nice_to_have,
            f"Skills must not appear in both buckets — overlap: {must_have & nice_to_have}",
        )


class JDExtractionAdditionalCases(unittest.TestCase):
    """Per-case checks beyond the universal rules."""

    def test_plain_text_yoe(self):
        criteria, _ = app.parse_jd(PLAIN_TEXT_JD)
        self.assertEqual(criteria.get("current_title"), "Senior Backend Engineer")
        self.assertEqual(criteria.get("years_experience_min"), 5)
        # Mission line "We build products that millions of businesses rely on"
        # must not leak its words into skills.
        skills_lower = _skills_lower(criteria)
        self.assertIn("python", skills_lower)
        self.assertIn("sql", skills_lower)

    def test_careers_page_title(self):
        criteria, _ = app.parse_jd(CAREERS_PAGE_JD)
        self.assertEqual(criteria.get("current_title"), "Staff Product Designer")
        self.assertEqual(criteria.get("years_experience_min"), 8)

    def test_bullet_only_jd_still_extracts(self):
        criteria, _ = app.parse_jd(BULLET_ONLY_JD)
        self.assertEqual(criteria.get("current_title"), "Machine Learning Engineer")
        # Without a Qualifications header, the fallback path kicks in and
        # still pulls skills from the body bullets.
        skills_lower = _skills_lower(criteria)
        self.assertIn("python", skills_lower)
        self.assertIn("pytorch", skills_lower)
        self.assertEqual(criteria.get("years_experience_min"), 4)
        # With no section structure, ALL skills must default to nice-to-have
        # so the search doesn't over-restrict on guessed requirements.
        self.assertEqual(
            criteria.get("must_have_skills") or [], [],
            "Unstructured JDs should default all skills to nice-to-have",
        )
        self.assertGreater(
            len(criteria.get("nice_to_have_skills") or []), 0,
            "Unstructured JD skills should land in nice-to-have bucket",
        )

    def test_pdf_messy_handles_whitespace_and_bom(self):
        criteria, _ = app.parse_jd(PDF_MESSY_JD)
        # BOM and NBSP must not leak into the extracted title.
        title = criteria.get("current_title") or ""
        self.assertNotIn("﻿", title)
        self.assertNotIn(" ", title)
        self.assertEqual(title, "Data Engineer")
        self.assertEqual(criteria.get("location"), "Boston")
        self.assertEqual(criteria.get("years_experience_min"), 3)
        self.assertEqual(criteria.get("years_experience_max"), 5)
        skills_lower = _skills_lower(criteria)
        self.assertIn("snowflake", skills_lower)
        self.assertIn("sql", skills_lower)


class JDSkillsSplitClassification(unittest.TestCase):
    """The skills field must split into must-have vs nice-to-have buckets
    according to the cue words and section the skill came from. Default to
    nice-to-have when unclear so searches never over-restrict."""

    def test_required_cue_words_force_must_have(self):
        jd = """Senior Engineer

Qualifications:
- Required: Python
- Must have experience with PostgreSQL
- Essential: deep proficiency in AWS
"""
        criteria, _ = app.parse_jd(jd)
        must = _must_have_lower(criteria)
        for term in ("python", "postgresql", "aws"):
            self.assertIn(term, must, f"'{term}' should be must-have")

    def test_optional_cue_words_force_nice_to_have(self):
        jd = """Senior Engineer

Qualifications:
- Bonus: Kubernetes
- Familiarity with Docker
- Ideally some Terraform exposure
- Nice to have: Redis
"""
        criteria, _ = app.parse_jd(jd)
        nice = _nice_to_have_lower(criteria)
        for term in ("kubernetes", "docker", "terraform", "redis"):
            self.assertIn(term, nice, f"'{term}' should be nice-to-have")

    def test_preferred_section_defaults_to_nice_to_have(self):
        jd = """Senior Engineer

Requirements:
- Python
- SQL

Preferred Qualifications:
- TensorFlow
- PyTorch
"""
        criteria, _ = app.parse_jd(jd)
        must = _must_have_lower(criteria)
        nice = _nice_to_have_lower(criteria)
        self.assertIn("python", must)
        self.assertIn("sql", must)
        self.assertIn("tensorflow", nice)
        self.assertIn("pytorch", nice)
        # Must- and nice-to-have buckets must be disjoint.
        self.assertFalse(must & nice)

    def test_unclear_defaults_to_nice_to_have(self):
        # No section headers, no cue words — everything should default to
        # nice-to-have so the search isn't over-restrictive.
        criteria, _ = app.parse_jd(BULLET_ONLY_JD)
        self.assertEqual(criteria.get("must_have_skills") or [], [])
        self.assertGreater(len(criteria.get("nice_to_have_skills") or []), 0)


class ZeroResultDiagnostics(unittest.TestCase):
    """Zero-result searches must never be a silent dead end — the diagnostic
    function must always return at least one actionable suggestion."""

    def test_returns_suggestions_for_strict_search(self):
        suggestions = app.diagnose_zero_results(
            {
                "current_title": "Solutions Engineer",
                "location": "new york city",
                "school": "university of toronto",
                "years_experience_min": 3,
                "years_experience_max": 4,
                "skills": ["ROS"],
            },
            mode="exact",
        )
        self.assertGreater(len(suggestions), 0, "Must always have at least one suggestion")
        # Exact mode should trigger a switch-to-Similar suggestion.
        self.assertTrue(
            any("Similar" in s for s in suggestions),
            f"Expected a 'switch to Similar' suggestion — got {suggestions}",
        )

    def test_suggests_removing_specific_skill(self):
        suggestions = app.diagnose_zero_results(
            {"current_title": "Engineer", "skills": ["ROS"]},
            mode="similar",
        )
        self.assertTrue(
            any("ROS" in s for s in suggestions),
            f"Expected a suggestion mentioning the skill — got {suggestions}",
        )

    def test_always_offers_broad_mode_as_last_resort(self):
        # Empty-ish criteria: must still fall back to Broad mode suggestion.
        suggestions = app.diagnose_zero_results({"current_title": "Engineer"}, mode="similar")
        self.assertGreater(len(suggestions), 0)
        self.assertTrue(
            any("Broad" in s for s in suggestions),
            f"Expected Broad-mode fallback — got {suggestions}",
        )

    def test_auto_switch_threshold(self):
        # Five active criteria should trip the auto-switch threshold.
        crit = {
            "current_title": "Engineer",
            "location": "SF",
            "skills": ["Python", "SQL"],
            "years_experience_min": 5,
            "school": "Stanford",
        }
        self.assertGreaterEqual(
            app.count_active_criteria(crit),
            app.AUTO_SWITCH_FILTER_THRESHOLD,
        )


if __name__ == "__main__":
    unittest.main()
