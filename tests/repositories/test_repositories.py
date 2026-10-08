from __future__ import annotations

import pytest

from app.models import (
    Cable,
    CableStatus,
    DelegatedPrefix,
    Device,
    IntegerResourcePool,
    Interface,
    IPAddress,
    IPStatus,
    Job,
    Prefix,
    ServiceInstance,
)
from app.models.orm_models import (
    PrefixPool,
    PrefixPoolType,
    Role,
    Site,
)
from app.repositories.allocation import get_delegated_prefix_by_name
from app.repositories.cable import get_cables_between_devices
from app.repositories.device import (
    get_all_device_names,
    get_all_devices,
    get_device_by_hostname,
    get_device_model_by_hostname,
    get_device_role_by_hostname,
    get_devices_by_role,
    get_devices_by_role_name,
)
from app.repositories.interface import (
    get_interface_names_by_device_without_cable_connected,
    get_intf_by_name_by_device,
    get_loopback_interface,
    get_used_interfaces_by_device,
)
from app.repositories.ip_address import (
    get_ips_for_interface,
    get_ips_for_pool,
    get_loopback_ip_from_device,
)
from app.repositories.job import get_all_jobs, get_job_by_id, get_job_by_name
from app.repositories.prefix import get_prefixes_by_pool
from app.repositories.prefix_pool import get_prefix_pool_by_name
from app.repositories.prefix_pool_type import get_prefix_pool_type_by_name
from app.repositories.resource_pool import get_resource_pool_by_name
from app.repositories.role import get_all_role_names, get_role_by_name
from app.repositories.service_instance import get_service_instance_by_name
from app.repositories.site import get_all_site_names, get_site_by_name

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _site(session, name):
    s = Site(name=name)
    session.add(s)
    session.flush()
    return s


def _role(session, name):
    r = Role(name=name)
    session.add(r)
    session.flush()
    return r


def _device(session, hostname, site, role, model="test_model_1"):
    d = Device(
        hostname=hostname,
        lag_name="lag",
        model_name=model,
        labels={},
        site=site,
        role=role,
    )
    session.add(d)
    session.flush()
    return d


def _iface(session, device, name, role="NNI", in_use=False):
    i = Interface(name=name, intf_role=role, device=device, in_use=in_use)
    session.add(i)
    session.flush()
    return i


def _cable(session, iface_a, iface_b):
    c = Cable(
        interface_a=iface_a,
        interface_b=iface_b,
        description="test cable",
        status=CableStatus.connected,
    )
    session.add(c)
    session.flush()
    return c


def _pool_and_type(session, pool_name, prefix="10.200.0.0/24"):
    pt = PrefixPoolType(name=f"_rp_pt_{pool_name}")
    session.add(pt)
    session.flush()
    p = PrefixPool(name=pool_name, prefix=prefix, type_id=pt.id)
    session.add(p)
    session.flush()
    return p


def _ip(session, pool, address, iface=None):
    ip = IPAddress(
        address=address,
        pool_id=pool.id,
        role="loopback",
        status=IPStatus.allocated,
        interface=iface,
    )
    session.add(ip)
    session.flush()
    return ip


# ---------------------------------------------------------------------------
# Device repository
# ---------------------------------------------------------------------------


