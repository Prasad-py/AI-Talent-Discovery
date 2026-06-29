"""Database engine + session helpers (SQLite for v1, swappable to Postgres later)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlmodel import Session, SQLModel, create_engine, select

from . import models  # noqa: F401  (ensures tables are registered on import)
from .config import get_settings

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db() -> None:
    """Create all tables if they don't exist."""
    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upsert_candidate_by_github(
    session: Session, login: str, **fields
) -> "models.Candidate":
    """Get-or-create a candidate keyed on their GitHub login."""
    stmt = select(models.Candidate).where(models.Candidate.primary_github_login == login)
    candidate = session.exec(stmt).first()
    if candidate is None:
        candidate = models.Candidate(primary_github_login=login, **fields)
        session.add(candidate)
        session.flush()  # assign an id
    else:
        for key, value in fields.items():
            if value is not None:
                setattr(candidate, key, value)
        candidate.updated_at = models.utcnow()
    return candidate


def upsert_candidate_by_identity(
    session: Session, platform: "models.Platform", handle: str, **fields
) -> "models.Candidate":
    """
    Get-or-create a candidate for a non-GitHub source (e.g. Codeforces), keyed on a
    platform identity rather than a GitHub login.
    """
    stmt = (
        select(models.Candidate)
        .join(models.Identity, models.Identity.candidate_id == models.Candidate.id)
        .where(models.Identity.platform == platform, models.Identity.handle == handle)
    )
    candidate = session.exec(stmt).first()
    if candidate is None:
        candidate = models.Candidate(**fields)
        session.add(candidate)
        session.flush()
        session.add(
            models.Identity(
                candidate_id=candidate.id,
                platform=platform,
                handle=handle,
                confidence=1.0,
            )
        )
        session.flush()
    else:
        for key, value in fields.items():
            if value is not None:
                setattr(candidate, key, value)
        candidate.updated_at = models.utcnow()
    return candidate


def add_signal(session: Session, candidate_id: int, **fields) -> "models.Signal":
    sig = models.Signal(candidate_id=candidate_id, **fields)
    session.add(sig)
    return sig


def get_identity(
    session: Session, candidate_id: int, platform: "models.Platform", handle: Optional[str]
) -> Optional["models.Identity"]:
    stmt = select(models.Identity).where(
        models.Identity.candidate_id == candidate_id,
        models.Identity.platform == platform,
        models.Identity.handle == handle,
    )
    return session.exec(stmt).first()


def add_identity(session: Session, candidate_id: int, **fields) -> "models.Identity":
    existing = get_identity(
        session, candidate_id, fields.get("platform"), fields.get("handle")
    )
    if existing:
        for key, value in fields.items():
            if value is not None:
                setattr(existing, key, value)
        return existing
    ident = models.Identity(candidate_id=candidate_id, **fields)
    session.add(ident)
    return ident
