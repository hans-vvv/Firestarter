from __future__ import annotations

import pytest

from app.models import Device, Interface
from app.models.orm_models import Role, Site
from app.services.service_handling.ce_lags import find_ce_lags
from app.services.service_handling.resource_pool_allocator import ResourcePoolAllocator
from app.services.topology_building.cable_builder import CableBuilder
from app.services.topology_building.ce_attachment_builder import CEAttachmentBuilder
from app.services.topology_building.device_builder import DeviceBuilder
from app.services.topology_building.device_factory import DeviceFactory
from app.services.topology_building.topology_builder import TopologyBuilder

# Unique names so these fixtures never collide with seeded_inventory data.
_SITE = "_dec_test_site"
_PE_ROLE = "_dec_test_pe"
_CE_ROLE = "_dec_test_ce"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def ce_site(session):
    site = Site(name=_SITE)
    session.add(site)
    session.flush()
    return site


@pytest.fixture
def pe_role(session):
    role = Role(name=_PE_ROLE)
    session.add(role)
    session.flush()
    return role


@pytest.fixture
def ce_role(session):
    role = Role(name=_CE_ROLE)
    session.add(role)
    session.flush()
    return role


@pytest.fixture
def ce_builder(session):
    rpa = ResourcePoolAllocator(session=session)
    factory = DeviceFactory(session=session)
    device_builder = DeviceBuilder(
        session=session,
        device_factory=factory,
        prefix_allocator=rpa,
    )
    topology_builder = TopologyBuilder(
        session=session,
        device_builder=device_builder,
        prefix_allocator=rpa,
        cable_builder=CableBuilder(session=session),
    )
    return CEAttachmentBuilder(session=session, topology_builder=topology_builder)


def _make_pe(session, hostname, site, role, *, pair_label=None, uni_count=2) -> Device:
    labels = {"pair_label": pair_label} if pair_label else {}
    device = Device(
        hostname=hostname,
        lag_name="lag",
        model_name="test_model_1",
        labels=labels,
        site=site,
        role=role,
    )
    session.add(device)
    session.flush()
    for i in range(uni_count):
        session.add(Interface(name=f"Ethernet0/0/{i}", intf_role="UNI", device=device))
    session.flush()
    return device


def _attach_dual_homed(session, ce_builder, ce_site, pe_role, *, ce_name, idx):
    """Attach a dual-homed CE to a fresh PE pair. Returns (pe_a, pe_b)."""
    label = f"pe-pair:{_SITE}-{idx}"
    pe_a = _make_pe(session, f"pe-{ce_name}-a", ce_site, pe_role, pair_label=label)
    pe_b = _make_pe(session, f"pe-{ce_name}-b", ce_site, pe_role, pair_label=label)
    ce_builder.attach_ce(
        site_name=_SITE,
        ce_name=ce_name,
        pe_role_name=_PE_ROLE,
        ce_role_name=_CE_ROLE,
        ce_model_name="test_model_1",
    )
    return pe_a, pe_b


def _attach_single_homed(session, ce_builder, ce_site, pe_role, *, ce_name):
    """Attach a single-homed CE to a fresh solo PE. Returns the PE."""
    pe = _make_pe(session, f"pe-{ce_name}", ce_site, pe_role, uni_count=3)
    # Pin to this PE: without connected_pe the builder auto-selects any PE on
    # the site with free UNIs, which would collide across multiple CEs.
    ce_builder.attach_ce(
        site_name=_SITE,
        ce_name=ce_name,
        pe_role_name=_PE_ROLE,
        ce_role_name=_CE_ROLE,
        ce_model_name="test_model_1",
        connected_pe=pe.hostname,
    )
    return pe


# ---------------------------------------------------------------------------
# Structural discovery
# ---------------------------------------------------------------------------
class TestFindCeLags:
    def test_discovers_dual_homed_ce_by_description(
        self, session, ce_builder, ce_site, pe_role, ce_role
    ):
        _attach_dual_homed(session, ce_builder, ce_site, pe_role, ce_name="ce-find", idx=1)

        by_ce = find_ce_lags(session)

        assert "ce-find" in by_ce
        assert len(by_ce["ce-find"]) == 2  # one LAG per PE

    def test_similar_hostnames_are_not_confused(
        self, session, ce_builder, ce_site, pe_role, ce_role
    ):
        # "ce1" must not match "ce10": the parser splits on " (", not a prefix.
        _attach_single_homed(session, ce_builder, ce_site, pe_role, ce_name="ce1")
        _attach_single_homed(session, ce_builder, ce_site, pe_role, ce_name="ce10")

        by_ce = find_ce_lags(session)

        assert len(by_ce["ce1"]) == 1
        assert len(by_ce["ce10"]) == 1
