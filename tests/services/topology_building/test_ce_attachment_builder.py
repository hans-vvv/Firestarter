from __future__ import annotations

import pytest

from app.models import Device, Interface
from app.models.orm_models import Role, Site
from app.services.service_handling.resource_pool_allocator import ResourcePoolAllocator
from app.services.topology_building.cable_builder import CableBuilder
from app.services.topology_building.ce_attachment_builder import CEAttachmentBuilder
from app.services.topology_building.device_builder import DeviceBuilder
from app.services.topology_building.device_factory import DeviceFactory
from app.services.topology_building.topology_builder import TopologyBuilder

# Unique names so these fixtures never collide with seeded_inventory data.
_SITE = "_ce_test_site"
_PE_ROLE = "_ce_test_pe"
_CE_ROLE = "_ce_test_ce"


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
    """Create a PE device with UNI interfaces and an optional pair_label."""
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


# ---------------------------------------------------------------------------
# Unit tests — static helpers, no DB required
# ---------------------------------------------------------------------------


class TestGetPairMap:
    """_get_pair_map groups devices by their pair_label."""

    def _stub(self, pair_label):
        from types import SimpleNamespace

        return SimpleNamespace(labels={"pair_label": pair_label} if pair_label else {})

    def test_groups_two_devices_under_same_label(self):
        pe_a = self._stub("pe-pair:site-1")
        pe_b = self._stub("pe-pair:site-1")
        result = CEAttachmentBuilder._get_pair_map("site", [pe_a, pe_b])
        assert len(result["pe-pair:site-1"]) == 2

    def test_devices_from_different_pairs_land_in_separate_buckets(self):
        pe_a = self._stub("pe-pair:site-1")
        pe_b = self._stub("pe-pair:site-2")
        result = CEAttachmentBuilder._get_pair_map("site", [pe_a, pe_b])
        assert len(result) == 2

    def test_device_with_mismatched_site_prefix_is_excluded(self):
        pe = self._stub("pe-pair:other-site-1")
        result = CEAttachmentBuilder._get_pair_map("site", [pe])
        assert result == {}

    def test_device_with_no_pair_label_is_excluded(self):
        pe = self._stub(None)
        result = CEAttachmentBuilder._get_pair_map("site", [pe])
        assert result == {}


class TestSortedPairs:
    """_sorted_pairs sorts pair entries by the trailing numeric index."""

    def test_returns_pairs_in_ascending_numeric_order(self):
        pair_map = {
            "pe-pair:site-3": [],
            "pe-pair:site-1": [],
            "pe-pair:site-2": [],
        }
        result = CEAttachmentBuilder._sorted_pairs(pair_map)
        labels = [label for label, _ in result]
        assert labels == ["pe-pair:site-1", "pe-pair:site-2", "pe-pair:site-3"]

    def test_non_numeric_suffix_raises(self):
        pair_map = {"pe-pair:site-notanumber": []}
        with pytest.raises(RuntimeError, match="Invalid PE pair label format"):
            CEAttachmentBuilder._sorted_pairs(pair_map)


# ---------------------------------------------------------------------------
# Integration tests — _select_pes_for_attachment
# ---------------------------------------------------------------------------


class TestSelectPesForAttachment:
    def test_returns_pair_when_one_exists(self, session, ce_builder, ce_site, pe_role):
        label = f"pe-pair:{_SITE}-1"
        _make_pe(session, "pe-sel-a", ce_site, pe_role, pair_label=label)
        _make_pe(session, "pe-sel-b", ce_site, pe_role, pair_label=label)

        pes, returned_label = ce_builder._select_pes_for_attachment(
            site_name=_SITE, pe_role_name=_PE_ROLE, pe_pair_label=None
        )

        assert len(pes) == 2
        assert returned_label == label

    def test_falls_back_to_single_pe_when_no_pair_label_present(
        self, session, ce_builder, ce_site, pe_role
    ):
        _make_pe(session, "pe-sel-solo", ce_site, pe_role)

        pes, label = ce_builder._select_pes_for_attachment(
            site_name=_SITE, pe_role_name=_PE_ROLE, pe_pair_label=None
        )

        assert len(pes) == 1
        assert label is None

    def test_explicit_label_override_selects_correct_pair(
        self, session, ce_builder, ce_site, pe_role
    ):
        target = f"pe-pair:{_SITE}-1"
        _make_pe(session, "pe-sel-c", ce_site, pe_role, pair_label=target)
        _make_pe(session, "pe-sel-d", ce_site, pe_role, pair_label=target)

        pes, label = ce_builder._select_pes_for_attachment(
            site_name=_SITE, pe_role_name=_PE_ROLE, pe_pair_label=target
        )

        assert len(pes) == 2
        assert label == target

    def test_nonexistent_pair_label_raises(self, session, ce_builder, ce_site, pe_role):
        _make_pe(session, "pe-sel-e", ce_site, pe_role)

        with pytest.raises(RuntimeError, match="not found on site"):
            ce_builder._select_pes_for_attachment(
                site_name=_SITE,
                pe_role_name=_PE_ROLE,
                pe_pair_label=f"pe-pair:{_SITE}-99",
            )

    def test_raises_when_no_pe_has_free_uni(self, session, ce_builder, ce_site, pe_role):
        pe = _make_pe(session, "pe-sel-f", ce_site, pe_role)
        for iface in pe.interfaces:
            iface.in_use = True
        session.flush()

        with pytest.raises(RuntimeError, match="No PE"):
            ce_builder._select_pes_for_attachment(
                site_name=_SITE, pe_role_name=_PE_ROLE, pe_pair_label=None
            )


