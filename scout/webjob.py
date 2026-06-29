"""
Web job runner: drives the full two-stage pipeline for a UI request and emits live
progress, then assembles a rich results payload for the browser.

Stage 1 (breadth): understand the brief -> multi-source discovery from public signals.
Stage 2 (depth):   360-degree profile of the top candidates -> multi-metric scorecard.
"""

from __future__ import annotations

from sqlmodel import SQLModel, select

from . import progress
from .config import clear_overrides, set_model, set_overrides
from .db import get_engine, init_db, session_scope
from .intake import build_plan, plan_to_overrides
from .models import Candidate, Contact, Dossier, Identity, Scorecard, Signal
from .scoring.scorecard import METRICS, PILLAR_ORDER


def _prerank_ids(limit: int) -> list[int]:
    """
    Cheap, LLM-free pre-rank so we only spend the expensive scorecard on the most
    promising slice of the pool (keeps runs responsive when sources are broad).
    """
    from sqlmodel import select

    scored: list[tuple[float, int]] = []
    with session_scope() as session:
        cands = session.exec(select(Candidate)).all()
        for c in cands:
            sigs = session.exec(select(Signal).where(Signal.candidate_id == c.id)).all()
            h = 0.0
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
            if (c.meta or {}).get("country"):
                h += 0.5  # in target geography
            scored.append((h, c.id))
    scored.sort(reverse=True)
    return [cid for _h, cid in scored[:limit]]


def reset_db() -> None:
    """Wipe and recreate all tables (fresh run)."""
    engine = get_engine()
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)


def build_results(top: int = 10) -> dict:
    """Assemble ranked candidates + scorecards + 360 data for the UI."""
    cands_out = []
    with session_scope() as session:
        cands = session.exec(
            select(Candidate).where(Candidate.composite_score.is_not(None))
            .order_by(Candidate.composite_score.desc()).limit(top)
        ).all()
        for rank, c in enumerate(cands, 1):
            sc = session.exec(
                select(Scorecard).where(Scorecard.candidate_id == c.id).order_by(Scorecard.created_at.desc())
            ).first()
            dossier = session.exec(
                select(Dossier).where(Dossier.candidate_id == c.id).order_by(Dossier.generated_at.desc())
            ).first()
            idents = session.exec(select(Identity).where(Identity.candidate_id == c.id)).all()
            contacts = session.exec(select(Contact).where(Contact.candidate_id == c.id)).all()
            st = (dossier.structured if dossier else {}) or {}
            meta = c.meta or {}

            metrics_out = []
            if sc and sc.metrics:
                for m, (pillar, _w, desc) in METRICS.items():
                    cell = sc.metrics.get(m, {})
                    metrics_out.append({
                        "metric": m.replace("_", " "),
                        "pillar": pillar,
                        "score": round(float(cell.get("score", 0.0)), 2),
                        "confidence": round(float(cell.get("confidence", 0.0)), 2),
                        "evidence": cell.get("evidence", ""),
                        "definition": desc,
                    })

            cands_out.append({
                "rank": rank,
                "name": c.name or c.primary_github_login,
                "github": c.primary_github_login,
                "headline": c.headline or st.get("role"),
                "location": c.location,
                "region": meta.get("region"),
                "role": st.get("role"),
                "company": st.get("company") or meta.get("company"),
                "seniority": st.get("seniority") or meta.get("seniority"),
                "career_stage": st.get("career_stage") or meta.get("career_stage"),
                "composite": round(float(sc.composite if sc else (c.composite_score or 0.0)), 3),
                "confidence": round(float(sc.confidence if sc else 0.0), 2),
                "recommendation": sc.recommendation if sc else None,
                "narrative": sc.narrative if sc else None,
                "pillars": [{"name": p, "score": round(float((sc.pillars or {}).get(p, 0.0)), 2)} for p in PILLAR_ORDER] if sc else [],
                "metrics": metrics_out,
                "summary": dossier.summary if dossier else "",
                "notable_work": st.get("notable_work") or [],
                "tech_stack": st.get("tech_stack") or [],
                "highlights": st.get("highlights") or [],
                "red_flags": st.get("red_flags") or [],
                "platform_presence": {k: v for k, v in (st.get("platform_presence") or {}).items() if v},
                "links_inventory": st.get("links_inventory") or {},
                "identities": [{"platform": i.platform.value, "url": i.url or i.handle, "confidence": round(i.confidence, 2)} for i in idents if (i.url or i.handle)],
                "contacts": [{"value": ct.value, "source": ct.source, "confidence": round(ct.confidence, 2)} for ct in contacts],
                "sources_count": len(dossier.citations) if dossier else 0,
            })
    return {"candidates": cands_out, "metric_legend": [
        {"metric": m.replace("_", " "), "pillar": p, "definition": d} for m, (p, _w, d) in METRICS.items()
    ]}


