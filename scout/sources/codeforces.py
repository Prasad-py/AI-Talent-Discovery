"""
Codeforces source - a free open database of (often young, often Indian) competitive
programmers. The public API exposes country / city / organization, so we can pull
India-based rated users directly. Great for "hungry student" talent the OSS-repo
crawl misses.

API: https://codeforces.com/api/user.ratedList (anonymous, free).
Candidates are keyed by their Codeforces identity (no GitHub login yet); the deep-dive
later resolves their GitHub / LinkedIn / contact.
"""

from __future__ import annotations

from rich.console import Console

from ..config import load_icp
from ..db import add_identity, add_signal, session_scope, upsert_candidate_by_identity
from ..geo import area_of, is_in_country
from ..http import get_json
from ..models import Platform, Stage

console = Console()

RATED_LIST = "https://codeforces.com/api/user.ratedList"


def rated_users_for_country(country: str = "India", min_rating: int = 1600, max_users: int = 50, active_only: bool = True) -> list[dict]:
    """Top Codeforces users in a given country by rating (filtered locally)."""
    try:
        data = get_json(
            RATED_LIST,
            params={"activeOnly": str(active_only).lower(), "includeRetired": "false"},
            ttl=60 * 60 * 24,
        )
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Codeforces API failed: {e}[/red]")
        return []
    if data.get("status") != "OK":
        return []

    want = (country or "").strip().lower()
    out = []
    for u in data.get("result", []):
        if want and want not in ("global", "any") and (u.get("country") or "").strip().lower() != want:
            continue
        if int(u.get("rating", 0)) < min_rating:
            continue
        out.append(u)
        if len(out) >= max_users:
            break
    return out


def india_rated_users(min_rating: int = 1600, max_users: int = 50, active_only: bool = True) -> list[dict]:
    """Back-compat alias for India."""
    return rated_users_for_country("India", min_rating, max_users, active_only)


def ingest_india(min_rating: int | None = None, max_users: int | None = None, country: str | None = None) -> int:
    icp = load_icp()
    cf_cfg = icp.get("codeforces", {})
    country = country or (icp.get("geo", {}) or {}).get("country", "India")
    min_rating = int(min_rating or cf_cfg.get("min_rating", 1600))
    max_users = int(max_users or cf_cfg.get("max_users", 50))

    from ..progress import emit

    emit("discover", f"Fetching {country}-based competitive programmers from Codeforces (rating>={min_rating})...")
    users = rated_users_for_country(country=country, min_rating=min_rating, max_users=max_users)
    console.print(f"[cyan]Codeforces[/cyan]: {len(users)} {country}-based rated users (rating>={min_rating})")
    emit("discover", f"Codeforces returned {len(users)} {country}-based users.", {"count": len(users)})

    total = 0
    for u in users:
        handle = u.get("handle")
        if not handle:
            continue
        name = " ".join(x for x in [u.get("firstName"), u.get("lastName")] if x) or None
        city = u.get("city")
        org = u.get("organization")
        rating = int(u.get("rating", 0))
        max_rating = int(u.get("maxRating", rating))
        location = ", ".join(x for x in [city, country] if x)

        with session_scope() as session:
            cand = upsert_candidate_by_identity(
                session, Platform.codeforces, handle,
                name=name, location=location,
                headline=(f"Codeforces {u.get('rank','')} - {org}" if org else f"Codeforces {u.get('rank','')}"),
                meta={
                    "country": country if is_in_country(location, country) else None,
                    "region": area_of(location) or city,
                    "organization": org,
                    "cf_rating": rating,
                    "cf_max_rating": max_rating,
                    "source": "codeforces",
                },
            )
            add_identity(
                session, cand.id, platform=Platform.codeforces, handle=handle,
                url=f"https://codeforces.com/profile/{handle}", confidence=1.0,
                raw={"rating": rating, "rank": u.get("rank"), "organization": org},
            )
            add_signal(
                session, cand.id, source="codeforces", kind="rating",
                title=f"Codeforces {u.get('rank','')} rating {rating} (max {max_rating})"
                      + (f", {org}" if org else ""),
                url=f"https://codeforces.com/profile/{handle}",
                weight=float(rating),
                raw={"rating": rating, "max_rating": max_rating, "rank": u.get("rank"),
                     "organization": org, "city": city},
            )
            if org:
                add_signal(session, cand.id, source="codeforces", kind="organization",
                           title=f"Affiliation: {org}", weight=0.0, raw={"organization": org})
            cand.stage = Stage.discovered
        total += 1
    return total
