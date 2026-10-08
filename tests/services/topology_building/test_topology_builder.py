from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import Cable, Device, Interface
from app.models.orm_models import PrefixPool, PrefixPoolType, Role, Site
from app.services.service_handling.resource_pool_allocator import ResourcePoolAllocator
from app.services.topology_building.cable_builder import CableBuilder
from app.services.topology_building.device_builder import DeviceBuilder
from app.services.topology_building.device_factory import DeviceFactory
from app.services.topology_building.topology_builder import TopologyBuilder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_builder() -> TopologyBuilder:
    """Minimal TopologyBuilder with mocked dependencies (sufficient for pure-logic tests)."""
    return TopologyBuilder(
        session=MagicMock(),
        device_builder=MagicMock(),
        prefix_allocator=MagicMock(),
        cable_builder=MagicMock(),
    )


def _dev(model_name: str, pair_label: str | None = None) -> SimpleNamespace:
    labels = {"pair_label": pair_label} if pair_label is not None else {}
    return SimpleNamespace(model_name=model_name, labels=labels)


_OVERRIDE_MODEL = "7250-IXR-e2-400"
_OTHER_MODEL = "7250-IXR-e2-100"
_PLAIN_MODEL = "some-other-model"


# ---------------------------------------------------------------------------
# _should_use_pair_override
# ---------------------------------------------------------------------------


class TestShouldUsePairOverride:
    def setup_method(self):
        self.tb = _make_builder()

    def test_returns_true_when_same_override_model_and_matching_pair_labels(self):
        a = _dev(_OVERRIDE_MODEL, "pe-pair:site-1")
        b = _dev(_OVERRIDE_MODEL, "pe-pair:site-1")
        assert self.tb._should_use_pair_override(a, b) is True

    def test_both_supported_override_models_qualify(self):
        """Both model variants listed in PAIR_OVERRIDE_MODELS should trigger the override."""
        a = _dev(_OTHER_MODEL, "pe-pair:site-1")
        b = _dev(_OTHER_MODEL, "pe-pair:site-1")
        assert self.tb._should_use_pair_override(a, b) is True

    def test_returns_false_when_model_not_in_override_set(self):
        a = _dev(_PLAIN_MODEL, "pe-pair:site-1")
        b = _dev(_PLAIN_MODEL, "pe-pair:site-1")
        assert self.tb._should_use_pair_override(a, b) is False

    def test_returns_false_when_models_differ(self):
        a = _dev(_OVERRIDE_MODEL, "pe-pair:site-1")
        b = _dev(_OTHER_MODEL, "pe-pair:site-1")
        assert self.tb._should_use_pair_override(a, b) is False

    def test_returns_false_when_pair_labels_differ(self):
        a = _dev(_OVERRIDE_MODEL, "pe-pair:site-1")
        b = _dev(_OVERRIDE_MODEL, "pe-pair:site-2")
        assert self.tb._should_use_pair_override(a, b) is False

    def test_returns_false_when_one_device_has_no_pair_label(self):
        a = _dev(_OVERRIDE_MODEL, "pe-pair:site-1")
        b = _dev(_OVERRIDE_MODEL, None)
        assert self.tb._should_use_pair_override(a, b) is False

    def test_returns_false_when_both_devices_have_no_pair_label(self):
        a = _dev(_OVERRIDE_MODEL, None)
        b = _dev(_OVERRIDE_MODEL, None)
        assert self.tb._should_use_pair_override(a, b) is False

    def test_pair_override_models_constant_contains_expected_entries(self):
        assert "7250-IXR-e2-400" in TopologyBuilder.PAIR_OVERRIDE_MODELS
        assert "7250-IXR-e2-100" in TopologyBuilder.PAIR_OVERRIDE_MODELS


# ---------------------------------------------------------------------------
# Integration fixtures — build_p2p_link
# ---------------------------------------------------------------------------


@pytest.fixture
def p2p_pool(session):
    pool_type = PrefixPoolType(name="_tbi_pool_type")
    session.add(pool_type)
    session.flush()
    pool = PrefixPool(
        name="_tbi_p2p_pool",
        prefix="10.99.0.0/24",
        type_id=pool_type.id,
    )
    session.add(pool)
    session.flush()
    return pool


