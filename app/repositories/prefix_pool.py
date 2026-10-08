"""Prefix pool repository — query helpers for the PrefixPool ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PrefixPool


def get_prefix_pool_by_name(session: Session, name: str) -> PrefixPool | None:
    """Return the PrefixPool with the given name, or None if not found."""
    return session.scalar(select(PrefixPool).where(PrefixPool.name == name))
