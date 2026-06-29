"""
Discovery orchestrator (Stage 1).

Two modes:
  - run_discovery():     mine contributors of high-OpenRank target repos (depth-first
                         on serious projects).
  - run_geo_discovery(): India area-wise GitHub user search (breadth-first on a
                         geography), favoring hungry / open-to-work builders.

Both funnel into a shared _ingest_github_user() that captures profile, OpenRank,
activity, identities, contacts, geo tags, and base craft signals.
"""

from __future__ import annotations

from rich.console import Console

from ..config import load_icp
from ..db import add_identity, add_signal, session_scope, upsert_candidate_by_github
from ..geo import area_of, is_in_country
from ..models import Contact, Platform, Stage
from ..progress import emit
from . import github, openrank

console = Console()


# --------------------------------------------------------------------------
# Shared user-level ingest
# --------------------------------------------------------------------------
def _ingest_github_user(login: str, source_meta: dict | None = None) -> int | None:
    """Ingest a GitHub user's profile-level signals. Returns candidate id or None."""
    profile = github.get_user(login) or {}
    if profile.get("type") == "Organization":
        return None

    dev_or = openrank.dev_openrank(login)
    activity = github.recent_activity(login)
    gql = github.graphql_contributions(login)
    location = profile.get("location")
    target_country = (load_icp().get("geo", {}) or {}).get("country", "India")
    in_country = is_in_country(location, target_country)
    region = area_of(location) or (source_meta or {}).get("geo_area")

    with session_scope() as session:
        meta = {
            "followers": profile.get("followers"),
            "public_repos": profile.get("public_repos"),
            "company": profile.get("company"),
            "github_created_at": profile.get("created_at"),
            "country": target_country if in_country else None,
            "region": region,
            "hireable": profile.get("hireable"),
        }
        if source_meta:
            meta.update(source_meta)

        cand = upsert_candidate_by_github(
            session,
            login,
            name=profile.get("name"),
            headline=(profile.get("bio") or None),
            location=location,
            meta=meta,
        )
        cid = cand.id

        add_identity(
            session, cid, platform=Platform.github, handle=login,
            url=profile.get("html_url") or f"https://github.com/{login}",
            confidence=1.0,
            raw={k: profile.get(k) for k in ("blog", "company", "followers", "public_repos")},
        )
        tw = profile.get("twitter_username")
        if tw:
            add_identity(
                session, cid, platform=Platform.x, handle=tw,
                url=f"https://x.com/{tw}", confidence=0.95, raw={"source": "github_profile"},
            )
        blog = (profile.get("blog") or "").strip()
        if blog:
            if not blog.startswith("http"):
                blog = "https://" + blog
            add_identity(session, cid, platform=Platform.website, handle=blog, url=blog, confidence=0.9)

        if profile.get("email"):
            session.add(Contact(candidate_id=cid, type="email", value=profile["email"],
                                source="github_profile", confidence=0.9))

        if dev_or.get("available"):
            add_signal(session, cid, source="openrank", kind="dev_influence",
                       title=f"OpenRank {dev_or['latest']:.2f} (peak {dev_or['peak']:.2f})",
                       url=f"https://github.com/{login}", weight=float(dev_or["latest"]), raw=dev_or)
        add_signal(session, cid, source="github_activity", kind="recency",
                   title=f"{activity['push_events']} recent pushes; last activity {activity['days_since_last']}d ago",
                   weight=float(activity["push_events"]), raw=activity)
        if gql:
            add_signal(session, cid, source="github_graphql", kind="contribution_stats",
                       title=f"{gql['reviews_1y']} reviews, {gql['prs_1y']} PRs in last year",
                       weight=float(gql["reviews_1y"]) + float(gql["prs_1y"]), raw=gql)

        # Geo signal (used by hireability scoring).
        if meta.get("region") or meta.get("country"):
            add_signal(session, cid, source="geo", kind="location",
                       title=f"Location: {location} (region: {meta.get('region')}, country: {meta.get('country')})",
                       weight=1.0 if meta.get("country") else 0.0,
                       raw={"location": location, "region": meta.get("region"), "country": meta.get("country")})
        cand.stage = Stage.discovered
    return cid


