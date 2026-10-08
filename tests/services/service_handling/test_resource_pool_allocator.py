from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import (
    IntegerAllocation,
    IntegerResourcePool,
    IPAddress,
    IPStatus,
    Prefix,
    PrefixPool,
    PrefixPoolType,
)
from app.services.service_handling.resource_pool_allocator import (
    ResourcePoolAllocator,
)
from app.utils import require


@pytest.fixture
def allocator(session):
    return ResourcePoolAllocator(session)


@pytest.fixture
def loopback_pool(session, seeded_inventory):
    return require(
        session.scalar(select(PrefixPool).where(PrefixPool.name.like("%loopback0%"))),
        "Loopback pool missing from seeded inventory",
    )


@pytest.fixture
def p2p_pool(session, seeded_inventory):
    return require(
        session.scalar(select(PrefixPool).where(PrefixPool.name.like("%p2p%"))),
        "P2P pool missing from seeded inventory",
    )


@pytest.fixture
def resource_pool(session, seeded_inventory):
    return require(
        session.scalar(select(IntegerResourcePool)),
        "IntegerResourcePool missing from seeded inventory",
    )


def test_allocate_loopback_allocates_next_free_ip(
    session,
    allocator,
    loopback_pool,
):
    ip = allocator.allocate_loopback(loopback_pool)

    assert isinstance(ip, IPAddress)
    assert ip.pool_id == loopback_pool.id
    assert ip.prefix_id is None
    assert ip.address.endswith("/32")

    persisted = session.scalars(select(IPAddress).where(IPAddress.id == ip.id)).one_or_none()
    assert persisted is not None


def test_allocate_loopback_fails_when_exhausted(
    session,
    allocator,
):
    pool_type = PrefixPoolType(name="loopback")
    session.add(pool_type)
    session.flush()

    pool = PrefixPool(
        name="tiny_loopback",
        prefix="192.0.2.0/32",
        type_id=pool_type.id,
    )
    session.add(pool)
    session.flush()

    allocator.allocate_loopback(pool)

    with pytest.raises(RuntimeError, match="No free /32"):
        allocator.allocate_loopback(pool)


def test_allocate_p2p_prefix_fails_when_exhausted(
    session,
    allocator,
):
    pool_type = PrefixPoolType(name="p2p")
    session.add(pool_type)
    session.flush()

    pool = PrefixPool(
        name="tiny_p2p",
        prefix="198.51.100.0/31",
        type_id=pool_type.id,
    )
    session.add(pool)
    session.flush()

    allocator.allocate_p2p_prefix(pool)

    with pytest.raises(RuntimeError, match="No free /31"):
        allocator.allocate_p2p_prefix(pool)


def test_allocate_ips_for_p2p_creates_two_ips(
    session,
    allocator,
    p2p_pool,
):
    prefix = allocator.allocate_p2p_prefix(p2p_pool)
    ips = allocator.allocate_ips_for_p2p(prefix)

    assert len(ips) == 2
    assert all(isinstance(ip, IPAddress) for ip in ips)
    assert {ip.prefix_id for ip in ips} == {prefix.id}


def test_allocate_ips_for_p2p_fails_for_non_31(
    session,
    allocator,
):
    pool_type = PrefixPoolType(name="p2p")
    session.add(pool_type)
    session.flush()

    pool = PrefixPool(
        name="bad",
        prefix="10.0.0.0/30",
        type_id=pool_type.id,
    )
    session.add(pool)
    session.flush()

    prefix = Prefix(
        prefix="10.0.0.0/30",
        pool_id=pool.id,
        status=IPStatus.allocated,
    )
    session.add(prefix)
    session.flush()

    with pytest.raises(RuntimeError, match="not a valid /31"):
        allocator.allocate_ips_for_p2p(prefix)


def test_allocate_full_p2p_returns_prefix_and_ips(
    session,
    allocator,
    p2p_pool,
):
    prefix, ips = allocator.allocate_full_p2p(p2p_pool)

    assert isinstance(prefix, Prefix)
    assert len(ips) == 2


def test_allocate_per_service_instance_is_idempotent(
    session,
    allocator,
):
    pool = IntegerResourcePool(
        name="test_vlan_pool",
        range_start=100,
        range_end=101,
    )
    session.add(pool)
    session.flush()

    allocations = {"vlan": "test_vlan_pool"}

    first = allocator.allocate_per_service_instance(
        allocation_name="svc1",
        allocations=allocations,
    )

    second = allocator.allocate_per_service_instance(
        allocation_name="svc1",
        allocations=allocations,
    )

    assert first == second

    row = require(
        session.scalars(
            select(IntegerAllocation)
            .where(IntegerAllocation.allocation_name == "svc1")
            .where(IntegerAllocation.pool_key == "vlan")
        ).one_or_none(),
        "IntegerAllocation missing",
    )
    assert row.value == first["vlan"]


def test_allocate_delegated_prefix_per_service_instance_is_idempotent(
    session,
    allocator,
):
    pool_type = PrefixPoolType(name="delegated")
    session.add(pool_type)
    session.flush()

    pool = PrefixPool(
        name="delegated_pool",
        prefix="10.10.0.0/24",
        type_id=pool_type.id,
    )
    session.add(pool)
    session.flush()

    first = allocator.allocate_delegated_prefix_per_service_instance(
        allocation_name="svc-prefix",
        pool=pool,
        prefixlen=28,
    )

    second = allocator.allocate_delegated_prefix_per_service_instance(
        allocation_name="svc-prefix",
        pool=pool,
        prefixlen=28,
    )

    assert first == second
    assert "prefix" in first


def test_different_allocation_names_get_unique_values(
    session,
    allocator,
):
    pool = IntegerResourcePool(
        name="global_pool",
        range_start=1,
        range_end=2,
    )
    session.add(pool)
    session.flush()

    first = allocator.allocate_per_service_instance(
        allocation_name="svc1",
        allocations={"x": "global_pool"},
    )
    second = allocator.allocate_per_service_instance(
        allocation_name="svc2",
        allocations={"x": "global_pool"},
    )

    assert first["x"] != second["x"]


def test_resource_pool_exhaustion_fails(session, allocator):
    pool = IntegerResourcePool(
        name="exhausted_pool",
        range_start=1,
        range_end=1,
    )
    session.add(pool)
    session.flush()

    allocator.allocate_per_service_instance(
        allocation_name="svc1",
        allocations={"a": "exhausted_pool"},
    )

    with pytest.raises(RuntimeError, match="exhausted"):
        allocator.allocate_per_service_instance(
            allocation_name="svc2",
            allocations={"b": "exhausted_pool"},
        )
