from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT_DIR = Path(__file__).resolve().parent.parent
WB_NAME = ROOT_DIR / "tests" / "test.xlsx"

# Test-owned addressing policies. The suite must never read the real
# app/services/service_handling/addressing_definitions/ — that is per-environment
# data (gitignored, only present on a seeded machine). See the autouse
# `addressing_policies` fixture below.
ADDRESSING_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "addressing"

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT_DIR)

import logging

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.engine import configure_sqlite_engine
from app.domain.file_locations import FileLocations


@pytest.fixture(scope="session", autouse=True)
def _redirect_logs(tmp_path_factory):
    """Point all file logging at a throwaway dir for the whole test session.

    ``create_app()`` (and the pipeline CLI) call ``setup_logging`` /
    ``setup_audit_logging``, which otherwise write ``logging.log`` /
    ``transactions.log`` into the repo tree. Overriding ``FIRESTARTER_LOG_DIR``
    (resolved at call time by :mod:`app.logging.logger`) keeps tests self
    contained. Any handlers already installed are cleared so setup re-runs
    against the tmp dir.
    """
    os.environ["FIRESTARTER_LOG_DIR"] = str(tmp_path_factory.mktemp("logs"))
    for name in ("app", "audit"):
        lg = logging.getLogger(name)
        for handler in lg.handlers[:]:
            lg.removeHandler(handler)
    # Setup-only: the override stays in place for the whole session (no teardown).
    return


from app.excel_data_handling.excel_data_handler import ExcelDataHandler
from app.excel_data_handling.seed import SeedHandler
from app.services.selectors.selector_engine import SelectorEngine
from app.services.service_handling.resource_pool_allocator import (
    ResourcePoolAllocator,
)
from app.services.service_handling.service_orchestrator import ServiceOrchestrator


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        echo=False,
        connect_args={"check_same_thread": False},
    )
    # Configure the test engine exactly like the application engine (BEGIN
    # control + pragmas) so the suite exercises production transaction
    # semantics — in particular the begin_nested() SAVEPOINTs the ring-migration
    # discovery pattern relies on.
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """A Session joined into an outer transaction that is rolled back at teardown.

    This is SQLAlchemy's documented "joining a session into an external
    transaction" recipe, including the restart-savepoint listener so that
    application code opening its own SAVEPOINTs via ``Session.begin_nested()``
    (e.g. RingMigration's discovery/render steps) nests correctly *inside* the
    per-test transaction. Without the restart, an app-level savepoint rollback
    would consume the outer transaction and later leak state between tests.
    """
    connection = engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autoflush=False, autocommit=False)
    session: Session = SessionLocal()

    # Open an outer SAVEPOINT and re-open it whenever a nested transaction ends,
    # so the enclosing `transaction` survives to be rolled back wholesale below.
    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, trans):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def dummy_service_builder(session):
    return SimpleNamespace(
        selector_engine=SelectorEngine(session=session),
        rpa=ResourcePoolAllocator(session=session),
    )


@pytest.fixture(autouse=True)
def addressing_policies(monkeypatch):
    """Point the addressing-policy resolver at the test fixture, not real data.

    ``AddressingPolicyResolver.install()`` globs ``ADDRESSING_DEF_LOC`` for
    ``*.yaml``. Left alone that reads the real addressing_definitions directory,
    which couples the suite to per-environment data that is gitignored and only
    present on a seeded machine — a fresh clone would fail with "No addressing
    policies found".

    Autouse so no test can silently reacquire that dependency. Tests that need
    different behaviour (e.g. asserting the "no policies at all" error) simply
    re-patch the same attribute themselves; their patch is applied after this
    one and therefore wins.
    """
    import app.services.service_handling.addressing_policy_resolver as resolver_mod

    monkeypatch.setattr(
        resolver_mod,
        "ADDRESSING_DEF_LOC",
        FileLocations(location=str(ADDRESSING_FIXTURE_DIR)),
    )


@pytest.fixture
def no_services(monkeypatch):
    """Silence ALL ServiceOrchestrator service definitions.

    Patches _discover_definitions_by_key to return an empty dict so that
    execute_job builds only topology (devices, cables, CEs) without running
    any service handler. This makes topology fixtures immune to new YAML
    service definitions being added to the project.
    """
    monkeypatch.setattr(
        ServiceOrchestrator,
        "_discover_definitions_by_key",
        lambda self: {},
    )


@pytest.fixture
def seeded_topology(session, no_services):
    """Seed sites, roles, pools and build the full topology (devices + cables).

    ServiceOrchestrator is silenced — no service allocations are made.
    Use this fixture for all tests that only need topology to be present.
    Adding new *_lab_def.yaml service definitions will never break these tests.
    """
    seeder = SeedHandler(session=session, wb_name=WB_NAME)
    edh = ExcelDataHandler(session=session, wb_name=WB_NAME)

    seeder.seed_sites()
    seeder.seed_roles()
    seeder.seed_resource_pools()
    seeder.seed_prefix_pool_types()
    seeder.seed_prefix_pools()

    edh.create_actions_blob_for_devices_loaded_from_excel()
    edh.create_actions_blob_for_cables_loaded_from_excel()
    edh.create_actions_blob_for_dist_devices_loaded_from_excel()
    edh.create_actions_blob_for_pe_ring_cables_from_half_open_rings()
    edh.create_actions_blob_for_ces_loaded_from_excel()
    edh.execute_job(job_name="test")

    session.flush()


@pytest.fixture
def seeded_inventory(seeded_topology):
    """Backward-compatible alias for seeded_topology.

    All existing tests that request seeded_inventory continue to work
    unchanged. New tests should prefer seeded_topology directly.
    """
