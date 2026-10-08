from __future__ import annotations

import ipaddress

import pytest
from sqlalchemy import select

from app.models import DelegatedPrefix, PrefixPool, PrefixPoolType
from app.services.service_handling.feature_handlers.vprn import VPRNFeatureHandler

VPRN_NAME = "CE-DHCP-100"
VARIANT = "ce_dhcp"


@pytest.fixture
def vprn_ctx():
    return {
        "service_data": {
            "service": "vprn",
            "tenant": "lab",
            "variant": VARIANT,
            "selectors": {
                "devices": {
                    "vprn": {
                        "match": {
                            "labels": {"tenant": "lab"},
                            "role": {"include": ["pe"]},
                        }
                    }
                }
            },
            "parameters": {
                "import_target_names": {},
                "vprns": [
                    {
                        "name": VPRN_NAME,
                        "rt_import": ["65000:100"],
                        "rt_export": ["65000:100"],
                        "use_policy": False,
                        "dhcp_server": {
                            "loopback_interface_name": "local_dhcp_loopback",
                            "prefix_pool_name": "delegated_pool_ce_dhcp_pool_lab",
                            "prefixlen": 28,
                            "dhcp_local_server_name": "ce_dhcp_server",
                            "dhcp_local_server_address": "192.0.2.1",
                            "dhcp_poolname": "ce_pool",
                            "dhcp_option_125_string": "0x00000de908c106100404040404",
                            "dhcp_option_42_string": "10.102.6.4",
                        },
                    }
                ],
            },
        }
    }


@pytest.fixture
def delegated_pool(session, seeded_inventory):
    pool = session.scalars(
        select(PrefixPool).where(PrefixPool.name == "delegated_pool_ce_dhcp_pool_lab")
    ).one_or_none()
    if pool is None:
        pool_type = session.scalars(
            select(PrefixPoolType).where(PrefixPoolType.name == "delegated")
        ).one_or_none()
        if pool_type is None:
            pool_type = PrefixPoolType(name="delegated")
            session.add(pool_type)
            session.flush()

        pool = PrefixPool(
            name="delegated_pool_ce_dhcp_pool_lab",
            prefix="10.200.0.0/24",
            type_id=pool_type.id,
        )
        session.add(pool)
        session.flush()
    return pool


def test_vprn_returns_device_scoped_output(
    session,
    seeded_inventory,
    delegated_pool,
    vprn_ctx,
    dummy_service_builder,
):
    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(vprn_ctx)

    assert result, "Expected at least one device in VPRN output"

    for _hostname, host_ctx in result.items():
        assert "vprn" in host_ctx
        assert "variant" in host_ctx["vprn"]
        assert VARIANT in host_ctx["vprn"]["variant"]

        svc = host_ctx["vprn"]["variant"][VARIANT][VPRN_NAME]
        assert svc["service_id"] == 100
        assert svc["rt_import"] == ["65000:100"]
        assert svc["rt_export"] == ["65000:100"]
        assert svc["use_policy"] is False


def test_vprn_builds_dhcp_context(
    session,
    seeded_inventory,
    delegated_pool,
    vprn_ctx,
    dummy_service_builder,
):
    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(vprn_ctx)

    for _hostname, host_ctx in result.items():
        svc = host_ctx["vprn"]["variant"][VARIANT][VPRN_NAME]

        assert "gi" in svc
        assert "pool_start" in svc
        assert "pool_end" in svc
        assert "subnet" in svc
        assert "irb_ip" in svc
        assert "vpls_name" in svc

        net = ipaddress.ip_network(svc["subnet"], strict=True)
        assert ipaddress.ip_address(svc["gi"]) in net
        assert ipaddress.ip_address(svc["pool_start"]) in net
        assert ipaddress.ip_address(svc["pool_end"]) in net
        assert ipaddress.ip_address(svc["irb_ip"]) in net

        assert svc["irb_interface_name"] == "CE-DHCP-1000"
        assert svc["dhcp_local_server_name"] == "ce_dhcp_server"
        assert svc["dhcp_local_server_address"] == "192.0.2.1"
        assert svc["dhcp_poolname"] == "ce_pool"
        assert svc["dhcp_option_42_string"] == "10.102.6.4"
        assert svc["loopback_interface_name"] == "local_dhcp_loopback"
        assert svc["vpls_name"].startswith("CE-DHCP-")


