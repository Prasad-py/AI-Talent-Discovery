"""
Talent Scout command line.

Usage (from the talent-scout/ directory):
    python -m scout.cli check
    python -m scout.cli init-db
    python -m scout.cli discover            # GitHub + OpenRank crawl
    python -m scout.cli score               # score everything discovered
    python -m scout.cli deep-dive           # enrich top candidates
    python -m scout.cli outreach            # draft messages for the shortlist
    python -m scout.cli run                 # full pipeline end to end
    python -m scout.cli list                # show the current shortlist
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from .config import get_settings, load_icp
from .db import init_db, session_scope
from . import models
from sqlmodel import select

app = typer.Typer(add_completion=False, help="AI talent discovery & screening pipeline")
console = Console()


@app.command()
def check() -> None:
    """Validate configuration and API keys."""
    s = get_settings()
    icp = load_icp()
    table = Table(title="Talent Scout config")
    table.add_column("Setting")
    table.add_column("Status")
    table.add_row("Anthropic key", "set" if s.anthropic_api_key else "[red]MISSING[/red]")
    table.add_row("OpenAI key (fallback)", "set" if s.openai_api_key else "-")
    table.add_row("Gemini key (fallback)", "set" if s.gemini_api_key else "-")
    table.add_row("GitHub token", "set" if s.github_token else "[yellow]unauth (low rate limit)[/yellow]")
    table.add_row("Model", s.model)
    table.add_row("DB", str(s.db_path))
    table.add_row("Role (ICP)", icp.get("role", "-"))
    table.add_row("Target repos", str(len(icp.get("target_repos", []))))
    console.print(table)


@app.command("init-db")
def init_db_cmd() -> None:
    """Create database tables."""
    init_db()
    console.print(f"[green]Database initialized[/green] at {get_settings().db_path}")


@app.command()
def discover(
    limit_repos: int = typer.Option(0, help="Only mine the top N repos by OpenRank (0 = all)"),
    max_per_repo: int = typer.Option(0, help="Cap contributors mined per repo (0 = use icp.yaml)"),
) -> None:
    """Crawl GitHub target repos (ranked by OpenRank) and store candidates + craft signals."""
    from .sources.discover import run_discovery

    init_db()
    n = run_discovery(limit_repos=limit_repos or None, max_per_repo=max_per_repo or None)
    console.print(f"[green]Discovery complete[/green]: {n} candidates in the pool.")


@app.command("discover-geo")
def discover_geo_cmd(
    max_per_query: int = typer.Option(0, help="Cap users per (area x language) query (0 = icp.yaml)"),
    open_to_work: bool = typer.Option(False, help="Only users with open-to-work signals in bio"),
    limit_areas: int = typer.Option(0, help="Only the first N areas from icp.yaml (0 = all)"),
) -> None:
    """India area-wise discovery via GitHub user search (location + language)."""
    from .sources.discover import run_geo_discovery

    init_db()
    n = run_geo_discovery(
        max_per_query=max_per_query or None,
        open_to_work_only=open_to_work,
        limit_areas=limit_areas or None,
    )
    console.print(f"[green]Geo discovery complete[/green]: {n} candidates ingested.")


@app.command("codeforces-india")
def codeforces_india_cmd(
    min_rating: int = typer.Option(0, help="Min Codeforces rating (0 = icp.yaml)"),
    max_users: int = typer.Option(0, help="Max users to ingest (0 = icp.yaml)"),
) -> None:
    """Ingest top India-based Codeforces users (open database of young talent)."""
    from .sources.codeforces import ingest_india

    init_db()
    n = ingest_india(min_rating=min_rating or None, max_users=max_users or None)
    console.print(f"[green]Codeforces ingest complete[/green]: {n} India-based users.")


@app.command("discover-all")
def discover_all_cmd(
    repos: bool = typer.Option(False, help="Also mine GitHub target-repo contributors"),
    huggingface: bool = typer.Option(False, help="Also ingest trending HuggingFace model authors"),
    publications: bool = typer.Option(False, help="Also ingest recent AI paper authors (Semantic Scholar)"),
    limit_areas: int = typer.Option(0, help="Limit India areas (0 = all)"),
    max_per_query: int = typer.Option(0, help="Cap users per geo query (0 = icp.yaml)"),
) -> None:
    """Stage 1: multi-source discovery (India geo + Codeforces by default; flags add more)."""
    from .sources.discover import run_geo_discovery, run_discovery
    from .sources.codeforces import ingest_india

    init_db()
    out = {}
    out["geo_india"] = run_geo_discovery(max_per_query=max_per_query or None, limit_areas=limit_areas or None)
    out["codeforces_india"] = ingest_india()
    if repos:
        out["repos"] = run_discovery()
    if huggingface:
        from .sources.huggingface import ingest_trending
        out["huggingface"] = ingest_trending()
    if publications:
        from .sources.scholar import ingest_topic_authors
        out["publications"] = ingest_topic_authors()
    console.print(out)


@app.command()
def score() -> None:
    """Score every candidate with the multi-pillar scorecard (12 metrics)."""
    from .scoring.scorecard import score_all

    init_db()
    n = score_all()
    console.print(f"[green]Scored[/green] {n} candidates.")


@app.command("deep-dive")
def deep_dive_cmd(
    top: int = typer.Option(10, help="360 deep view on the top N candidates"),
    max_rounds: int = typer.Option(4, help="Max recursive research rounds per candidate"),
) -> None:
    """Stage 2: 360 deep view (multi-source APIs + multi-round web research) for top candidates."""
    from .enrich.profile360 import profile360_top

    init_db()
    ids = profile360_top(top=top, max_rounds=max_rounds)
    console.print(f"[green]360 deep view done[/green] for {len(ids)} candidates.")


@app.command()
def report(top: int = typer.Option(25, help="How many ranked candidates to include")) -> None:
    """Generate the professional HTML shortlist report."""
    from .report import build_report

    init_db()
    path = build_report(top=top)
    console.print(f"[green]Report written:[/green] {path}")


@app.command("deep-run")
def deep_run_cmd(
    top: int = typer.Option(10, help="How many top candidates to 360-enrich + final-rank"),
    max_rounds: int = typer.Option(4, help="Recursive research rounds per candidate"),
    repos: bool = typer.Option(False, help="Include GitHub target-repo contributors in Stage 1"),
    huggingface: bool = typer.Option(False, help="Include HuggingFace trending authors in Stage 1"),
    publications: bool = typer.Option(False, help="Include Semantic Scholar paper authors in Stage 1"),
    limit_areas: int = typer.Option(0, help="Limit India areas (0 = all)"),
    max_per_query: int = typer.Option(0, help="Cap users per geo query (0 = icp.yaml)"),
) -> None:
    """Full two-stage deep pipeline: discover (breadth) -> score -> 360 (depth) -> rescore -> report."""
    from .pipeline import run_deep

    init_db()
    run_deep(
        top=top, max_rounds=max_rounds, repos=repos, huggingface=huggingface,
        publications=publications, limit_areas=limit_areas or None, max_per_query=max_per_query or None,
    )
    console.print("[green]Deep run complete.[/green]")


@app.command()
def outreach(
    top: int = typer.Option(10, help="Draft outreach for the top N shortlisted candidates"),
) -> None:
    """Draft personalized outreach for the shortlist."""
    from .outreach.drafter import draft_for_shortlist

    init_db()
    n = draft_for_shortlist(top=top)
    console.print(f"[green]Drafted outreach[/green] for {n} candidates.")


@app.command("interview-questions")
def interview_questions_cmd(candidate: int = typer.Option(..., help="Candidate id")) -> None:
    """Generate a tailored first-round interview question set for a candidate."""
    from .interview.interviewer import generate_questions

    init_db()
    for i, q in enumerate(generate_questions(candidate), 1):
        console.print(f"{i}. {q}")


@app.command("interview-score")
def interview_score_cmd(
    candidate: int = typer.Option(..., help="Candidate id"),
    transcript: str = typer.Option(..., help="Path to a transcript text file"),
) -> None:
    """Score an interview transcript against the rubric (transcript mode)."""
    from .interview.interviewer import score_transcript

    init_db()
    with open(transcript, "r", encoding="utf-8") as f:
        text = f.read()
    result = score_transcript(candidate, text)
    console.print(result)


@app.command()
def outcome(
    candidate: int = typer.Option(..., help="Candidate id"),
    label: str = typer.Option(..., help="advanced | hired | rejected"),
    note: str = typer.Option("", help="optional note"),
) -> None:
    """Record a human outcome (feeds the feedback loop)."""
    from .feedback.loop import record_outcome

    init_db()
    record_outcome(candidate, label, note or None)
    console.print(f"[green]Recorded[/green] outcome '{label}' for candidate {candidate}.")


@app.command("retrain")
def retrain_cmd() -> None:
    """Recompute composite scoring weights from labeled outcomes."""
    from .feedback.loop import update_weights_from_outcomes

    init_db()
    console.print(update_weights_from_outcomes())


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Host to bind"),
    port: int = typer.Option(8000, help="Port"),
) -> None:
    """Launch the web UI (FastAPI) on localhost."""
    import uvicorn

    init_db()
    console.print(f"[green]Talent Scout UI[/green] -> http://{host}:{port}")
    uvicorn.run("webapp.server:app", host=host, port=port, log_level="info")


@app.command("list")
def list_candidates(
    top: int = typer.Option(25, help="How many to show"),
) -> None:
    """Show the current ranked shortlist."""
    init_db()
    table = Table(title="Talent Scout - ranked candidates")
    table.add_column("#", justify="right")
    table.add_column("GitHub")
    table.add_column("Name")
    table.add_column("Score", justify="right")
    table.add_column("Stage")
    with session_scope() as session:
        stmt = (
            select(models.Candidate)
            .order_by(models.Candidate.composite_score.desc().nullslast())
            .limit(top)
        )
        rows = session.exec(stmt).all()
        for i, c in enumerate(rows, 1):
            score_str = f"{c.composite_score:.3f}" if c.composite_score is not None else "-"
            table.add_row(str(i), c.primary_github_login or "-", c.name or "-", score_str, c.stage.value)
    console.print(table)


if __name__ == "__main__":
    app()
