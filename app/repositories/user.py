"""User repository — query helpers for the User and UserRole ORM models."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User, UserRole


def get_user_by_username(session: Session, username: str) -> User | None:
    """Return the User with the given username, or None if not found."""
    return session.scalar(select(User).where(User.username == username))


def get_user_by_id(session: Session, user_id: int) -> User | None:
    """Return the User with the given id, or None if not found."""
    return session.scalar(select(User).where(User.id == user_id))


def get_all_users(session: Session) -> list[User]:
    """Return all User rows ordered by username."""
    return list(session.scalars(select(User).order_by(User.username)))


def count_users(session: Session) -> int:
    """Return the total number of User rows."""
    return session.scalar(select(func.count()).select_from(User)) or 0


def get_user_role_by_name(session: Session, name: str) -> UserRole | None:
    """Return the UserRole with the given name, or None if not found."""
    return session.scalar(select(UserRole).where(UserRole.name == name))


def get_all_user_roles(session: Session) -> list[UserRole]:
    """Return all UserRole rows ordered by name."""
    return list(session.scalars(select(UserRole).order_by(UserRole.name)))


def count_admins(session: Session) -> int:
    """Return the number of users holding the ``admin`` role.

    Used by the delete/role-change safety rails so the dashboard can never be
    left without a single administrator.
    """
    return (
        session.scalar(
            select(func.count())
            .select_from(User)
            .join(UserRole, User.role_id == UserRole.id)
            .where(UserRole.name == "admin")
        )
        or 0
    )
