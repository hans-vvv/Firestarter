from __future__ import annotations

import pytest

from app.models import Device, Interface
from app.models.orm_models import Role, Site
from app.services.context.device_context import DeviceContextComposer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def site(session):
    s = Site(name="_ctx_site")
    session.add(s)
    session.flush()
    return s


@pytest.fixture
def role(session):
    r = Role(name="_ctx_role")
    session.add(r)
    session.flush()
    return r


@pytest.fixture
def device(session, site, role):
    dev = Device(
        hostname="_ctx_device",
        lag_name="lag",
        model_name="test_model_1",
        labels={"tenant": "lab"},
        site=site,
        role=role,
    )
    session.add(dev)
    session.flush()
    return dev


@pytest.fixture
def composer(session):
    return DeviceContextComposer(session=session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_iface(session, device, *, name, role="NNI", in_use=True, parent=None):
    iface = Interface(
        name=name,
        intf_role=role,
        device=device,
        in_use=in_use,
        parent=parent,
    )
    session.add(iface)
    session.flush()
    return iface


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestComposeRaisesOnMissingDevice:
    def test_raises_if_device_not_found(self, composer):
        with pytest.raises(ValueError, match="No dB record"):
            composer.compose(hostname="nonexistent.host")


# ---------------------------------------------------------------------------
# Device-level fields
# ---------------------------------------------------------------------------


class TestComposeDeviceFields:
    def test_returns_hostname(self, device, composer):
        result = composer.compose(hostname="_ctx_device")
        assert result["hostname"] == "_ctx_device"

    def test_returns_role_name(self, device, composer):
        result = composer.compose(hostname="_ctx_device")
        assert result["device_role_name"] == "_ctx_role"

    def test_returns_model_name(self, device, composer):
        result = composer.compose(hostname="_ctx_device")
        assert result["device_model_name"] == "test_model_1"

    def test_returns_tenant_label(self, device, composer):
        result = composer.compose(hostname="_ctx_device")
        assert result["tenant"] == "lab"

    def test_no_tenant_label_returns_none(self, session, site, role, composer):
        dev = Device(
            hostname="_ctx_no_tenant",
            lag_name="lag",
            model_name="test_model_1",
            labels={},
            site=site,
            role=role,
        )
        session.add(dev)
        session.flush()
        result = composer.compose(hostname="_ctx_no_tenant")
        assert result["tenant"] is None


# ---------------------------------------------------------------------------
# Interface render types
# ---------------------------------------------------------------------------


class TestInterfaceRenderType:
    def test_loopback_gets_skip_render_type(self, session, device, composer):
        _add_iface(session, device, name="Loopback0")
        result = composer.compose(hostname="_ctx_device")
        loopback = next(i for i in result["interfaces"] if i["name"] == "Loopback0")
        assert loopback["render_type"] == "skip"

    def test_loopback_case_insensitive(self, session, device, composer):
        _add_iface(session, device, name="loopback1")
        result = composer.compose(hostname="_ctx_device")
        lo = next(i for i in result["interfaces"] if i["name"] == "loopback1")
        assert lo["render_type"] == "skip"

    def test_standalone_physical_gets_physical_render_type(self, session, device, composer):
        _add_iface(session, device, name="Ethernet0/0/0")
        result = composer.compose(hostname="_ctx_device")
        iface = next(i for i in result["interfaces"] if i["name"] == "Ethernet0/0/0")
        assert iface["render_type"] == "physical"

    def test_lag_parent_gets_lag_parent_render_type(self, session, device, composer):
        lag = _add_iface(session, device, name="lag-1")
        _add_iface(session, device, name="Ethernet0/0/0", parent=lag)
        result = composer.compose(hostname="_ctx_device")
        lag_ctx = next(i for i in result["interfaces"] if i["name"] == "lag-1")
        assert lag_ctx["render_type"] == "lag_parent"

    def test_lag_member_gets_lag_member_render_type(self, session, device, composer):
        lag = _add_iface(session, device, name="lag-1")
        _add_iface(session, device, name="Ethernet0/0/0", parent=lag)
        result = composer.compose(hostname="_ctx_device")
        member = next(i for i in result["interfaces"] if i["name"] == "Ethernet0/0/0")
        assert member["render_type"] == "lag_member"

    def test_lag_member_references_parent_name(self, session, device, composer):
        lag = _add_iface(session, device, name="lag-1")
        _add_iface(session, device, name="Ethernet0/0/0", parent=lag)
        result = composer.compose(hostname="_ctx_device")
        member = next(i for i in result["interfaces"] if i["name"] == "Ethernet0/0/0")
        assert member["parent"] == "lag-1"

    def test_lag_parent_members_list_is_sorted(self, session, device, composer):
        lag = _add_iface(session, device, name="lag-1")
        _add_iface(session, device, name="Ethernet0/0/1", parent=lag)
        _add_iface(session, device, name="Ethernet0/0/0", parent=lag)
        result = composer.compose(hostname="_ctx_device")
        lag_ctx = next(i for i in result["interfaces"] if i["name"] == "lag-1")
        assert lag_ctx["members"] == ["Ethernet0/0/0", "Ethernet0/0/1"]

    def test_lag_parent_members_excludes_not_in_use(self, session, device, composer):
        lag = _add_iface(session, device, name="lag-1")
        _add_iface(session, device, name="Ethernet0/0/0", parent=lag)
        _add_iface(session, device, name="Ethernet0/0/1", parent=lag, in_use=False)
        result = composer.compose(hostname="_ctx_device")
        lag_ctx = next(i for i in result["interfaces"] if i["name"] == "lag-1")
        assert lag_ctx["members"] == ["Ethernet0/0/0"]


# ---------------------------------------------------------------------------
# in_use filtering
# ---------------------------------------------------------------------------


class TestInUseFiltering:
    def test_interfaces_not_in_use_are_excluded(self, session, device, composer):
        _add_iface(session, device, name="Ethernet0/0/0", in_use=True)
        _add_iface(session, device, name="Ethernet0/0/1", in_use=False)
        result = composer.compose(hostname="_ctx_device")
        names = [i["name"] for i in result["interfaces"]]
        assert "Ethernet0/0/0" in names
        assert "Ethernet0/0/1" not in names

    def test_device_with_no_in_use_interfaces_returns_empty_list(self, session, device, composer):
        _add_iface(session, device, name="Ethernet0/0/0", in_use=False)
        result = composer.compose(hostname="_ctx_device")
        assert result["interfaces"] == []
