from __future__ import annotations

import pytest

from app.repositories import get_all_devices
from app.services.service_handling.feature_handlers.isis import ISISCoreFeatureHandler


@pytest.fixture
def isis_service_ctx():
    return {
        "service_data": {
            "tenant": "lab",
            "service": "isis",
            "variant": "default",
            "selectors": {
                "devices": {
                    "isis": {
                        "match": {
                            "role": {
                                "include": [
                                    "core",
                                    "rr",
                                    "pe",
                                ]
                            },
                        }
                    }
                }
            },
            "features": {
                "isis": {
                    "instance_id": 0,
                    "level": "L2",
                    "metric_style": "wide",
                    "set_overload_bit_for_roles": ["rr"],
                }
            },
            "interface_features": {
                "isis": {
                    "network_type": "point-to-point",
                }
            },
        }
    }


def test_isis_returns_device_scoped_output(
    session,
    seeded_inventory,
    isis_service_ctx,
    dummy_service_builder,
):
    handler = ISISCoreFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = handler.compute(isis_service_ctx)

    all_devices = get_all_devices(session)
    sel = isis_service_ctx["service_data"]["selectors"]["devices"]["isis"]
    selected = dummy_service_builder.selector_engine.select(all_devices, sel)

    expected_hostnames = {d.hostname for d in selected if d.hostname in result}

    assert set(result.keys()) == expected_hostnames

    for hostname in expected_hostnames:
        assert "isis" in result[hostname]
        assert "instances" in result[hostname]["isis"]
        assert "0" in result[hostname]["isis"]["instances"]

        svc = result[hostname]["isis"]["instances"]["0"]
        assert "interfaces" in svc
        assert isinstance(svc["interfaces"], list)
        assert len(svc["interfaces"]) > 0


def test_isis_output_is_deterministic(
    session,
    seeded_inventory,
    isis_service_ctx,
    dummy_service_builder,
):
    handler = ISISCoreFeatureHandler(session=session, service_builder=dummy_service_builder)

    first = handler.compute(isis_service_ctx)
    second = handler.compute(isis_service_ctx)

    assert first == second


def test_isis_overload_bit_is_set_for_configured_roles(
    session,
    seeded_inventory,
    isis_service_ctx,
    dummy_service_builder,
):
    handler = ISISCoreFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = handler.compute(isis_service_ctx)

    all_devices = get_all_devices(session)
    sel = isis_service_ctx["service_data"]["selectors"]["devices"]["isis"]
    selected = dummy_service_builder.selector_engine.select(all_devices, sel)

    overload_roles = set(
        isis_service_ctx["service_data"]["features"]["isis"]["set_overload_bit_for_roles"]
    )

    for dev in selected:
        if dev.hostname not in result:
            continue

        svc = result[dev.hostname]["isis"]["instances"]["0"]
        expected = dev.role.name in overload_roles
        assert svc["set_overload_bit"] is expected


def test_isis_interfaces_are_device_local_and_named(
    session,
    seeded_inventory,
    isis_service_ctx,
    dummy_service_builder,
):
    handler = ISISCoreFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = handler.compute(isis_service_ctx)

    for _hostname, dev_ctx in result.items():
        interfaces = dev_ctx["isis"]["instances"]["0"]["interfaces"]

        for iface in interfaces:
            assert "iface_name" in iface
            assert isinstance(iface["iface_name"], str)
            assert iface["iface_name"]
            assert iface["network_type"] == "point-to-point"


def test_isis_omits_devices_without_eligible_interfaces(
    session,
    seeded_inventory,
    isis_service_ctx,
    dummy_service_builder,
):
    handler = ISISCoreFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = handler.compute(isis_service_ctx)

    all_devices = get_all_devices(session)
    sel = isis_service_ctx["service_data"]["selectors"]["devices"]["isis"]
    selected = dummy_service_builder.selector_engine.select(all_devices, sel)

    result_hostnames = set(result.keys())
    selected_hostnames = {d.hostname for d in selected}

    assert result_hostnames.issubset(selected_hostnames)
