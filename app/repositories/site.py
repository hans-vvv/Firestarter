"""Site repository — query helpers for the Site ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Site


def get_site_by_name(session: Session, name: str) -> Site | None:
    """Return the Site with the given name, or None if not found."""
    return session.scalar(select(Site).where(Site.name == name))


def get_all_site_names(session: Session) -> list[str]:
    """Return all site names sorted alphabetically."""
    return list(session.scalars(select(Site.name).order_by(Site.name)))