def test_vprn_delegated_prefix_allocation_is_idempotent(
    session,
    seeded_inventory,
    delegated_pool,
    vprn_ctx,
    dummy_service_builder,
):
    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    first = handler.compute(vprn_ctx)
    session.flush()
    second = handler.compute(vprn_ctx)

    assert first == second


def test_vprn_output_is_deterministic(
    session,
    seeded_inventory,
    delegated_pool,
    vprn_ctx,
    dummy_service_builder,
):
    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    first = handler.compute(vprn_ctx)
    second = handler.compute(vprn_ctx)

    assert first == second


def test_vprn_vpls_name_derived_from_vprn_name(
    session,
    seeded_inventory,
    delegated_pool,
    vprn_ctx,
    dummy_service_builder,
):
    """CE-DHCP-100 -> CE-DHCP-1000 (suffix x 10)."""
    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(vprn_ctx)

    for _hostname, host_ctx in result.items():
        svc = host_ctx["vprn"]["variant"][VARIANT][VPRN_NAME]
        assert svc["vpls_name"] == "CE-DHCP-1000"
        assert svc["irb_interface_name"] == "CE-DHCP-1000"


def test_vprn_irb_ips_are_device_local(
    session,
    seeded_inventory,
    delegated_pool,
    vprn_ctx,
    dummy_service_builder,
):
    """Each device's irb_ip must be within its own subnet; devices sharing a subnet get distinct IPs."""
    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(vprn_ctx)

    irb_ips_by_subnet: dict[str, list[str]] = {}
    for hostname, host_ctx in result.items():
        svc = host_ctx["vprn"]["variant"][VARIANT][VPRN_NAME]
        subnet = svc["subnet"]
        irb_ip = svc["irb_ip"]

        net = ipaddress.ip_network(subnet, strict=True)
        assert ipaddress.ip_address(irb_ip) in net, (
            f"{hostname}: irb_ip {irb_ip} not in subnet {subnet}"
        )

        irb_ips_by_subnet.setdefault(subnet, []).append(irb_ip)

    for subnet, ips in irb_ips_by_subnet.items():
        assert len(ips) == len(set(ips)), f"Duplicate irb_ips {ips} found for subnet {subnet}"


def test_vprn_dhcp_pool_boundaries_do_not_overlap(
    session,
    seeded_inventory,
    delegated_pool,
    vprn_ctx,
    dummy_service_builder,
):
    """Devices sharing a /28 must have non-overlapping DHCP pool ranges."""
    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(vprn_ctx)

    by_subnet: dict[str, dict[str, dict]] = {}
    for hostname, host_ctx in result.items():
        svc = host_ctx["vprn"]["variant"][VARIANT][VPRN_NAME]
        by_subnet.setdefault(svc["subnet"], {})[hostname] = svc

    for subnet, devices in by_subnet.items():
        if len(devices) < 2:
            continue

        ranges = [
            (
                ipaddress.ip_address(svc["pool_start"]),
                ipaddress.ip_address(svc["pool_end"]),
            )
            for svc in devices.values()
        ]

        for i, (s1, e1) in enumerate(ranges):
            for j, (s2, e2) in enumerate(ranges):
                if i >= j:
                    continue
                overlaps = not (e1 < s2 or e2 < s1)
                assert not overlaps, f"DHCP pool overlap in {subnet}: [{s1}-{e1}] vs [{s2}-{e2}]"


def test_vprn_fails_if_prefix_pool_missing(
    session,
    seeded_inventory,
    vprn_ctx,
    dummy_service_builder,
):
    """Handler must raise when the delegated prefix pool is absent from the DB."""
    bad_ctx = {
        "service_data": {
            **vprn_ctx["service_data"],
            "parameters": {
                **vprn_ctx["service_data"]["parameters"],
                "vprns": [
                    {
                        **vprn_ctx["service_data"]["parameters"]["vprns"][0],
                        "dhcp_server": {
                            **vprn_ctx["service_data"]["parameters"]["vprns"][0]["dhcp_server"],
                            "prefix_pool_name": "definitely_missing_prefix_pool",
                        },
                    }
                ],
            },
        }
    }

    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    with pytest.raises(ValueError, match="No db record found for definitely_missing_prefix_pool"):
        handler.compute(bad_ctx)


