"""AI-company candidate focus taxonomy and deterministic signals.

The search product needs to distinguish three common AI-company hiring lanes:
research, model engineering, and infrastructure/systems. This module keeps the
lane vocabulary and lightweight evidence extraction in one place so intake,
filters, ranking, cards, and exports all speak the same language.
"""

from __future__ import annotations

import re

AI_FOCUS_AREAS = ("research", "model_engineering", "infrastructure_systems")

AI_FOCUS_LABELS = {
    "research": "Research",
    "model_engineering": "Model Engineering",
    "infrastructure_systems": "Infrastructure and Systems",
}

AI_FOCUS_TITLES = {
    "research": [
        "Research Scientist",
        "Applied Scientist",
        "Research Engineer",
        "AI Researcher",
        "Machine Learning Scientist",
        "Deep Learning Scientist",
        "NLP Scientist",
        "Computer Vision Scientist",
    ],
    "model_engineering": [
        "Machine Learning Engineer",
        "ML Engineer",
        "AI Engineer",
        "Inference Engineer",
        "Model Optimization Engineer",
        "LLM Engineer",
        "Applied ML Engineer",
        "Deep Learning Engineer",
    ],
    "infrastructure_systems": [
        "GPU Engineer",
        "Infrastructure Engineer",
        "ML Infrastructure Engineer",
        "AI Infrastructure Engineer",
        "Cloud Engineer",
        "Distributed Systems Engineer",
        "Platform Engineer",
        "Systems Engineer",
        "Compiler Engineer",
    ],
}

AI_FOCUS_KEYWORDS = {
    "research": [
        "research scientist", "applied scientist", "research engineer",
        "publication", "published", "paper", "conference", "neurips", "icml",
        "iclr", "acl", "cvpr", "arxiv", "phd", "postdoc", "scientist",
        "nlp", "computer vision", "reinforcement learning", "deep learning",
        "foundation model", "transformer", "llm research",
    ],
    "model_engineering": [
        "machine learning engineer", "ml engineer", "ai engineer",
        "inference", "serving", "model optimization", "fine tuning",
        "finetuning", "training", "evaluation", "evals", "rag", "llm",
        "pytorch", "tensorflow", "jax", "transformers", "embedding",
        "feature store", "mlops", "production ml", "model deployment",
    ],
    "infrastructure_systems": [
        "gpu", "cuda", "tensorrt", "triton", "nccl", "kernel",
        "distributed systems", "infrastructure", "platform", "kubernetes",
        "ray", "spark", "slurm", "cloud", "aws", "gcp", "azure",
        "compiler", "storage", "networking", "observability", "terraform",
        "latency", "throughput", "cluster", "hpc",
    ],
}

AI_COMPANY_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "ml", "deep learning",
    "llm", "large language model", "foundation model", "generative ai",
    "nlp", "computer vision", "robotics", "inference", "model serving",
    "pytorch", "tensorflow", "jax", "cuda", "gpu", "mlops",
]

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_focus(value: str | None) -> str:
    key = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "research_science": "research",
        "scientist": "research",
        "applied_science": "research",
        "model_engineer": "model_engineering",
        "ml_engineering": "model_engineering",
        "machine_learning_engineering": "model_engineering",
        "infrastructure": "infrastructure_systems",
        "infra": "infrastructure_systems",
        "systems": "infrastructure_systems",
        "systems_infrastructure": "infrastructure_systems",
    }
    key = aliases.get(key, key)
    return key if key in AI_FOCUS_AREAS else ""


def focus_label(focus: str | None) -> str:
    return AI_FOCUS_LABELS.get(normalize_focus(focus), "")


def title_queries_for_focus(focus: str | None) -> list[str]:
    return list(AI_FOCUS_TITLES.get(normalize_focus(focus), []))


def _text_blob(values) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple, set)):
            parts.extend(str(x) for x in value if x is not None)
    return " ".join(parts).lower()


def _has_keyword(blob: str, keyword: str) -> bool:
    kw = keyword.lower()
    if " " in kw:
        return kw in blob
    return kw in set(_TOKEN_RE.findall(blob))


def _matched_keywords(blob: str, keywords: list[str], limit: int = 4) -> list[str]:
    out = []
    for keyword in keywords:
        if _has_keyword(blob, keyword):
            out.append(keyword)
        if len(out) >= limit:
            break
    return out


