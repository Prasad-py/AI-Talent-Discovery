"""
Two-stage orchestrator.

Stage 1 (breadth): understand the brief -> discover public signals from many sources.
Stage 2 (depth):   360 profile of the best -> multi-metric scorecard -> report.

The web UI (scout.webjob) drives the same stages with live progress; this is the
headless/CLI equivalent.
"""

from __future__ import annotations

from rich.console import Console

from .db import init_db

console = Console()


def run_deep(
    top: int = 10,
    max_rounds: int = 4,
    geo: bool = True,
    codeforces: bool = True,
    repos: bool = False,
    huggingface: bool = False,
    publications: bool = False,
    limit_areas: int | None = None,
    max_per_query: int | None = None,
    geo_strict: bool = True,
) -> dict:
    from .authenticity import classifier as authenticity
    from .config import load_icp
    from .enrich.profile360 import profile360
    from .parallel import run_parallel
    from .report import build_report
    from .scoring.scorecard import prerank_ids, score_candidate
    from .scoring.triage import role_relevance
    from .sources.codeforces import ingest_india
    from .sources.discover import enrich_top_repos, run_discovery, run_geo_discovery

    init_db()
    summary: dict = {}

    console.rule("[bold]Stage 1 - Discovery (breadth)")
    if repos:
        summary["repos"] = run_discovery()
    if geo:
        summary["geo"] = run_geo_discovery(max_per_query=max_per_query, limit_areas=limit_areas)
    if codeforces:
        summary["codeforces"] = ingest_india()
    if huggingface:
        from .sources.huggingface import ingest_trending
        summary["huggingface"] = ingest_trending()
    if publications:
        from .sources.scholar import ingest_topic_authors
        summary["publications"] = ingest_topic_authors()

    console.rule(f"[bold]Stage 2 - Select (role-relevance) + 360 deep view on top {top}")
    icp = load_icp()
    triage_n = max(top * 3, 30)
    narrowed = prerank_ids(triage_n, in_geo_only=geo_strict) or prerank_ids(triage_n, in_geo_only=False)
    enrich_top_repos(narrowed)
    ranked = role_relevance(narrowed, icp.get("role"), icp.get("description"), [])
    sel_ids = [cid for cid, _rel in ranked[:top]] or narrowed[:top]
    run_parallel(lambda cid: profile360(cid, max_rounds=max_rounds), sel_ids, workers=5)
    summary["deep_dived"] = len(sel_ids)

    console.rule("[bold]Stage 3 - Score each candidate on the full 360 profile")
    run_parallel(authenticity.assess, sel_ids, workers=6)
    run_parallel(score_candidate, sel_ids, workers=6)
    summary["scored"] = len(sel_ids)

    console.rule("[bold]Report")
    summary["report"] = build_report(top=max(top, 25))
    console.print(summary)
    return summary
