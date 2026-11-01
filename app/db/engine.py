"""SQLAlchemy engine setup: SQLite pragmas + correct transaction control.

The stdlib ``sqlite3`` driver (pysqlite) does two things that quietly break
SQLAlchemy's transaction and SAVEPOINT bookkeeping:

* it emits ``BEGIN`` **lazily** — only right before the first DML statement, so a
  logical transaction that issues only ``SELECT``s never actually opens one; and
* it issues an implicit ``COMMIT`` **before any DDL**, silently ending the
  transaction you thought you were in.

Together these corrupt nested-transaction (``begin_nested()`` / SAVEPOINT)
semantics — which this codebase relies on for the ring-migration "discovery"
pattern (mutate → render → roll the savepoint back). The remedy SQLAlchemy
documents is to take BEGIN into our own hands: set ``isolation_level = None`` so
the driver stops managing transactions, and emit ``BEGIN`` ourselves from the
engine's ``begin`` event.

Setting ``isolation_level = None`` has a second benefit: connection pragmas then
run in true autocommit, which some of them (``foreign_keys``, ``journal_mode``)
require — they are silently ignored if executed inside an open transaction.

The pragmas tune SQLite for this app's access pattern (one local file, a
synchronous Flask/gunicorn front end, occasional bulk data loads): WAL so
readers never block the writer, ``synchronous=NORMAL`` (durable under WAL, far
cheaper than FULL), and a generous ``busy_timeout`` so a writer waits rather
than instantly failing with "database is locked" while a bulk load holds the
write lock.
"""

from __future__ import annotations

from sqlite3 import Connection as SQLite3Connection

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from app.config import get_database_url

# Per-connection SQLite pragmas. ``journal_mode=WAL`` is persisted in the
# database header (so re-asserting it every connect is a harmless no-op); the
# rest are connection-scoped and must be set on every connect.
_SQLITE_PRAGMAS = (
    "PRAGMA foreign_keys=ON;",  # enforce FK constraints (off by default in SQLite)
    "PRAGMA journal_mode=WAL;",  # readers don't block the single writer
    "PRAGMA synchronous=NORMAL;",  # safe under WAL; much cheaper than FULL
    "PRAGMA busy_timeout=15000;",  # wait up to 15s on a locked DB (bulk loads)
    "PRAGMA cache_size=-64000;",  # 64 MiB page cache (negative value = KiB)
    "PRAGMA temp_store=MEMORY;",  # temp tables / sort scratch in RAM
)


def configure_sqlite_engine(engine: Engine) -> Engine:
    """Register the connect/begin handlers that make SQLite behave correctly.

    Wires up two engine events:

    * ``connect`` — hand BEGIN control to SQLAlchemy (``isolation_level = None``)
      and apply :data:`_SQLITE_PRAGMAS` in autocommit; and
    * ``begin`` — emit the ``BEGIN`` the driver no longer emits.

    Applied to the application engine below and, in the test suite, to the
    in-memory engine, so tests exercise the same transaction semantics as
    production. Returns *engine* for convenient chaining.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, connection_record):
        if not isinstance(dbapi_connection, SQLite3Connection):
            return
        # Disable pysqlite's implicit BEGIN/COMMIT handling. Must happen before
        # the pragmas so they run in true autocommit (foreign_keys / journal_mode
        # are ignored inside a transaction).
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            for pragma in _SQLITE_PRAGMAS:
                cursor.execute(pragma)
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        # Now that the driver won't, we emit BEGIN ourselves so every logical
        # transaction (and any SAVEPOINT nested within it) is genuinely open.
        conn.exec_driver_sql("BEGIN")

    return engine


DATABASE_URL = get_database_url()

# SQLite-specific connect args (only applied when needed)
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args=connect_args,
)

if DATABASE_URL.startswith("sqlite"):
    configure_sqlite_engine(engine)