class TestDeviceRepository:
    def test_get_device_by_hostname_found(self, session):
        site = _site(session, "_rp_d_site")
        role = _role(session, "_rp_d_role")
        _device(session, "_rp_d_host", site, role)
        result = get_device_by_hostname(session, "_rp_d_host")
        assert result is not None
        assert result.hostname == "_rp_d_host"

    def test_get_device_by_hostname_missing_returns_none(self, session):
        assert get_device_by_hostname(session, "_rp_d_missing") is None

    def test_get_all_devices_returns_all(self, session):
        site = _site(session, "_rp_all_site")
        role = _role(session, "_rp_all_role")
        _device(session, "_rp_all_a", site, role)
        _device(session, "_rp_all_b", site, role)
        result = get_all_devices(session)
        hostnames = [d.hostname for d in result]
        assert "_rp_all_a" in hostnames
        assert "_rp_all_b" in hostnames

    def test_get_all_device_names_returns_sorted(self, session):
        site = _site(session, "_rp_names_site")
        role = _role(session, "_rp_names_role")
        _device(session, "_rp_names_z", site, role)
        _device(session, "_rp_names_a", site, role)
        names = get_all_device_names(session)
        rp_names = [n for n in names if n.startswith("_rp_names_")]
        assert rp_names == sorted(rp_names)

    def test_get_device_model_by_hostname_found(self, session):
        site = _site(session, "_rp_mdl_site")
        role = _role(session, "_rp_mdl_role")
        _device(session, "_rp_mdl_host", site, role, model="7250-IXR-e2-400")
        assert get_device_model_by_hostname(session, "_rp_mdl_host") == "7250-IXR-e2-400"

    def test_get_device_model_by_hostname_missing_returns_none(self, session):
        assert get_device_model_by_hostname(session, "_rp_mdl_missing") is None

    def test_get_devices_by_role(self, session):
        site = _site(session, "_rp_dr_site")
        role = _role(session, "_rp_dr_role")
        _device(session, "_rp_dr_a", site, role)
        _device(session, "_rp_dr_b", site, role)
        result = get_devices_by_role(session, role)
        assert len(result) == 2

    def test_get_devices_by_role_name(self, session):
        site = _site(session, "_rp_drn_site")
        role = _role(session, "_rp_drn_role")
        _device(session, "_rp_drn_a", site, role)
        result = get_devices_by_role_name(session, "_rp_drn_role")
        assert len(result) == 1
        assert result[0].hostname == "_rp_drn_a"

    def test_get_devices_by_role_name_no_match_returns_empty(self, session):
        assert get_devices_by_role_name(session, "_rp_drn_nonexistent") == []

    def test_get_device_role_by_hostname_found(self, session):
        site = _site(session, "_rp_drl_site")
        role = _role(session, "_rp_drl_role")
        _device(session, "_rp_drl_host", site, role)
        assert get_device_role_by_hostname(session, "_rp_drl_host") == "_rp_drl_role"

    def test_get_device_role_by_hostname_missing_returns_none(self, session):
        assert get_device_role_by_hostname(session, "_rp_drl_missing") is None


# ---------------------------------------------------------------------------
# Interface repository
# ---------------------------------------------------------------------------


