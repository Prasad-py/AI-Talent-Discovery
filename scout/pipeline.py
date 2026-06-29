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
) -> dict:
    from .enrich.profile360 import profile360_top
    from .report import build_report
    from .scoring.scorecard import score_all, score_ids
    from .sources.codeforces import ingest_india
    from .sources.discover import run_discovery, run_geo_discovery

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

    console.rule("[bold]Stage 1.5 - Score the pool")
    summary["scored"] = score_all()

    console.rule(f"[bold]Stage 2 - 360 deep view on top {top}")
    dived = profile360_top(top=top, max_rounds=max_rounds)
    summary["deep_dived"] = len(dived)

    console.rule("[bold]Stage 2.5 - Re-score with full 360 evidence")
    summary["rescored"] = score_ids(dived)

    console.rule("[bold]Report")
    summary["report"] = build_report(top=max(top, 25))
    console.print(summary)
    return summary
