"""Role repository — query helpers for the Role ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Role


def get_role_by_name(session: Session, name: str) -> Role | None:
    """Return the Role with the given name, or None if not found."""
    return session.scalar(select(Role).where(Role.name == name))


def get_all_role_names(session: Session) -> list[str]:
    """Return all role names sorted alphabetically."""
    return list(session.scalars(select(Role.name).order_by(Role.name)))
