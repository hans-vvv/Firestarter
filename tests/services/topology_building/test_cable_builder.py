from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Cable, CableStatus, Device, Interface
from app.models.orm_models import Role, Site
from app.services.topology_building.cable_builder import CableBuilder

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_interfaces(session):
    site = Site(name="_cb_site")
    role = Role(name="_cb_role")
    session.add_all([site, role])
    session.flush()

    dev_a = Device(
        hostname="_cb_dev_a",
        lag_name="lag",
        model_name="test_model_1",
        labels={},
        site=site,
        role=role,
    )
    dev_b = Device(
        hostname="_cb_dev_b",
        lag_name="lag",
        model_name="test_model_1",
        labels={},
        site=site,
        role=role,
    )
    session.add_all([dev_a, dev_b])
    session.flush()

    iface_a = Interface(name="Ethernet0/0/0", intf_role="NNI", device=dev_a)
    iface_b = Interface(name="Ethernet0/0/0", intf_role="NNI", device=dev_b)
    session.add_all([iface_a, iface_b])
    session.flush()

    return iface_a, iface_b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCableBuilderConnect:
    def test_returns_cable_object(self, session, two_interfaces):
        iface_a, iface_b = two_interfaces
        cable = CableBuilder(session=session).connect(iface_a, iface_b)
        assert isinstance(cable, Cable)

    def test_cable_references_correct_interfaces(self, session, two_interfaces):
        iface_a, iface_b = two_interfaces
        cable = CableBuilder(session=session).connect(iface_a, iface_b)
        assert cable.interface_a_id == iface_a.id
        assert cable.interface_b_id == iface_b.id

    def test_description_contains_both_hostnames(self, session, two_interfaces):
        iface_a, iface_b = two_interfaces
        cable = CableBuilder(session=session).connect(iface_a, iface_b)
        assert "_cb_dev_a" in cable.description
        assert "_cb_dev_b" in cable.description

    def test_description_contains_both_interface_names(self, session, two_interfaces):
        iface_a, iface_b = two_interfaces
        cable = CableBuilder(session=session).connect(iface_a, iface_b)
        assert "Ethernet0/0/0" in cable.description

    def test_cable_is_persisted_in_db(self, session, two_interfaces):
        iface_a, iface_b = two_interfaces
        cable = CableBuilder(session=session).connect(iface_a, iface_b)
        fetched = session.scalars(select(Cable).where(Cable.id == cable.id)).one_or_none()
        assert fetched is not None

    def test_status_set_to_connected(self, session, two_interfaces):
        iface_a, iface_b = two_interfaces
        cable = CableBuilder(session=session).connect(iface_a, iface_b)
        assert cable.status == CableStatus.connected
