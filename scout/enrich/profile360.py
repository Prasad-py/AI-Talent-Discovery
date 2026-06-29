"""
Stage 2: the 360-degree deep view.

When a candidate looks promising from ANY public signal, we investigate them
everywhere and fuse it into one cited profile:

  A) Structured API pulls (hard data): Hugging Face (authored models + downloads),
     Semantic Scholar (papers/citations/hIndex), Stack Overflow (reputation/tags),
     plus the GitHub/OpenRank/Codeforces signals already on the candidate.
  B) Multi-round LLM web research (the long tail): LinkedIn, X, Reddit, Kaggle,
     personal site, blog, YouTube/conference talks, Devpost/Devfolio, news - the model
     decides its own follow-up searches (bounded), so we dig deeper on what we find.
  C) Synthesis: a rich structured 360 profile with a categorized inventory of EVERY
     published link, platform-presence map, contact, identity confidence, and red flags.

Everything is grounded - sources are kept on the dossier.
"""

from __future__ import annotations

from rich.console import Console
from sqlmodel import select

from ..contact.finder import find_contacts
from ..db import session_scope
from ..llm import get_llm
from ..models import Candidate, Dossier, Identity, Signal, Stage
from ..resolve.identity import resolve_from_research
from ..sources import huggingface as hf
from ..sources import scholar, stackexchange

console = Console()

FOLLOWUP_SYSTEM = (
    "You are directing a 360-degree OSINT investigation of a software/AI engineer for "
    "recruiting. Given findings so far, decide if one more targeted web search would "
    "materially improve the picture - e.g. confirm identity, find a missing profile "
    "(LinkedIn/Kaggle/HuggingFace/Reddit/personal site/talks), a notable project, current "
    "role/company, seniority/availability, or contact. Return JSON ONLY: "
    '{ "done": true|false, "next_query": "specific search instruction", "why": "..." }'
)

SYNTH_SYSTEM = (
    "You compile a rigorous 360-degree recruiting dossier from multi-source findings. "
    "Reconcile UNVERIFIED name-matched data (Semantic Scholar, Stack Overflow) against "
    "everything else and DROP matches that aren't clearly the same person. Be accurate; "
    "only state what the findings support. Capture EVERY public link you saw, categorized. "
    "Return JSON ONLY:\n"
    "{\n"
    '  "summary": "concise narrative (5-9 sentences): who they are, what they have built, why notable",\n'
    '  "role": "current role/title or null",\n'
    '  "company": "current company/org or null",\n'
    '  "location": "city, country or null",\n'
    '  "seniority": "student|junior|mid|senior|staff|lead|founder|unknown",\n'
    '  "career_stage": "student|early-career|mid-career|senior|founder|unknown",\n'
    '  "notable_work": ["specific projects/models/papers/products"],\n'
    '  "key_achievements": ["3-5 concrete, impressive accomplishments with specifics/numbers where possible '
    '- e.g. authored a library with X stars/downloads, won/placed in a named hackathon or competition, core '
    'contributor to a well-known project, shipped a product used by N people, published a cited paper"],\n'
    '  "tech_stack": ["..."],\n'
    '  "highlights": ["sharp, specific facts useful for personalized outreach"],\n'
    '  "platform_presence": {"github": "metric/summary or null", "huggingface": "...", "kaggle": "...", "x": "...", "linkedin": "...", "reddit": "...", "scholar": "...", "stackoverflow": "...", "codeforces": "...", "website": "...", "youtube": "..."},\n'
    '  "links_inventory": {"code": ["urls"], "profiles": ["urls"], "writing": ["urls"], "talks": ["urls"], "papers": ["urls"], "social": ["urls"], "other": ["urls"]},\n'
    '  "contact": {"emails": ["..."], "handles": ["..."]},\n'
    '  "identity_confidence": 0.0-1.0,\n'
    '  "red_flags": ["e.g. likely-different-person matches, inactivity, mismatch"]\n'
    "}"
)


