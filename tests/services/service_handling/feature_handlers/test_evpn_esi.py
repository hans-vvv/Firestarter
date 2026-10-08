from __future__ import annotations

from collections import defaultdict

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models import Device, IntegerAllocation, IntegerResourcePool, Interface
from app.services.service_handling.feature_handlers.evpn_esi import (
    EVPN_ESIFeatureHandler,
)


@pytest.fixture
def evpn_esi_ctx():
    return {
        "service_data": {
            "tenant": "lab",
            "service": "evpn_evi",
            "variant": "default",
            "selectors": {
                "devices": {
                    "evpn_esi": {
                        "match": {
                            "role": {"include": ["pe"]},
                        }
                    }
                }
            },
            "parameters": {
                "allocations": {
                    "evpn_esi": {
                        "pool": "evpn_esi_pool_lab",
                    }
                }
            },
        }
    }


@pytest.fixture
def evpn_esi_ready_interfaces(session, seeded_inventory):
    def get_device(hostname: str) -> Device:
        dev = session.scalars(select(Device).where(Device.hostname == hostname)).one_or_none()
        assert dev is not None, f"Device {hostname} not found"
        return dev

    pe1 = get_device("pe1.tst-001")
    pe2 = get_device("pe2.tst-001")

    interfaces = []

    for dev in (pe1, pe2):
        iface = Interface(
            name="lag-33",
            device=dev,
            intf_role="NNI",
            evpn_esi="needs esi",
            in_use=True,
        )
        session.add(iface)
        interfaces.append(iface)

    session.flush()
    return interfaces


def test_evpn_esi_assigns_esi_to_all_interfaces_marked_needs_esi(
    session,
    seeded_inventory,
    evpn_esi_ready_interfaces,
    dummy_service_builder,
    evpn_esi_ctx,
):
    handler = EVPN_ESIFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    handler.compute(svc_ctx=evpn_esi_ctx)

    refreshed = list(
        session.scalars(
            select(Interface).where(Interface.id.in_([i.id for i in evpn_esi_ready_interfaces]))
        )
    )

    for iface in refreshed:
        assert iface.evpn_esi is not None
        assert iface.evpn_esi != ""
        assert iface.evpn_esi != "needs esi"


def test_evpn_esi_is_shared_per_site_and_interface_name_group(
    session,
    seeded_inventory,
    evpn_esi_ready_interfaces,
    dummy_service_builder,
    evpn_esi_ctx,
):
    handler = EVPN_ESIFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    handler.compute(svc_ctx=evpn_esi_ctx)

    ifaces = list(
        session.scalars(
            select(Interface)
            .options(selectinload(Interface.device).selectinload(Device.site))
            .where(Interface.id.in_([i.id for i in evpn_esi_ready_interfaces]))
        )
    )

    groups: dict[tuple[str, str], set[str]] = defaultdict(set)

    for iface in ifaces:
        site_name = iface.device.site.name
        groups[(site_name, iface.name)].add(iface.evpn_esi)

    for (_, _), esi_values in groups.items():
        assert len(esi_values) == 1


def test_evpn_esi_is_idempotent_no_new_allocations_on_rerun(
    session,
    seeded_inventory,
    evpn_esi_ready_interfaces,
    dummy_service_builder,
    evpn_esi_ctx,
):
    handler = EVPN_ESIFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    pool = session.scalars(
        select(IntegerResourcePool).where(IntegerResourcePool.name == "evpn_esi_pool_lab")
    ).one()

    def allocation_count() -> int:
        return session.scalar(
            select(func.count())
            .select_from(IntegerAllocation)
            .where(IntegerAllocation.pool_id == pool.id)
        )

    before_first = allocation_count()
    handler.compute(svc_ctx=evpn_esi_ctx)
    session.flush()
    after_first = allocation_count()

    assert after_first > before_first

    snapshot = {i.id: i.evpn_esi for i in session.scalars(select(Interface))}

    before_second = allocation_count()
    handler.compute(svc_ctx=evpn_esi_ctx)
    session.flush()
    after_second = allocation_count()

    assert after_second == before_second

    snapshot_after = {i.id: i.evpn_esi for i in session.scalars(select(Interface))}

    assert snapshot_after == snapshot


def test_evpn_esi_returns_device_scoped_context(
    session,
    seeded_inventory,
    evpn_esi_ready_interfaces,
    dummy_service_builder,
    evpn_esi_ctx,
):
    handler = EVPN_ESIFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    result = handler.compute(svc_ctx=evpn_esi_ctx)

    assert "pe1.tst-001" in result
    assert "pe2.tst-001" in result

    for hostname in ("pe1.tst-001", "pe2.tst-001"):
        assert "evpn_esi" in result[hostname]
        assert "variant" in result[hostname]["evpn_esi"]
        assert "default" in result[hostname]["evpn_esi"]["variant"]

        interfaces = result[hostname]["evpn_esi"]["variant"]["default"]["interfaces"]
        assert isinstance(interfaces, list)
        assert interfaces[0]["if_name"] == "lag-33"
        assert "esi" in interfaces[0]
