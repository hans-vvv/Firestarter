"""Declarative base shared by all SQLAlchemy ORM models."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Common SQLAlchemy declarative base. All ORM models inherit from this."""

    pass
