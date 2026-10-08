"""Engine-level guarantees for the SQLite tuning + transaction-control setup.

These exercise :func:`app.db.engine.configure_sqlite_engine` against a real
on-disk database (WAL is a no-op on ``:memory:``), covering both halves of the
fix: the performance/safety pragmas, and the hand-rolled BEGIN control that
makes ``begin_nested()`` SAVEPOINTs behave — the ring-migration discovery
pattern's "mutate → roll back the savepoint" depends on the latter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.engine import configure_sqlite_engine


@pytest.fixture
def file_engine(tmp_path: Path):
    """A configured, file-backed SQLite engine with a trivial table."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'probe.db').as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    configure_sqlite_engine(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)"))
    return engine


def test_connection_pragmas_applied(file_engine):
    """Every pooled connection comes up with the tuned pragmas set."""
    with file_engine.connect() as conn:
        journal = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        fk = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
        busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        synchronous = conn.exec_driver_sql("PRAGMA synchronous").scalar()
        temp_store = conn.exec_driver_sql("PRAGMA temp_store").scalar()

    assert journal.lower() == "wal"
    assert fk == 1
    assert busy == 15000
    assert synchronous == 1  # NORMAL
    assert temp_store == 2  # MEMORY


def test_savepoint_rollback_isolates_inner_transaction(file_engine):
    """A begin_nested() SAVEPOINT that rolls back must leave no trace, while
    work committed outside it survives — the guarantee the ring-migration
    discovery step relies on."""
    with Session(file_engine) as session:
        session.execute(text("INSERT INTO t (id, v) VALUES (1, 100)"))
        session.commit()

        sp = session.begin_nested()
        session.execute(text("UPDATE t SET v = 999 WHERE id = 1"))
        session.execute(text("INSERT INTO t (id, v) VALUES (2, 200)"))
        session.flush()
        sp.rollback()

        session.commit()

    with file_engine.connect() as conn:
        rows = conn.execute(text("SELECT id, v FROM t ORDER BY id")).all()

    # Row 1 keeps its committed value; row 2 (inserted inside the savepoint)
    # is gone entirely.
    assert rows == [(1, 100)]


def test_select_only_transaction_then_write(file_engine):
    """With BEGIN under our control, a transaction that opens with only reads
    still commits later writes correctly (the lazy-BEGIN failure mode)."""
    with Session(file_engine) as session:
        # Read first — under pysqlite's default lazy BEGIN no transaction would
        # be open here.
        assert session.execute(text("SELECT count(*) FROM t")).scalar() == 0
        session.execute(text("INSERT INTO t (id, v) VALUES (7, 7)"))
        session.commit()

    with file_engine.connect() as conn:
        assert conn.execute(text("SELECT v FROM t WHERE id = 7")).scalar() == 7
