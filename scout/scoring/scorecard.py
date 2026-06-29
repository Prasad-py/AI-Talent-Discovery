"""
Multi-pillar scorecard (the sophisticated metric system).

12 evidence-backed metrics grouped into 5 pillars. Each metric gets a 0-1 score, a
0-1 confidence, and a one-line evidence citation - graded by Claude against the full
360 profile + structured API data. Produces a weighted composite, a recommendation,
and a narrative. Also writes a compact 5-dim ScoreBreakdown for quick ranking.
"""

from __future__ import annotations

from rich.console import Console
from sqlmodel import select

from ..authenticity import classifier as authenticity
from ..config import load_icp
from ..db import session_scope
from ..models import Candidate, Dossier, ScoreBreakdown, Scorecard, Signal, Stage, advance_stage

console = Console()

# metric -> (pillar, default weight, definition)
METRICS: dict[str, tuple[str, float, str]] = {
    "engineering_depth": ("Technical Craft", 0.12, "Depth of real engineering: hard problems, systems, kernels, infra, non-trivial PRs."),
    "code_quality_judgment": ("Technical Craft", 0.08, "Quality of thought: PR/review quality, simplification, testing, architectural reasoning."),
    "open_source_impact": ("Technical Craft", 0.10, "Impact of shipped work: OpenRank, used libraries/models, dependents, real adoption."),
    "ai_domain_depth": ("AI Specialization", 0.12, "Depth specifically in AI/ML/LLMs (training, inference, agents, RL, evals)."),
    "ahead_of_curve": ("AI Specialization", 0.08, "Early hands-on adoption of new models/harnesses/papers; frontier awareness."),
    "shipping_velocity": ("Output & Momentum", 0.08, "How much they ship; cadence, side projects, finishing things."),
    "consistency_momentum": ("Output & Momentum", 0.07, "Sustained, recent, improving activity (not a one-time burst)."),
    "research_depth": ("Knowledge & Communication", 0.06, "Publications/citations/h-index or research-grade understanding (if any)."),
    "communication_writing": ("Knowledge & Communication", 0.06, "Quality of writing/talks/blogs/docs that explain reasoning."),
    "community_standing": ("Knowledge & Communication", 0.05, "Recognized standing: SO rep, Kaggle tier, CF rating, HF downloads, talks."),
    "authenticity": ("Fit", 0.08, "A real human builder (not bot/influencer/recruiter/company)."),
    "hireability": ("Fit", 0.10, "Can we realistically hire now: hungry, early-career, reachable, in target geo (India); LOW for arrived founders/CTOs/FAANG-anchored or out-of-geo."),
}

PILLAR_ORDER = ["Technical Craft", "AI Specialization", "Output & Momentum", "Knowledge & Communication", "Fit"]

LEGACY_MAP = {
    "craft": ["engineering_depth", "code_quality_judgment", "open_source_impact"],
    "hunger": ["shipping_velocity", "consistency_momentum"],
    "ahead_of_curve": ["ai_domain_depth", "ahead_of_curve"],
    "authenticity": ["authenticity"],
    "hireability": ["hireability"],
}


def _system() -> str:
    lines = ["You are a world-class technical hiring evaluator for a lean, hungry AI team "
             "that hires in India first and relocates the best to Dubai. Grade the candidate "
             "on each metric using ONLY the provided evidence. Be calibrated and skeptical; "
             "reward hard-to-fake signals, discount vanity metrics; reconcile unverified "
             "name-matched data. Each metric: score 0.0-1.0, confidence 0.0-1.0 (how sure "
             "given evidence), and a one-line evidence citation.\n\nMETRICS:"]
    for m, (pillar, _w, desc) in METRICS.items():
        lines.append(f"  - {m} [{pillar}]: {desc}")
    lines.append(
        "\nReturn JSON ONLY:\n{\n"
        '  "metrics": { "<metric>": {"score": 0.0-1.0, "confidence": 0.0-1.0, "evidence": "..."}, ... },\n'
        '  "recommendation": "Strong Yes|Yes|Maybe|No",\n'
        '  "narrative": "4-7 sentence verdict: strengths, risks, and why this score"\n'
        "}"
    )
    return "\n".join(lines)


def _weights(icp: dict) -> dict:
    # Priority: weights learned from real outcomes > brief emphasis override > defaults.
    from ..feedback.loop import get_learned_weights

    learned = get_learned_weights() or {}
    override = icp.get("scorecard_weights", {}) or {}
    weights = {m: learned.get(m, override.get(m, METRICS[m][1])) for m in METRICS}
    total = sum(weights.values()) or 1.0
    return {m: w / total for m, w in weights.items()}


