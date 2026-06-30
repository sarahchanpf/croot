"""Opt-in profile enrichment.

GET /api/profile?linkedin_url=...  -> {profiles: [enriched profile]}
GET /api/profile?linkedin_url=...&full=true  -> {profiles, full_profile}

This is the expensive Crustdata call (~4 cr/profile), so it's gated behind an
explicit user action and cached 30 days (per-URL, inside crustdata.enrich).
"""

from flask import Blueprint, jsonify, request

from ..core import crustdata
from ..core.crustdata import CrustdataError

bp = Blueprint("profile", __name__)


def _first(profile: dict, *keys, default=""):
    for key in keys:
        value = profile.get(key)
        if value not in (None, "", []):
            return value
    return default


def _url_from(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _first(value, "url", "profile_url", "html_url", "link", "href")
    return ""


def _list(value) -> list:
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def _employer_row(emp: dict) -> dict:
    if not isinstance(emp, dict):
        return {}
    return {
        "title": _first(emp, "title", "employee_title"),
        "company": _first(emp, "name", "employer_name"),
        "start_date": emp.get("start_date") or "",
        "end_date": emp.get("end_date") or "",
        "seniority": emp.get("seniority_level") or "",
        "industry": emp.get("company_industry") or emp.get("company_industries") or "",
    }


def _education_row(edu: dict) -> dict:
    if not isinstance(edu, dict):
        return {}
    return {
        "school": _first(edu, "institute_name", "school"),
        "degree": edu.get("degree_name") or edu.get("degree") or "",
        "field": edu.get("field_of_study") or edu.get("field") or "",
        "start_date": edu.get("start_date") or "",
        "end_date": edu.get("end_date") or "",
    }


def _article_row(item) -> dict:
    if isinstance(item, str):
        return {"title": item, "url": "", "venue": "", "year": ""}
    if not isinstance(item, dict):
        return {}
    return {
        "title": _first(item, "title", "name", "headline"),
        "url": _first(item, "url", "link", "href"),
        "venue": _first(item, "venue", "publisher", "journal", "conference"),
        "year": str(_first(item, "year", "published_year", "publication_year")),
    }


def _repo_row(item) -> dict:
    if isinstance(item, str):
        return {"name": item, "url": "", "description": ""}
    if not isinstance(item, dict):
        return {}
    return {
        "name": _first(item, "name", "repo_name", "title"),
        "url": _first(item, "url", "html_url", "link"),
        "description": _first(item, "description", "summary"),
    }


def _full_profile(profile: dict) -> dict:
    contact = profile.get("personal_contact_info") or {}
    github = _first(profile, "github_profile_url", "github_url")
    if not github:
        github = _url_from(profile.get("github"))
    scholar = _first(profile, "google_scholar_url", "scholar_url")
    if not scholar:
        scholar = _url_from(profile.get("google_scholar"))
    articles = []
    for key in ("publications", "articles", "research_articles"):
        articles += [_article_row(item) for item in _list(profile.get(key))]
    repos = []
    for key in ("github_repositories", "repositories", "projects"):
        repos += [_repo_row(item) for item in _list(profile.get(key))]

    return {
        "name": _first(profile, "name", "full_name"),
        "headline": profile.get("headline") or "",
        "summary": profile.get("summary") or "",
        "location": _first(profile, "location", "region"),
        "linkedin_url": _first(profile, "linkedin_profile_url", "linkedin_flagship_url"),
        "github_url": github,
        "scholar_url": scholar,
        "profile_picture_url": profile.get("profile_picture_url") or "",
        "connections": profile.get("num_of_connections") or "",
        "skills": [s for s in _list(profile.get("skills")) if isinstance(s, str)][:20],
        "languages": [s for s in _list(profile.get("languages")) if isinstance(s, str)][:10],
        "current_employers": [_employer_row(e) for e in _list(profile.get("current_employers"))][:5],
        "past_employers": [_employer_row(e) for e in _list(profile.get("past_employers"))][:8],
        "education": [_education_row(e) for e in _list(profile.get("education_background"))][:5],
        "certifications": _list(profile.get("certifications"))[:8],
        "honors": _list(profile.get("honors"))[:8],
        "github_repositories": [r for r in repos if r.get("name") or r.get("url")][:8],
        "scholar_articles": [a for a in articles if a.get("title") or a.get("url")][:10],
        "personal_emails": contact.get("personal_emails") or [],
        "phone_numbers": contact.get("phone_numbers") or [],
    }


@bp.route("/api/profile")
def profile():
    url = (request.args.get("linkedin_url") or "").strip()
    if not url:
        return jsonify({"error": "linkedin_url is required."}), 400
    full = (request.args.get("full") or "").lower() in ("1", "true", "yes")
    try:
        data = crustdata.enrich([url], include_contact=True, include_full=full)
    except CrustdataError as exc:
        return jsonify({"error": str(exc)}), exc.status
    if not data["profiles"]:
        return jsonify({"error": "Profile not found."}), 404
    if full:
        data["full_profile"] = _full_profile(data["profiles"][0])
    return jsonify(data)
