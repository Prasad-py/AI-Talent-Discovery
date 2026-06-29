"""Tiny cached HTTP helper so re-runs don't hammer APIs (free-first posture)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import get_settings

DEFAULT_TTL = 60 * 60 * 24  # 24h


def _cache_path(key: str) -> Path:
    settings = get_settings()
    settings.http_cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return settings.http_cache_dir / f"{digest}.json"


def _read_cache(key: str, ttl: int) -> Optional[Any]:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if time.time() - blob.get("_ts", 0) > ttl:
        return None
    return blob.get("data")


def _write_cache(key: str, data: Any) -> None:
    try:
        _cache_path(key).write_text(
            json.dumps({"_ts": time.time(), "data": data}), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def get_json(
    url: str,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    ttl: int = DEFAULT_TTL,
    use_cache: bool = True,
) -> Any:
    key = f"GET {url} {json.dumps(params or {}, sort_keys=True)}"
    if use_cache:
        cached = _read_cache(key, ttl)
        if cached is not None:
            return cached
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
    _write_cache(key, data)
    return data


def post_json(
    url: str,
    payload: dict,
    headers: Optional[dict] = None,
    ttl: int = DEFAULT_TTL,
    use_cache: bool = True,
) -> Any:
    key = f"POST {url} {json.dumps(payload, sort_keys=True)}"
    if use_cache:
        cached = _read_cache(key, ttl)
        if cached is not None:
            return cached
    with httpx.Client(timeout=60) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    _write_cache(key, data)
    return data


def get_text(
    url: str, headers: Optional[dict] = None, ttl: int = DEFAULT_TTL, use_cache: bool = True
) -> Optional[str]:
    """Fetch a page as text (used for deep-dive scraping). Returns None on failure."""
    key = f"GET_TEXT {url}"
    if use_cache:
        cached = _read_cache(key, ttl)
        if cached is not None:
            return cached
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(url, headers=headers or {"User-Agent": "talent-scout/0.1"})
            resp.raise_for_status()
            text = resp.text
    except Exception:  # noqa: BLE001
        return None
    _write_cache(key, text)
    return text
