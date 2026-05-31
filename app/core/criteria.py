"""The Criteria contract.

Every input path (chat intake, JD/URL extraction, manual form edits) produces a
`Criteria` and every downstream step (filter building, ranking, summaries)
consumes one. This is the single shared shape — keep it stable.

Field set is ported from the `source-candidates` skill's parsed-criteria block.
Two rules carried over from the skill:
  * YoE always carries BOTH a floor and a ceiling (never an open "N+").
  * `tenure_floor_months` defaults to 6 (drops the just-joined "honeymoon"
    cohort); set to None only when recent joiners are explicitly allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

# Anchor strategy: how we narrow the pool to a cluster of relevant people.
# Mirrors the skill's Step-0 decision — pick exactly one.
ANCHOR_STRATEGIES = ("companies", "industries", "both", "none")

DEFAULT_TENURE_FLOOR_MONTHS = 6


@dataclass
class EducationSignals:
    majors: list[str] = field(default_factory=list)
    schools: list[str] = field(default_factory=list)   # autocompleted enum values
    degrees: list[str] = field(default_factory=list)


@dataclass
class Criteria:
    # --- Identity ---
    title: str = ""
    title_variants: list[str] = field(default_factory=list)   # ["backend engineer", "swe"]
    seniority: str = ""
    yoe_min: Optional[int] = None
    yoe_max: Optional[int] = None                              # always set alongside yoe_min

    # --- Location (region, not company HQ) ---
    location: str = ""                                         # city or country name
    location_country: str = ""                                 # set instead of `location` for country-wide
    remote_ok: bool = False                                    # if True, geo clause is skipped

    # --- Skills ---
    must_have_skills: list[str] = field(default_factory=list)  # become hard filter clauses
    nice_to_have_skills: list[str] = field(default_factory=list)  # ranking-only, never filtered

    # --- Domain / education / career signals ---
    domain_signals: list[str] = field(default_factory=list)    # industries / sub-specialties
    education: EducationSignals = field(default_factory=EducationSignals)
    career_path_signals: list[str] = field(default_factory=list)  # prose flags used at ranking

    # --- Anchoring ("they came from...") ---
    anchor_strategy: str = "none"                              # one of ANCHOR_STRATEGIES
    anchor_companies: list[str] = field(default_factory=list)  # resolved to company_ids later
    anchor_industries: list[str] = field(default_factory=list) # autocompleted enum values
    cluster_categories: list[str] = field(default_factory=list)  # curated category keys → companies (see clusters.py)

    # --- Exclusions ---
    exclude_employers: list[str] = field(default_factory=list)
    title_excludes: list[str] = field(default_factory=list)    # local post-filter, not a Crustdata clause

    # --- Stability / dedup ---
    tenure_floor_months: Optional[int] = DEFAULT_TENURE_FLOOR_MONTHS
    hiring_company: str = ""                                    # current employer to dedup OUT

    def is_empty(self) -> bool:
        """True when there's nothing to search on — guards /api/search."""
        return not any([
            self.title, self.title_variants, self.location, self.location_country,
            self.must_have_skills, self.domain_signals, self.anchor_companies,
            self.anchor_industries, self.education.schools, self.education.majors,
            self.seniority, self.yoe_min, self.yoe_max,
        ])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Criteria":
        data = dict(data or {})
        edu = data.pop("education", None) or {}
        if isinstance(edu, EducationSignals):
            edu = asdict(edu)
        known = {f for f in cls.__dataclass_fields__ if f != "education"}
        clean = {k: v for k, v in data.items() if k in known}
        c = cls(**clean)
        c.education = EducationSignals(
            majors=list(edu.get("majors", []) or []),
            schools=list(edu.get("schools", []) or []),
            degrees=list(edu.get("degrees", []) or []),
        )
        return c
