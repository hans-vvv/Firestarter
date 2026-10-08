"""Generates human-readable reports from job execution results."""

from __future__ import annotations

import ipaddress
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.domain.file_locations import TOPOLOGY_EXCEL_LOC
from app.models import (
    Cable,
    CableStatus,
    CeMgmtAddress,
    Device,
    IntegerAllocation,
    Interface,
    IPAddress,
)
from app.services.service_handling.ce_lags import find_ce_lags

WB_NAME = TOPOLOGY_EXCEL_LOC.path


def write_report_tabs(session: Session) -> None:
    """
    Build DB-derived reports and write them into the current workbook
    as tabs: report_devices, report_links and report_ce_mgmt.
    """
    df_devices = build_report_devices(session)
    df_links = build_report_links(session)
    df_ce_mgmt = build_report_ce_mgmt(session)

    with pd.ExcelWriter(
        WB_NAME,
        engine="openpyxl",
        mode="a",
        if_sheet_exists="replace",
    ) as writer:
        df_devices.to_excel(writer, sheet_name="report_devices", index=False)
        df_links.to_excel(writer, sheet_name="report_links", index=False)
        df_ce_mgmt.to_excel(writer, sheet_name="report_ce_mgmt", index=False)


def build_report_devices(session: Session) -> pd.DataFrame:
    """
    Build a normalized device report with tenant, hostname, system IP,
    management IP, and prefix SID.

    This function queries devices together with their system and management
    IP addresses and constructs a pandas DataFrame with the following columns:

    - tenant
    - hostname
    - system_ip
    - mgmt_ip
    - prefix_sid

    Returns:
        pd.DataFrame with columns:
            - tenant
            - hostname
            - system_ip
            - mgmt_ip
            - prefix_sid
    """
    system_ip = aliased(IPAddress)
    mgmt_ip = aliased(IPAddress)

    stmt = (
        select(Device, system_ip.address, mgmt_ip.address)
        .join(Interface, Interface.device_id == Device.id)
        .outerjoin(
            system_ip,
            (system_ip.interface_id == Interface.id) & (system_ip.role == "system"),
        )
        .outerjoin(
            mgmt_ip,
            (mgmt_ip.interface_id == Interface.id) & (mgmt_ip.role == "management"),
        )
    )

    by_host: dict[str, dict] = {}

    for dev, sys_ip, management_ip in session.execute(stmt):
        tenant = dev.labels.get("tenant")
        if tenant is None:
            continue

        rec = by_host.setdefault(
            dev.hostname,
            {
                "tenant": tenant,
                "system_ips": [],
                "mgmt_ips": [],
            },
        )

        if sys_ip:
            rec["system_ips"].append(sys_ip)
        if management_ip:
            rec["mgmt_ips"].append(management_ip)

    sr_rows = session.scalars(
        select(IntegerAllocation).where(IntegerAllocation.allocation_name.startswith("sr_"))
    ).all()

    sid_by_hostname: dict[str, int] = {r.pool_key: r.value for r in sr_rows}

    rows = []
    for hostname in sorted(by_host):
        rec = by_host[hostname]

        system_ips = sorted(set(rec["system_ips"]))
        mgmt_ips = sorted(set(rec["mgmt_ips"]))

        if len(system_ips) > 1:
            raise ValueError(f"Multiple system IPs for {hostname}: {system_ips}")
        if len(system_ips) == 0:
            raise ValueError(f"No system IP found for {hostname}")

        if len(mgmt_ips) > 1:
            raise ValueError(f"Multiple management IPs for {hostname}: {mgmt_ips}")
        if len(mgmt_ips) == 0:
            raise ValueError(f"No management IP found for {hostname}")

        rows.append(
            {
                "tenant": rec["tenant"],
                "hostname": hostname,
                "system_ip": system_ips[0],
                "mgmt_ip": mgmt_ips[0],
                "prefix_sid": sid_by_hostname.get(hostname, pd.NA),
            }
        )

    return pd.DataFrame(rows)


def _single_link_ip(link_ips: list[str], *, context: str) -> str | Any:
    """
    Resolve a single unique IP address from a collection of link IPs.

    The function filters out falsy values (e.g., empty strings, None), deduplicates
    the remaining IPs, and returns a single IP if exactly one unique value exists.
    """
    ips = sorted({ip for ip in link_ips if ip})
    if len(ips) == 0:
        return pd.NA
    if len(ips) == 1:
        return ips[0]
    raise ValueError(f"Multiple link IPs for {context}: {ips}")