def _gather_structured(login: str | None, name: str | None, meta: dict, handles: dict) -> dict:
    """Hard-data API pulls keyed on the candidate's known handles/name."""
    from ..progress import emit

    data: dict = {}

    # Hugging Face - try github login as HF handle (very common overlap).
    hf_handle = handles.get("huggingface") or login
    if hf_handle:
        emit("360", "Fetching authored models from Hugging Face...")
        hf_data = hf.author_models(hf_handle)
        if hf_data.get("exists"):
            data["huggingface"] = hf_data

    # Semantic Scholar - by name (unverified), hinted by company/org.
    if name:
        emit("360", "Checking research publications (Semantic Scholar)...")
        sch = scholar.author_metrics(name, affiliation_hint=meta.get("company") or meta.get("organization"))
        if sch.get("found"):
            data["semantic_scholar"] = sch

    # Stack Overflow - by name (unverified).
    if name:
        emit("360", "Checking Stack Overflow standing...")
        so = stackexchange.user_by_name(name)
        if so.get("found"):
            data["stackoverflow"] = so

    return data


def _candidate_context(snap: dict, idents: list[dict], sigs: list[dict], structured: dict) -> str:
    ident_lines = "\n".join(f"  - {i['platform']}: {i['handle'] or i['url']} (conf {i['confidence']})" for i in idents)
    top_signals = sorted(sigs, key=lambda s: s["weight"], reverse=True)[:10]
    sig_lines = "\n".join(f"  - {s['source']}: {s['title']}" for s in top_signals)
    struct_lines = []
    if "huggingface" in structured:
        h = structured["huggingface"]
        struct_lines.append(f"  - HuggingFace: {h['count']} models, {h['total_downloads']:,} downloads (VERIFIED via handle)")
    if "semantic_scholar" in structured:
        s = structured["semantic_scholar"]
        struct_lines.append(f"  - Semantic Scholar (UNVERIFIED name match): {s['paperCount']} papers, {s['citationCount']} citations, h-index {s['hIndex']}, aff={s.get('affiliations')}")
    if "stackoverflow" in structured:
        s = structured["stackoverflow"]
        struct_lines.append(f"  - StackOverflow (UNVERIFIED name match): rep {s['reputation']}, tags {s.get('top_tags')}")
    return (
        f"Name: {snap.get('name') or '(unknown)'}\n"
        f"GitHub: {snap.get('login')}\n"
        f"Headline/bio: {snap.get('headline')}\n"
        f"Location: {snap.get('location')}\n"
        f"Meta: {snap.get('meta')}\n"
        f"Known profiles:\n{ident_lines or '  (none)'}\n"
        f"Structured API data:\n" + ("\n".join(struct_lines) or "  (none)") + "\n"
        f"Top evidence signals:\n{sig_lines or '  (none)'}"
    )


