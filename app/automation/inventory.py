"""Generate a Nornir inventory from the topology database.

The inventory is **derived, never hand-maintained**. It is rebuilt from scratch on
every run and the previous files are overwritten, so it cannot drift from the
database the way a hand-edited ``hosts.yaml`` would. Both files are written in a
single pass, which is what guarantees that every group a host references actually
exists — Nornir errors out otherwise.

Two properties make the result safe to point at real equipment:

* **Only ``active`` devices are included.** On this project a device becomes
  ``active`` on the strength of the lab's IS-IS database (see the state-validation
  module), so membership is *evidence-based*: a device is in the inventory because
  the network was observed to carry it, not because someone remembered to add it.
* **The scope follows the data.** The inventory is built from whichever database
  this server holds, and environment data is per-server (see
  ``docs/adr/0001-code-vs-environment-data.md``). A test server holding lab data
  can only ever produce a lab inventory.

Deliberately **no credentials**. The inventory says *what to reach and how*, never
*who as* — username and password are injected at run time by the caller
(``nr.inventory.defaults.username`` / ``.password``). That keeps the seam clean for
TACACS later, where identity moves elsewhere entirely.

This module writes YAML and touches no network, so it is fully testable without a
testbed; only the tasks that consume the inventory need real devices.
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.automation.driver import DEVICE_TYPE
from app.domain.file_locations import GENERATED_LOC
from app.models import Device, DeviceStatus, Interface, IPAddress
from app.services.service_handling.ce_lags import find_ce_lags

# Generated inventory is derived output, so it lands under the data root (via the
# registry) rather than the code tree — which lets the image run read-only.
DEFAULT_OUT_DIR = GENERATED_LOC.path

HOSTS_FILENAME = "hosts.yaml"
GROUPS_FILENAME = "groups.yaml"

# The IPAddress.role that marks the in-band management address (Loopback1, carried
# in the INB-MGMT VPRN). Loopback0 — the system IP — is role "system" and is NOT
# what we connect to.
MGMT_IP_ROLE = "management"


# Connection profile shared by every SR OS node. This is environment-independent —
# an SR OS device is `nokia_sros` on lab and production alike — so it is code, not
# data, and lives here rather than in a hand-maintained file that the generator
# would have to avoid clobbering.
#
# `platform` names the OS; `device_type` selects the netmiko driver. We ask for
# our own (app/automation/driver.py) rather than stock `nokia_sros`, whose
# config-mode handling does not match our MD-CLI dialect. Reads are identical
# either way — only the config-write path differs — but naming one driver
# everywhere avoids two paths that could diverge unnoticed.
SROS_GROUP = "sros"
SROS_GROUP_DEF: dict = {
    "platform": "nokia_sros",
    "connection_options": {"netmiko": {"extras": {"device_type": DEVICE_TYPE}}},
}


class InventoryError(Exception):
    """The inventory could not be built from the current database contents."""


@dataclass(frozen=True)
class HostEntry:
    """One resolved device, ready to be written to ``hosts.yaml``."""

    hostname: str
    address: str
    role: str
    site: str | None
    tenant: str | None


@dataclass(frozen=True)
class InventoryResult:
    """What a build produced, for the caller to report."""

    hosts_file: Path
    groups_file: Path
    hosts: list[HostEntry]
    roles: list[str]


def resolve_active_hosts(session: Session) -> list[HostEntry]:
    """Return every ``active`` device with its management address, sorted by hostname.

    Raises :class:`InventoryError` if any active device cannot be resolved — a
    device with no management address, more than one, or no role. Failing is
    deliberate: silently omitting a device produces a run that reports success
    while a router was never contacted. All problems are collected and reported
    together so one pass tells you everything that needs fixing.
    """
    mgmt = aliased(IPAddress)
    stmt = (
        select(Device, mgmt.address)
        # Both joins are OUTER on purpose. An inner join on Interface would drop a
        # device that has no interfaces at all before it could ever be reported as
        # a problem — i.e. it would vanish from the inventory silently, which is
        # the exact failure this function exists to prevent.
        .outerjoin(Interface, Interface.device_id == Device.id)
        .outerjoin(
            mgmt,
            (mgmt.interface_id == Interface.id) & (mgmt.role == MGMT_IP_ROLE),
        )
        .where(Device.status == DeviceStatus.active)
    )

    # A device has many interfaces, so it yields many rows — most with a NULL
    # management address. Collect the distinct non-null addresses per device.
    addresses: dict[str, set[str]] = defaultdict(set)
    devices: dict[str, Device] = {}
    for device, address in session.execute(stmt).all():
        devices[device.hostname] = device
        if address:
            addresses[device.hostname].add(address)

    # CEs (access switches and the like) are NOT SSH/Nornir-managed: they are
    # reached through their PE and own no in-band 'management' address, so they
    # are neither inventory entries nor problems. They can legitimately be
    # ``active`` — an active CE is in service — but including one would make
    # this function fail on a device it could never reach anyway, taking down
    # every inventory rebuild. "Is a CE" is decided structurally (the device has
    # ``CE:`` access LAGs on a PE — see ``ce_lags``), not by a role-name list.
    ce_hostnames = set(find_ce_lags(session))

    problems: list[str] = []
    hosts: list[HostEntry] = []
    for hostname in sorted(devices):
        if hostname in ce_hostnames:
            continue
        device = devices[hostname]

        found = sorted(addresses.get(hostname, set()))

        if not found:
            problems.append(f"{hostname}: no '{MGMT_IP_ROLE}' IP address")
            continue
        if len(found) > 1:
            problems.append(f"{hostname}: multiple management IPs {found}")
            continue
        if device.role is None:
            problems.append(f"{hostname}: no role assigned")
            continue

        hosts.append(
            HostEntry(
                hostname=hostname,
                # Stored as a host prefix (e.g. 10.201.4.3/32); Nornir wants the
                # bare address. ip_interface also validates it while parsing.
                address=str(ipaddress.ip_interface(found[0]).ip),
                role=device.role.name,
                site=device.site.name if device.site else None,
                tenant=(device.labels or {}).get("tenant"),
            )
        )

    if problems:
        raise InventoryError(
            "Cannot build a Nornir inventory — "
            f"{len(problems)} active device(s) could not be resolved:\n  " + "\n  ".join(problems)
        )
    return hosts


def _header(kind: str, *, count: int) -> str:
    """Comment block stamped on each generated file.

    The timestamp lives in a *comment* on purpose: it makes the freshness of a
    run obvious to whoever opens the file, while leaving the parsed YAML fully
    deterministic so it can be asserted on in tests and diffed between runs.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"# GENERATED — do not edit. Rebuilt from the topology database on every run.\n"
        f"# {kind}: {count}\n"
        f"# generated at {stamp} by app.automation.inventory\n"
    )


