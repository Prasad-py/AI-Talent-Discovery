"""
Identity resolution (Stage 2).

Stitches the same person across platforms (LinkedIn, X, HuggingFace, Kaggle, Scholar,
personal site). GitHub already gives us some links for free (the `twitter_username`
field, the `blog` URL). This module uses an LLM entity-match over web-research findings
to add the rest, each with a confidence score - never asserting a link we can't justify.
"""

from __future__ import annotations

from sqlmodel import select

from ..db import add_identity, session_scope
from ..llm import get_llm
from ..models import Candidate, Identity, Platform

_PLATFORMS = {
    "github": Platform.github,
    "x": Platform.x,
    "twitter": Platform.x,
    "linkedin": Platform.linkedin,
    "reddit": Platform.reddit,
    "huggingface": Platform.huggingface,
    "kaggle": Platform.kaggle,
    "website": Platform.website,
    "scholar": Platform.scholar,
}

EXTRACT_SYSTEM = (
    "You extract a person's verified online profiles from research findings. Only "
    "include a profile if the evidence reasonably ties it to THIS specific person "
    "(same name + corroborating details). Assign a confidence 0-1. Return JSON ONLY:\n"
    '{ "profiles": [ {"platform": "linkedin|x|github|huggingface|kaggle|scholar|reddit|website", '
    '"url": "...", "handle": "...", "confidence": 0.0-1.0, "evidence": "..."} ] }'
)


def resolve_from_research(candidate_id: int, research_text: str) -> int:
    """Extract + persist cross-platform identities from deep-research text."""
    llm = get_llm()
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        ctx = (
            f"Person: {candidate.name or candidate.primary_github_login}\n"
            f"GitHub: {candidate.primary_github_login}\n"
            f"Known location: {candidate.location}\n\n"
            f"Research findings:\n{research_text}"
        )
    try:
        data = llm.complete_json(EXTRACT_SYSTEM, ctx)
    except Exception:  # noqa: BLE001
        return 0

    profiles = data.get("profiles", []) if isinstance(data, dict) else []
    added = 0
    with session_scope() as session:
        for p in profiles:
            platform = _PLATFORMS.get(str(p.get("platform", "")).lower())
            if not platform:
                continue
            url = p.get("url")
            handle = p.get("handle") or url
            if not (url or handle):
                continue
            try:
                conf = float(p.get("confidence", 0.5))
            except (TypeError, ValueError):
                conf = 0.5
            add_identity(
                session,
                candidate_id,
                platform=platform,
                handle=handle,
                url=url,
                confidence=conf,
                raw={"evidence": p.get("evidence"), "source": "deep_dive"},
            )
            added += 1
    return added


def get_identities(candidate_id: int) -> list[Identity]:
    with session_scope() as session:
        return session.exec(
            select(Identity).where(Identity.candidate_id == candidate_id)
        ).all()