def build_report_links(session: Session) -> pd.DataFrame:
    """
    Reports link info between nodes and IPs assigned on the links
    """

    IA = aliased(Interface)
    IB = aliased(Interface)
    DA = aliased(Device)
    DB = aliased(Device)

    # Cable -> physical interfaces -> devices. ``retired`` cables are kept in the
    # DB for audit but are decommissioned links, so they must not appear in the
    # report. Every other status (planned/connected/active) stays, matching the
    # report's existing contents.
    cables = list(
        session.execute(
            select(Cable, IA, IB, DA, DB)
            .join(IA, Cable.interface_a_id == IA.id)
            .join(IB, Cable.interface_b_id == IB.id)
            .join(DA, IA.device_id == DA.id)
            .join(DB, IB.device_id == DB.id)
            .where(Cable.status != CableStatus.retired)
        )
    )

    # Preload link IPs per interface_id (role="link")
    link_ips_by_iface: dict[int, list[str]] = {}
    for iface_id, addr in session.execute(
        select(IPAddress.interface_id, IPAddress.address).where(IPAddress.role == "link")
    ):
        if iface_id is None:
            continue
        link_ips_by_iface.setdefault(iface_id, []).append(addr)

    rows: list[dict] = []

    for _cable, ia, ib, da, db in cables:
        tenant = da.labels.get("tenant")
        if tenant is None:
            continue

        # physical endpoints
        devA, portA = da.hostname, ia.name
        devB, portB = db.hostname, ib.name

        # L3 interface for IP lookup (parent if LAG member)
        l3a = ia.parent if ia.parent_id else ia
        l3b = ib.parent if ib.parent_id else ib

        ipA = _single_link_ip(
            link_ips_by_iface.get(l3a.id, []),
            context=f"{devA}:{portA} (l3={l3a.name})",
        )
        ipB = _single_link_ip(
            link_ips_by_iface.get(l3b.id, []),
            context=f"{devB}:{portB} (l3={l3b.name})",
        )

        # canonicalize ordering to stabilize diffs
        if (devB, portB) < (devA, portA):
            devA, portA, ipA, devB, portB, ipB = devB, portB, ipB, devA, portA, ipA

        rows.append(
            {
                "tenant": tenant,
                "deviceA": devA,
                "portA": portA,
                "ipA": ipA,
                "deviceB": devB,
                "portB": portB,
                "ipB": ipB,
            }
        )
        # print(f"{devA}:{portA} -> {ipA} | {devB}:{portB} -> {ipB} (tenant={tenant})")

    df = (
        pd.DataFrame(
            rows,
            columns=["tenant", "deviceA", "portA", "ipA", "deviceB", "portB", "ipB"],
        )
        .sort_values(
            ["tenant", "deviceA", "portA", "deviceB", "portB"],
            kind="mergesort",
        )
        .reset_index(drop=True)
    )

    return df


def build_report_ce_mgmt(session: Session) -> pd.DataFrame:
    """Report the persisted management IP of every CE.

    Reads the ``CeMgmtAddress`` assignments (populated by
    ``ce_mgmt_allocator.allocate_ce_mgmt_addresses``) and enriches each with the
    CE's role/model/site (from its ``Device`` row) and the PE(s) it attaches
    to plus the subnet's VRRP gateway (``network + 1``). Rows are ordered by
    address, which — since each /28 is disjoint — is network-then-host order.

    The per-CE ``mgmt_ip`` is rendered in CIDR form (``w.x.y.z/ab``, e.g.
    ``10.202.0.4/28``) so an operator can paste it straight onto an interface, and
    the VRRP gateway is the final column.

    Columns:
        - tenant
        - site
        - ce_hostname
        - ce_role
        - model
        - pes
        - mgmt_subnet
        - mgmt_ip        (CIDR, w.x.y.z/ab)
        - vrrp_gateway   (last)
    """
    columns = [
        "tenant",
        "site",
        "ce_hostname",
        "ce_role",
        "model",
        "pes",
        "pe1",
        "port1",
        "pe2",
        "port2",
        "mgmt_subnet",
        "mgmt_ip",
        "vrrp_gateway",
    ]

    assignments = session.scalars(select(CeMgmtAddress)).all()
    if not assignments:
        return pd.DataFrame(columns=columns)

    lags_by_ce = find_ce_lags(session)

    ce_hostnames = [a.ce_hostname for a in assignments]
    devices = session.scalars(select(Device).where(Device.hostname.in_(ce_hostnames))).all()
    device_by_host = {d.hostname: d for d in devices}

    rows: list[dict[str, Any]] = []
    for assignment in assignments:
        ce = device_by_host.get(assignment.ce_hostname)
        lags = lags_by_ce.get(assignment.ce_hostname, [])

        pes = sorted({lag.device.hostname for lag in lags})

        # Each physical link to the CE is one (PE, member-port) pair — the
        # in-use member interfaces of the PE access LAGs. The two column
        # slots hold one link each: pe1/port1 then pe2/port2. A dual-homed
        # CE yields one pair per PE; a single-homed CE on a two-member LAG
        # yields two pairs on the SAME PE, so pe2 repeats pe1 and the
        # second port lands in port2. Sorted by (hostname, port) for determinism.
        links = sorted(
            (lag.device.hostname, c.name)
            for lag in lags
            for c in lag.children
            if c.in_use and c.name
        )

        pe1, port1 = links[0] if len(links) > 0 else ("", "")
        pe2, port2 = links[1] if len(links) > 1 else ("", "")

        tenant = next(
            (lag.device.labels.get("tenant") for lag in lags if lag.device.labels.get("tenant")),
            None,
        )

        network = ipaddress.ip_network(assignment.prefix, strict=True)
        gateway = str(network.network_address + 1)
        mgmt_ip_cidr = f"{assignment.address}/{network.prefixlen}"

        rows.append(
            {
                "tenant": tenant,
                "site": ce.site.name
                if (ce and ce.site)
                else assignment.ce_hostname.split(".", 1)[-1],
                "ce_hostname": assignment.ce_hostname,
                "ce_role": ce.role.name if (ce and ce.role) else None,
                "model": ce.model_name if ce else None,
                "pes": ", ".join(pes),
                "pe1": pe1,
                "port1": port1,
                "pe2": pe2,
                "port2": port2,
                "mgmt_subnet": assignment.prefix,
                "mgmt_ip": mgmt_ip_cidr,
                "vrrp_gateway": gateway,
            }
        )

    rows.sort(key=lambda r: ipaddress.ip_address(r["mgmt_ip"].split("/", 1)[0]))
    return pd.DataFrame(rows, columns=columns)
