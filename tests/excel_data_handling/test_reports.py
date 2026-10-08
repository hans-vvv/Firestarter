from __future__ import annotations

import pytest

from app.excel_data_handling.reports import build_report_ce_mgmt, build_report_links
from app.models import Cable, CableStatus, DelegatedPrefix, Device, Interface
from app.models.orm_models import Role, Site
from app.services.service_handling import ce_mgmt_allocator as alloc
from app.services.service_handling.ce_mgmt_allocator import allocate_ce_mgmt_addresses

_PAIR_LABEL = "pe-pair:amt-001-1"
_PAIR_PREFIX = "10.20.0.0/28"

# Physical port each pe uses to cable to switch1 (its access LAG's member).
_switch_PORTS = {"pe1.amt-001": "1/1/c5/1", "pe2.amt-001": "1/1/c7/1"}

_EXPECTED_COLUMNS = [
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


@pytest.fixture
def switch_scene(session):
    site = Site(name="amt-001")
    pe_role = Role(name="pe")
    switch_role = Role(name="switch")
    session.add_all([site, pe_role, switch_role])
    session.flush()

    pes = []
    for host in ("pe1.amt-001", "pe2.amt-001"):
        dev = Device(
            hostname=host,
            role_id=pe_role.id,
            site_id=site.id,
            labels={"tenant": "production", "pair_label": _PAIR_LABEL},
        )
        session.add(dev)
        session.flush()
        pes.append(dev)

    switch = Device(
        hostname="switch1.amt-001",
        role_id=switch_role.id,
        site_id=site.id,
        model_name="MF2",
        labels={},
    )
    session.add(switch)
    session.flush()
    for pe in pes:  # dual-homed
        lag = Interface(
            name="lag-1",
            device_id=pe.id,
            parent_id=None,
            description="CE:switch1.amt-001 (access LAG id 1)",
        )
        session.add(lag)
        session.flush()
        # One in-use member port per LAG: the physical port cabling to the switch.
        session.add(
            Interface(
                name=_switch_PORTS[pe.hostname],
                device_id=pe.id,
                parent_id=lag.id,
                in_use=True,
            )
        )
    session.add(
        DelegatedPrefix(
            name=f"ce_mgmt_{_PAIR_LABEL}",
            in_use=True,
            reservations={"prefix": _PAIR_PREFIX},
        )
    )
    session.flush()


def _cable_with_status(session, devA, devB, *, status: CableStatus) -> None:
    """Wire one cable between devA and devB with the given status.

    The port names encode the status so the report rows can be identified.
    """
    tag = status.value
    ifa = Interface(name=f"{tag}-a", device_id=devA.id, parent_id=None)
    ifb = Interface(name=f"{tag}-b", device_id=devB.id, parent_id=None)
    session.add_all([ifa, ifb])
    session.flush()
    session.add(Cable(interface_a_id=ifa.id, interface_b_id=ifb.id, status=status))
    session.flush()


class TestReportLinksExcludesRetired:
    def test_retired_cables_absent_others_present(self, session):
        devA = Device(hostname="r1.tst-001", labels={"tenant": "production"})
        devB = Device(hostname="r2.tst-001", labels={"tenant": "production"})
        session.add_all([devA, devB])
        session.flush()

        for status in (
            CableStatus.planned,
            CableStatus.connected,
            CableStatus.active,
            CableStatus.retired,
        ):
            _cable_with_status(session, devA, devB, status=status)

        df = build_report_links(session)

        ports = set(df["portA"]) | set(df["portB"])
        # planned/connected/active links remain in the report ...
        assert "planned-a" in ports
        assert "connected-a" in ports
        assert "active-a" in ports
        # ... the retired link is gone.
        assert "retired-a" not in ports
        assert "retired-b" not in ports
        assert len(df) == 3


def test_report_is_empty_with_correct_columns_when_unallocated(session):
    df = build_report_ce_mgmt(session)
    assert list(df.columns) == _EXPECTED_COLUMNS
    assert df.empty


def test_report_row_reflects_allocation(session, switch_scene, monkeypatch):
    monkeypatch.setattr(alloc, "_load_excel_ce_order", lambda wb_name: ["switch1.amt-001"])
    allocate_ce_mgmt_addresses(session=session)

    df = build_report_ce_mgmt(session)
    assert list(df.columns) == _EXPECTED_COLUMNS
    assert len(df) == 1

    row = df.iloc[0].to_dict()
    assert row["tenant"] == "production"
    assert row["site"] == "amt-001"
    assert row["ce_hostname"] == "switch1.amt-001"
    assert row["ce_role"] == "switch"
    assert row["model"] == "MF2"
    assert row["pes"] == "pe1.amt-001, pe2.amt-001"
    # New per-pe port columns, ordered to match pes.
    assert row["pe1"] == "pe1.amt-001"
    assert row["port1"] == "1/1/c5/1"
    assert row["pe2"] == "pe2.amt-001"
    assert row["port2"] == "1/1/c7/1"
    assert row["mgmt_subnet"] == _PAIR_PREFIX
    assert row["mgmt_ip"] == "10.20.0.4/28"  # 4th usable, CIDR form
    assert row["vrrp_gateway"] == "10.20.0.1"  # network + 1, last column


def test_report_ordered_by_address(session, switch_scene, monkeypatch):
    # Add a second switch so ordering is observable; it takes .5.
    site_id = session.query(Site).one().id
    switch_role_id = session.query(Role).filter(Role.name == "switch").one().id
    pes = session.query(Device).filter(Device.hostname.like("pe%")).all()
    switch2 = Device(
        hostname="switch2.amt-001",
        role_id=switch_role_id,
        site_id=site_id,
        model_name="MF2",
        labels={},
    )
    session.add(switch2)
    session.flush()
    for pe in pes:
        session.add(
            Interface(
                name="lag-2",
                device_id=pe.id,
                parent_id=None,
                description="CE:switch2.amt-001 (access LAG id 2)",
            )
        )
    session.flush()

    monkeypatch.setattr(
        alloc, "_load_excel_ce_order", lambda wb_name: ["switch1.amt-001", "switch2.amt-001"]
    )
    allocate_ce_mgmt_addresses(session=session)

    df = build_report_ce_mgmt(session)
    assert list(df["mgmt_ip"]) == ["10.20.0.4/28", "10.20.0.5/28"]
    assert list(df["ce_hostname"]) == ["switch1.amt-001", "switch2.amt-001"]
    # GW is the final column and identical across CEs in the same /28.
    assert df.columns[-1] == "vrrp_gateway"
    assert list(df["vrrp_gateway"]) == ["10.20.0.1", "10.20.0.1"]


def _minimal_ce_scene(session, *, pe_ports: dict[str, list[str]]):
    """Build one switch dual/single-homed to the given pes, each cabling to
    the switch via an access LAG carrying the listed member port(s)."""
    site = Site(name="amt-001")
    pe_role = Role(name="pe")
    switch_role = Role(name="switch")
    session.add_all([site, pe_role, switch_role])
    session.flush()

    for host, ports in pe_ports.items():
        pe = Device(
            hostname=host,
            role_id=pe_role.id,
            site_id=site.id,
            labels={"tenant": "production", "pair_label": _PAIR_LABEL},
        )
        session.add(pe)
        session.flush()
        lag = Interface(
            name="lag-1",
            device_id=pe.id,
            parent_id=None,
            description="CE:switch1.amt-001 (access LAG id 1)",
        )
        session.add(lag)
        session.flush()
        for port in ports:
            session.add(Interface(name=port, device_id=pe.id, parent_id=lag.id, in_use=True))

    switch = Device(
        hostname="switch1.amt-001",
        role_id=switch_role.id,
        site_id=site.id,
        model_name="MF2",
        labels={},
    )
    session.add(switch)
    session.add(
        DelegatedPrefix(
            name=f"ce_mgmt_{_PAIR_LABEL}",
            in_use=True,
            reservations={"prefix": _PAIR_PREFIX},
        )
    )
    session.flush()


def test_single_homed_ce_leaves_second_pe_and_port_blank(session, monkeypatch):
    _minimal_ce_scene(session, pe_ports={"pe1.amt-001": ["1/1/c5/1"]})
    monkeypatch.setattr(alloc, "_load_excel_ce_order", lambda wb_name: ["switch1.amt-001"])
    allocate_ce_mgmt_addresses(session=session)

    row = build_report_ce_mgmt(session).iloc[0].to_dict()
    assert row["pe1"] == "pe1.amt-001"
    assert row["port1"] == "1/1/c5/1"
    assert row["pe2"] == ""
    assert row["port2"] == ""


def test_single_homed_two_member_lag_splits_ports_across_slots(session, monkeypatch):
    """A single pe with two links to the switch fills BOTH slots: pe2
    repeats pe1's name and the second port lands in port2 (one link/slot,
    not both ports crammed into port1)."""
    _minimal_ce_scene(
        session,
        pe_ports={"pe1.amt-001": ["1/1/c7/1", "1/1/c5/1"]},  # unsorted on purpose
    )
    monkeypatch.setattr(alloc, "_load_excel_ce_order", lambda wb_name: ["switch1.amt-001"])
    allocate_ce_mgmt_addresses(session=session)

    row = build_report_ce_mgmt(session).iloc[0].to_dict()
    assert row["pe1"] == "pe1.amt-001"
    assert row["port1"] == "1/1/c5/1"
    assert row["pe2"] == "pe1.amt-001"
    assert row["port2"] == "1/1/c7/1"
