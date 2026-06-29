"""
Semantic Scholar source (free Academic Graph API, no auth).

- Enrichment: research depth for a candidate by name (papers, citations, hIndex).
  Name matching is fuzzy, so results are returned as UNVERIFIED signals; the Stage-2
  360 synthesis (Claude) reconciles whether the author record is really them.
- Discovery (optional): authors of recent AI papers on a topic.

Base: https://api.semanticscholar.org/graph/v1
"""

from __future__ import annotations

import time

from ..http import get_json

BASE = "https://api.semanticscholar.org/graph/v1"


def _s2_get(path: str, params: dict, ttl: int, retries: int = 4):
    """Semantic Scholar's no-key tier rate-limits hard (429); retry with backoff."""
    last = None
    for attempt in range(retries):
        try:
            return get_json(f"{BASE}/{path}", params=params, ttl=ttl)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("semantic scholar failed")


def author_metrics(name: str, affiliation_hint: str | None = None) -> dict:
    """Best-effort research metrics for a person by name (unverified)."""
    if not name:
        return {"found": False}
    try:
        search = _s2_get(
            "author/search",
            params={"query": name, "fields": "name,hIndex,paperCount,citationCount,affiliations,url", "limit": 5},
            ttl=86400 * 7,
        )
    except Exception:  # noqa: BLE001
        return {"found": False}

    results = search.get("data") if isinstance(search, dict) else None
    if not results:
        return {"found": False}

    # Prefer a result whose affiliation matches the hint; else the most-cited.
    def score(a: dict) -> tuple:
        aff = " ".join(a.get("affiliations") or []).lower()
        hint_match = 1 if (affiliation_hint and affiliation_hint.lower() in aff) else 0
        return (hint_match, int(a.get("citationCount") or 0))

    best = sorted(results, key=score, reverse=True)[0]
    return {
        "found": True,
        "name": best.get("name"),
        "hIndex": int(best.get("hIndex") or 0),
        "paperCount": int(best.get("paperCount") or 0),
        "citationCount": int(best.get("citationCount") or 0),
        "affiliations": best.get("affiliations") or [],
        "url": best.get("url"),
        "unverified": True,
        "candidates_considered": len(results),
    }


def ingest_topic_authors(topic: str = "large language models", limit: int = 20) -> int:
    """Stage-1 discovery: ingest authors of recent AI papers (identity-keyed)."""
    from ..db import add_identity, add_signal, session_scope, upsert_candidate_by_identity
    from ..models import Platform, Stage

    authors = topic_paper_authors(topic, limit=limit)
    total = 0
    for a in authors:
        aid = a["authorId"]
        with session_scope() as session:
            cand = upsert_candidate_by_identity(
                session, Platform.scholar, aid,
                name=a["name"],
                headline=f"Researcher (paper: {a.get('via_paper','')[:60]})",
                meta={"source": "semantic_scholar", "scholar_author_id": aid},
            )
            add_identity(session, cand.id, platform=Platform.scholar, handle=aid,
                         url=f"https://www.semanticscholar.org/author/{aid}", confidence=0.7,
                         raw={"via_paper": a.get("via_paper")})
            add_signal(session, cand.id, source="semantic_scholar", kind="publication",
                       title=f"Author of recent paper: {a.get('via_paper','')[:80]}",
                       url=f"https://www.semanticscholar.org/author/{aid}", weight=1.0, raw=a)
            cand.stage = Stage.discovered
        total += 1
    return total


def topic_paper_authors(topic: str = "large language models", year: str = "2024-2026", limit: int = 30) -> list[dict]:
    """Authors of recent papers on a topic (Stage-1 discovery, optional)."""
    try:
        data = _s2_get(
            "paper/search",
            params={"query": topic, "year": year, "fields": "title,authors,year,citationCount", "limit": limit},
            ttl=86400,
        )
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set()
    for p in (data.get("data") if isinstance(data, dict) else []) or []:
        for a in p.get("authors", []) or []:
            name = a.get("name")
            aid = a.get("authorId")
            if name and aid and aid not in seen:
                seen.add(aid)
                out.append({"name": name, "authorId": aid, "via_paper": p.get("title")})
    return out
