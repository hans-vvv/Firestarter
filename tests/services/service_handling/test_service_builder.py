from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.models import ServiceInstance
from app.services.service_handling.service_builder import ServiceBuilder

# ---------------------------------------------------------------------------
# _import_handler — pure static method
# ---------------------------------------------------------------------------


class TestImportHandler:
    def test_imports_class_from_stdlib(self):
        cls = ServiceBuilder._import_handler("collections.OrderedDict")
        import collections

        assert cls is collections.OrderedDict

    def test_imports_class_from_app(self):
        cls = ServiceBuilder._import_handler(
            "app.services.service_handling.service_builder.ServiceBuilder"
        )
        assert cls is ServiceBuilder

    def test_returns_callable_class(self):
        cls = ServiceBuilder._import_handler("io.StringIO")
        import io

        assert cls is io.StringIO
        # Should be instantiable
        instance = cls()
        assert instance is not None

    def test_raises_on_nonexistent_module(self):
        with pytest.raises(ModuleNotFoundError):
            ServiceBuilder._import_handler("no_such_module.SomeClass")

    def test_raises_on_nonexistent_class_in_existing_module(self):
        with pytest.raises(AttributeError):
            ServiceBuilder._import_handler("collections.NonExistentClass")

    def test_raises_on_missing_dot(self):
        with pytest.raises(ValueError, match="not enough values to unpack"):
            ServiceBuilder._import_handler("nodot")


# ---------------------------------------------------------------------------
# prune_orphans — deletes ServiceInstance rows without a definition
# ---------------------------------------------------------------------------


def _builder(session) -> ServiceBuilder:
    """A ServiceBuilder wired only for DB work — engines mocked (unused here)."""
    return ServiceBuilder(
        session=session,
        selector_engine=MagicMock(),
        resource_pool_allocator=MagicMock(),
    )


def _add_instance(session, svc_name: str, tenant: str, variant: str) -> None:
    session.add(ServiceInstance(svc_name=svc_name, tenant=tenant, variant=variant, computed={}))
    session.flush()


def _remaining_keys(session) -> set[tuple[str, str, str]]:
    rows = session.execute(
        select(ServiceInstance.svc_name, ServiceInstance.tenant, ServiceInstance.variant)
    ).all()
    return {tuple(r) for r in rows}


class TestPruneOrphans:
    def test_removes_orphan_and_keeps_live(self, session):
        _add_instance(session, "vprn", "production", "legacy")  # orphan (renamed away)
        _add_instance(session, "vprn", "demo", "customer_a")
        _add_instance(session, "isis", "production", "default")

        valid = {
            ("vprn", "demo", "customer_a"),
            ("isis", "production", "default"),
        }
        pruned = _builder(session).prune_orphans(valid_keys=valid)

        assert pruned == [("vprn", "production", "legacy")]
        assert _remaining_keys(session) == valid

    def test_no_orphans_returns_empty_and_deletes_nothing(self, session):
        _add_instance(session, "isis", "production", "default")
        _add_instance(session, "vprn", "demo", "customer_a")
        valid = {
            ("isis", "production", "default"),
            ("vprn", "demo", "customer_a"),
        }

        pruned = _builder(session).prune_orphans(valid_keys=valid)

        assert pruned == []
        assert _remaining_keys(session) == valid

    def test_empty_table_returns_empty(self, session):
        assert _builder(session).prune_orphans(valid_keys={("isis", "lab", "default")}) == []

    def test_empty_valid_keys_prunes_everything(self, session):
        _add_instance(session, "isis", "lab", "default")
        _add_instance(session, "bgp", "lab", "default")

        pruned = _builder(session).prune_orphans(valid_keys=set())

        assert pruned == [("bgp", "lab", "default"), ("isis", "lab", "default")]
        assert _remaining_keys(session) == set()

    def test_multiple_orphans_returned_sorted(self, session):
        _add_instance(session, "vprn", "production", "zzz")
        _add_instance(session, "vprn", "production", "aaa")
        _add_instance(session, "isis", "lab", "default")  # live

        pruned = _builder(session).prune_orphans(valid_keys={("isis", "lab", "default")})

        assert pruned == [
            ("vprn", "production", "aaa"),
            ("vprn", "production", "zzz"),
        ]
