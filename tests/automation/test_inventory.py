from __future__ import annotations

"""Tests for the Nornir inventory generator.

The generator is a pure database → YAML transformation: it opens no sockets and
needs no testbed, so everything here is hermetic. Topologies are built directly
from ORM objects rather than seeded from the workbook, so each test states
exactly the situation it is about.
"""

import re
from pathlib import Path

import pytest
import yaml

from app.automation.driver import DEVICE_TYPE
from app.automation.inventory import (
    GROUPS_FILENAME,
    HOSTS_FILENAME,
    SROS_GROUP,
    InventoryError,
    build_inventory,
    resolve_active_hosts,
)
from app.models import (
    Device,
    DeviceStatus,
    Interface,
    IPAddress,
    PrefixPool,
    PrefixPoolType,
    Role,
    Site,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def pool(session) -> PrefixPool:
    pool_type = PrefixPoolType(name="loopback")
    session.add(pool_type)
    session.flush()
    p = PrefixPool(name="loopback_lab", prefix="10.201.4.0/24", type_id=pool_type.id)
    session.add(p)
    session.flush()
    return p


def _make_device(
    session,
    *,
    hostname: str,
    role: str | None = "core",
    status: DeviceStatus = DeviceStatus.active,
    site: str | None = "tst-001",
    tenant: str | None = "lab",
) -> Device:
    role_row = None
    if role is not None:
        role_row = session.query(Role).filter_by(name=role).one_or_none() or Role(name=role)
        session.add(role_row)
        session.flush()

    site_row = None
    if site is not None:
        site_row = session.query(Site).filter_by(name=site).one_or_none() or Site(name=site)
        session.add(site_row)
        session.flush()

    device = Device(
        hostname=hostname,
        status=status,
        labels={"tenant": tenant} if tenant else {},
        role_id=role_row.id if role_row else None,
        site_id=site_row.id if site_row else None,
    )
    session.add(device)
    session.flush()
    return device


def _give_ip(session, pool: PrefixPool, device: Device, address: str, *, role: str) -> None:
    """Attach an IP with the given semantic role to a fresh interface."""
    iface = Interface(name=f"Loopback-{address}", device_id=device.id)
    session.add(iface)
    session.flush()
    session.add(IPAddress(address=address, pool_id=pool.id, role=role, interface_id=iface.id))
    session.flush()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_resolves_active_device_and_strips_prefix_length(session, pool):
    dev = _make_device(session, hostname="core1.tst-001", role="core")
    _give_ip(session, pool, dev, "10.201.4.3/32", role="management")

    hosts = resolve_active_hosts(session)

    assert len(hosts) == 1
    assert hosts[0].hostname == "core1.tst-001"
    assert hosts[0].address == "10.201.4.3"  # /32 stripped — Nornir wants a bare address
    assert hosts[0].role == "core"
    assert hosts[0].site == "tst-001"
    assert hosts[0].tenant == "lab"


@pytest.mark.parametrize(
    "status",
    [DeviceStatus.planned, DeviceStatus.unassigned, DeviceStatus.retired],
)
def test_only_active_devices_are_included(session, pool, status):
    dev = _make_device(session, hostname="core1.tst-001", status=status)
    _give_ip(session, pool, dev, "10.201.4.3/32", role="management")

    assert resolve_active_hosts(session) == []


def test_hosts_are_sorted_by_hostname(session, pool):
    for name, addr in [("core2.tst-001", "10.201.4.4/32"), ("core1.tst-001", "10.201.4.3/32")]:
        dev = _make_device(session, hostname=name)
        _give_ip(session, pool, dev, addr, role="management")

    assert [h.hostname for h in resolve_active_hosts(session)] == [
        "core1.tst-001",
        "core2.tst-001",
    ]


# ---------------------------------------------------------------------------
# Failing loudly — a silently missing device is the dangerous outcome
# ---------------------------------------------------------------------------


def test_active_device_without_management_ip_raises(session, pool):
    _make_device(session, hostname="core1.tst-001")

    with pytest.raises(InventoryError, match=re.escape("core1.tst-001: no 'management' IP")):
        resolve_active_hosts(session)


def test_system_ip_does_not_count_as_management(session, pool):
    """Loopback0 (role 'system') must never be mistaken for the management address."""
    dev = _make_device(session, hostname="core1.tst-001")
    _give_ip(session, pool, dev, "10.201.0.3/32", role="system")

    with pytest.raises(InventoryError, match="no 'management' IP"):
        resolve_active_hosts(session)


def test_multiple_management_ips_raises(session, pool):
    dev = _make_device(session, hostname="core1.tst-001")
    _give_ip(session, pool, dev, "10.201.4.3/32", role="management")
    _give_ip(session, pool, dev, "10.201.4.99/32", role="management")

    with pytest.raises(InventoryError, match="multiple management IPs"):
        resolve_active_hosts(session)


def test_device_without_role_raises(session, pool):
    dev = _make_device(session, hostname="core1.tst-001", role=None)
    _give_ip(session, pool, dev, "10.201.4.3/32", role="management")

    with pytest.raises(InventoryError, match="no role assigned"):
        resolve_active_hosts(session)


def _attach_ce(session, *, ce_hostname: str, pe: Device) -> None:
    """Give *pe* the ``CE:`` access LAG that marks *ce_hostname* as a CE."""
    session.add(
        Interface(
            name="lag-1",
            device_id=pe.id,
            parent_id=None,
            description=f"CE:{ce_hostname} (access LAG id 1)",
        )
    )
    session.flush()


@pytest.mark.parametrize("role", ["switch", "test-switch", "future-ce-role"])
def test_ces_are_skipped_not_raised(session, pool, role):
    """An active CE has no management IP but is managed through its PE, so it is
    skipped — never reported as an unresolvable problem. A CE is recognised by
    its ``CE:`` access LAG, whatever its role is called."""
    pe = _make_device(session, hostname="pe1.tst-001", role="pe")
    _give_ip(session, pool, pe, "10.201.4.9/32", role="management")
    ce = f"{role}1.tst-001"
    _make_device(session, hostname=ce, role=role)  # no management IP
    _attach_ce(session, ce_hostname=ce, pe=pe)

    assert [h.hostname for h in resolve_active_hosts(session)] == ["pe1.tst-001"]


def test_active_ce_does_not_break_a_valid_fleet(session, pool):
    """An active CE without a management IP must not abort the whole inventory —
    the reachable routers still resolve."""
    good = _make_device(session, hostname="core1.tst-001", role="core")
    _give_ip(session, pool, good, "10.201.4.3/32", role="management")
    _make_device(session, hostname="switch1.tst-001", role="switch")  # active, no mgmt IP
    _attach_ce(session, ce_hostname="switch1.tst-001", pe=good)

    hosts = resolve_active_hosts(session)

    assert [h.hostname for h in hosts] == ["core1.tst-001"]


def test_all_problems_are_reported_together(session, pool):
    """One pass should tell you everything that needs fixing, not just the first."""
    _make_device(session, hostname="core1.tst-001")
    _make_device(session, hostname="core2.tst-001")

    with pytest.raises(InventoryError) as exc:
        resolve_active_hosts(session)

    message = str(exc.value)
    assert "core1.tst-001" in message
    assert "core2.tst-001" in message
    assert "2 active device(s)" in message


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------


@pytest.fixture
def two_hosts(session, pool):
    for name, addr, role in [
        ("rr1.tst-001", "10.201.4.7/32", "rr"),
        ("core1.tst-001", "10.201.4.3/32", "core"),
    ]:
        dev = _make_device(session, hostname=name, role=role)
        _give_ip(session, pool, dev, addr, role="management")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_writes_both_files(session, tmp_path, two_hosts):
    result = build_inventory(session=session, out_dir=tmp_path)

    assert result.hosts_file == tmp_path / HOSTS_FILENAME
    assert result.groups_file == tmp_path / GROUPS_FILENAME
    assert result.hosts_file.exists()
    assert result.groups_file.exists()
    assert result.roles == ["core", "rr"]


def test_every_group_a_host_references_exists(session, tmp_path, two_hosts):
    """Nornir errors on an undefined group — the two files must stay consistent."""
    build_inventory(session=session, out_dir=tmp_path)

    hosts = _load(tmp_path / HOSTS_FILENAME)
    groups = _load(tmp_path / GROUPS_FILENAME)

    referenced = {g for host in hosts.values() for g in host["groups"]}
    assert referenced <= set(groups), f"undefined groups: {referenced - set(groups)}"


def test_host_entry_shape(session, tmp_path, two_hosts):
    hosts = _load((build_inventory(session=session, out_dir=tmp_path)).hosts_file)

    assert hosts["core1.tst-001"] == {
        "hostname": "10.201.4.3",
        "groups": [SROS_GROUP, "core"],
        "data": {"role": "core", "site": "tst-001", "tenant": "lab"},
    }


def test_inventory_never_carries_credentials(session, tmp_path, two_hosts):
    """Identity is injected at run time — the inventory says what to reach, not who as.

    This is the seam that keeps TACACS a credential-source change only.
    """
    result = build_inventory(session=session, out_dir=tmp_path)

    for path in (result.hosts_file, result.groups_file):
        raw = path.read_text(encoding="utf-8")
        assert "username" not in raw
        assert "password" not in raw


def test_groups_carry_the_sros_connection_profile(session, tmp_path, two_hosts):
    groups = _load((build_inventory(session=session, out_dir=tmp_path)).groups_file)

    # platform names the OS; device_type selects the netmiko driver, and we ask
    # for our MD-CLI subclass rather than netmiko's stock nokia_sros.
    assert groups[SROS_GROUP]["platform"] == "nokia_sros"
    assert groups[SROS_GROUP]["connection_options"]["netmiko"]["extras"]["device_type"] == (
        DEVICE_TYPE
    )
    assert DEVICE_TYPE != "nokia_sros"
    # Role groups exist so hosts can be filtered by role; empty is fine.
    assert groups["rr"] == {}
    assert groups["core"] == {}


def test_output_is_deterministic(session, tmp_path, two_hosts):
    """Same database, same parsed YAML — the timestamp lives in a comment only."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    build_inventory(session=session, out_dir=a)
    build_inventory(session=session, out_dir=b)

    for name in (HOSTS_FILENAME, GROUPS_FILENAME):
        assert _load(a / name) == _load(b / name)


def test_regeneration_overwrites_stale_entries(session, tmp_path, pool):
    """A device that leaves the active set must not survive in the next inventory."""
    dev = _make_device(session, hostname="core1.tst-001")
    _give_ip(session, pool, dev, "10.201.4.3/32", role="management")
    build_inventory(session=session, out_dir=tmp_path)
    assert "core1.tst-001" in _load(tmp_path / HOSTS_FILENAME)

    dev.status = DeviceStatus.retired
    session.flush()
    other = _make_device(session, hostname="core2.tst-001")
    _give_ip(session, pool, other, "10.201.4.4/32", role="management")

    build_inventory(session=session, out_dir=tmp_path)

    hosts = _load(tmp_path / HOSTS_FILENAME)
    assert "core1.tst-001" not in hosts
    assert "core2.tst-001" in hosts


def test_generated_files_are_marked_as_generated(session, tmp_path, two_hosts):
    result = build_inventory(session=session, out_dir=tmp_path)

    for path in (result.hosts_file, result.groups_file):
        assert path.read_text(encoding="utf-8").startswith("# GENERATED")


def test_creates_output_directory_if_absent(session, tmp_path, two_hosts):
    target = tmp_path / "does" / "not" / "exist"

    result = build_inventory(session=session, out_dir=target)

    assert result.hosts_file.exists()