# ---------------------------------------------------------------------------
# Integration tests — attach_ce
# ---------------------------------------------------------------------------


class TestAttachCe:
    def test_creates_ce_with_correct_role_and_site(
        self, session, ce_builder, ce_site, pe_role, ce_role
    ):
        label = f"pe-pair:{_SITE}-1"
        _make_pe(session, "pe-att-a", ce_site, pe_role, pair_label=label)
        _make_pe(session, "pe-att-b", ce_site, pe_role, pair_label=label)

        ce = ce_builder.attach_ce(
            site_name=_SITE,
            ce_name="test-ce-001",
            pe_role_name=_PE_ROLE,
            ce_role_name=_CE_ROLE,
            ce_model_name="test_model_1",
        )

        assert ce.hostname == "test-ce-001"
        assert ce.role.name == _CE_ROLE
        assert ce.site.name == _SITE

    def test_dual_homed_marks_exactly_one_uni_per_pe(
        self, session, ce_builder, ce_site, pe_role, ce_role
    ):
        label = f"pe-pair:{_SITE}-1"
        pe_a = _make_pe(session, "pe-att-c", ce_site, pe_role, pair_label=label)
        pe_b = _make_pe(session, "pe-att-d", ce_site, pe_role, pair_label=label)

        ce_builder.attach_ce(
            site_name=_SITE,
            ce_name="test-ce-002",
            pe_role_name=_PE_ROLE,
            ce_role_name=_CE_ROLE,
            ce_model_name="test_model_1",
        )

        session.refresh(pe_a)
        session.refresh(pe_b)
        for pe in [pe_a, pe_b]:
            used_unis = [
                i
                for i in pe.interfaces
                if i.intf_role == "UNI" and i.in_use and not i.name.startswith("lag")
            ]
            assert len(used_unis) == 1, f"{pe.hostname} should have exactly 1 UNI in use"

    def test_single_homed_marks_two_unis_on_pe(
        self, session, ce_builder, ce_site, pe_role, ce_role
    ):
        # No pair_label -> single-homed -> two physical UNIs in the LAG.
        pe = _make_pe(session, "pe-att-e", ce_site, pe_role, uni_count=3)

        ce_builder.attach_ce(
            site_name=_SITE,
            ce_name="test-ce-003",
            pe_role_name=_PE_ROLE,
            ce_role_name=_CE_ROLE,
            ce_model_name="test_model_1",
        )

        session.refresh(pe)
        used_unis = [
            i
            for i in pe.interfaces
            if i.intf_role == "UNI" and i.in_use and not i.name.startswith("lag")
        ]
        assert len(used_unis) == 2

    def test_lag_and_member_descriptions_reference_ce_name(
        self, session, ce_builder, ce_site, pe_role, ce_role
    ):
        pe = _make_pe(session, "pe-att-f", ce_site, pe_role, uni_count=3)

        ce_builder.attach_ce(
            site_name=_SITE,
            ce_name="test-ce-desc",
            pe_role_name=_PE_ROLE,
            ce_role_name=_CE_ROLE,
            ce_model_name="test_model_1",
        )

        session.refresh(pe)

        lag = next(i for i in pe.interfaces if i.intf_role == "UNI" and i.name.startswith("lag"))
        assert "test-ce-desc" in lag.description

        members = [
            i
            for i in pe.interfaces
            if i.intf_role == "UNI" and i.in_use and not i.name.startswith("lag")
        ]
        for member in members:
            assert "test-ce-desc" in member.description
