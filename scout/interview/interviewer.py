"""
AI first-round interview (Stage 7).

This is the integration point for an AI voice/async interviewer. In production this
would call the existing HR interview system (or Ribbon / Vapi). Those are wired as
optional providers: if a provider API key is present we create a real interview link;
otherwise we operate in "transcript" mode - we still generate a tailored question set
and, given a transcript, score it against a rubric exactly like a hosted interviewer.

The output (rubric scores + recommendation) feeds the composite ranking and the
feedback loop.
"""

from __future__ import annotations

import os
from typing import Optional

from rich.console import Console
from sqlmodel import select

from ..config import load_icp
from ..db import session_scope
from ..llm import get_llm
from ..models import Candidate, Dossier, Interview, Stage, advance_stage

console = Console()

QUESTION_SYSTEM = (
    "You are an expert technical interviewer. Design a focused first-round interview for "
    "this specific candidate and role. Probe real depth (not trivia), tailored to their "
    "actual work. Mix: 2 technical-depth, 2 problem-solving/design, 1 'ahead-of-curve' "
    "(recent models/tools), 1 motivation/craft. Return JSON ONLY: "
    '{ "questions": ["...", ...] }'
)

SCORE_SYSTEM = (
    "You are an interview evaluator. Score the transcript against the rubric. Each "
    "dimension is 0-100 and MUST be tied to specific evidence from the transcript. "
    "Return JSON ONLY:\n"
    "{\n"
    '  "technical_knowledge": 0-100,\n'
    '  "problem_solving": 0-100,\n'
    '  "communication": 0-100,\n'
    '  "role_fit": 0-100,\n'
    '  "ahead_of_curve": 0-100,\n'
    '  "recommendation": "Strong Yes|Yes|Maybe|No",\n'
    '  "rationale": "evidence-backed, cite transcript moments"\n'
    "}"
)


def generate_questions(candidate_id: int, n: int = 6) -> list[str]:
    icp = load_icp()
    llm = get_llm()
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        dossier = session.exec(
            select(Dossier)
            .where(Dossier.candidate_id == candidate_id)
            .order_by(Dossier.generated_at.desc())
        ).first()
        ctx = (
            f"Role: {icp.get('role')}\n"
            f"Candidate: {candidate.name or candidate.primary_github_login}\n"
            f"Headline: {candidate.headline}\n"
            f"Deep-dive: {dossier.summary if dossier else '(none)'}\n"
            f"Notable work: {(dossier.structured or {}).get('notable_work') if dossier else None}\n"
            f"Tech stack: {(dossier.structured or {}).get('tech_stack') if dossier else None}"
        )
    try:
        data = llm.complete_json(QUESTION_SYSTEM, f"{ctx}\n\nProduce {n} questions.")
        qs = data.get("questions") if isinstance(data, dict) else data
        return [q for q in (qs or []) if isinstance(q, str)][:n]
    except Exception:  # noqa: BLE001
        return []


def create_remote_interview(candidate_id: int) -> Optional[dict]:
    """
    Best-effort creation of a hosted interview via Ribbon/Vapi if keys exist.
    Returns provider info or None (then use transcript mode). Kept minimal and safe.
    """
    ribbon = os.getenv("RIBBON_API_KEY")
    vapi = os.getenv("VAPI_API_KEY")
    if not (ribbon or vapi):
        return None
    # Placeholder: real wiring would POST to the provider API with the questions.
    questions = generate_questions(candidate_id)
    provider = "ribbon" if ribbon else "vapi"
    with session_scope() as session:
        iv = Interview(candidate_id=candidate_id, provider=provider, questions=questions)
        session.add(iv)
    console.print(
        f"[yellow]{provider} key detected[/yellow] - hook the provider's create-interview "
        "endpoint here to generate a candidate link."
    )
    return {"provider": provider, "questions": questions}


def score_transcript(candidate_id: int, transcript: str, questions: Optional[list] = None) -> dict:
    """Score an interview transcript against the rubric and persist an Interview row."""
    llm = get_llm()
    icp = load_icp()
    user = (
        f"Role: {icp.get('role')}\n"
        f"Questions asked: {questions or '(unspecified)'}\n\n"
        f"TRANSCRIPT:\n{transcript}\n\nScore now."
    )
    try:
        scores = llm.complete_json(SCORE_SYSTEM, user)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]interview scoring failed: {e}[/red]")
        return {}

    dims = ["technical_knowledge", "problem_solving", "communication", "role_fit", "ahead_of_curve"]
    vals = []
    for d in dims:
        try:
            vals.append(max(0.0, min(100.0, float(scores.get(d, 0)))))
        except (TypeError, ValueError):
            vals.append(0.0)
    composite = round(sum(vals) / (len(vals) * 100.0), 4) if vals else 0.0

    with session_scope() as session:
        iv = Interview(
            candidate_id=candidate_id,
            provider="transcript",
            questions=questions or [],
            transcript=transcript,
            rubric_scores={d: v for d, v in zip(dims, vals)},
            composite=composite,
            recommendation=scores.get("recommendation"),
        )
        session.add(iv)
        candidate = session.get(Candidate, candidate_id)
        candidate.stage = advance_stage(candidate.stage, Stage.interviewing)

    return {
        "composite": composite,
        "recommendation": scores.get("recommendation"),
        "rubric_scores": {d: v for d, v in zip(dims, vals)},
        "rationale": scores.get("rationale", ""),
    }