def infer_focus_from_criteria(criteria) -> str:
    explicit = normalize_focus(getattr(criteria, "ai_focus", ""))
    if explicit:
        return explicit
    blob = _text_blob([
        getattr(criteria, "title", ""),
        getattr(criteria, "title_variants", []),
        getattr(criteria, "must_have_skills", []),
        getattr(criteria, "nice_to_have_skills", []),
        getattr(criteria, "domain_signals", []),
        getattr(criteria, "career_path_signals", []),
    ])
    if not blob:
        return ""
    # Opt-in: only infer a lane when the search is actually AI-related. Generic
    # infra/cloud terms (Kubernetes, AWS, Spark, "platform") must NOT pull a
    # fintech/backend search into an AI focus — that wrongly added an AI-focus
    # rubric slot and dropped otherwise-perfect scores.
    if not _matched_keywords(blob, AI_COMPANY_KEYWORDS, limit=1):
        return ""
    scored = []
    for focus, keywords in AI_FOCUS_KEYWORDS.items():
        scored.append((len(_matched_keywords(blob, keywords, limit=20)), focus))
    scored.sort(reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else ""


def candidate_blob(cand: dict) -> str:
    return _text_blob([
        cand.get("current_title") or "",
        cand.get("headline") or "",
        cand.get("summary") or "",
        cand.get("titles") or [],
        cand.get("top_skills") or [],
        cand.get("industries") or [],
    ])


def ai_company_evidence(cand: dict) -> list[str]:
    return _matched_keywords(candidate_blob(cand), AI_COMPANY_KEYWORDS, limit=5)


def score_candidate_for_focus(cand: dict, focus: str | None) -> dict:
    focus = normalize_focus(focus)
    if not focus:
        return {"focus": "", "label": "", "fraction": None, "evidence": [], "flags": []}

    current_title = (cand.get("current_title") or "").lower()
    all_titles = _text_blob([cand.get("titles") or [], cand.get("headline") or ""])
    skills = _text_blob([cand.get("top_skills") or []])
    domain = _text_blob([cand.get("summary") or "", cand.get("industries") or []])

    title_hits = _matched_keywords(
        " ".join([current_title, all_titles]),
        [t.lower() for t in AI_FOCUS_TITLES[focus]] + AI_FOCUS_KEYWORDS[focus],
        limit=4,
    )
    skill_hits = _matched_keywords(skills, AI_FOCUS_KEYWORDS[focus], limit=4)
    domain_hits = _matched_keywords(domain, AI_FOCUS_KEYWORDS[focus], limit=4)

    title_score = 1.0 if any(hit in current_title for hit in title_hits) else (0.6 if title_hits else 0.0)
    skill_score = min(1.0, len(skill_hits) / 2) if skills else 0.0
    domain_score = min(1.0, len(domain_hits) / 2) if domain else 0.0
    fraction = (0.5 * title_score) + (0.35 * skill_score) + (0.15 * domain_score)

    evidence = []
    for label, hits in (("title", title_hits), ("skills", skill_hits), ("profile", domain_hits)):
        if hits:
            evidence.append(f"{label}: {', '.join(hits[:3])}")
    flags = []
    classified = classify_candidate(cand)
    if classified["focus"] and classified["focus"] != focus and classified["confidence"] >= 0.45:
        flags.append(f"leans {classified['label']}")
    if not evidence:
        flags.append("limited AI-lane evidence")
    return {
        "focus": focus,
        "label": AI_FOCUS_LABELS[focus],
        "fraction": max(0.0, min(1.0, fraction)),
        "evidence": evidence,
        "flags": flags[:2],
    }


def classify_candidate(cand: dict) -> dict:
    scored = []
    for focus in AI_FOCUS_AREAS:
        detail = score_candidate_for_focus_without_classification(cand, focus)
        scored.append((detail["fraction"], focus, detail["evidence"]))
    scored.sort(reverse=True)
    fraction, focus, evidence = scored[0]
    if fraction <= 0:
        return {"focus": "", "label": "", "confidence": 0, "evidence": []}
    return {
        "focus": focus,
        "label": AI_FOCUS_LABELS[focus],
        "confidence": round(fraction, 2),
        "evidence": evidence[:3],
    }


def score_candidate_for_focus_without_classification(cand: dict, focus: str) -> dict:
    focus = normalize_focus(focus)
    current_title = (cand.get("current_title") or "").lower()
    all_titles = _text_blob([cand.get("titles") or [], cand.get("headline") or ""])
    skills = _text_blob([cand.get("top_skills") or []])
    domain = _text_blob([cand.get("summary") or "", cand.get("industries") or []])
    title_hits = _matched_keywords(
        " ".join([current_title, all_titles]),
        [t.lower() for t in AI_FOCUS_TITLES[focus]] + AI_FOCUS_KEYWORDS[focus],
        limit=4,
    )
    skill_hits = _matched_keywords(skills, AI_FOCUS_KEYWORDS[focus], limit=4)
    domain_hits = _matched_keywords(domain, AI_FOCUS_KEYWORDS[focus], limit=4)
    title_score = 1.0 if any(hit in current_title for hit in title_hits) else (0.6 if title_hits else 0.0)
    skill_score = min(1.0, len(skill_hits) / 2) if skills else 0.0
    domain_score = min(1.0, len(domain_hits) / 2) if domain else 0.0
    evidence = []
    for label, hits in (("title", title_hits), ("skills", skill_hits), ("profile", domain_hits)):
        if hits:
            evidence.append(f"{label}: {', '.join(hits[:3])}")
    return {
        "focus": focus,
        "label": AI_FOCUS_LABELS[focus],
        "fraction": max(0.0, min(1.0, (0.5 * title_score) + (0.35 * skill_score) + (0.15 * domain_score))),
        "evidence": evidence,
    }
