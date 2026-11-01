from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.models import Device, IntegerAllocation, IntegerResourcePool, Interface
from app.repositories import get_all_devices
from app.services.service_handling.feature_handlers.evpn_vpls import (
    EVPN_VPLSFeatureHandler,
)
from app.utils import require

VPLS_NAME = "MGMT-CE"
VPLS_NAME_DC = "INB-MGMT-1200"  # DC convention: numeric suffix encodes EVI and S-VLAN


@pytest.fixture
def evpn_vpls_ctx():
    return {
        "service_data": {
            "service": "evpn_vpls",
            "tenant": "lab",
            "variant": "ce_mgmt",
            "selectors": {
                "devices": {
                    "evpn_vpls": {
                        "match": {
                            "labels": {"tenant": "lab"},
                            "role": {"include": ["pe"]},
                        }
                    }
                }
            },
            "parameters": {
                "vplss": [
                    {
                        "name": VPLS_NAME,
                        "service_id_pool": {"name": "evpn_vpls_ce_mgmt_pool_lab"},
                        "s_vlan": "20.*",
                        "qos_policy_name": "DEMO-CUSTOMER-SIDE-VLAN-QOS",
                        "split_horizon_group_name": "MGMT-CE",
                        "routed_vpls": True,
                    }
                ],
            },
        }
    }


@pytest.fixture
def evpn_vpls_pool(session, seeded_inventory):
    pool = session.scalars(
        select(IntegerResourcePool).where(IntegerResourcePool.name == "evpn_vpls_ce_mgmt_pool_lab")
    ).one_or_none()
    if pool is None:
        pool = IntegerResourcePool(
            name="evpn_vpls_ce_mgmt_pool_lab",
            range_start=1000,
            range_end=2000,
        )
        session.add(pool)
        session.flush()
    return pool


@pytest.fixture
def pe_devices(session, dummy_service_builder):
    all_devices = get_all_devices(session)
    selector = {
        "match": {
            "labels": {"tenant": "lab"},
            "role": {"include": ["pe"]},
        }
    }
    devices = dummy_service_builder.selector_engine.select(all_devices, selector)
    assert devices, "No pe lab devices found for EVPN VPLS test"
    return devices


@pytest.fixture
def evpn_vpls_ready_interfaces(session, pe_devices):
    # The topology builder marks CE-facing interfaces with evpn_esi="needs esi".
    # Clear those markers so the VPLS handler sees a ready underlay.
    session.execute(
        update(Interface).where(Interface.evpn_esi == "needs esi").values(evpn_esi=None)
    )
    session.flush()

    interfaces = []

    for idx, dev in enumerate(pe_devices[:2], start=1):
        db_dev = session.scalars(select(Device).where(Device.id == dev.id)).one()
        iface = Interface(
            name=f"Bundle-Ether20{idx}",
            device=db_dev,
            intf_role="NNI",
            evpn_esi=f"0000.0000.0000.0000.000{idx}",
            in_use=True,
        )
        session.add(iface)
        interfaces.append(iface)

    session.flush()
    return interfaces


def test_evpn_vpls_returns_device_scoped_output(
    session,
    seeded_inventory,
    evpn_vpls_pool,
    evpn_vpls_ready_interfaces,
    evpn_vpls_ctx,
    dummy_service_builder,
):
    handler = EVPN_VPLSFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    result = handler.compute(evpn_vpls_ctx)

    all_devices = get_all_devices(session)
    sel = evpn_vpls_ctx["service_data"]["selectors"]["devices"]["evpn_vpls"]
    expected = {d.hostname for d in dummy_service_builder.selector_engine.select(all_devices, sel)}

    assert set(result.keys()) == expected

    for hostname in expected:
        svc = result[hostname]["evpn_vpls"]["variant"]["ce_mgmt"][VPLS_NAME]
        assert "interfaces" in svc
        assert "evi_id" in svc
        assert svc["qos_policy_name"] == "DEMO-CUSTOMER-SIDE-VLAN-QOS"
        assert svc["s_vlan"] == "20.*"
        assert svc["vpls_name"] == VPLS_NAME
        assert svc["routed_vpls"] is True
        assert svc["split_horizon_group_name"] == "MGMT-CE"


def test_evpn_vpls_fails_if_underlay_not_ready(
    session,
    seeded_inventory,
    evpn_vpls_pool,
    pe_devices,
    evpn_vpls_ctx,
    dummy_service_builder,
):
    dev = session.scalars(select(Device).where(Device.id == pe_devices[0].id)).one()
    session.add(
        Interface(
            name="Bundle-Ether999",
            device=dev,
            intf_role="NNI",
            evpn_esi="needs esi",
            in_use=True,
        )
    )
    session.flush()

    handler = EVPN_VPLSFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    with pytest.raises(RuntimeError, match="Underlay not ready"):
        handler.compute(evpn_vpls_ctx)