class TestInterfaceRepository:
    def test_get_used_interfaces_only_returns_in_use(self, session):
        site = _site(session, "_rp_ui_site")
        role = _role(session, "_rp_ui_role")
        dev = _device(session, "_rp_ui_dev", site, role)
        _iface(session, dev, "Eth0/0/0", in_use=True)
        _iface(session, dev, "Eth0/0/1", in_use=False)
        result = get_used_interfaces_by_device(session, dev)
        assert len(result) == 1
        assert result[0].name == "Eth0/0/0"

    def test_get_loopback_interface_found(self, session):
        site = _site(session, "_rp_lo_site")
        role = _role(session, "_rp_lo_role")
        dev = _device(session, "_rp_lo_dev", site, role)
        _iface(session, dev, "Loopback0", in_use=True)
        result = get_loopback_interface(session, dev, 0)
        assert result is not None
        assert result.name == "Loopback0"

    def test_get_loopback_interface_missing_returns_none(self, session):
        site = _site(session, "_rp_lo2_site")
        role = _role(session, "_rp_lo2_role")
        dev = _device(session, "_rp_lo2_dev", site, role)
        assert get_loopback_interface(session, dev, 0) is None

    def test_get_intf_by_name_by_device_found(self, session):
        site = _site(session, "_rp_ibn_site")
        role = _role(session, "_rp_ibn_role")
        dev = _device(session, "_rp_ibn_dev", site, role)
        _iface(session, dev, "Eth0/0/0")
        result = get_intf_by_name_by_device(session, dev, "Eth0/0/0")
        assert result is not None

    def test_get_intf_by_name_by_device_missing_returns_none(self, session):
        site = _site(session, "_rp_ibn2_site")
        role = _role(session, "_rp_ibn2_role")
        dev = _device(session, "_rp_ibn2_dev", site, role)
        assert get_intf_by_name_by_device(session, dev, "nonexistent") is None

    def test_get_interface_names_without_cable_excludes_loopbacks(self, session):
        site = _site(session, "_rp_nc_site")
        role = _role(session, "_rp_nc_role")
        dev = _device(session, "_rp_nc_dev", site, role)
        _iface(session, dev, "Ethernet0/0/0")
        _iface(session, dev, "Loopback0")
        result = get_interface_names_by_device_without_cable_connected(session, dev)
        assert "Ethernet0/0/0" in result
        assert "Loopback0" not in result

    def test_get_interface_names_without_cable_excludes_cabled_interfaces(self, session):
        site = _site(session, "_rp_nc2_site")
        role = _role(session, "_rp_nc2_role")
        dev_a = _device(session, "_rp_nc2_a", site, role)
        dev_b = _device(session, "_rp_nc2_b", site, role)
        iface_a = _iface(session, dev_a, "Eth0/0/0")
        iface_b = _iface(session, dev_b, "Eth0/0/0")
        free = _iface(session, dev_a, "Eth0/0/1")
        _cable(session, iface_a, iface_b)
        result = get_interface_names_by_device_without_cable_connected(session, dev_a)
        assert "Eth0/0/0" not in result
        assert free.name in result


# ---------------------------------------------------------------------------
# Cable repository
# ---------------------------------------------------------------------------


class TestCableRepository:
    def _setup(self, session):
        site = _site(session, "_rp_cab_site")
        role = _role(session, "_rp_cab_role")
        dev_a = _device(session, "_rp_cab_a", site, role)
        dev_b = _device(session, "_rp_cab_b", site, role)
        ia = _iface(session, dev_a, "Eth0/0/0")
        ib = _iface(session, dev_b, "Eth0/0/0")
        return dev_a, dev_b, ia, ib

    def test_returns_cable_when_present(self, session):
        dev_a, dev_b, ia, ib = self._setup(session)
        _cable(session, ia, ib)
        result = get_cables_between_devices(session, dev_a, dev_b)
        assert len(result) == 1

    def test_bidirectional_lookup_finds_same_cable(self, session):
        dev_a, dev_b, ia, ib = self._setup(session)
        _cable(session, ia, ib)
        fwd = get_cables_between_devices(session, dev_a, dev_b)
        rev = get_cables_between_devices(session, dev_b, dev_a)
        assert len(fwd) == 1
        assert len(rev) == 1
        assert fwd[0].id == rev[0].id

    def test_no_cable_returns_empty_list(self, session):
        dev_a, dev_b, _, _ = self._setup(session)
        result = get_cables_between_devices(session, dev_a, dev_b)
        assert result == []

    def test_cable_length_defaults_to_one_metre(self, session):
        _, _, ia, ib = self._setup(session)
        cable = _cable(session, ia, ib)  # no cable_length passed
        session.refresh(cable)
        assert cable.cable_length == 1

    def test_cable_length_can_be_set_explicitly(self, session):
        _, _, ia, ib = self._setup(session)
        cable = Cable(
            interface_a=ia,
            interface_b=ib,
            status=CableStatus.connected,
            cable_length=42,
        )
        session.add(cable)
        session.flush()
        session.refresh(cable)
        assert cable.cable_length == 42


# ---------------------------------------------------------------------------
# IP address repository
# ---------------------------------------------------------------------------


