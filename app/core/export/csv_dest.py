"""CSV export destination — the first concrete implementation.

Uses csv.writer (QUOTE_MINIMAL) rather than hand-rolling, so commas/quotes in
names and titles are escaped correctly. Phone numbers are prefixed with a
single apostrophe so spreadsheet apps don't read a leading '+' as a formula.
"""

from __future__ import annotations

import csv
import io

from .base import Destination, ExportResult

COLUMNS = [
    "Rank", "Score", "Name", "Current Company", "Current Title", "YoE",
    "Region", "AI Focus", "Target AI Focus", "AI Fit Score", "AI Evidence",
    "Top Skills", "Prior Employers", "LinkedIn URL",
    "Personal Email", "Personal Phone", "Headline", "Rationale", "Flags",
]


def _phone(value: str) -> str:
    value = (value or "").strip()
    return f"'{value}" if value.startswith("+") else value


class CSVDestination(Destination):
    kind = "csv"

    def write(self, candidates: list[dict], meta: dict) -> ExportResult:
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        w.writerow(COLUMNS)
        for i, c in enumerate(candidates, start=1):
            w.writerow([
                i,
                c.get("score", ""),
                c.get("name", ""),
                c.get("current_company", ""),
                c.get("current_title", ""),
                c.get("yoe", ""),
                c.get("region", ""),
                c.get("ai_focus_label", ""),
                c.get("target_ai_focus_label", ""),
                c.get("ai_fit_score", ""),
                c.get("ai_fit_rationale") or ", ".join(c.get("ai_company_evidence", [])[:5]),
                ", ".join(c.get("top_skills", [])[:5]),
                ", ".join(c.get("prior_employers", [])[:3]),
                c.get("linkedin_url", ""),
                c.get("personal_email", ""),
                _phone(c.get("personal_phone", "")),
                c.get("headline", ""),
                c.get("rationale", ""),
                ", ".join(c.get("flags", [])),
            ])
        role = (meta.get("role") or "candidates").strip().replace(" ", "-").lower()
        return ExportResult(
            kind=self.kind,
            filename=f"croot-{role}.csv",
            content=buf.getvalue().encode("utf-8"),
            detail=f"{len(candidates)} candidates",
        )