def run_job(job_id: str, params: dict) -> None:
    """Entry point for the background worker thread."""
    progress.bind(job_id)
    try:
        top = int(params.get("top", 8))
        max_rounds = int(params.get("max_rounds", 3))
        user_sources = params.get("sources", {}) or {}
        auto_sources = params.get("auto_sources", True)

        set_model(params.get("model"))
        init_db()
        if params.get("fresh"):
            progress.emit("setup", "Starting a fresh run (clearing previous pool)...")
            reset_db()

        # Stage 0: understand the brief.
        progress.emit("stage", "Stage 1 - Understanding your brief", level="stage")
        plan = build_plan(
            description=params.get("description", ""),
            location=params.get("location", ""),
            role=params.get("role"),
            notes=params.get("notes"),
        )
        overrides = plan_to_overrides(plan)
        set_overrides(overrides)

        # Agentic source selection: by default follow the plan's recommendation; a user
        # can force sources on by ticking boxes (those are unioned in).
        if auto_sources:
            sources = dict(plan.get("sources") or {})
            sources.setdefault("github_geo", bool((plan.get("geo") or {}).get("areas")))
            for k, v in user_sources.items():
                if v:
                    sources[k] = True
        else:
            sources = user_sources
        progress.emit("plan", f"Active sources: {', '.join(k for k,v in sources.items() if v) or 'github_geo'}")

        # Stage 1: multi-source discovery.
        progress.emit("stage", "Stage 1 - Discovering public signals", level="stage")
        from .sources.discover import run_geo_discovery, run_discovery
        from .sources.codeforces import ingest_india

        counts = {}
        if sources.get("github_geo", True):
            counts["github_geo"] = run_geo_discovery(
                max_per_query=int(params.get("max_per_query", 2)),
                limit_areas=int(params["limit_areas"]) if params.get("limit_areas") else None,
            )
        if sources.get("codeforces", True):
            counts["codeforces"] = ingest_india()
        if sources.get("repos"):
            counts["repos"] = run_discovery(
                limit_repos=int(params.get("limit_repos", 2)),
                max_per_repo=int(params.get("repo_contributors", 12)),
            )
        if sources.get("huggingface"):
            from .sources.huggingface import ingest_trending
            counts["huggingface"] = ingest_trending(limit=20)
        if sources.get("publications"):
            from .sources.scholar import ingest_topic_authors
            counts["publications"] = ingest_topic_authors()
        progress.emit("discover", f"Discovery complete: {sum(counts.values())} candidates.", {"counts": counts})

        # Stage 1.5: rank the pool (cap the expensive scorecard to the most promising slice).
        max_pool = int(params.get("max_pool", 30))
        from .authenticity import classifier as authenticity
        from .scoring.scorecard import score_ids

        pool_ids = _prerank_ids(max_pool)
        progress.emit("stage", f"Stage 1.5 - Scoring the top {len(pool_ids)} of the pool", level="stage")
        progress.emit("score", f"Pre-ranked the pool; assessing authenticity for {len(pool_ids)} candidates...")
        for cid in pool_ids:
            authenticity.assess(cid)
        score_ids(pool_ids)

        # Stage 2: 360 deep view on the best.
        progress.emit("stage", f"Stage 2 - Building 360 profiles for the top {top}", level="stage")
        from .enrich.profile360 import profile360_top
        dived = profile360_top(top=top, max_rounds=max_rounds)

        progress.emit("stage", "Stage 2.5 - Final scoring with full 360 evidence", level="stage")
        score_ids(dived)

        progress.emit("stage", "Compiling results", level="stage")
        results = build_results(top=max(top, 10))
        progress.set_result(job_id, results)
        progress.emit("done", f"Done. {len(results['candidates'])} ranked candidates ready.",
                      {"count": len(results["candidates"])}, level="success")
    except Exception as e:  # noqa: BLE001
        import traceback
        progress.fail(job_id, f"{e}\n{traceback.format_exc()[:1200]}")
    finally:
        clear_overrides()
        progress.finish(job_id)