@pytest.fixture
def two_plain_devices(session):
    site = Site(name="_tbi_site")
    role = Role(name="_tbi_role")
    session.add_all([site, role])
    session.flush()

    dev_a = Device(
        hostname="_tbi_dev_a",
        lag_name="lag",
        model_name="test_model_1",
        labels={},
        site=site,
        role=role,
    )
    dev_b = Device(
        hostname="_tbi_dev_b",
        lag_name="lag",
        model_name="test_model_1",
        labels={},
        site=site,
        role=role,
    )
    session.add_all([dev_a, dev_b])
    session.flush()

    for i in range(3):
        session.add(Interface(name=f"Ethernet0/0/{i}", intf_role="NNI", device=dev_a))
        session.add(Interface(name=f"Ethernet0/0/{i}", intf_role="NNI", device=dev_b))
    session.flush()

    return dev_a, dev_b


@pytest.fixture
def tb(session):
    rpa = ResourcePoolAllocator(session=session)
    factory = DeviceFactory(session=session)
    device_builder = DeviceBuilder(
        session=session,
        device_factory=factory,
        prefix_allocator=rpa,
    )
    return TopologyBuilder(
        session=session,
        device_builder=device_builder,
        prefix_allocator=rpa,
        cable_builder=CableBuilder(session=session),
    )


# ---------------------------------------------------------------------------
# build_p2p_link — error handling
# ---------------------------------------------------------------------------


class TestBuildP2pLinkErrors:
    def test_raises_if_device_a_not_found(self, tb, two_plain_devices, p2p_pool):
        with pytest.raises(ValueError, match="nonexistent_a"):
            tb.build_p2p_link(
                "nonexistent_a",
                "_tbi_dev_b",
                p2p_pool,
                on_lag=False,
            )

    def test_raises_if_device_b_not_found(self, tb, two_plain_devices, p2p_pool):
        with pytest.raises(ValueError, match="nonexistent_b"):
            tb.build_p2p_link(
                "_tbi_dev_a",
                "nonexistent_b",
                p2p_pool,
                on_lag=False,
            )

    def test_raises_if_supplied_interface_already_in_use(
        self, session, tb, two_plain_devices, p2p_pool
    ):
        dev_a, _ = two_plain_devices
        iface = next(i for i in dev_a.interfaces if i.intf_role == "NNI")
        iface.in_use = True
        session.flush()

        with pytest.raises(RuntimeError, match="already in use"):
            tb.build_p2p_link(
                "_tbi_dev_a",
                "_tbi_dev_b",
                p2p_pool,
                on_lag=False,
                iface_a_name=iface.name,
            )


# ---------------------------------------------------------------------------
# build_p2p_link — non-LAG path
# ---------------------------------------------------------------------------


class TestBuildP2pLinkNonLag:
    def test_returns_cable(self, tb, two_plain_devices, p2p_pool):
        cable = tb.build_p2p_link(
            "_tbi_dev_a",
            "_tbi_dev_b",
            p2p_pool,
            on_lag=False,
        )
        assert isinstance(cable, Cable)

    def test_marks_selected_interfaces_in_use(self, session, tb, two_plain_devices, p2p_pool):
        tb.build_p2p_link("_tbi_dev_a", "_tbi_dev_b", p2p_pool, on_lag=False)
        dev_a, dev_b = two_plain_devices
        session.refresh(dev_a)
        session.refresh(dev_b)
        used_a = [i for i in dev_a.interfaces if i.in_use and i.intf_role == "NNI"]
        used_b = [i for i in dev_b.interfaces if i.in_use and i.intf_role == "NNI"]
        assert len(used_a) == 1
        assert len(used_b) == 1

    def test_ip_assigned_directly_to_physical_interface(
        self, session, tb, two_plain_devices, p2p_pool
    ):
        tb.build_p2p_link("_tbi_dev_a", "_tbi_dev_b", p2p_pool, on_lag=False)
        dev_a, _ = two_plain_devices
        session.refresh(dev_a)
        used = next(i for i in dev_a.interfaces if i.in_use and i.intf_role == "NNI")
        assert len(used.ip_addresses) == 1

    def test_descriptions_cross_reference_remote_device(
        self, session, tb, two_plain_devices, p2p_pool
    ):
        tb.build_p2p_link("_tbi_dev_a", "_tbi_dev_b", p2p_pool, on_lag=False)
        dev_a, dev_b = two_plain_devices
        session.refresh(dev_a)
        session.refresh(dev_b)
        used_a = next(i for i in dev_a.interfaces if i.in_use and i.intf_role == "NNI")
        used_b = next(i for i in dev_b.interfaces if i.in_use and i.intf_role == "NNI")
        assert "_tbi_dev_b" in used_a.description
        assert "_tbi_dev_a" in used_b.description

    def test_uses_caller_supplied_interface_names(self, session, tb, two_plain_devices, p2p_pool):
        tb.build_p2p_link(
            "_tbi_dev_a",
            "_tbi_dev_b",
            p2p_pool,
            on_lag=False,
            iface_a_name="Ethernet0/0/1",
            iface_b_name="Ethernet0/0/2",
        )
        dev_a, dev_b = two_plain_devices
        session.refresh(dev_a)
        session.refresh(dev_b)
        iface_a = next(i for i in dev_a.interfaces if i.name == "Ethernet0/0/1")
        iface_b = next(i for i in dev_b.interfaces if i.name == "Ethernet0/0/2")
        assert iface_a.in_use is True
        assert iface_b.in_use is True