class TestIpAddressRepository:
    def test_get_ips_for_pool(self, session):
        pool = _pool_and_type(session, "_rp_ip_pool")
        _ip(session, pool, "10.200.0.1/32")
        _ip(session, pool, "10.200.0.2/32")
        result = get_ips_for_pool(session, pool)
        assert len(result) == 2

    def test_get_ips_for_pool_empty(self, session):
        pool = _pool_and_type(session, "_rp_ip_pool2")
        assert get_ips_for_pool(session, pool) == []

    def test_get_ips_for_interface(self, session):
        site = _site(session, "_rp_ifi_site")
        role = _role(session, "_rp_ifi_role")
        dev = _device(session, "_rp_ifi_dev", site, role)
        iface = _iface(session, dev, "Eth0/0/0")
        pool = _pool_and_type(session, "_rp_ifi_pool")
        _ip(session, pool, "10.200.1.1/32", iface=iface)
        result = get_ips_for_interface(session, iface)
        assert len(result) == 1
        assert result[0].address == "10.200.1.1/32"

    def test_get_loopback_ip_from_device_found(self, session):
        site = _site(session, "_rp_lip_site")
        role = _role(session, "_rp_lip_role")
        dev = _device(session, "_rp_lip_dev", site, role)
        lo = _iface(session, dev, "Loopback0", in_use=True)
        pool = _pool_and_type(session, "_rp_lip_pool")
        _ip(session, pool, "10.200.2.1/32", iface=lo)
        result = get_loopback_ip_from_device(session, dev, 0)
        assert result is not None
        assert result.address == "10.200.2.1/32"

    def test_get_loopback_ip_no_loopback_interface_returns_none(self, session):
        site = _site(session, "_rp_lip2_site")
        role = _role(session, "_rp_lip2_role")
        dev = _device(session, "_rp_lip2_dev", site, role)
        assert get_loopback_ip_from_device(session, dev, 0) is None

    def test_get_loopback_ip_loopback_exists_but_no_ip_returns_none(self, session):
        site = _site(session, "_rp_lip3_site")
        role = _role(session, "_rp_lip3_role")
        dev = _device(session, "_rp_lip3_dev", site, role)
        _iface(session, dev, "Loopback0", in_use=True)
        assert get_loopback_ip_from_device(session, dev, 0) is None


# ---------------------------------------------------------------------------
# Prefix repository
# ---------------------------------------------------------------------------


class TestPrefixRepository:
    def test_get_prefixes_by_pool(self, session):
        pool = _pool_and_type(session, "_rp_pfx_pool", prefix="10.201.0.0/24")
        p = Prefix(prefix="10.201.0.0/31", pool_id=pool.id, status=IPStatus.allocated)
        session.add(p)
        session.flush()
        result = get_prefixes_by_pool(session, pool)
        assert len(result) == 1

    def test_get_prefixes_by_pool_empty(self, session):
        pool = _pool_and_type(session, "_rp_pfx_pool2", prefix="10.202.0.0/24")
        assert get_prefixes_by_pool(session, pool) == []


# ---------------------------------------------------------------------------
# Simple lookup repositories — found / missing pattern
# ---------------------------------------------------------------------------


