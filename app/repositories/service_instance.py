"""Service instance repository — query helpers for the ServiceInstance ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ServiceInstance


def get_service_instance_by_name(session: Session, name: str) -> ServiceInstance | None:
    """Return the ServiceInstance with the given svc_name, or None if not found."""
    return session.scalars(
        select(ServiceInstance).where(ServiceInstance.svc_name == name)
    ).one_or_none()
