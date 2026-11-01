from __future__ import annotations

"""Tests for ``ExcelDataHandler.wipe_db`` — specifically that a rebuild keeps the
dashboard accounts.

``wipe_db`` is a staticmethod that talks to the module-level ``engine`` rather
than to an injected session, so these tests point that engine at a temp file
database. An in-memory engine will not do: ``drop_all``/``create_all`` need the
same database to still be there afterwards.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.excel_data_handling.excel_data_handler as edh_mod
from app.db import Base
from app.models import Role, User, UserRole


@pytest.fixture
def wired_engine(tmp_path, monkeypatch):
    """A temp file-backed engine wired into the handler module, with logging stubbed."""
    engine = create_engine(f"sqlite:///{tmp_path / 'wipe.db'}", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(edh_mod, "engine", engine)
    # archive_log/setup_logging rotate real files on disk; irrelevant here.
    monkeypatch.setattr(edh_mod, "archive_log", lambda: None)
    monkeypatch.setattr(edh_mod, "setup_logging", lambda: None)

    return engine


def _seed(engine) -> None:
    """One account (with its role) and one piece of pipeline-derived topology."""
    with Session(engine) as s:
        role = UserRole(name="admin")
        s.add(role)
        s.flush()
        s.add(
            User(
                username="admin",
                password_hash="not-a-real-hash",
                must_change_password=False,
                role_id=role.id,
            )
        )
        s.add(Role(name="core"))
        s.commit()


def test_wipe_db_keeps_dashboard_accounts(wired_engine):
    """Accounts must survive a rebuild: no input file can regenerate them.

    Before this behaviour existed, a rebuild on a live instance dropped every
    account, and the web bootstrap then re-seeded admin/changeme — silently
    resetting the deployment to a well-known credential.
    """
    _seed(wired_engine)

    edh_mod.ExcelDataHandler.wipe_db()

    with Session(wired_engine) as s:
        users = s.execute(select(User)).scalars().all()
        assert [u.username for u in users] == ["admin"]
        assert users[0].password_hash == "not-a-real-hash"
        assert [r.name for r in s.execute(select(UserRole)).scalars().all()] == ["admin"]


def test_wipe_db_still_drops_pipeline_data(wired_engine):
    """The rest of the schema must still be wiped — that is the point of it."""
    _seed(wired_engine)

    edh_mod.ExcelDataHandler.wipe_db()

    with Session(wired_engine) as s:
        assert s.execute(select(Role)).scalars().all() == []


def test_wipe_db_creates_auth_tables_on_a_fresh_database(tmp_path, monkeypatch):
    """On an empty database ``create_all`` must still produce the auth tables.

    Excluding them from the drop must not turn into excluding them from the
    schema, or a first run on a new machine would have nowhere to seed the
    bootstrap admin.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    monkeypatch.setattr(edh_mod, "engine", engine)
    monkeypatch.setattr(edh_mod, "archive_log", lambda: None)
    monkeypatch.setattr(edh_mod, "setup_logging", lambda: None)

    edh_mod.ExcelDataHandler.wipe_db()

    with Session(engine) as s:
        assert s.execute(select(User)).scalars().all() == []
        assert s.execute(select(UserRole)).scalars().all() == []
