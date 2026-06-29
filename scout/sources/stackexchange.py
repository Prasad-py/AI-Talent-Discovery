"""
Stack Exchange source (free public API, no key needed for low volume).

Enrichment only: community-standing signal (Stack Overflow reputation + top tags).
Matching is by display name, so results are UNVERIFIED and reconciled in Stage-2.

API: https://api.stackexchange.com/2.3
"""

from __future__ import annotations

from ..http import get_json

BASE = "https://api.stackexchange.com/2.3"


def user_by_name(name: str) -> dict:
    """Best-effort Stack Overflow reputation + top tags for a person by name."""
    if not name:
        return {"found": False}
    try:
        data = get_json(
            f"{BASE}/users",
            params={"inname": name, "site": "stackoverflow", "sort": "reputation", "order": "desc", "pagesize": 5},
            ttl=86400 * 7,
        )
    except Exception:  # noqa: BLE001
        return {"found": False}

    items = data.get("items") if isinstance(data, dict) else None
    if not items:
        return {"found": False}
    top = items[0]
    user_id = top.get("user_id")

    tags = []
    try:
        tag_data = get_json(
            f"{BASE}/users/{user_id}/top-tags",
            params={"site": "stackoverflow", "pagesize": 8},
            ttl=86400 * 7,
        )
        tags = [t.get("tag_name") for t in (tag_data.get("items") or [])][:8]
    except Exception:  # noqa: BLE001
        pass

    return {
        "found": True,
        "display_name": top.get("display_name"),
        "reputation": int(top.get("reputation") or 0),
        "top_tags": tags,
        "url": top.get("link"),
        "unverified": True,
    }
