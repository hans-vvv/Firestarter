"""Application configuration — resolves the database URL from environment or defaults."""

from __future__ import annotations

import os

from app.domain.file_locations import PRODUCTION_DB_LOC


def get_database_url() -> str:
    """
    Returns the database URL from env, or a deterministic default.

    Default: the SQLite file at ``PRODUCTION_DB_LOC`` under the live data root
    (the repository root for a plain checkout, or ``FIRESTARTER_DATA`` when set —
    e.g. a mounted volume in a container). ``DATABASE_URL`` overrides it entirely.
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    return f"sqlite:///{PRODUCTION_DB_LOC.path.as_posix()}"
