"""Prefix pool type repository — query helpers for the PrefixPoolType ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PrefixPoolType


def get_prefix_pool_type_by_name(session: Session, name: str) -> PrefixPoolType | None:
    """Return the PrefixPoolType with the given name, or None if not found."""
    return session.scalar(select(PrefixPoolType).where(PrefixPoolType.name == name))