# ---------------------------------------------------------------------------
# build_p2p_link — LAG path
# ---------------------------------------------------------------------------


class TestBuildP2pLinkLag:
    def test_lag_interfaces_created_on_both_devices(self, session, tb, two_plain_devices, p2p_pool):
        tb.build_p2p_link("_tbi_dev_a", "_tbi_dev_b", p2p_pool, on_lag=True)
        dev_a, dev_b = two_plain_devices
        session.refresh(dev_a)
        session.refresh(dev_b)
        lags_a = [i for i in dev_a.interfaces if i.name.startswith("lag-") and i.intf_role == "NNI"]
        lags_b = [i for i in dev_b.interfaces if i.name.startswith("lag-") and i.intf_role == "NNI"]
        assert len(lags_a) == 1
        assert len(lags_b) == 1

    def test_ip_assigned_to_lag_not_physical(self, session, tb, two_plain_devices, p2p_pool):
        tb.build_p2p_link("_tbi_dev_a", "_tbi_dev_b", p2p_pool, on_lag=True)
        dev_a, _ = two_plain_devices
        session.refresh(dev_a)
        lag = next(i for i in dev_a.interfaces if i.name.startswith("lag-"))
        physical = next(i for i in dev_a.interfaces if i.in_use and not i.name.startswith("lag-"))
        assert len(lag.ip_addresses) == 1
        assert len(physical.ip_addresses) == 0

    def test_physical_member_attached_to_lag(self, session, tb, two_plain_devices, p2p_pool):
        tb.build_p2p_link("_tbi_dev_a", "_tbi_dev_b", p2p_pool, on_lag=True)
        dev_a, _ = two_plain_devices
        session.refresh(dev_a)
        physical = next(i for i in dev_a.interfaces if i.in_use and not i.name.startswith("lag-"))
        assert physical.parent is not None
        assert physical.parent.name.startswith("lag-")


# ---------------------------------------------------------------------------
# build_p2p_link — pair override path
# ---------------------------------------------------------------------------


class TestBuildP2pLinkPairOverride:
    @pytest.fixture
    def override_devices(self, session, p2p_pool):
        site = Site(name="_tbi_ov_site")
        role = Role(name="_tbi_ov_role")
        session.add_all([site, role])
        session.flush()

        label = "pe-pair:_tbi_ov_site-1"
        dev_a = Device(
            hostname="_tbi_ov_dev_a",
            lag_name="lag",
            model_name="7250-IXR-e2-400",
            labels={"pair_label": label},
            site=site,
            role=role,
        )
        dev_b = Device(
            hostname="_tbi_ov_dev_b",
            lag_name="lag",
            model_name="7250-IXR-e2-400",
            labels={"pair_label": label},
            site=site,
            role=role,
        )
        session.add_all([dev_a, dev_b])
        session.flush()

        # Pair override expects specific fixed port names
        session.add(Interface(name="1/1/c2/1", intf_role="NNI", device=dev_a))
        session.add(Interface(name="1/1/c1/1", intf_role="NNI", device=dev_b))
        session.flush()

        return dev_a, dev_b

    def test_pair_override_uses_fixed_port_names(self, session, tb, override_devices, p2p_pool):
        dev_a, dev_b = override_devices
        tb.build_p2p_link(
            dev_a.hostname,
            dev_b.hostname,
            p2p_pool,
            on_lag=False,
        )
        session.refresh(dev_a)
        session.refresh(dev_b)
        iface_a = next(i for i in dev_a.interfaces if i.name == "1/1/c2/1")
        iface_b = next(i for i in dev_b.interfaces if i.name == "1/1/c1/1")
        assert iface_a.in_use is True
        assert iface_b.in_use is True