def test_evpn_vpls_is_idempotent(
    session,
    seeded_inventory,
    evpn_vpls_pool,
    evpn_vpls_ready_interfaces,
    evpn_vpls_ctx,
    dummy_service_builder,
):
    handler = EVPN_VPLSFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    first = handler.compute(evpn_vpls_ctx)
    session.flush()
    second = handler.compute(evpn_vpls_ctx)

    assert first == second


def test_evpn_vpls_shares_evi_id_per_pair_label(
    session,
    seeded_inventory,
    evpn_vpls_pool,
    evpn_vpls_ready_interfaces,
    evpn_vpls_ctx,
    dummy_service_builder,
):
    handler = EVPN_VPLSFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    result = handler.compute(evpn_vpls_ctx)

    by_pair_label: dict[str, int] = {}
    for dev in evpn_vpls_ready_interfaces:
        hostname = dev.device.hostname
        db_dev = require(
            session.scalars(select(Device).where(Device.hostname == hostname)).one_or_none(),
            f"Device {hostname} missing",
        )
        pair_label = db_dev.labels.get("pair_label") or db_dev.hostname
        evi_id = result[hostname]["evpn_vpls"]["variant"]["ce_mgmt"][VPLS_NAME]["evi_id"]

        if pair_label in by_pair_label:
            assert by_pair_label[pair_label] == evi_id
        else:
            by_pair_label[pair_label] = evi_id


def test_evpn_vpls_persists_allocation_per_pair_label(
    session,
    seeded_inventory,
    evpn_vpls_pool,
    evpn_vpls_ready_interfaces,
    evpn_vpls_ctx,
    dummy_service_builder,
):
    handler = EVPN_VPLSFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    result = handler.compute(evpn_vpls_ctx)
    session.flush()

    for hostname in result:
        db_dev = require(
            session.scalars(select(Device).where(Device.hostname == hostname)).one_or_none(),
            f"Device {hostname} missing",
        )
        raw_pair_label = db_dev.labels.get("pair_label") or db_dev.hostname
        pair_label = raw_pair_label.split(":", 1)[-1]
        allocation_name = f"evpn_vpls_ce_mgmt_{pair_label}_{VPLS_NAME}"

        row = session.scalars(
            select(IntegerAllocation)
            .where(IntegerAllocation.allocation_name == allocation_name)
            .where(IntegerAllocation.pool_key == pair_label)
        ).one_or_none()
        assert row is not None
        assert row.value is not None


# ------------------------------------------------------------------ #
# Declared attachment model — att_circuits: [] edge case              #
# ------------------------------------------------------------------ #


@pytest.fixture
def evpn_vpls_declared_ctx():
    """
    Service context using the declared attachment model.

    att_circuits is an explicit empty list — the key is present but the list
    is empty.  This is the edge case that caused the regression: the original
    _build_att_circuits_context used `is not None` to detect the declared
    model, but the refactored _attachment_model incorrectly used a truthy
    check, treating [] as falsy and falling through to topology_driven.

    No service_id_pool is defined here intentionally: if the handler
    incorrectly routes this to topology_driven it will crash with KeyError,
    which is the exact signal that the model detection is broken.
    """
    return {
        "service_data": {
            "service": "evpn_vpls",
            "tenant": "lab",
            "variant": "dc_mgmt",
            "selectors": {
                "devices": {
                    "evpn_vpls": {
                        "match": {
                            "labels": {"tenant": "lab"},
                            "role": {"include": ["pe"]},
                        }
                    }
                }
            },
            "parameters": {
                "vplss": [
                    {
                        "name": VPLS_NAME_DC,
                        "att_circuits": [],
                    }
                ],
            },
        }
    }


def test_evpn_vpls_declared_empty_att_circuits_derives_evi_from_name(
    session,
    seeded_inventory,
    evpn_vpls_ready_interfaces,
    evpn_vpls_declared_ctx,
    dummy_service_builder,
):
    """
    att_circuits: [] must be treated as declared, not topology_driven.

    The declared model derives EVI and S-VLAN from the VPLS name suffix
    instead of allocating from a pool.  No service_id_pool is required.
    """
    handler = EVPN_VPLSFeatureHandler(
        session=session,
        service_builder=dummy_service_builder,
    )

    result = handler.compute(evpn_vpls_declared_ctx)

    assert result, "Handler returned no output"
    for hostname in result:
        vpls = result[hostname]["evpn_vpls"]["variant"]["dc_mgmt"][VPLS_NAME_DC]
        assert vpls["evi_id"] == 1200  # derived from "INB-MGMT-1200" suffix
        assert vpls["s_vlan"] == "120"  # 1200 // 10
        assert vpls["interfaces"] == []  # empty att_circuits produces no interfaces
        assert vpls["vpls_name"] == VPLS_NAME_DC