def _evidence(candidate: Candidate, signals: list[Signal], dossier: Dossier | None) -> str:
    meta = candidate.meta or {}
    sig_lines = "\n".join(f"  - [{s.source}] {s.title}" for s in sorted(signals, key=lambda x: x.weight, reverse=True)[:15])
    parts = [
        f"GitHub: {candidate.primary_github_login} | Name: {candidate.name}",
        f"Location: {candidate.location} (region: {meta.get('region')}, country: {meta.get('country')})",
        f"Seniority/stage hints: seniority={meta.get('seniority')}, stage={meta.get('career_stage')}, "
        f"company={meta.get('company')}, org={meta.get('organization')}, cf_rating={meta.get('cf_rating')}",
        f"Signals:\n{sig_lines or '  (none)'}",
    ]
    if dossier and dossier.structured:
        st = dossier.structured
        parts.append(f"360 SUMMARY: {dossier.summary}")
        parts.append(f"Notable work: {st.get('notable_work')}")
        parts.append(f"Tech stack: {st.get('tech_stack')}")
        parts.append(f"Platform presence: {st.get('platform_presence')}")
        parts.append(f"Structured data: {st.get('data_sources')}")
        parts.append(f"Highlights: {st.get('highlights')}")
        parts.append(f"Red flags: {st.get('red_flags')}")
    return "\n".join(parts)


