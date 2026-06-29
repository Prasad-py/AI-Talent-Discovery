"""
Contact discovery (free-first).

Sources, in confidence order:
  1. Public GitHub profile email (captured at discovery).
  2. Emails from the candidate's recent public commits (PushEvent payloads).
  3. Emails scraped from their personal site / blog.
  4. Emails surfaced by the LLM deep-dive (passed in).

No paid enrichment in v1 - those plug in here later as additional sources.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from sqlmodel import select

from ..db import session_scope
from ..http import get_text
from ..models import Candidate, Contact, Identity, Platform
from ..sources import github

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NOREPLY = ("noreply", "no-reply", "users.noreply.github.com", "example.com")


def _is_real_email(email: str) -> bool:
    e = email.lower()
    return not any(bad in e for bad in _NOREPLY)


def _extract_emails(text: str) -> list[str]:
    return [e for e in set(EMAIL_RE.findall(text or "")) if _is_real_email(e)]


def github_commit_emails(login: str) -> list[str]:
    """Pull author emails from the user's recent public push events."""
    try:
        events = github._safe_get_json(
            f"{github.REST}/users/{login}/events/public",
            params={"per_page": 100},
            ttl=86400,
        )
    except Exception:  # noqa: BLE001
        return []
    emails: set[str] = set()
    for e in events or []:
        if e.get("type") != "PushEvent":
            continue
        for commit in (e.get("payload") or {}).get("commits", []) or []:
            author = (commit.get("author") or {})
            email = author.get("email")
            if email and _is_real_email(email):
                emails.add(email)
    return list(emails)


def scrape_emails_from_url(url: str) -> list[str]:
    """Fetch a personal site/blog and extract emails (mailto: + plain text)."""
    if not url:
        return []
    if not url.startswith("http"):
        url = "https://" + url
    html = get_text(url)
    if not html:
        return []
    found = set(_extract_emails(html))
    for m in re.findall(r'mailto:([^"\'>\s]+)', html):
        if _is_real_email(m):
            found.add(m)
    return list(found)


def _save_contacts(candidate_id: int, contacts: Iterable[tuple[str, str, float]]) -> int:
    """contacts: iterable of (value, source, confidence). Dedupes on value."""
    added = 0
    with session_scope() as session:
        existing = {
            c.value.lower()
            for c in session.exec(
                select(Contact).where(Contact.candidate_id == candidate_id)
            ).all()
        }
        for value, source, confidence in contacts:
            if not value or value.lower() in existing:
                continue
            existing.add(value.lower())
            session.add(
                Contact(
                    candidate_id=candidate_id,
                    type="email",
                    value=value,
                    source=source,
                    confidence=confidence,
                )
            )
            added += 1
    return added


def find_contacts(candidate_id: int, extra_emails: Optional[list[str]] = None) -> int:
    """Run all free contact sources for a candidate and persist results."""
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        login = candidate.primary_github_login if candidate else None
        site = session.exec(
            select(Identity).where(
                Identity.candidate_id == candidate_id, Identity.platform == Platform.website
            )
        ).first()
        site_url = site.url if site else None

    collected: list[tuple[str, str, float]] = []
    if login:
        for email in github_commit_emails(login):
            collected.append((email, "github_commit", 0.8))
    if site_url:
        for email in scrape_emails_from_url(site_url):
            collected.append((email, "personal_site", 0.7))
    for email in extra_emails or []:
        if _is_real_email(email):
            collected.append((email, "deep_dive", 0.6))

    return _save_contacts(candidate_id, collected)
