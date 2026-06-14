"""Multi-country region → country list, for region-level location filters.

Crustdata filters on a single `location_country` (full name) per value, but a
brief like "engineers in Europe" spans many countries. Intake picks a canonical
region key; the filter builder expands it to `location_country in [countries]`
and the ranker scores against the same set. Country names MUST match Crustdata's
`location_country` values (full names, e.g. "United Kingdom", not "UK").
"""

from __future__ import annotations

REGION_COUNTRIES: dict[str, list[str]] = {
    "europe": [
        "United Kingdom", "Ireland", "Germany", "France", "Netherlands", "Belgium",
        "Luxembourg", "Spain", "Portugal", "Italy", "Switzerland", "Austria",
        "Sweden", "Norway", "Denmark", "Finland", "Iceland", "Poland", "Czechia",
        "Romania", "Hungary", "Greece", "Ukraine", "Estonia", "Lithuania", "Latvia",
        "Bulgaria", "Croatia", "Slovakia", "Slovenia", "Serbia",
    ],
    "uk_ireland": ["United Kingdom", "Ireland"],
    "dach": ["Germany", "Austria", "Switzerland"],
    "benelux": ["Belgium", "Netherlands", "Luxembourg"],
    "nordics": ["Sweden", "Norway", "Denmark", "Finland", "Iceland"],
    "north_america": ["United States", "Canada", "Mexico"],
    "latam": [
        "Mexico", "Brazil", "Argentina", "Chile", "Colombia", "Peru", "Uruguay",
        "Costa Rica", "Ecuador", "Panama", "Guatemala",
    ],
    "apac": [
        "India", "China", "Japan", "South Korea", "Singapore", "Australia",
        "New Zealand", "Indonesia", "Malaysia", "Thailand", "Vietnam",
        "Philippines", "Hong Kong", "Taiwan",
    ],
    "southeast_asia": [
        "Singapore", "Indonesia", "Malaysia", "Thailand", "Vietnam", "Philippines",
    ],
    "south_asia": ["India", "Pakistan", "Bangladesh", "Sri Lanka", "Nepal"],
    "middle_east": [
        "United Arab Emirates", "Israel", "Saudi Arabia", "Qatar", "Turkey",
        "Jordan", "Lebanon", "Bahrain", "Kuwait", "Oman",
    ],
    "mena": [
        "United Arab Emirates", "Israel", "Saudi Arabia", "Qatar", "Turkey",
        "Jordan", "Lebanon", "Bahrain", "Kuwait", "Oman", "Egypt", "Morocco",
        "Tunisia", "Algeria",
    ],
    "africa": [
        "Nigeria", "Kenya", "South Africa", "Egypt", "Ghana", "Morocco",
        "Ethiopia", "Tanzania", "Uganda", "Rwanda",
    ],
    "oceania": ["Australia", "New Zealand"],
}

# Common phrasings → canonical key (matched on a normalized form: lowercased,
# spaces/hyphens → underscore).
_ALIASES: dict[str, str] = {
    "eu": "europe", "european_union": "europe", "emea": "europe",
    "uk": "uk_ireland", "britain": "uk_ireland", "british_isles": "uk_ireland",
    "scandinavia": "nordics",
    "asia_pacific": "apac", "asia": "apac",
    "sea": "southeast_asia", "se_asia": "southeast_asia",
    "indian_subcontinent": "south_asia",
    "gulf": "middle_east", "gcc": "middle_east",
    "latin_america": "latam", "south_america": "latam",
    "anz": "oceania", "australia_new_zealand": "oceania",
    "namerica": "north_america",
}


def _normalize(region: str) -> str:
    return (region or "").strip().lower().replace("-", "_").replace(" ", "_")


def countries_for(region: str) -> list[str]:
    """Country list for a region key or common alias; [] if unrecognized."""
    key = _normalize(region)
    if not key:
        return []
    key = _ALIASES.get(key, key)
    return REGION_COUNTRIES.get(key, [])
