"""Job repository — query helpers for the Job ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job


def get_job_by_name(session: Session, name: str) -> Job | None:
    """Return the Job with the given name, or None if not found."""
    return session.scalar(select(Job).where(Job.name == name))


def get_job_by_id(session: Session, job_id: int) -> Job | None:
    """Return the Job with the given id, or None if not found."""
    return session.scalar(select(Job).where(Job.id == job_id))


def get_all_jobs(session: Session) -> list[Job]:
    """Return all Job rows ordered by id."""
    return list(session.scalars(select(Job).order_by(Job.id)))
