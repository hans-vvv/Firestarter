from __future__ import annotations

"""Tests for Printer's service-instance exclusion filter.

The per-service snippet view renders a device with a chosen subset of its
service instances excluded, so it can diff against the full render. These
tests cover the merge-level behaviour of ``exclude_instance_ids`` directly,
using the in-memory ``session`` fixture — no rendering, no templates.
"""

from app.models import ServiceInstance
from app.printing.printer import Printer


def _add_instance(session, *, svc_name, variant, computed):
    inst = ServiceInstance(
        svc_name=svc_name,
        tenant="lab",
        variant=variant,
        computed=computed,
    )
    session.add(inst)
    session.flush()
    return inst


class TestExcludeInstanceIds:
    def test_default_merges_every_instance(self, session):
        _add_instance(session, svc_name="vprn", variant="v1", computed={"r1": {"vprn": {"a": 1}}})
        _add_instance(session, svc_name="isis", variant="v1", computed={"r1": {"isis": {"b": 2}}})

        merged = Printer(session=session)._collect_services_computed

        assert set(merged["r1"]) == {"vprn", "isis"}

    def test_excluded_instance_is_dropped_from_merge(self, session):
        vprn = _add_instance(
            session, svc_name="vprn", variant="v1", computed={"r1": {"vprn": {"a": 1}}}
        )
        _add_instance(session, svc_name="isis", variant="v1", computed={"r1": {"isis": {"b": 2}}})

        merged = Printer(session=session, exclude_instance_ids={vprn.id})._collect_services_computed

        assert set(merged["r1"]) == {"isis"}

    def test_excluding_all_yields_empty_intent(self, session):
        a = _add_instance(
            session, svc_name="vprn", variant="v1", computed={"r1": {"vprn": {"a": 1}}}
        )
        b = _add_instance(
            session, svc_name="isis", variant="v1", computed={"r1": {"isis": {"b": 2}}}
        )

        merged = Printer(
            session=session, exclude_instance_ids={a.id, b.id}
        )._collect_services_computed

        assert merged == {}

    def test_unknown_id_in_exclude_set_is_harmless(self, session):
        _add_instance(session, svc_name="isis", variant="v1", computed={"r1": {"isis": {"b": 2}}})

        merged = Printer(session=session, exclude_instance_ids={9999})._collect_services_computed

        assert set(merged["r1"]) == {"isis"}


def _vprn_instance(session, *, objects):
    """A vprn instance whose 'vprn' ctx bundles several named VPRNs."""
    computed = {
        "r1": {"vprn": {"variant": {"v1": {name: {"k": name} for name in objects}}}},
    }
    return _add_instance(session, svc_name="vprn", variant="v1", computed=computed)


class TestExcludeObjects:
    def test_prunes_only_the_named_object(self, session):
        inst = _vprn_instance(session, objects=["A", "B"])

        merged = Printer(
            session=session, exclude_objects={inst.id: [("vprn", "v1", "A")]}
        )._collect_services_computed

        assert set(merged["r1"]["vprn"]["variant"]["v1"]) == {"B"}

    def test_does_not_mutate_the_orm_blob(self, session):
        inst = _vprn_instance(session, objects=["A", "B"])

        _ = Printer(
            session=session, exclude_objects={inst.id: [("vprn", "v1", "A")]}
        )._collect_services_computed

        assert set(inst.computed["r1"]["vprn"]["variant"]["v1"]) == {"A", "B"}

    def test_unknown_object_name_is_harmless(self, session):
        inst = _vprn_instance(session, objects=["A"])

        merged = Printer(
            session=session, exclude_objects={inst.id: [("vprn", "v1", "NOPE")]}
        )._collect_services_computed

        assert set(merged["r1"]["vprn"]["variant"]["v1"]) == {"A"}

    def test_pruning_all_objects_leaves_empty_variant(self, session):
        inst = _vprn_instance(session, objects=["A", "B"])

        merged = Printer(
            session=session,
            exclude_objects={inst.id: [("vprn", "v1", "A"), ("vprn", "v1", "B")]},
        )._collect_services_computed

        assert merged["r1"]["vprn"]["variant"]["v1"] == {}
