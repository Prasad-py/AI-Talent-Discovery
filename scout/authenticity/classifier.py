"""
Authenticity layer (Stage 3).

Decides: is this a REAL human builder, or a bot / influencer / recruiter / company?

Per 2026 research, content-only AI detection is unreliable, so we FUSE:
  (a) an LLM classification of the person's public persona (xPool-style), and
  (b) behavioral features that are hard to fake (GitHub account age, merged PRs,
      sustained real activity, real name/email).

Output: an authenticity sub-score in [0, 1] with reasoning, stored as a Signal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from ..db import add_signal, session_scope
from ..llm import get_llm
from ..models import Candidate, Identity, Platform, Signal
from ..sources import x_source

# How much each account type counts as a "real builder" for our purposes.
TYPE_BASE = {
    "developer": 1.0,
    "unknown": 0.6,
    "influencer": 0.4,
    "recruiter": 0.2,
    "company": 0.15,
    "bot": 0.0,
}

JUDGE_SYSTEM = (
    "You are a skeptical technical talent analyst. Based on the evidence, classify the "
    "person and judge whether they are a REAL human engineer who builds (vs an "
    "influencer, recruiter, company account, or bot/AI-generated account). Return JSON "
    "ONLY:\n"
    "{\n"
    '  "type": "developer|influencer|recruiter|company|bot|unknown",\n'
    '  "confidence": 0-100,\n'
    '  "is_human": true|false,\n'
    '  "reasoning": "1-2 sentences citing concrete evidence"\n'
    "}"
)


def _behavioral(candidate: Candidate, signals: list[Signal]) -> tuple[float, dict]:
    """Hard-to-fake humanness signals from GitHub. Returns (score 0-1, detail)."""
    detail: dict = {}
    score = 0.0

    # Account age (older accounts are far more likely to be genuine humans).
    created = (candidate.meta or {}).get("github_created_at")
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            years = (datetime.now(timezone.utc) - dt).days / 365.0
            detail["account_age_years"] = round(years, 1)
            score += min(years / 4.0, 1.0) * 0.35
        except Exception:  # noqa: BLE001
            pass

    # Merged PRs require maintainer trust loops -> very hard to fake.
    merged = sum(
        (s.raw or {}).get("count", 0) for s in signals if s.source == "github_pr"
    )
    detail["merged_prs"] = merged
    if merged > 0:
        score += min(merged / 20.0, 1.0) * 0.30

    # Sustained real activity.
    pushes = max((s.weight for s in signals if s.source == "github_activity"), default=0.0)
    detail["recent_pushes"] = pushes
    if pushes > 0:
        score += min(pushes / 30.0, 1.0) * 0.20

    # Real name present.
    if candidate.name:
        detail["has_real_name"] = True
        score += 0.15

    return min(score, 1.0), detail


def _llm_classify(candidate: Candidate, signals: list[Signal], x_handle: Optional[str]) -> dict:
    """One cheap Claude judgment from available context (no live web research)."""
    llm = get_llm()

    # If X is enabled and we have a handle, use the xPool-style classifier directly.
    if x_handle and x_source.x_enabled():
        try:
            return x_source.classify_user(x_handle)
        except Exception:  # noqa: BLE001
            pass

    sig_lines = "\n".join(f"- {s.source}: {s.title}" for s in signals[:12])
    user = (
        f"GitHub login: {candidate.primary_github_login}\n"
        f"Name: {candidate.name}\n"
        f"Bio/headline: {candidate.headline}\n"
        f"Location: {candidate.location}\n"
        f"Meta: {candidate.meta}\n"
        f"X handle (if any): @{x_handle}\n"
        f"Evidence signals:\n{sig_lines}\n\n"
        "Classify this person."
    )
    try:
        return llm.complete_json(JUDGE_SYSTEM, user)
    except Exception:  # noqa: BLE001
        return {"type": "unknown", "confidence": 40, "is_human": True, "reasoning": "Low data."}


def assess(candidate_id: int) -> dict:
    """Compute and persist the authenticity sub-score for one candidate."""
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        signals = session.exec(
            select(Signal).where(Signal.candidate_id == candidate_id)
        ).all()
        x_ident = session.exec(
            select(Identity).where(
                Identity.candidate_id == candidate_id, Identity.platform == Platform.x
            )
        ).first()
        x_handle = x_ident.handle if x_ident else None

        behavioral_score, behavioral_detail = _behavioral(candidate, signals)
        cls = _llm_classify(candidate, signals, x_handle)

        type_base = TYPE_BASE.get(str(cls.get("type", "unknown")).lower(), 0.5)
        conf = float(cls.get("confidence", 50)) / 100.0
        # Weight the type judgment by its confidence (never fully zero on low confidence).
        type_score = type_base * (0.5 + 0.5 * conf)

        authenticity = round(0.55 * type_score + 0.45 * behavioral_score, 4)
        if str(cls.get("type")).lower() == "bot":
            authenticity = min(authenticity, 0.2)

        reasoning = cls.get("reasoning", "")
        add_signal(
            session,
            candidate_id,
            source="authenticity",
            kind=str(cls.get("type", "unknown")),
            title=f"authenticity={authenticity:.2f} ({cls.get('type','unknown')})",
            weight=authenticity,
            raw={
                "authenticity": authenticity,
                "type": cls.get("type"),
                "confidence": cls.get("confidence"),
                "is_human": cls.get("is_human"),
                "behavioral": behavioral_detail,
                "behavioral_score": round(behavioral_score, 4),
                "reasoning": reasoning,
            },
        )
        return {
            "authenticity": authenticity,
            "type": cls.get("type"),
            "reasoning": reasoning,
        }


def assess_all(only_unassessed: bool = True) -> int:
    """Run authenticity assessment over candidates that lack it."""
    with session_scope() as session:
        candidates = session.exec(select(Candidate)).all()
        ids = [c.id for c in candidates]
        if only_unassessed:
            done = {
                s.candidate_id
                for s in session.exec(
                    select(Signal).where(Signal.source == "authenticity")
                ).all()
            }
            ids = [i for i in ids if i not in done]

    for cid in ids:
        assess(cid)
    return len(ids)


def get_authenticity(session, candidate_id: int) -> Optional[float]:
    """Read the most recent authenticity score for a candidate, if assessed."""
    sig = session.exec(
        select(Signal)
        .where(Signal.candidate_id == candidate_id, Signal.source == "authenticity")
        .order_by(Signal.captured_at.desc())
    ).first()
    if sig:
        return float((sig.raw or {}).get("authenticity", sig.weight))
    return None
