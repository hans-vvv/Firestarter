"""Schema-layer tests for the status enums on Prefix, IPAddress, Cable and Device."""

from __future__ import annotations

import pytest

from app.models import (
    Cable,
    CableStatus,
    Device,
    DeviceStatus,
    Interface,
    IPAddress,
    IPStatus,
    Prefix,
    PrefixPool,
    PrefixPoolType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_pool(session) -> PrefixPool:
    pool_type = PrefixPoolType(name="p2p")
    session.add(pool_type)
    session.flush()
    pool = PrefixPool(name="p2p_lab", prefix="10.0.0.0/24", type_id=pool_type.id)
    session.add(pool)
    session.flush()
    return pool


def _make_device(session, hostname: str) -> Device:
    dev = Device(hostname=hostname)
    session.add(dev)
    session.flush()
    return dev


def _make_iface(session, device: Device, name: str, role: str = "NNI") -> Interface:
    iface = Interface(name=name, device_id=device.id, intf_role=role)
    session.add(iface)
    session.flush()
    return iface


def _make_cable(
    session,
    iface_a: Interface,
    iface_b: Interface,
    *,
    status: CableStatus = CableStatus.planned,
) -> Cable:
    cable = Cable(interface_a_id=iface_a.id, interface_b_id=iface_b.id, status=status)
    session.add(cable)
    session.flush()
    return cable


# ---------------------------------------------------------------------------
# IPStatus enum
# ---------------------------------------------------------------------------
def test_ip_status_enum_values():
    assert {s.value for s in IPStatus} == {"available", "reserved", "allocated", "retired"}


def test_prefix_default_status_is_available(session):
    pool = _make_pool(session)
    p = Prefix(prefix="10.0.0.0/31", pool_id=pool.id)
    session.add(p)
    session.flush()
    session.refresh(p)
    assert p.status == IPStatus.available


def test_ip_address_default_status_is_available(session):
    pool = _make_pool(session)
    ip = IPAddress(address="10.0.0.1/32", pool_id=pool.id, role="link")
    session.add(ip)
    session.flush()
    session.refresh(ip)
    assert ip.status == IPStatus.available


@pytest.mark.parametrize("status", list(IPStatus))
def test_prefix_status_round_trips_for_every_enum_value(session, status):
    pool = _make_pool(session)
    p = Prefix(prefix=f"10.0.{status.value[:1]}.0/31", pool_id=pool.id, status=status)
    session.add(p)
    session.flush()
    session.expire(p)
    assert p.status == status


@pytest.mark.parametrize(
    "transition",
    [
        (IPStatus.available, IPStatus.reserved),
        (IPStatus.reserved, IPStatus.allocated),
        (IPStatus.allocated, IPStatus.retired),
        (IPStatus.reserved, IPStatus.available),
    ],
)
def test_ip_status_supports_expected_lifecycle_transitions(session, transition):
    initial, target = transition
    pool = _make_pool(session)
    ip = IPAddress(address="10.0.0.1/32", pool_id=pool.id, role="link", status=initial)
    session.add(ip)
    session.flush()
    ip.status = target
    session.flush()
    session.expire(ip)
    assert ip.status == target


# ---------------------------------------------------------------------------
# CableStatus enum
# ---------------------------------------------------------------------------
def test_cable_status_enum_values():
    assert {s.value for s in CableStatus} == {"planned", "connected", "active", "retired"}


def test_cable_default_status_is_planned(session):
    dev_a = _make_device(session, "host-a")
    dev_b = _make_device(session, "host-b")
    ia = _make_iface(session, dev_a, "Eth0/0/0")
    ib = _make_iface(session, dev_b, "Eth0/0/0")
    cable = Cable(interface_a_id=ia.id, interface_b_id=ib.id)
    session.add(cable)
    session.flush()
    session.refresh(cable)
    assert cable.status == CableStatus.planned


@pytest.mark.parametrize("status", list(CableStatus))
def test_cable_status_round_trips_for_every_enum_value(session, status):
    dev_a = _make_device(session, f"host-a-{status.value}")
    dev_b = _make_device(session, f"host-b-{status.value}")
    ia = _make_iface(session, dev_a, "Eth0/0/0")
    ib = _make_iface(session, dev_b, "Eth0/0/0")
    cable = _make_cable(session, ia, ib, status=status)
    session.expire(cable)
    assert cable.status == status


# ---------------------------------------------------------------------------
# DeviceStatus enum
# ---------------------------------------------------------------------------
def test_device_status_enum_values():
    # Lifecycle: planned → preactivated → reachable → active. `preactivated` is a
    # location declared live (links up in ISIS) but not yet loginable; `reachable`
    # is onboarding having proved the production credential; `active` requires that
    # proof and is the operator-acknowledged in-service state.
    assert {s.value for s in DeviceStatus} == {
        "planned",
        "preactivated",
        "reachable",
        "active",
        "unassigned",
        "retired",
    }


def test_device_default_status_is_planned(session):
    dev = _make_device(session, "status-default-host")
    session.refresh(dev)
    assert dev.status == DeviceStatus.planned


@pytest.mark.parametrize("status", list(DeviceStatus))
def test_device_status_round_trips(session, status):
    dev = _make_device(session, f"host-{status.value}")
    dev.status = status
    session.flush()
    session.expire(dev)
    assert dev.status == status
