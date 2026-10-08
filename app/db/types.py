"""Custom SQLAlchemy column types.

Currently:
  UTCDateTime — a DateTime column that guarantees timezone-aware UTC on both
                sides of the database, even on backends (like SQLite) that
                cannot preserve timezone information in storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.types import DateTime as SADateTime, TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """A DateTime column that round-trips timezone-aware UTC.

    Why this exists
    ---------------
    SQLAlchemy's stock ``DateTime(timezone=True)`` is, on SQLite, only a hint.
    SQLite has no native datetime/timezone type — values are stored as ISO
    strings, and the default adapter strips ``tzinfo`` on write and returns
    naive datetimes on read. That makes ``DateTime(timezone=True)`` misleading:
    the schema claims aware, but reality is naive.

    UTCDateTime fixes the public API:
      * Writes: rejects naive datetimes (fail loud); normalises any aware
        input to UTC.
      * Reads: re-tags the stored value with ``UTC`` so consumers always see
        an aware UTC datetime.

    Storage on SQLite is still a naive ISO string under the hood — the
    column is implicitly UTC. On Postgres / other backends with native
    ``timestamptz`` the underlying storage would also be tz-aware; this
    wrapper is harmless there.
    """

    impl = SADateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                f"UTCDateTime requires a timezone-aware datetime; got naive ({value!r})."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)