def test_vprn_raises_when_policy_import_target_name_missing(
    session,
    seeded_inventory,
    vprn_ctx,
    dummy_service_builder,
):
    """A use_policy VPRN importing an RT absent from import_target_names must fail
    at compute time, naming the offending VPRN and RT — rather than rendering
    fine and only blowing up later as a Jinja UndefinedError."""
    bad_ctx = {
        "service_data": {
            **vprn_ctx["service_data"],
            "parameters": {
                "import_target_names": {},  # deliberately empty
                "vprns": [
                    {
                        "name": VPRN_NAME,
                        "rt_import": ["65000:100", "65000:120"],
                        "rt_export": ["65000:100"],
                        "use_policy": True,
                    }
                ],
            },
        }
    }

    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    with pytest.raises(ValueError, match=r"CE-DHCP-100.*65000:100"):
        handler.compute(bad_ctx)


def test_vprn_use_policy_ok_when_all_import_targets_named(
    session,
    seeded_inventory,
    vprn_ctx,
    dummy_service_builder,
):
    """A use_policy VPRN whose every rt_import has an import_target_names entry
    computes without raising and carries the names into the render context."""
    ctx = {
        "service_data": {
            **vprn_ctx["service_data"],
            "parameters": {
                "import_target_names": {"65000:100": "CE-DHCP-100"},
                "vprns": [
                    {
                        "name": VPRN_NAME,
                        "rt_import": ["65000:100"],
                        "rt_export": ["65000:100"],
                        "use_policy": True,
                    }
                ],
            },
        }
    }

    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(ctx)

    assert result, "Expected at least one device in VPRN output"
    for _hostname, host_ctx in result.items():
        svc = host_ctx["vprn"]["variant"][VARIANT][VPRN_NAME]
        assert svc["use_policy"] is True
        assert svc["import_target_names"] == {"65000:100": "CE-DHCP-100"}


def test_vprn_persists_allocation_per_pair_label(
    session,
    seeded_inventory,
    delegated_pool,
    vprn_ctx,
    dummy_service_builder,
):
    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(vprn_ctx)
    session.flush()

    for _hostname, host_ctx in result.items():
        device_vprn = host_ctx["vprn"]["variant"][VARIANT][VPRN_NAME]
        subnet = device_vprn["subnet"]

        alloc = session.scalars(
            select(DelegatedPrefix).where(
                DelegatedPrefix.reservations["prefix"].as_string() == subnet
            )
        ).one_or_none()
        assert alloc is not None
        assert alloc.in_use is True


def test_vprn_ce_mgmt_subnet_is_keyed_by_shared_name(
    session,
    seeded_inventory,
    delegated_pool,
    vprn_ctx,
    dummy_service_builder,
):
    """``subnet_info.ce_mgmt: true`` stores the delegated prefix under the
    ``ce_mgmt_<pair_label>`` name the CE management allocator reads — not under
    this VPRN's service/variant name."""
    vprn_cfg = vprn_ctx["service_data"]["parameters"]["vprns"][0]
    del vprn_cfg["dhcp_server"]
    vprn_cfg["subnet_info"] = {
        "prefix_pool_name": delegated_pool.name,
        "prefixlen": 28,
        "ce_mgmt": True,
    }
    handler = VPRNFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(vprn_ctx)
    session.flush()

    names = {row.name for row in session.scalars(select(DelegatedPrefix)).all()}
    assert names
    assert all(name.startswith("ce_mgmt_") for name in names)
    assert not any(VARIANT in name for name in names)

    # The two members of the pair share one subnet; the standalone PEs get their own.
    subnets = {
        host: ctx["vprn"]["variant"][VARIANT][VPRN_NAME]["subnet"] for host, ctx in result.items()
    }
    assert subnets["pe1.tst-001"] == subnets["pe2.tst-001"]
    assert subnets["pe3.tst-001"] != subnets["pe1.tst-001"]
