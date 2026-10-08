"""Prefix repository — query helpers for the Prefix ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Prefix, PrefixPool


def get_prefixes_by_pool(session: Session, pool: PrefixPool) -> list[Prefix]:
    """Return all Prefix rows belonging to a pool, ordered by prefix string."""
    return list(
        session.scalars(select(Prefix).where(Prefix.pool_id == pool.id).order_by(Prefix.prefix))
    )
