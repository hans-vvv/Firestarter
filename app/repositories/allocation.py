"""DelegatedPrefix repository — query helpers for the DelegatedPrefix ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DelegatedPrefix


def get_delegated_prefix_by_name(session: Session, name: str) -> DelegatedPrefix | None:
    """Return the DelegatedPrefix with the given name, or None if not found."""
    return session.scalar(select(DelegatedPrefix).where(DelegatedPrefix.name == name))
