"""
Role-relevance triage.

Between cheap discovery and the expensive 360, this asks Claude a focused question for
each candidate: *given THIS role, how well does their demonstrated work actually match?*
It reads their bio, top repos (names/topics/descriptions), and contribution signals -
so the 360/score budget is spent on genuinely role-relevant people, not just whoever is
most active in the city. This is the main lever against everyone landing in "Maybe".
"""

from __future__ import annotations

from sqlmodel import select

from ..db import add_signal, session_scope
from ..llm import get_llm
from ..models import Candidate, Signal

SYSTEM = (
    "You screen candidates for a SPECIFIC role. For each candidate, rate role_relevance in "
    "[0,1]: how well their DEMONSTRATED work (repos, bio, contributions) matches the role's "
    "domain, stack, and seniority intent. Reward concrete domain evidence - repo names, "
    "topics, and descriptions, and a bio that match the brief. HEAVILY penalize people whose "
    "work is clearly in a different domain, even if they are strong engineers (e.g. a DevOps "
    "or frontend specialist for an applied-AI role). Be decisive: real matches should score "
    ">=0.7, clear mismatches <=0.3.\n"
    'Return JSON ONLY: {"results": [{"id": <int>, "relevance": 0.0-1.0, "reason": "<short>"}]}'
)


def _chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _context(cand: Candidate, signals: list[Signal]) -> dict:
    meta = cand.meta or {}
    repos = []
    for r in (meta.get("top_repos") or [])[:6]:
        desc = (r.get("description") or "")[:90]
        topics = ", ".join((r.get("topics") or [])[:5])
        repos.append(f"{r.get('name')} [{r.get('language')}, {r.get('stars')}*] {desc}"
                     + (f" (topics: {topics})" if topics else ""))
    sig = [s.title for s in sorted(signals, key=lambda x: x.weight, reverse=True)
           if s.source in ("github_pr", "openrank", "github_graphql", "codeforces")][:4]
    return {
        "id": cand.id,
        "github": cand.primary_github_login,
        "name": cand.name,
        "bio": cand.headline,
        "location": cand.location,
        "company": meta.get("company"),
        "repos": repos,
        "signals": sig,
    }


def role_relevance(ids: list[int], role: str | None, description: str | None,
                   keywords=None, batch_size: int = 8) -> list[tuple[int, float]]:
    """Score role relevance for each id and return ids sorted by relevance (desc)."""
    if not ids:
        return []
    with session_scope() as session:
        contexts = []
        order = {cid: i for i, cid in enumerate(ids)}
        for cid in ids:
            cand = session.get(Candidate, cid)
            if not cand:
                continue
            sigs = session.exec(select(Signal).where(Signal.candidate_id == cid)).all()
            contexts.append(_context(cand, sigs))

    llm = get_llm()
    results: dict[int, tuple[float, str]] = {}
    header = (
        f"ROLE: {role or '(infer from description)'}\n"
        f"DESCRIPTION: {description or ''}\n"
        f"KEY SIGNALS TO MATCH: {', '.join(keywords or [])[:600]}\n\n"
    )
    for batch in _chunks(contexts, batch_size):
        user = header + "CANDIDATES (JSON):\n" + str(batch) + "\n\nScore every candidate by id."
        try:
            data = llm.complete_json(SYSTEM, user, max_tokens=2000)
            for r in (data.get("results", []) if isinstance(data, dict) else []):
                try:
                    results[int(r["id"])] = (max(0.0, min(1.0, float(r.get("relevance", 0)))),
                                             str(r.get("reason", "")))
                except (TypeError, ValueError, KeyError):
                    continue
        except Exception:  # noqa: BLE001
            continue

    # Persist a role_relevance signal and rank.
    with session_scope() as session:
        for cid, (rel, reason) in results.items():
            add_signal(session, cid, source="role_relevance", kind="match",
                       title=f"role relevance {rel:.2f}: {reason}", weight=rel,
                       raw={"relevance": rel, "reason": reason})

    def key(cid):
        rel = results.get(cid, (0.0, ""))[0]
        return (rel, -order.get(cid, 0))  # relevance desc, stable by original order

    ranked = sorted(ids, key=key, reverse=True)
    return [(cid, results.get(cid, (0.0, ""))[0]) for cid in ranked]