# --------------------------------------------------------------------------
# Repo-based discovery (depth on serious OSS)
# --------------------------------------------------------------------------
def run_discovery(limit_repos: int | None = None, max_per_repo: int | None = None) -> int:
    icp = load_icp()
    repos = icp.get("target_repos", [])
    gh_cfg = icp.get("github", {})
    max_contrib = int(max_per_repo or gh_cfg.get("max_contributors_per_repo", 60))

    ranked = openrank.rank_repos(repos)
    if limit_repos:
        ranked = ranked[:limit_repos]

    console.print("[bold]Target repos ranked by OpenRank:[/bold]")
    for full, score in ranked:
        console.print(f"  {full:40s} OpenRank={score:.2f}")

    seen_logins: set[str] = set()
    total = 0
    for full, repo_or in ranked:
        owner, repo = full.split("/", 1)
        console.print(f"\n[cyan]Mining[/cyan] {full} ...")
        try:
            contributors = github.get_contributors(owner, repo, max_contrib)
        except github.GitHubRateLimit as e:
            console.print(f"[red]{e}[/red] Stopping discovery; partial results kept.")
            break
        for c in contributors:
            login = c["login"]
            try:
                first_time = login not in seen_logins
                seen_logins.add(login)
                created = _ingest_contributor(login, full, c["contributions"], repo_or, run_user_ingest=first_time)
                if created:
                    total += 1
            except github.GitHubRateLimit as e:
                console.print(f"[red]{e}[/red] Stopping discovery; partial results kept.")
                return total
    return total


def _ingest_contributor(login: str, full: str, contributions: int, repo_or: float, run_user_ingest: bool = True) -> bool:
    owner, repo = full.split("/", 1)
    if run_user_ingest:
        cid = _ingest_github_user(login, source_meta={"seed_repo": full})
        if cid is None:
            return False
    prs = github.count_merged_prs(owner, repo, login)
    with session_scope() as session:
        cand = upsert_candidate_by_github(session, login)
        add_signal(session, cand.id, source="github_contrib", kind="repo_contributions",
                   title=f"{contributions} contributions to {full}",
                   url=f"https://github.com/{full}", weight=float(contributions),
                   raw={"repo": full, "contributions": contributions, "repo_openrank": repo_or})
        if prs["count"]:
            add_signal(session, cand.id, source="github_pr", kind="merged_prs",
                       title=f"{prs['count']} merged PRs into {full}",
                       url=f"https://github.com/{full}/pulls?q=is:pr+is:merged+author:{login}",
                       weight=float(prs["count"]), raw=prs)
    return run_user_ingest


# --------------------------------------------------------------------------
# Geo-based discovery (breadth on India, area-wise)
# --------------------------------------------------------------------------
def run_geo_discovery(
    max_per_query: int | None = None,
    open_to_work_only: bool = False,
    limit_areas: int | None = None,
) -> int:
    """India area-wise GitHub user search. Targets hungry/reachable builders by city."""
    icp = load_icp()
    geo = icp.get("geo", {})
    gh = icp.get("github", {})
    country = geo.get("country", "India")
    areas = geo.get("areas", [country])
    if limit_areas:
        areas = areas[:limit_areas]
    languages = gh.get("languages", ["Python"])
    min_followers = int(gh.get("min_followers_geo", 3))
    per_query = int(max_per_query or gh.get("max_users_per_query", 30))
    bio_terms = geo.get("open_to_work_bio", ["open to work", "looking for", "seeking"])

    seen: set[str] = set()
    total = 0
    emit("discover", f"Monitoring location specificity: {country} - {len(areas)} area(s)", {"areas": areas})
    for area in areas:
        emit("discover", f"Searching candidates in {area}, {country}...")
        for lang in languages:
            q = f'location:"{area}" language:{lang} followers:>={min_followers}'
            if open_to_work_only and bio_terms:
                q += f' "{bio_terms[0]}" in:bio'
            console.print(f"[cyan]Geo search[/cyan] {q}")
            emit("discover", f"Fetching GitHub developers: {area} / {lang}")
            try:
                logins = github.search_users(q, max_users=per_query)
            except github.GitHubRateLimit as e:
                console.print(f"[red]{e}[/red] Stopping geo discovery; partial results kept.")
                return total
            for login in logins:
                if login in seen:
                    continue
                seen.add(login)
                try:
                    cid = _ingest_github_user(login, source_meta={"geo_query": q, "geo_area": area})
                    if cid:
                        total += 1
                except github.GitHubRateLimit as e:
                    console.print(f"[red]{e}[/red] Stopping geo discovery; partial results kept.")
                    return total
    console.print(f"Geo discovery ingested {total} candidates across {len(areas)} area(s).")
    emit("discover", f"GitHub geo discovery found {total} candidates.", {"count": total})
    return total
