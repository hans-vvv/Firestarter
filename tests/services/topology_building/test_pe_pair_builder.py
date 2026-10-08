from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Cable, Device, Interface
from app.models.orm_models import PrefixPool, PrefixPoolType, Role, Site
from app.services.service_handling.resource_pool_allocator import ResourcePoolAllocator
from app.services.topology_building.cable_builder import CableBuilder
from app.services.topology_building.device_builder import DeviceBuilder
from app.services.topology_building.device_factory import DeviceFactory
from app.services.topology_building.pe_pair_builder import PEPairBuilder
from app.services.topology_building.topology_builder import TopologyBuilder

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def p2p_pool(session):
    pool_type = PrefixPoolType(name="_pp_pool_type")
    session.add(pool_type)
    session.flush()
    pool = PrefixPool(
        name="_pp_p2p_pool",
        prefix="10.88.0.0/24",
        type_id=pool_type.id,
    )
    session.add(pool)
    session.flush()
    return pool


@pytest.fixture
def same_site_devices(session):
    site = Site(name="_pp_site")
    role = Role(name="_pp_role")
    session.add_all([site, role])
    session.flush()

    dev_a = Device(
        hostname="_pp_dev_a",
        lag_name="lag",
        model_name="test_model_1",
        labels={},
        site=site,
        role=role,
    )
    dev_b = Device(
        hostname="_pp_dev_b",
        lag_name="lag",
        model_name="test_model_1",
        labels={},
        site=site,
        role=role,
    )
    session.add_all([dev_a, dev_b])
    session.flush()

    for i in range(2):
        session.add(Interface(name=f"Ethernet0/0/{i}", intf_role="NNI", device=dev_a))
        session.add(Interface(name=f"Ethernet0/0/{i}", intf_role="NNI", device=dev_b))
    session.flush()

    return dev_a, dev_b


@pytest.fixture
def different_site_devices(session):
    site_a = Site(name="_pp_site_x")
    site_b = Site(name="_pp_site_y")
    role = Role(name="_pp_role_cross")
    session.add_all([site_a, site_b, role])
    session.flush()

    dev_a = Device(
        hostname="_pp_cross_a",
        lag_name="lag",
        model_name="test_model_1",
        labels={},
        site=site_a,
        role=role,
    )
    dev_b = Device(
        hostname="_pp_cross_b",
        lag_name="lag",
        model_name="test_model_1",
        labels={},
        site=site_b,
        role=role,
    )
    session.add_all([dev_a, dev_b])
    session.flush()

    for i in range(2):
        session.add(Interface(name=f"Ethernet0/0/{i}", intf_role="NNI", device=dev_a))
        session.add(Interface(name=f"Ethernet0/0/{i}", intf_role="NNI", device=dev_b))
    session.flush()

    return dev_a, dev_b


@pytest.fixture
def pair_builder(session):
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
    return PEPairBuilder(
        session=session,
        topology_builder=topology_builder,
        device_builder=device_builder,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreatePePair:
    def test_returns_cable(self, pair_builder, same_site_devices, p2p_pool):
        dev_a, dev_b = same_site_devices
        cable = pair_builder.create_pe_pair(
            on_lag=False,
            p2p_pool=p2p_pool,
            dev_a=dev_a,
            dev_b=dev_b,
        )
        assert isinstance(cable, Cable)

    def test_assigns_pair_label_to_both_devices(
        self, session, pair_builder, same_site_devices, p2p_pool
    ):
        dev_a, dev_b = same_site_devices
        pair_builder.create_pe_pair(
            on_lag=False,
            p2p_pool=p2p_pool,
            dev_a=dev_a,
            dev_b=dev_b,
        )
        session.refresh(dev_a)
        session.refresh(dev_b)
        assert "pair_label" in dev_a.labels
        assert "pair_label" in dev_b.labels

    def test_both_devices_share_same_pair_label(
        self, session, pair_builder, same_site_devices, p2p_pool
    ):
        dev_a, dev_b = same_site_devices
        pair_builder.create_pe_pair(
            on_lag=False,
            p2p_pool=p2p_pool,
            dev_a=dev_a,
            dev_b=dev_b,
        )
        session.refresh(dev_a)
        session.refresh(dev_b)
        assert dev_a.labels["pair_label"] == dev_b.labels["pair_label"]

    def test_pair_label_includes_site_name(
        self, session, pair_builder, same_site_devices, p2p_pool
    ):
        dev_a, dev_b = same_site_devices
        pair_builder.create_pe_pair(
            on_lag=False,
            p2p_pool=p2p_pool,
            dev_a=dev_a,
            dev_b=dev_b,
        )
        session.refresh(dev_a)
        assert "_pp_site" in dev_a.labels["pair_label"]

    def test_pair_label_follows_pe_pair_convention(
        self, session, pair_builder, same_site_devices, p2p_pool
    ):
        dev_a, dev_b = same_site_devices
        pair_builder.create_pe_pair(
            on_lag=False,
            p2p_pool=p2p_pool,
            dev_a=dev_a,
            dev_b=dev_b,
        )
        session.refresh(dev_a)
        assert dev_a.labels["pair_label"].startswith("pe-pair:")

    def test_raises_if_devices_on_different_sites(
        self, pair_builder, different_site_devices, p2p_pool
    ):
        dev_a, dev_b = different_site_devices
        with pytest.raises(ValueError, match="same Site"):
            pair_builder.create_pe_pair(
                on_lag=False,
                p2p_pool=p2p_pool,
                dev_a=dev_a,
                dev_b=dev_b,
            )

    def test_creates_connecting_cable(self, session, pair_builder, same_site_devices, p2p_pool):
        dev_a, dev_b = same_site_devices
        cable = pair_builder.create_pe_pair(
            on_lag=False,
            p2p_pool=p2p_pool,
            dev_a=dev_a,
            dev_b=dev_b,
        )
        fetched = session.scalars(select(Cable).where(Cable.id == cable.id)).one_or_none()
        assert fetched is not None
