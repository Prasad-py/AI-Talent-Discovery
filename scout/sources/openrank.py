"""
OpenRank / OpenDigger client.

OpenRank is X-lab's PageRank-style influence metric computed over the real GitHub
collaboration graph (issues/PRs). OpenDigger publishes it as free static JSON:

    repo: https://oss.open-digger.cn/github/{owner}/{repo}/openrank.json
    user: https://oss.open-digger.cn/github/{login}/openrank.json

We use it two ways (per the plan):
  1. Rank target repos so we mine contributors of the most "serious" projects first.
  2. Attach each developer's OpenRank trajectory as a hard-to-fake craft signal.
"""

from __future__ import annotations

import re
from typing import Optional

from ..http import get_json

BASE = "https://oss.open-digger.cn/github"
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _fetch(path: str) -> Optional[dict]:
    url = f"{BASE}/{path}/openrank.json"
    try:
        data = get_json(url, ttl=60 * 60 * 24 * 7)  # 7-day cache; data updates monthly
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        return None
    return None


def _monthly_series(data: Optional[dict]) -> dict[str, float]:
    if not data:
        return {}
    return {k: float(v) for k, v in data.items() if _MONTH_RE.match(k)}


def _latest(series: dict[str, float]) -> float:
    if not series:
        return 0.0
    latest_key = max(series.keys())
    return series[latest_key]


def _recent_avg(series: dict[str, float], months: int = 12) -> float:
    if not series:
        return 0.0
    keys = sorted(series.keys())[-months:]
    return sum(series[k] for k in keys) / max(len(keys), 1)


def repo_openrank(owner: str, repo: str) -> float:
    """Latest OpenRank for a repository (0.0 if unavailable)."""
    series = _monthly_series(_fetch(f"{owner}/{repo}"))
    return _latest(series)


def dev_openrank(login: str) -> dict:
    """
    Developer OpenRank summary:
        {"latest": float, "recent_avg_12m": float, "peak": float, "available": bool}
    """
    series = _monthly_series(_fetch(login))
    if not series:
        return {"latest": 0.0, "recent_avg_12m": 0.0, "peak": 0.0, "available": False}
    return {
        "latest": _latest(series),
        "recent_avg_12m": _recent_avg(series, 12),
        "peak": max(series.values()),
        "available": True,
    }


def rank_repos(repos: list[str]) -> list[tuple[str, float]]:
    """Sort 'owner/repo' strings by repo OpenRank, descending."""
    scored: list[tuple[str, float]] = []
    for full in repos:
        if "/" not in full:
            continue
        owner, repo = full.split("/", 1)
        scored.append((full, repo_openrank(owner, repo)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored
