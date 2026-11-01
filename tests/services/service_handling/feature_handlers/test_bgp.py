from __future__ import annotations

import pytest

from app.repositories import get_all_devices, get_loopback_ip_from_device
from app.services.service_handling.feature_handlers.bgp import BGPFeatureHandler


@pytest.fixture
def bgp_service_ctx_rr():
    return {
        "service_data": {
            "tenant": "lab",
            "service": "bgp",
            "variant": "default",
            "selectors": {
                "devices": {
                    "bgp": {
                        "match": {
                            "role": {
                                "include": [
                                    "core",
                                    "pe",
                                    "rr",
                                ]
                            }
                        }
                    },
                    "route_reflectors": {"match": {"role": {"include": ["rr"]}}},
                }
            },
            "features": {
                "bgp": {
                    "asn": 65000,
                    "rr": {
                        "mode": "rr",
                        "rr_peer_group_name": "IBGP_RR",
                        "rrc_peer_group_name": "IBGP_CLIENT",
                    },
                }
            },
            "parameters": {
                "bgp_afs": {
                    "role": {
                        "pe": ["evpn", "vpn-ipv4", "route-target"],
                        "core": ["vpn-ipv4", "route-target"],
                    },
                    "pg_suffix": {
                        "pe": "evpn_vpn-ipv4",
                        "core": "vpn-ipv4",
                    },
                }
            },
        }
    }


@pytest.fixture
def bgp_service_ctx_fullmesh():
    return {
        "service_data": {
            "tenant": "lab",
            "service": "bgp",
            "variant": "default",
            "selectors": {
                "devices": {
                    "bgp": {
                        "match": {
                            "role": {
                                "include": [
                                    "core",
                                    "pe",
                                    "rr",
                                ]
                            }
                        }
                    },
                    "route_reflectors": {"match": {"role": {"include": ["rr"]}}},
                }
            },
            "features": {
                "bgp": {
                    "asn": 65000,
                    "rr": {
                        "mode": "fullmesh",
                        "rr_peer_group_name": "IBGP_RR",
                        "rrc_peer_group_name": "IBGP_CLIENT",
                    },
                }
            },
            "parameters": {
                "bgp_afs": {
                    "role": {
                        "pe": ["evpn", "vpn-ipv4", "route-target"],
                        "core": ["vpn-ipv4", "route-target"],
                    },
                    "pg_suffix": {
                        "pe": "evpn_vpn-ipv4",
                        "core": "vpn-ipv4",
                    },
                }
            },
        }
    }


def _selected_devices(session, dummy_service_builder, selector: dict) -> list:
    all_devices = get_all_devices(session)
    return dummy_service_builder.selector_engine.select(all_devices, selector)


def test_bgp_rr_returns_device_scoped_output(
    session,
    seeded_inventory,
    bgp_service_ctx_rr,
    dummy_service_builder,
):
    handler = BGPFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = dict(handler.compute(bgp_service_ctx_rr))

    sel_bgp = bgp_service_ctx_rr["service_data"]["selectors"]["devices"]["bgp"]
    expected = {d.hostname for d in _selected_devices(session, dummy_service_builder, sel_bgp)}

    assert set(result.keys()) == expected

    for hostname in expected:
        assert "bgp" in result[hostname]
        assert "variant" in result[hostname]["bgp"]
        assert "default" in result[hostname]["bgp"]["variant"]

        svc = result[hostname]["bgp"]["variant"]["default"]
        assert "asn" in svc
        assert "neighbors" in svc
        assert "peer_groups" in svc
        assert "topology_role" in svc
        assert "rapid_update_afs" in svc


def test_bgp_rr_and_client_roles_are_correct(
    session,
    seeded_inventory,
    bgp_service_ctx_rr,
    dummy_service_builder,
):
    handler = BGPFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = dict(handler.compute(bgp_service_ctx_rr))

    sel_bgp = bgp_service_ctx_rr["service_data"]["selectors"]["devices"]["bgp"]
    sel_rr = bgp_service_ctx_rr["service_data"]["selectors"]["devices"]["route_reflectors"]

    bgp_devices = _selected_devices(session, dummy_service_builder, sel_bgp)
    rr_devices = _selected_devices(session, dummy_service_builder, sel_rr)
    rr_hostnames = {d.hostname for d in rr_devices}

    for dev in bgp_devices:
        svc = result[dev.hostname]["bgp"]["variant"]["default"]
        if dev.hostname in rr_hostnames:
            assert svc["topology_role"] == "rr"
        else:
            assert svc["topology_role"] == "client"