def build_inventory(*, session: Session, out_dir: Path | None = None) -> InventoryResult:
    """Write ``hosts.yaml`` and ``groups.yaml`` for the current active topology.

    Both files are rewritten in one pass, overwriting whatever was there. Writing
    them together is what keeps them consistent: every role group referenced by a
    host is emitted into the group file in the same operation, so a host can never
    point at a group that does not exist.
    """
    out = out_dir if out_dir is not None else DEFAULT_OUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    hosts = resolve_active_hosts(session)
    roles = sorted({h.role for h in hosts})

    hosts_doc: dict = {}
    for h in hosts:
        data = {"role": h.role}
        if h.site is not None:
            data["site"] = h.site
        if h.tenant is not None:
            data["tenant"] = h.tenant
        hosts_doc[h.hostname] = {
            "hostname": h.address,
            "groups": [SROS_GROUP, h.role],
            "data": data,
        }

    # The shared SR OS profile plus one (empty) group per role. The role groups
    # carry no data today; they exist so hosts can be filtered by role
    # (nr.filter(F(groups__contains="pe"))) and so role-specific connection
    # options have somewhere to live later.
    groups_doc: dict = {SROS_GROUP: SROS_GROUP_DEF}
    for role in roles:
        groups_doc[role] = {}

    hosts_file = out / HOSTS_FILENAME
    groups_file = out / GROUPS_FILENAME
    _write_yaml(hosts_file, hosts_doc, header=_header("hosts", count=len(hosts)))
    _write_yaml(groups_file, groups_doc, header=_header("groups", count=len(groups_doc)))

    return InventoryResult(
        hosts_file=hosts_file,
        groups_file=groups_file,
        hosts=hosts,
        roles=roles,
    )


def _write_yaml(path: Path, doc: dict, *, header: str) -> None:
    """Write *doc* as YAML with *header* prepended, preserving insertion order."""
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    path.write_text(header + body, encoding="utf-8")