def score_candidate(candidate_id: int) -> dict:
    icp = load_icp()
    weights = _weights(icp)
    from ..llm import get_llm

    llm = get_llm()

    from ..progress import emit

    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        signals = session.exec(select(Signal).where(Signal.candidate_id == candidate_id)).all()
        dossier = session.exec(
            select(Dossier).where(Dossier.candidate_id == candidate_id).order_by(Dossier.generated_at.desc())
        ).first()
        stored_auth = authenticity.get_authenticity(session, candidate_id)
        evidence = _evidence(candidate, signals, dossier)
        cand_label = candidate.name or candidate.primary_github_login
    emit("score", f"Scoring {cand_label} against the rubric...")

    geo = icp.get("geo", {})
    user = (
        f"ROLE: {icp.get('role')}\nIDEAL: {icp.get('description')}\n"
        f"TARGET GEOGRAPHY: {geo.get('country')} (areas: {', '.join(geo.get('areas', [])[:12])}). "
        "Out-of-geo => low hireability.\n\n"
        f"CANDIDATE EVIDENCE:\n{evidence}\n\nGrade every metric now."
    )

    try:
        result = llm.complete_json(_system(), user, max_tokens=4000)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]scorecard failed for {candidate_id}: {e}[/red]")
        return {}

    raw_metrics = result.get("metrics", {}) if isinstance(result, dict) else {}
    metrics: dict[str, dict] = {}
    for m in METRICS:
        cell = raw_metrics.get(m, {}) if isinstance(raw_metrics.get(m), dict) else {}
        try:
            score = max(0.0, min(1.0, float(cell.get("score", 0.0))))
        except (TypeError, ValueError):
            score = 0.0
        try:
            conf = max(0.0, min(1.0, float(cell.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        metrics[m] = {"score": score, "confidence": conf, "evidence": cell.get("evidence", "")}

    # Dedicated authenticity stage is authoritative.
    if stored_auth is not None:
        metrics["authenticity"]["score"] = stored_auth
        metrics["authenticity"]["confidence"] = max(metrics["authenticity"]["confidence"], 0.8)

    composite = round(sum(weights[m] * metrics[m]["score"] for m in METRICS), 4)
    confidence = round(sum(weights[m] * metrics[m]["confidence"] for m in METRICS), 4)

    pillars: dict[str, list] = {}
    for m, (pillar, _w, _d) in METRICS.items():
        pillars.setdefault(pillar, []).append(metrics[m]["score"])
    pillars = {p: round(sum(v) / len(v), 4) for p, v in pillars.items()}

    legacy = {dim: round(sum(metrics[k]["score"] for k in keys) / len(keys), 4) for dim, keys in LEGACY_MAP.items()}
    shortlist_threshold = float(icp.get("thresholds", {}).get("shortlist", 0.70))

    with session_scope() as session:
        session.add(Scorecard(
            candidate_id=candidate_id, metrics=metrics, pillars=pillars,
            composite=composite, confidence=confidence,
            recommendation=result.get("recommendation"), narrative=result.get("narrative", ""),
        ))
        session.add(ScoreBreakdown(
            candidate_id=candidate_id, craft=legacy["craft"], hunger=legacy["hunger"],
            ahead_of_curve=legacy["ahead_of_curve"], authenticity=legacy["authenticity"],
            hireability=legacy["hireability"], composite=composite,
            rationale=result.get("narrative", ""),
        ))
        cand = session.get(Candidate, candidate_id)
        cand.composite_score = composite
        target = Stage.shortlisted if composite >= shortlist_threshold else Stage.scored
        cand.stage = advance_stage(cand.stage, target)

    emit("score", f"{cand_label}: {composite:.2f} ({result.get('recommendation','-')})",
         {"composite": composite, "recommendation": result.get("recommendation")})
    return {
        "composite": composite, "confidence": confidence, "pillars": pillars,
        "recommendation": result.get("recommendation"), "metrics": metrics,
        "narrative": result.get("narrative", ""),
    }


_GENERIC_KW = {
    "engineer", "engineers", "india", "developer", "developers", "model", "models",
    "building", "build", "builders", "software", "experience", "strong", "hands",
    "people", "candidate", "candidates", "who", "the", "and", "for", "with", "role",
    "early", "career", "ship", "shipping", "real",
}


def _keyword_tokens(keywords) -> set[str]:
    """Significant domain tokens from the brief's keywords (drops generic recruiting words)."""
    import re

    toks: set[str] = set()
    for phrase in (keywords or []):
        for w in re.findall(r"[a-zA-Z][a-zA-Z\-\+]+", str(phrase).lower()):
            if len(w) >= 2 and w not in _GENERIC_KW:
                toks.add(w)
    return toks


def prerank_ids(limit: int, in_geo_only: bool = False, geo_boost: float = 6.0,
                keywords=None) -> list[int]:
    """
    Cheap, LLM-free triage to choose WHO gets the expensive 360 + scoring, using the hard
    discovery signals we already have (OpenRank, merged PRs, Codeforces rating, HF
    downloads, recent activity).

    Location handling:
      - in_geo_only=True  : only consider candidates located in the target geography.
      - in_geo_only=False : strongly prefer in-geo candidates (geo_boost) but allow a
                            standout out-of-geo person to make the cut if clearly stronger.
    A candidate is "in-geo" when discovery tagged their location as the target country.
    """
    kw = _keyword_tokens(keywords)
    scored: list[tuple[float, int]] = []
    with session_scope() as session:
        for c in session.exec(select(Candidate)).all():
            in_geo = bool((c.meta or {}).get("country"))
            if in_geo_only and not in_geo:
                continue
            sigs = session.exec(select(Signal).where(Signal.candidate_id == c.id)).all()
            h = 0.0
            # Domain relevance: boost candidates whose bio/name matches the brief.
            if kw:
                text = " ".join(filter(None, [
                    c.name, c.headline, c.primary_github_login,
                    str((c.meta or {}).get("company") or ""),
                ])).lower()
                hits = sum(1 for k in kw if k in text)
                h += min(hits, 4) * 1.25
            for s in sigs:
                w = float(s.weight or 0.0)
                if s.source == "openrank":
                    h += min(w, 50) / 50 * 2.0
                elif s.source == "github_pr":
                    h += min(w, 100) / 100 * 2.0
                elif s.source == "codeforces" and s.kind == "rating":
                    h += min(w, 3500) / 3500 * 2.0
                elif s.source == "huggingface":
                    h += min(w, 1_000_000) / 1_000_000 * 2.0
                elif s.source == "github_graphql":
                    h += min(w, 200) / 200 * 1.0
                elif s.source == "github_activity":
                    h += min(w, 50) / 50 * 0.5
            if in_geo and not in_geo_only:
                h += geo_boost  # heavily prefer in-geo; only standout outsiders slip through
            scored.append((h, c.id))
    scored.sort(reverse=True)
    return [cid for _h, cid in scored[:limit]]


def score_ids(ids: list[int]) -> int:
    """Score a specific set of candidates (authenticity assumed already assessed)."""
    for cid in ids:
        score_candidate(cid)
    return len(ids)


def score_all(assess_authenticity: bool = True) -> int:
    if assess_authenticity:
        n = authenticity.assess_all(only_unassessed=True)
        if n:
            console.print(f"  authenticity assessed for {n} new candidates")
    with session_scope() as session:
        with_signals = {s.candidate_id for s in session.exec(select(Signal)).all()}
        ids = [c.id for c in session.exec(select(Candidate)).all() if c.id in with_signals]
    for cid in ids:
        score_candidate(cid)
    return len(ids)
