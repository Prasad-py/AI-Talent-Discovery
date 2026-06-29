"""
Hugging Face source (free public API, no auth).

- Enrichment: given a handle, fetch the models/datasets/spaces they authored, with
  downloads + likes (the meaningful AI-builder signal is *authoring a used model*).
- Discovery: authors of trending / most-downloaded models as Stage-1 entry points.

REST: https://huggingface.co/api/models?author={handle}
"""

from __future__ import annotations

from ..http import get_json

HF = "https://huggingface.co/api"


def _author_of(model_id: str | None, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if model_id and "/" in model_id:
        return model_id.split("/", 1)[0]
    return None


def author_models(handle: str, limit: int = 50) -> dict:
    """Models authored by `handle` with aggregate downloads/likes (enrichment)."""
    try:
        data = get_json(
            f"{HF}/models",
            params={"author": handle, "limit": limit, "full": "true", "sort": "downloads"},
            ttl=86400 * 3,
        )
    except Exception:  # noqa: BLE001
        return {"handle": handle, "exists": False, "count": 0, "total_downloads": 0, "total_likes": 0, "top": []}
    if not isinstance(data, list) or not data:
        return {"handle": handle, "exists": False, "count": 0, "total_downloads": 0, "total_likes": 0, "top": []}

    models, total_dl, total_likes = [], 0, 0
    for m in data:
        dl = int(m.get("downloads") or 0)
        lk = int(m.get("likes") or 0)
        total_dl += dl
        total_likes += lk
        models.append({
            "id": m.get("id"),
            "downloads": dl,
            "likes": lk,
            "pipeline_tag": m.get("pipeline_tag"),
            "url": f"https://huggingface.co/{m.get('id')}",
        })
    models.sort(key=lambda x: x["downloads"], reverse=True)
    return {
        "handle": handle,
        "exists": True,
        "count": len(models),
        "total_downloads": total_dl,
        "total_likes": total_likes,
        "top": models[:10],
        "url": f"https://huggingface.co/{handle}",
    }


def ingest_trending(limit: int = 30) -> int:
    """Stage-1 discovery: ingest authors behind trending models (identity-keyed)."""
    from ..db import add_identity, add_signal, session_scope, upsert_candidate_by_identity
    from ..models import Platform, Stage

    authors = trending_authors(limit=limit)
    total = 0
    for a in authors:
        handle = a["author"]
        models = author_models(handle)
        with session_scope() as session:
            cand = upsert_candidate_by_identity(
                session, Platform.huggingface, handle,
                name=handle,
                headline=f"HuggingFace author ({models.get('total_downloads', 0):,} downloads)",
                meta={"source": "huggingface", "hf_downloads": models.get("total_downloads", 0)},
            )
            add_identity(session, cand.id, platform=Platform.huggingface, handle=handle,
                         url=f"https://huggingface.co/{handle}", confidence=1.0, raw=models)
            add_signal(session, cand.id, source="huggingface", kind="models",
                       title=f"{models.get('count',0)} models, {models.get('total_downloads',0):,} downloads (via {a.get('via_model')})",
                       url=f"https://huggingface.co/{handle}", weight=float(models.get("total_downloads", 0)),
                       raw=models)
            cand.stage = Stage.discovered
        total += 1
    return total


def trending_authors(limit: int = 40, sort: str = "trendingScore") -> list[dict]:
    """Authors behind trending / most-downloaded models (Stage-1 discovery)."""
    try:
        data = get_json(
            f"{HF}/models",
            params={"sort": sort, "limit": limit, "full": "true"},
            ttl=86400,
        )
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set()
    for m in data or []:
        author = _author_of(m.get("id"), m.get("author"))
        if not author or author in seen:
            continue
        seen.add(author)
        out.append({
            "author": author,
            "via_model": m.get("id"),
            "downloads": int(m.get("downloads") or 0),
            "likes": int(m.get("likes") or 0),
        })
    return out