class TestSimpleLookupRepositories:
    def test_get_delegated_prefix_by_name_found(self, session):
        a = DelegatedPrefix(name="_rp_alloc", in_use=False, reservations={})
        session.add(a)
        session.flush()
        assert get_delegated_prefix_by_name(session, "_rp_alloc") is not None

    def test_get_delegated_prefix_by_name_missing(self, session):
        assert get_delegated_prefix_by_name(session, "_rp_alloc_missing") is None

    def test_get_job_by_name_found(self, session):
        j = Job(name="_rp_job", actions_blob=[])
        session.add(j)
        session.flush()
        assert get_job_by_name(session, "_rp_job") is not None

    def test_get_job_by_name_missing(self, session):
        assert get_job_by_name(session, "_rp_job_missing") is None

    def test_get_job_by_id_found(self, session):
        j = Job(name="_rp_job_by_id", actions_blob=[])
        session.add(j)
        session.flush()
        assert get_job_by_id(session, j.id) is not None

    def test_get_job_by_id_missing(self, session):
        assert get_job_by_id(session, 999_999) is None

    def test_get_all_jobs_returns_all(self, session):
        session.add(Job(name="_rp_all_jobs_a", actions_blob=[]))
        session.add(Job(name="_rp_all_jobs_b", actions_blob=[]))
        session.flush()
        names = [j.name for j in get_all_jobs(session)]
        assert "_rp_all_jobs_a" in names
        assert "_rp_all_jobs_b" in names

    def test_get_all_jobs_ordered_by_id(self, session):
        session.add(Job(name="_rp_order_jobs_a", actions_blob=[]))
        session.add(Job(name="_rp_order_jobs_b", actions_blob=[]))
        session.flush()
        jobs = [j for j in get_all_jobs(session) if j.name.startswith("_rp_order_jobs_")]
        assert jobs[0].id < jobs[1].id

    def test_get_prefix_pool_by_name_found(self, session):
        _pool_and_type(session, "_rp_pp_name")
        assert get_prefix_pool_by_name(session, "_rp_pp_name") is not None

    def test_get_prefix_pool_by_name_missing(self, session):
        assert get_prefix_pool_by_name(session, "_rp_pp_missing") is None

    def test_get_prefix_pool_type_by_name_found(self, session):
        pt = PrefixPoolType(name="_rp_ppt_name")
        session.add(pt)
        session.flush()
        assert get_prefix_pool_type_by_name(session, "_rp_ppt_name") is not None

    def test_get_prefix_pool_type_by_name_missing(self, session):
        assert get_prefix_pool_type_by_name(session, "_rp_ppt_missing") is None

    def test_get_resource_pool_by_name_found(self, session):
        rp = IntegerResourcePool(name="_rp_rpool", range_start=1, range_end=100)
        session.add(rp)
        session.flush()
        assert get_resource_pool_by_name(session, "_rp_rpool") is not None

    def test_get_resource_pool_by_name_missing(self, session):
        assert get_resource_pool_by_name(session, "_rp_rpool_missing") is None

    def test_get_role_by_name_found(self, session):
        r = Role(name="_rp_role_name")
        session.add(r)
        session.flush()
        assert get_role_by_name(session, "_rp_role_name") is not None

    def test_get_role_by_name_missing(self, session):
        assert get_role_by_name(session, "_rp_role_missing") is None

    def test_get_all_role_names_sorted(self, session):
        session.add(Role(name="_rp_role_z"))
        session.add(Role(name="_rp_role_a"))
        session.flush()
        names = get_all_role_names(session)
        rp_names = [n for n in names if n.startswith("_rp_role_")]
        assert rp_names == sorted(rp_names)

    def test_get_site_by_name_found(self, session):
        s = Site(name="_rp_site_name")
        session.add(s)
        session.flush()
        assert get_site_by_name(session, "_rp_site_name") is not None

    def test_get_site_by_name_missing(self, session):
        assert get_site_by_name(session, "_rp_site_missing") is None

    def test_get_all_site_names_sorted(self, session):
        session.add(Site(name="_rp_site_z"))
        session.add(Site(name="_rp_site_a"))
        session.flush()
        names = get_all_site_names(session)
        rp_names = [n for n in names if n.startswith("_rp_site_")]
        assert rp_names == sorted(rp_names)

    def test_get_service_instance_by_name_found(self, session):
        si = ServiceInstance(svc_name="_rp_svc", tenant="lab", variant="default", computed={})
        session.add(si)
        session.flush()
        assert get_service_instance_by_name(session, "_rp_svc") is not None

    def test_get_service_instance_by_name_missing(self, session):
        assert get_service_instance_by_name(session, "_rp_svc_missing") is None
