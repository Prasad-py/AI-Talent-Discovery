"""
Outreach drafting (Stage 6).

Research is unambiguous: generic dev outreach gets ~4-8% replies; messages that
reference the person's ACTUAL work get ~15-40% (often 6x). So every draft must open
with a specific, true reference to their work, stay under ~150 words, and make a
low-friction ask (a 15-min chat, not "apply"). We produce a short multi-touch sequence.

Drafts are never sent automatically - they surface in the dashboard for human approval.
"""

from __future__ import annotations

from rich.console import Console
from sqlmodel import select

from ..config import load_icp
from ..db import session_scope
from ..llm import get_llm
from ..models import Candidate, Contact, Dossier, OutreachDraft, Signal, Stage

console = Console()

DRAFT_SYSTEM = (
    "You write outbound recruiting messages to elite engineers. Rules:\n"
    "- Open with a SPECIFIC, TRUE reference to their actual work (a repo, PR, project, "
    "talk). Never generic flattery or anything visible only on a profile headline.\n"
    "- Under 150 words. Plain, peer-to-peer tone. No corporate fluff, no emojis.\n"
    "- One line on what we're building and why it's interesting.\n"
    "- Low-friction ask: a short 15-minute chat, not a formal application.\n"
    "- Produce a 3-step sequence (initial + 2 short follow-ups).\n"
    "Return JSON ONLY:\n"
    "{ \"messages\": [ {\"step\": 1, \"channel\": \"email|x_dm|github\", "
    "\"subject\": \"(email only, <=6 words referencing their work)\", \"body\": \"...\"} ] }"
)


def _channel_for(contacts: list[Contact], identities_have_x: bool) -> str:
    if any(c.type == "email" for c in contacts):
        return "email"
    if identities_have_x:
        return "x_dm"
    return "github"


def draft_for_candidate(candidate_id: int, steps: int = 3) -> int:
    icp = load_icp()
    llm = get_llm()

    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        dossier = session.exec(
            select(Dossier)
            .where(Dossier.candidate_id == candidate_id)
            .order_by(Dossier.generated_at.desc())
        ).first()
        signals = session.exec(select(Signal).where(Signal.candidate_id == candidate_id)).all()
        contacts = session.exec(select(Contact).where(Contact.candidate_id == candidate_id)).all()
        from ..models import Identity, Platform

        has_x = bool(
            session.exec(
                select(Identity).where(
                    Identity.candidate_id == candidate_id, Identity.platform == Platform.x
                )
            ).first()
        )

        # Build the most specific, true context we can.
        highlights = []
        notable = []
        if dossier and dossier.structured:
            highlights = dossier.structured.get("highlights", []) or []
            notable = dossier.structured.get("notable_work", []) or []
        top_signals = [s.title for s in sorted(signals, key=lambda x: x.weight, reverse=True)[:6]]
        context = (
            f"Candidate: {candidate.name or candidate.primary_github_login} "
            f"(GitHub {candidate.primary_github_login})\n"
            f"Headline: {candidate.headline}\n"
            f"Notable work: {notable}\n"
            f"Highlights: {highlights}\n"
            f"Top signals: {top_signals}\n"
            f"Deep-dive summary: {dossier.summary if dossier else '(none)'}"
        )
        default_channel = _channel_for(contacts, has_x)

    user = (
        f"We are hiring for: {icp.get('role')}\n"
        f"About us (one line you may adapt): building a sovereign-intelligence AI team; "
        f"small, elite, craft-obsessed, with serious resources.\n"
        f"Preferred channel: {default_channel}\n\n"
        f"CANDIDATE CONTEXT:\n{context}\n\n"
        f"Write the {steps}-step outreach sequence now."
    )

    try:
        data = llm.complete_json(DRAFT_SYSTEM, user)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]outreach draft failed for {candidate_id}: {e}[/red]")
        return 0

    messages = data.get("messages", []) if isinstance(data, dict) else data
    if not messages:
        return 0

    with session_scope() as session:
        # Replace any prior drafts for a clean re-run.
        for old in session.exec(
            select(OutreachDraft).where(OutreachDraft.candidate_id == candidate_id)
        ).all():
            session.delete(old)
        for m in messages:
            session.add(
                OutreachDraft(
                    candidate_id=candidate_id,
                    channel=m.get("channel", default_channel),
                    subject=m.get("subject"),
                    message=m.get("body", ""),
                    sequence_step=int(m.get("step", 1)),
                    status="draft",
                )
            )
    return len(messages)


def draft_for_shortlist(top: int = 10) -> int:
    """Draft outreach for the top shortlisted candidates."""
    icp = load_icp()
    threshold = float(icp.get("thresholds", {}).get("shortlist", 0.70))
    with session_scope() as session:
        rows = session.exec(
            select(Candidate)
            .where(Candidate.composite_score >= threshold)
            .where(Candidate.stage != Stage.rejected)
            .order_by(Candidate.composite_score.desc())
            .limit(top)
        ).all()
        ids = [c.id for c in rows]

    done = 0
    for cid in ids:
        if draft_for_candidate(cid):
            done += 1
    return done