def profile360(candidate_id: int, max_rounds: int = 4) -> dict:
    llm = get_llm()
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        identities = session.exec(select(Identity).where(Identity.candidate_id == candidate_id)).all()
        signals = session.exec(select(Signal).where(Signal.candidate_id == candidate_id)).all()
        login = candidate.primary_github_login
        name = candidate.name
        snap = {
            "login": login, "name": name, "headline": candidate.headline,
            "location": candidate.location, "meta": dict(candidate.meta or {}),
        }
        idents = [{"platform": i.platform.value, "handle": i.handle, "url": i.url, "confidence": i.confidence} for i in identities]
        sigs = [{"source": s.source, "title": s.title, "weight": s.weight} for s in signals]
        handles = {i["platform"]: i["handle"] for i in idents}

    from ..progress import emit

    console.print(f"[cyan]360 deep view[/cyan] {login or name} ...")
    emit("360", f"Building 360-degree profile for {name or login}...")

    # A) Structured API pulls.
    structured_data = _gather_structured(login, name, snap["meta"], handles)
    context = _candidate_context(snap, idents, sigs, structured_data)

    # B) Multi-round web research.
    seed = (
        "Build a complete 360-degree professional picture of this person for recruiting. "
        "Check EVERY relevant platform: LinkedIn, X/Twitter, GitHub, Hugging Face, Kaggle, "
        "Reddit, Google Scholar, Stack Overflow, personal site/blog, YouTube/conference "
        "talks, Devpost/Devfolio/hackathons, and news. Confirm it is the SAME person, and "
        "collect every public link, their role/company, location, seniority, notable work, "
        "tech stack, and any contact info. Reconcile the UNVERIFIED name-matched data below. "
        "Cite sources.\n\n"
        f"What we already know:\n{context}"
    )
    findings, sources = [], []
    emit("360", "Searching across LinkedIn, X, Kaggle, Reddit, blogs, talks and news...")
    first = llm.research(seed, max_uses=8)
    findings.append(first["text"])
    sources.extend(first.get("sources", []))

    for _ in range(max_rounds):
        accumulated = "\n\n---\n\n".join(findings)
        try:
            decision = llm.complete_json(
                FOLLOWUP_SYSTEM, f"Person: {name or login}\n\nFindings so far:\n{accumulated}"
            )
        except Exception:  # noqa: BLE001
            break
        if not isinstance(decision, dict) or decision.get("done") or not decision.get("next_query"):
            break
        console.print(f"  follow-up: {decision.get('next_query')}")
        emit("360", f"Digging deeper: {decision.get('next_query')}")
        more = llm.research(decision["next_query"], max_uses=6)
        findings.append(more["text"])
        sources.extend(more.get("sources", []))

    # C) Synthesis.
    all_findings = "\n\n---\n\n".join(findings)
    try:
        profile = llm.complete_json(
            SYNTH_SYSTEM,
            f"Person: {name or login} (GitHub {login})\n\n"
            f"Structured API data (JSON): {structured_data}\n\n"
            f"All web findings:\n{all_findings}",
            max_tokens=8000,
        )
    except Exception:  # noqa: BLE001
        profile = {"summary": all_findings[:2000], "links_inventory": {}, "contact": {"emails": []}}

    profile["data_sources"] = structured_data  # keep the hard data on the profile

    # Dedupe sources.
    seen, uniq = set(), []
    for s in sources:
        u = s.get("url")
        if u and u not in seen:
            seen.add(u)
            uniq.append(s)

    with session_scope() as session:
        session.add(Dossier(
            candidate_id=candidate_id,
            summary=profile.get("summary", ""),
            citations=uniq,
            structured=profile,
        ))
        cand = session.get(Candidate, candidate_id)
        if profile.get("location") and not cand.location:
            cand.location = profile.get("location")
        cand.meta = {
            **(cand.meta or {}),
            "company": profile.get("company") or (cand.meta or {}).get("company"),
            "seniority": profile.get("seniority"),
            "career_stage": profile.get("career_stage"),
        }
        cand.stage = Stage.deep_dived

    # Resolve identities from links + findings.
    links = profile.get("links_inventory") or {}
    links_block = "\n".join(f"{cat}: {u}" for cat, urls in links.items() for u in (urls or []))
    resolve_from_research(candidate_id, links_block + "\n\n" + all_findings)

    # Contact discovery.
    emails = (profile.get("contact") or {}).get("emails") or []
    find_contacts(candidate_id, extra_emails=emails)

    return {"profile": profile, "sources": uniq, "rounds": len(findings), "structured": structured_data}


def profile360_top(top: int = 10, max_rounds: int = 4) -> list[int]:
    """Run the 360 deep view on the top candidates by composite score. Returns their ids."""
    from ..config import load_icp

    threshold = float(load_icp().get("thresholds", {}).get("deep_dive", 0.55))
    with session_scope() as session:
        rows = session.exec(
            select(Candidate).where(Candidate.composite_score >= threshold)
            .order_by(Candidate.composite_score.desc()).limit(top)
        ).all()
        if not rows:
            rows = session.exec(
                select(Candidate).order_by(Candidate.composite_score.desc().nullslast()).limit(top)
            ).all()
        ids = [c.id for c in rows]

    from ..progress import emit

    done = []
    for i, cid in enumerate(ids, 1):
        emit("360", f"Building a full 360-degree picture - candidate {i} of {len(ids)}",
             {"current": i, "total": len(ids), "step": "profile"})
        try:
            profile360(cid, max_rounds=max_rounds)
            done.append(cid)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]360 failed for candidate {cid}: {e}[/red]")
    return done
