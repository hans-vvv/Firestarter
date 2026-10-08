from __future__ import annotations

import pytest

from app.models import Device, Interface
from app.models.orm_models import Role, Site
from app.services.topology_building.device_builder import DeviceBuilder


@pytest.fixture
def simple_device(session):
    """
    Minimal device with 2 NNI and 2 UNI physical interfaces.
    Does not depend on seeded_inventory — builds only what the tests need.
    """
    site = Site(name="_tb_site")
    role = Role(name="_tb_role")
    session.add_all([site, role])
    session.flush()

    device = Device(
        hostname="_tb_device",
        lag_name="lag",
        model_name="test_model_1",
        labels={},
        site=site,
        role=role,
    )
    session.add(device)
    session.flush()

    for i in range(2):
        session.add(Interface(name=f"Ethernet0/0/{i}", intf_role="NNI", device=device))
    for i in range(2, 4):
        session.add(Interface(name=f"Ethernet0/0/{i}", intf_role="UNI", device=device))
    session.flush()
    return device


class TestSelectFreeInterface:
    def test_select_free_uni_returns_a_free_uni(self, simple_device):
        iface = DeviceBuilder.select_free_uni(simple_device)
        assert iface.intf_role == "UNI"
        assert iface.in_use is False

    def test_select_free_nni_returns_a_free_nni(self, simple_device):
        iface = DeviceBuilder.select_free_nni(simple_device)
        assert iface.intf_role == "NNI"
        assert iface.in_use is False

    def test_select_free_uni_raises_when_all_in_use(self, simple_device):
        for iface in simple_device.interfaces:
            if iface.intf_role == "UNI":
                iface.in_use = True
        with pytest.raises(RuntimeError, match="No free UNI"):
            DeviceBuilder.select_free_uni(simple_device)

    def test_select_free_nni_raises_when_all_in_use(self, simple_device):
        for iface in simple_device.interfaces:
            if iface.intf_role == "NNI":
                iface.in_use = True
        with pytest.raises(RuntimeError, match="No free NNI"):
            DeviceBuilder.select_free_nni(simple_device)


class TestLagIndexing:
    def test_nni_index_defaults_to_1_when_no_lags_exist(self, simple_device):
        assert DeviceBuilder._next_nni_lag_index(simple_device) == 1

    def test_uni_index_defaults_to_33_when_no_lags_exist(self, simple_device):
        assert DeviceBuilder._next_uni_lag_index(simple_device) == 33

    def test_nni_index_returns_lowest_unused_when_higher_index_exists(self, session, simple_device):
        """Hole-filling: a higher-numbered LAG present doesn't push the
        next index past lag-1 if lag-1 is unused. This makes
        detach-then-rebuild name-preserving for the migration tool."""
        session.add(Interface(name="lag-3", intf_role="NNI", device=simple_device))
        session.flush()
        assert DeviceBuilder._next_nni_lag_index(simple_device) == 1

    def test_uni_index_returns_lowest_unused_when_higher_index_exists(self, session, simple_device):
        session.add(Interface(name="lag-35", intf_role="UNI", device=simple_device))
        session.flush()
        assert DeviceBuilder._next_uni_lag_index(simple_device) == 33

    def test_nni_index_skips_used_indices_to_find_lowest_unused(self, session, simple_device):
        session.add(Interface(name="lag-1", intf_role="NNI", device=simple_device))
        session.add(Interface(name="lag-2", intf_role="NNI", device=simple_device))
        session.add(Interface(name="lag-4", intf_role="NNI", device=simple_device))
        session.flush()
        # 1 and 2 used, 3 unused → 3
        assert DeviceBuilder._next_nni_lag_index(simple_device) == 3

    def test_uni_index_skips_used_indices_to_find_lowest_unused(self, session, simple_device):
        session.add(Interface(name="lag-33", intf_role="UNI", device=simple_device))
        session.add(Interface(name="lag-34", intf_role="UNI", device=simple_device))
        session.add(Interface(name="lag-36", intf_role="UNI", device=simple_device))
        session.flush()
        # 33 and 34 used, 35 unused → 35
        assert DeviceBuilder._next_uni_lag_index(simple_device) == 35

    def test_nni_index_ignores_uni_lags(self, session, simple_device):
        # A UNI LAG at index 40 must not shift the NNI starting index.
        session.add(Interface(name="lag-40", intf_role="UNI", device=simple_device))
        session.flush()
        assert DeviceBuilder._next_nni_lag_index(simple_device) == 1

    def test_uni_index_ignores_nni_lags(self, session, simple_device):
        # An NNI LAG at index 5 must not shift the UNI starting index.
        session.add(Interface(name="lag-5", intf_role="NNI", device=simple_device))
        session.flush()
        assert DeviceBuilder._next_uni_lag_index(simple_device) == 33
