"""
SQLModel entities for the talent pipeline.

Design principle (from the plan): everything is a continuous 0-1 score with
attached evidence. Raw signals are kept verbatim so a human can always trace
*why* a candidate was ranked where they are.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Stage(str, enum.Enum):
    """Where a candidate is in the funnel (Kanban columns)."""

    discovered = "discovered"
    scored = "scored"
    deep_dived = "deep_dived"
    shortlisted = "shortlisted"
    contacted = "contacted"
    replied = "replied"
    interviewing = "interviewing"
    rejected = "rejected"
    hired = "hired"


# Forward order of funnel stages (used to only ever advance, never regress).
STAGE_ORDER = [
    "discovered",
    "scored",
    "deep_dived",
    "shortlisted",
    "contacted",
    "replied",
    "interviewing",
    "hired",
]


def advance_stage(current: "Stage", target: "Stage") -> "Stage":
    """Return whichever stage is further along (rejected is terminal/explicit)."""
    if current == Stage.rejected:
        return current
    try:
        if STAGE_ORDER.index(target.value) > STAGE_ORDER.index(current.value):
            return target
    except ValueError:
        pass
    return current


class Platform(str, enum.Enum):
    github = "github"
    x = "x"
    linkedin = "linkedin"
    reddit = "reddit"
    huggingface = "huggingface"
    kaggle = "kaggle"
    website = "website"
    scholar = "scholar"
    codeforces = "codeforces"
    peerlist = "peerlist"
    devfolio = "devfolio"
    other = "other"


class Candidate(SQLModel, table=True):
    __tablename__ = "candidate"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None

    # Canonical handle used for dedup / linking back to the discovery source.
    primary_github_login: Optional[str] = Field(default=None, index=True, unique=True)

    stage: Stage = Field(default=Stage.discovered, index=True)
    composite_score: Optional[float] = Field(default=None, index=True)

    # Free-form provenance, e.g. {"seed_repo": "vllm-project/vllm"}
    meta: dict = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Identity(SQLModel, table=True):
    __tablename__ = "identity"
    __table_args__ = (UniqueConstraint("candidate_id", "platform", "handle", name="uq_identity"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True)
    platform: Platform = Field(index=True)
    handle: Optional[str] = None
    url: Optional[str] = None
    confidence: float = 1.0  # 0-1 likelihood this account belongs to the candidate
    raw: dict = Field(default_factory=dict, sa_column=Column(JSON))


class Signal(SQLModel, table=True):
    """A single piece of evidence: a merged PR, a tweet, a HF model, a forum answer."""

    __tablename__ = "signal"

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True)
    source: str = Field(index=True)  # e.g. github_pr, github_review, openrank, x_tweet
    kind: Optional[str] = None       # finer-grained type
    title: Optional[str] = None
    snippet: Optional[str] = None
    url: Optional[str] = None
    weight: float = 0.0              # contribution this signal lends to scoring
    captured_at: datetime = Field(default_factory=utcnow)
    raw: dict = Field(default_factory=dict, sa_column=Column(JSON))


class Dossier(SQLModel, table=True):
    """LLM deep-research output: a narrative summary with per-claim citations."""

    __tablename__ = "dossier"

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True)
    summary: str = ""
    citations: list = Field(default_factory=list, sa_column=Column(JSON))
    structured: dict = Field(default_factory=dict, sa_column=Column(JSON))
    generated_at: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    __tablename__ = "contact"

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True)
    type: str = "email"  # email | handle | site | phone
    value: str = ""
    source: Optional[str] = None
    confidence: float = 0.5


class ScoreBreakdown(SQLModel, table=True):
    __tablename__ = "score_breakdown"

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True)
    craft: float = 0.0
    hunger: float = 0.0
    ahead_of_curve: float = 0.0
    authenticity: float = 0.0
    hireability: float = 0.0  # hungry + early-career + reachable + in target geo
    composite: float = 0.0
    rationale: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class OutreachDraft(SQLModel, table=True):
    __tablename__ = "outreach_draft"

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True)
    channel: str = "email"  # email | x_dm | github
    subject: Optional[str] = None
    message: str = ""
    sequence_step: int = 1
    status: str = "draft"  # draft | approved | sent
    created_at: datetime = Field(default_factory=utcnow)


class Scorecard(SQLModel, table=True):
    """Rich multi-pillar evaluation (Stage 2 output) used by the professional report."""

    __tablename__ = "scorecard"

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True)
    metrics: dict = Field(default_factory=dict, sa_column=Column(JSON))   # {metric: {score, confidence, evidence}}
    pillars: dict = Field(default_factory=dict, sa_column=Column(JSON))   # {pillar: score}
    composite: float = 0.0
    confidence: float = 0.0
    recommendation: Optional[str] = None  # Strong Yes | Yes | Maybe | No
    narrative: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Interview(SQLModel, table=True):
    """AI first-round interview record (Stage 7)."""

    __tablename__ = "interview"

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True)
    provider: str = "transcript"  # transcript | ribbon | vapi
    external_id: Optional[str] = None      # interview/link id from a provider
    questions: list = Field(default_factory=list, sa_column=Column(JSON))
    transcript: Optional[str] = None
    rubric_scores: dict = Field(default_factory=dict, sa_column=Column(JSON))
    composite: Optional[float] = None       # 0-1
    recommendation: Optional[str] = None    # Strong Yes | Yes | Maybe | No
    created_at: datetime = Field(default_factory=utcnow)


class Outcome(SQLModel, table=True):
    """Final human decision per candidate - fuel for the feedback loop."""

    __tablename__ = "outcome"

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True, unique=True)
    label: str = "unknown"  # advanced | hired | rejected | unknown
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
