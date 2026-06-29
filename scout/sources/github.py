"""
GitHub mining.

Best-practice signal (per research): read the *contribution graph*, not stars.
We capture merged PRs into serious repos, code reviews, contribution counts, recent
activity (for hunger), and profile fields (name, bio, blog, twitter, email, location).

Works unauthenticated with tight rate limits, but a GITHUB_TOKEN in .env is strongly
recommended (GraphQL requires it; REST limits jump from 60/hr to 5000/hr).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ..config import get_settings
from ..http import get_json, post_json

REST = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"


class GitHubRateLimit(RuntimeError):
    pass


def _headers() -> dict:
    s = get_settings()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "talent-scout/0.1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if s.github_token:
        headers["Authorization"] = f"Bearer {s.github_token}"
    return headers


def has_token() -> bool:
    return bool(get_settings().github_token)


def _safe_get_json(url: str, params: Optional[dict] = None, ttl: int = 86400):
    try:
        return get_json(url, headers=_headers(), params=params, ttl=ttl)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (403, 429):
            raise GitHubRateLimit(
                "GitHub rate limit hit. Add GITHUB_TOKEN to .env to raise limits."
            ) from e
        raise


def get_contributors(owner: str, repo: str, max_contributors: int = 60) -> list[dict]:
    """Top contributors to a repo: [{login, contributions}]."""
    out: list[dict] = []
    page = 1
    while len(out) < max_contributors:
        data = _safe_get_json(
            f"{REST}/repos/{owner}/{repo}/contributors",
            params={"per_page": 100, "page": page, "anon": "false"},
        )
        if not data:
            break
        for c in data:
            login = c.get("login")
            if login and not login.endswith("[bot]"):
                out.append({"login": login, "contributions": c.get("contributions", 0)})
        if len(data) < 100:
            break
        page += 1
    return out[:max_contributors]


def search_users(query: str, max_users: int = 50) -> list[str]:
    """
    Search users via GitHub's user-search API (qualifiers like
    `location:Bengaluru language:Python "open to work" in:bio followers:>5`).
    Returns logins. Up to 1000 results available; paginates as needed.
    """
    logins: list[str] = []
    page = 1
    while len(logins) < max_users and page <= 10:
        try:
            data = _safe_get_json(
                f"{REST}/search/users",
                params={"q": query, "per_page": 100, "page": page},
                ttl=86400,
            )
        except GitHubRateLimit:
            raise
        except Exception:  # noqa: BLE001
            break
        items = data.get("items") or []
        if not items:
            break
        for it in items:
            login = it.get("login")
            if login and it.get("type") != "Organization" and not login.endswith("[bot]"):
                logins.append(login)
        if len(items) < 100:
            break
        page += 1
    return logins[:max_users]


def get_user(login: str) -> Optional[dict]:
    """Public profile fields for a user."""
    try:
        return _safe_get_json(f"{REST}/users/{login}", ttl=86400 * 3)
    except GitHubRateLimit:
        raise
    except Exception:  # noqa: BLE001
        return None


def top_repos(login: str, limit: int = 10) -> list[dict]:
    """
    The user's own (non-fork) repos with names/descriptions/topics/languages/stars -
    the single richest signal for judging what someone actually builds.
    """
    try:
        data = _safe_get_json(
            f"{REST}/users/{login}/repos",
            params={"sort": "pushed", "per_page": 60, "type": "owner"},
            ttl=86400 * 3,
        )
    except GitHubRateLimit:
        raise
    except Exception:  # noqa: BLE001
        return []
    repos = []
    for r in data or []:
        if r.get("fork"):
            continue
        repos.append({
            "name": r.get("name"),
            "description": r.get("description"),
            "language": r.get("language"),
            "stars": int(r.get("stargazers_count") or 0),
            "topics": r.get("topics") or [],
        })
    repos.sort(key=lambda x: x["stars"], reverse=True)
    return repos[:limit]


def count_merged_prs(owner: str, repo: str, login: str) -> dict:
    """
    Merged PRs by `login` into `owner/repo` via the search API.
    Returns {"count": int, "samples": [{title,url,created_at}]}.
    """
    q = f"repo:{owner}/{repo} type:pr is:merged author:{login}"
    try:
        data = _safe_get_json(
            f"{REST}/search/issues",
            params={"q": q, "per_page": 5},
            ttl=86400,
        )
    except GitHubRateLimit:
        raise
    except Exception:  # noqa: BLE001
        return {"count": 0, "samples": []}
    samples = [
        {
            "title": it.get("title"),
            "url": it.get("html_url"),
            "created_at": it.get("created_at"),
        }
        for it in (data.get("items") or [])
    ]
    return {"count": int(data.get("total_count", 0)), "samples": samples}


def recent_activity(login: str) -> dict:
    """
    Approximate hunger/recency from public events.
    Returns {"events": int, "days_since_last": Optional[int], "push_events": int}.
    """
    try:
        events = _safe_get_json(
            f"{REST}/users/{login}/events/public", params={"per_page": 100}, ttl=86400
        )
    except GitHubRateLimit:
        raise
    except Exception:  # noqa: BLE001
        return {"events": 0, "days_since_last": None, "push_events": 0}
    if not events:
        return {"events": 0, "days_since_last": None, "push_events": 0}
    push = sum(1 for e in events if e.get("type") == "PushEvent")
    last = events[0].get("created_at")
    days = None
    if last:
        try:
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - dt).days
        except Exception:  # noqa: BLE001
            days = None
    return {"events": len(events), "days_since_last": days, "push_events": push}


def graphql_contributions(login: str) -> Optional[dict]:
    """
    Richer craft signal via GraphQL (requires a token): total merged PRs, reviews,
    and a 1-year contribution count. Returns None if no token / on failure.
    """
    if not has_token():
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    query = """
    query($login:String!, $from:DateTime!) {
      user(login:$login) {
        name
        bio
        contributionsCollection(from:$from) {
          totalCommitContributions
          totalPullRequestContributions
          totalPullRequestReviewContributions
          totalIssueContributions
        }
        pullRequests(states:MERGED) { totalCount }
        followers { totalCount }
        repositories(first:1, ownerAffiliations:OWNER, orderBy:{field:STARGAZERS, direction:DESC}) {
          nodes { stargazerCount }
        }
      }
    }
    """
    try:
        data = post_json(
            GRAPHQL,
            {"query": query, "variables": {"login": login, "from": since}},
            headers=_headers(),
            ttl=86400 * 3,
        )
    except Exception:  # noqa: BLE001
        return None
    user = (data.get("data") or {}).get("user")
    if not user:
        return None
    cc = user.get("contributionsCollection", {})
    return {
        "merged_prs_total": (user.get("pullRequests") or {}).get("totalCount", 0),
        "reviews_1y": cc.get("totalPullRequestReviewContributions", 0),
        "prs_1y": cc.get("totalPullRequestContributions", 0),
        "commits_1y": cc.get("totalCommitContributions", 0),
        "issues_1y": cc.get("totalIssueContributions", 0),
    }
