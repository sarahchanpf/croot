"""Curated company clusters — the authoritative source for common categories.

The source-candidates skill's core move is expanding a category ("top fintech
companies", "big banks") into a concrete set of well-known employers. Letting
the LLM free-form that produced generic / inconsistent picks, so for the common
categories we pin curated lists here and the intake just tags the category
(`cluster_categories`). The LLM still hand-lists companies for niche categories
not covered here.

Names are chosen to resolve cleanly through Crustdata `identify`.
"""

from __future__ import annotations

CURATED_CLUSTERS: dict[str, list[str]] = {
    "faang": ["Meta", "Apple", "Amazon", "Netflix", "Google"],
    "big_tech": ["Google", "Microsoft", "Amazon", "Meta", "Apple", "Nvidia",
                  "Salesforce", "Adobe", "Oracle"],
    "fintech": ["Stripe", "Plaid", "Adyen", "Brex", "Ramp", "Block", "PayPal",
                 "Chime", "Robinhood", "Affirm", "Coinbase", "Klarna", "Marqeta"],
    "big_banks": ["Goldman Sachs", "JPMorgan Chase", "Morgan Stanley",
                   "Bank of America", "Citigroup", "Wells Fargo", "Barclays",
                   "Deutsche Bank", "UBS", "HSBC"],
    "quant_trading": ["Two Sigma", "Citadel", "Jane Street", "Hudson River Trading",
                       "Jump Trading", "D. E. Shaw", "Renaissance Technologies",
                       "Optiver", "Susquehanna International Group", "Five Rings"],
    "consulting": ["McKinsey & Company", "Boston Consulting Group", "Bain & Company",
                    "Deloitte", "PwC", "EY", "KPMG", "Accenture", "Oliver Wyman"],
    "top_startups": ["Stripe", "Airbnb", "Notion", "Figma", "Databricks",
                      "Snowflake", "Canva", "Rippling", "Scale AI", "OpenAI",
                      "Anthropic", "Vercel", "Linear", "Ramp", "Brex"],
    "defense": ["Lockheed Martin", "Raytheon", "Northrop Grumman", "Boeing",
                 "General Dynamics", "Booz Allen Hamilton", "BAE Systems",
                 "L3Harris", "Anduril", "Palantir"],
    "big_pharma": ["Pfizer", "Johnson & Johnson", "Merck", "Novartis", "Roche",
                    "AstraZeneca", "AbbVie", "Eli Lilly", "GSK", "Bristol-Myers Squibb"],
}

CLUSTER_KEYS = tuple(CURATED_CLUSTERS.keys())


def expand(categories) -> list[str]:
    """Expand category keys -> a flat, de-duplicated company-name list."""
    out: list[str] = []
    seen: set[str] = set()
    for key in (categories or []):
        for name in CURATED_CLUSTERS.get(key, []):
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out