def test_bgp_rr_does_not_peer_with_rr(
    session,
    seeded_inventory,
    bgp_service_ctx_rr,
    dummy_service_builder,
):
    handler = BGPFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = dict(handler.compute(bgp_service_ctx_rr))

    sel_rr = bgp_service_ctx_rr["service_data"]["selectors"]["devices"]["route_reflectors"]
    rr_devices = _selected_devices(session, dummy_service_builder, sel_rr)
    rr_hostnames = {d.hostname for d in rr_devices}

    for rr in rr_devices:
        svc = result[rr.hostname]["bgp"]["variant"]["default"]
        for n in svc["neighbors"]:
            assert n["neighbor_hostname"] not in rr_hostnames


def test_bgp_clients_peer_only_with_rrs(
    session,
    seeded_inventory,
    bgp_service_ctx_rr,
    dummy_service_builder,
):
    handler = BGPFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = dict(handler.compute(bgp_service_ctx_rr))

    sel_bgp = bgp_service_ctx_rr["service_data"]["selectors"]["devices"]["bgp"]
    sel_rr = bgp_service_ctx_rr["service_data"]["selectors"]["devices"]["route_reflectors"]

    bgp_devices = _selected_devices(session, dummy_service_builder, sel_bgp)
    rr_devices = _selected_devices(session, dummy_service_builder, sel_rr)
    rr_hostnames = {d.hostname for d in rr_devices}

    for dev in bgp_devices:
        if dev.hostname in rr_hostnames:
            continue

        svc = result[dev.hostname]["bgp"]["variant"]["default"]
        assert svc["topology_role"] == "client"

        for n in svc["neighbors"]:
            assert n["neighbor_hostname"] in rr_hostnames


def test_bgp_neighbor_ips_come_from_loopback0_rr_mode(
    session,
    seeded_inventory,
    bgp_service_ctx_rr,
    dummy_service_builder,
):
    handler = BGPFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = dict(handler.compute(bgp_service_ctx_rr))

    all_by_hostname = {d.hostname: d for d in get_all_devices(session)}

    for _hostname, dev_ctx in result.items():
        svc = dev_ctx["bgp"]["variant"]["default"]
        for n in svc["neighbors"]:
            peer = all_by_hostname[n["neighbor_hostname"]]
            lo = get_loopback_ip_from_device(session, device=peer, loop_index=0)
            assert lo is not None
            assert n["neighbor_ip"] == lo.address.split("/")[0]


def test_bgp_is_deterministic_rr_mode(
    session,
    seeded_inventory,
    bgp_service_ctx_rr,
    dummy_service_builder,
):
    handler = BGPFeatureHandler(session=session, service_builder=dummy_service_builder)

    first = dict(handler.compute(bgp_service_ctx_rr))
    second = dict(handler.compute(bgp_service_ctx_rr))

    assert first == second


def test_bgp_fullmesh_peer_group_and_no_self_peering(
    session,
    seeded_inventory,
    bgp_service_ctx_fullmesh,
    dummy_service_builder,
):
    handler = BGPFeatureHandler(session=session, service_builder=dummy_service_builder)
    result = dict(handler.compute(bgp_service_ctx_fullmesh))

    sel_bgp = bgp_service_ctx_fullmesh["service_data"]["selectors"]["devices"]["bgp"]
    bgp_devices = _selected_devices(session, dummy_service_builder, sel_bgp)
    hostnames = {d.hostname for d in bgp_devices}

    assert set(result.keys()) == hostnames

    for dev in bgp_devices:
        svc = result[dev.hostname]["bgp"]["variant"]["default"]
        assert svc["peer_group"] == "FULLMESH"
        assert svc["asn"] == bgp_service_ctx_fullmesh["service_data"]["features"]["bgp"]["asn"]

        neighbor_hostnames = {n["neighbor_hostname"] for n in svc["neighbors"]}
        assert dev.hostname not in neighbor_hostnames


def test_bgp_is_deterministic_fullmesh(
    session,
    seeded_inventory,
    bgp_service_ctx_fullmesh,
    dummy_service_builder,
):
    handler = BGPFeatureHandler(session=session, service_builder=dummy_service_builder)

    first = dict(handler.compute(bgp_service_ctx_fullmesh))
    second = dict(handler.compute(bgp_service_ctx_fullmesh))

    assert first == second
