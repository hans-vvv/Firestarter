"""IntegerResourcePool repository — query helpers for the IntegerResourcePool ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IntegerResourcePool


def get_resource_pool_by_name(session: Session, name: str) -> IntegerResourcePool | None:
    """Return the IntegerResourcePool with the given name, or None if not found."""
    return session.scalar(select(IntegerResourcePool).where(IntegerResourcePool.name == name))
