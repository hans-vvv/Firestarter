"""Alembic migration environment for Firestarter.

This project historically built its schema with ``Base.metadata.create_all``
(see ``app/web/__init__.py:ensure_schema`` and ``ExcelDataHandler.wipe_db``).
Alembic is introduced here as the forward path for schema changes against the
live, git-tracked ``app.db`` — additive migrations run *over* the existing
database without dropping anything.

Two things make the adoption seamless:

* The database URL is taken from :func:`app.config.get_database_url` (honouring
  ``DATABASE_URL``), so migrations always target the same DB as the app.
* ``target_metadata`` is the shared :class:`app.db.base.Base` metadata, so
  ``alembic revision --autogenerate`` diffs against the real ORM models.

The migration scripts live under ``migrations/versions``.

SQLite needs batch mode (``render_as_batch=True``) for any ALTER TABLE, so it is
enabled for both offline and online runs.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the models package so every ORM table is registered on Base.metadata
# before autogenerate diffs against it.
import app.models  # noqa: F401
from app.config import get_database_url
from app.db.base import Base

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

# Resolve the real database URL at runtime, overriding the ini placeholder.
config.set_main_option("sqlalchemy.url", get_database_url())

if config.config_file_name is not None:
    # disable_existing_loggers=False so running migrations in-process (e.g. from
    # the test suite) does not tear down loggers configured elsewhere.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no DBAPI)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
