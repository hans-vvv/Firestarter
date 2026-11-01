from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import app.models  # register tables on Base.metadata
from app.db.base import Base

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    # Pin script_location absolutely so the test is CWD-independent.
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point ``get_database_url`` (via ``DATABASE_URL``) at a throwaway sqlite file.

    ``env.py`` resolves the URL at run time, so setting the env var before
    invoking Alembic targets this temp database.
    """
    db_path = tmp_path / "alembic_test.db"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def test_upgrade_creates_ce_mgmt_address(temp_db):
    command.upgrade(_alembic_config(), "head")

    engine = create_engine(temp_db)
    inspector = inspect(engine)
    assert inspector.has_table("ce_mgmt_address")
    assert inspector.has_table("alembic_version")
    col_names = {c["name"] for c in inspector.get_columns("ce_mgmt_address")}
    assert {"ce_hostname", "pair_label", "prefix", "address", "created_at"} <= col_names


def test_upgrade_is_idempotent(temp_db):
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    # Running again must be a no-op, not an error.
    command.upgrade(cfg, "head")
    assert inspect(create_engine(temp_db)).has_table("ce_mgmt_address")


def test_upgrade_noops_when_table_preexists(temp_db):
    # Simulate a create_all-built database that already has the table; the
    # guarded migration must skip creation instead of failing.
    engine = create_engine(temp_db)
    Base.metadata.tables["ce_mgmt_address"].create(engine)

    command.upgrade(_alembic_config(), "head")  # must not raise
    assert inspect(engine).has_table("ce_mgmt_address")


def test_downgrade_drops_table(temp_db):
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert not inspect(create_engine(temp_db)).has_table("ce_mgmt_address")


def test_cable_length_backfills_existing_rows(temp_db):
    # Simulate a live create_all-built database whose ``cable`` table predates the
    # ``cable_length`` column and already holds rows. The migration must add the
    # column, backfill existing rows to 1, and leave it NOT NULL.
    from sqlalchemy import text

    engine = create_engine(temp_db)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE cable (id INTEGER PRIMARY KEY, status TEXT)"))
        conn.execute(text("INSERT INTO cable (id, status) VALUES (1, 'connected'), (2, 'planned')"))

    command.upgrade(_alembic_config(), "head")

    inspector = inspect(engine)
    col = next(c for c in inspector.get_columns("cable") if c["name"] == "cable_length")
    assert col["nullable"] is False
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT cable_length FROM cable ORDER BY id")).fetchall()
    assert [r[0] for r in rows] == [1, 1]


def test_drop_migration_job_removes_table_and_reservation_columns(temp_db):
    # Simulate a live database from before the ring-migration tool was removed:
    # the ``migration_job`` table exists and ``prefix`` still carries the two
    # reservation FKs, their indexes and the xor CHECK constraint. The migration
    # must drop all of it while keeping the rows.
    from sqlalchemy import text

    engine = create_engine(temp_db)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE migration_job (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL,"
                " description VARCHAR(512), status VARCHAR(11) NOT NULL, snippets_blob JSON)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE prefix (id INTEGER PRIMARY KEY, prefix VARCHAR(64) NOT NULL UNIQUE,"
                " status VARCHAR(9) NOT NULL, pool_id INTEGER NOT NULL,"
                " reserved_by_job_id INTEGER, retired_by_job_id INTEGER,"
                " CONSTRAINT fk_prefix_reserved_by_job FOREIGN KEY(reserved_by_job_id)"
                " REFERENCES migration_job (id),"
                " CONSTRAINT fk_prefix_retired_by_job FOREIGN KEY(retired_by_job_id)"
                " REFERENCES migration_job (id),"
                " CONSTRAINT ck_prefix_reservation_xor_retirement CHECK"
                " (NOT (reserved_by_job_id IS NOT NULL AND retired_by_job_id IS NOT NULL)))"
            )
        )
        conn.execute(
            text("CREATE INDEX ix_prefix_reserved_by_job_id ON prefix (reserved_by_job_id)")
        )
        conn.execute(text("CREATE INDEX ix_prefix_retired_by_job_id ON prefix (retired_by_job_id)"))
        conn.execute(text("INSERT INTO migration_job (name, status) VALUES ('m1', 'prepared')"))
        conn.execute(
            text(
                "INSERT INTO prefix (prefix, status, pool_id, reserved_by_job_id)"
                " VALUES ('10.0.0.0/31', 'reserved', 1, 1)"
            )
        )

    command.upgrade(_alembic_config(), "head")

    inspector = inspect(engine)
    assert not inspector.has_table("migration_job")
    assert {c["name"] for c in inspector.get_columns("prefix")} == {
        "id",
        "prefix",
        "status",
        "pool_id",
    }
    assert inspector.get_check_constraints("prefix") == []
    assert inspector.get_indexes("prefix") == []
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT prefix, status FROM prefix")).fetchall()
    assert rows == [("10.0.0.0/31", "reserved")]


def test_drop_connector_type_removes_table(temp_db):
    from sqlalchemy import text

    engine = create_engine(temp_db)
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE connector_type (id INTEGER PRIMARY KEY, name VARCHAR(32) NOT NULL)")
        )
        conn.execute(text("INSERT INTO connector_type (name) VALUES ('c1-400g')"))

    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    assert not inspect(engine).has_table("connector_type")

    # Downgrade recreates the (empty) vocabulary table; upgrade drops it again.
    command.downgrade(cfg, "0003")
    assert inspect(engine).has_table("connector_type")
    command.upgrade(cfg, "head")
    assert not inspect(engine).has_table("connector_type")


def test_drop_bng_cluster_removes_table_and_device_column(temp_db):
    # Simulate a live database from before the BNG/HSI services were removed:
    # ``bng_cluster`` exists and ``device`` still carries the FK column with its
    # index. The migration must drop both while keeping the device rows.
    from sqlalchemy import text

    engine = create_engine(temp_db)
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE bng_cluster (id INTEGER PRIMARY KEY, name VARCHAR(64) NOT NULL)")
        )
        conn.execute(
            text(
                "CREATE TABLE device (id INTEGER PRIMARY KEY, hostname VARCHAR(64) NOT NULL,"
                " bng_cluster_id INTEGER,"
                " CONSTRAINT fk_device_bng_cluster FOREIGN KEY(bng_cluster_id)"
                " REFERENCES bng_cluster (id))"
            )
        )
        conn.execute(text("CREATE INDEX ix_device_bng_cluster_id ON device (bng_cluster_id)"))
        conn.execute(text("INSERT INTO bng_cluster (name) VALUES ('c1')"))
        conn.execute(
            text("INSERT INTO device (hostname, bng_cluster_id) VALUES ('pe1.tst-001', 1)")
        )

    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    assert not inspector.has_table("bng_cluster")
    assert {c["name"] for c in inspector.get_columns("device")} == {"id", "hostname"}
    assert inspector.get_indexes("device") == []
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT hostname FROM device")).fetchall()
    assert rows == [("pe1.tst-001",)]

    # Downgrade puts the (empty) table and column back; upgrade drops them again.
    command.downgrade(cfg, "0004")
    inspector = inspect(engine)
    assert inspector.has_table("bng_cluster")
    assert "bng_cluster_id" in {c["name"] for c in inspector.get_columns("device")}
    command.upgrade(cfg, "head")
    assert not inspect(engine).has_table("bng_cluster")
