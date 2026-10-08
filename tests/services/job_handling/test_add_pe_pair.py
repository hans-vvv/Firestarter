from __future__ import annotations

import pytest

from app.models.dataclass_models import DeviceSelectorView, RoleView
from app.repositories import (
    get_cables_between_devices,
    get_device_by_hostname,
    get_prefix_pool_by_name,
)
from app.services.job_handling import AddPEPairJobHandler
from app.services.job_handling.job_executor import JobExecutor
from app.utils import require


@pytest.fixture
def add_pe_pair_step():
    return {
        "action": "add_pe_pair",
        "params": {
            "hostname_a": "pe5.tst-001",
            "hostname_b": "pe6.tst-001",
            "model_name": "7250-IXR-e2-400",
            "role": "pe",
            "site": "tst-001",
            "tenant": "lab",
            "ring": "ring1",
        },
    }


@pytest.fixture
def prepared_executor(session):
    executor = JobExecutor(session)
    executor.addressing_policy_resolver.install()
    return executor


def test_add_pe_pair_creates_devices_and_link(
    session,
    seeded_inventory,
    prepared_executor,
    add_pe_pair_step,
):
    handler = AddPEPairJobHandler(prepared_executor)

    result = handler.handle(add_pe_pair_step)

    assert result["status"] == "created"
    assert result["topology_changed"] is True
    assert "dev_a" in result
    assert "dev_b" in result
    assert "cable" in result

    host_a = add_pe_pair_step["params"]["hostname_a"]
    host_b = add_pe_pair_step["params"]["hostname_b"]

    dev_a = get_device_by_hostname(session, host_a)
    dev_b = get_device_by_hostname(session, host_b)

    assert dev_a is not None
    assert dev_b is not None
    assert dev_a.id == result["dev_a"]
    assert dev_b.id == result["dev_b"]

    cables = get_cables_between_devices(session, dev_a, dev_b)
    assert len(cables) == 1
    assert cables[0].id == result["cable"]


def test_add_pe_pair_is_idempotent_no_duplicate_cables_or_devices(
    session,
    seeded_inventory,
    prepared_executor,
    add_pe_pair_step,
):
    handler = AddPEPairJobHandler(prepared_executor)

    first = handler.handle(add_pe_pair_step)
    assert first["status"] == "created"

    host_a = add_pe_pair_step["params"]["hostname_a"]
    host_b = add_pe_pair_step["params"]["hostname_b"]

    dev_a = require(get_device_by_hostname(session, host_a), "dev_a missing after create")
    dev_b = require(get_device_by_hostname(session, host_b), "dev_b missing after create")

    cables_after_first = get_cables_between_devices(session, dev_a, dev_b)
    assert len(cables_after_first) == 1

    second = handler.handle(add_pe_pair_step)
    assert second["status"] == "exists"
    assert second["dev_a"] == dev_a.id
    assert second["dev_b"] == dev_b.id
    assert second["cable"] == cables_after_first[0].id

    cables_after_second = get_cables_between_devices(session, dev_a, dev_b)
    assert len(cables_after_second) == 1


def test_add_pe_pair_fails_if_only_one_device_exists(
    session,
    seeded_inventory,
    prepared_executor,
    add_pe_pair_step,
):
    handler = AddPEPairJobHandler(prepared_executor)

    params = add_pe_pair_step["params"]
    host_a = params["hostname_a"]
    host_b = params["hostname_b"]

    view = DeviceSelectorView(
        hostname=host_a,
        labels={"tenant": params["tenant"]},
        role=RoleView(name=params["role"]),
    )

    loop0_pool_name = prepared_executor.addressing_policy_resolver.resolve_loopback0_pool(view)
    loop0_pool = require(
        get_prefix_pool_by_name(session, loop0_pool_name),
        f"PrefixPool '{loop0_pool_name}' missing",
    )

    loop1_pool_name = prepared_executor.addressing_policy_resolver.resolve_loopback1_pool(view)
    loop1_pool = require(
        get_prefix_pool_by_name(session, loop1_pool_name),
        f"PrefixPool '{loop1_pool_name}' missing",
    )

    prepared_executor.device_builder.build_device(
        hostname=host_a,
        model_name=params["model_name"],
        role_name=params["role"],
        site_name=params["site"],
        loopback0_pool=loop0_pool,
        loopback1_pool=loop1_pool,
        tenant=params["tenant"],
        ring=params.get("ring"),
    )
    session.flush()

    assert get_device_by_hostname(session, host_a) is not None
    assert get_device_by_hostname(session, host_b) is None

    with pytest.raises(RuntimeError, match="Only one PE exists"):
        handler.handle(add_pe_pair_step)


def test_add_pe_pair_fails_if_devices_exist_but_no_cable_connects_them(
    session,
    seeded_inventory,
    prepared_executor,
    add_pe_pair_step,
):
    handler = AddPEPairJobHandler(prepared_executor)

    params = add_pe_pair_step["params"]
    host_a = params["hostname_a"]
    host_b = params["hostname_b"]

    view = DeviceSelectorView(
        hostname=host_a,
        labels={"tenant": params["tenant"]},
        role=RoleView(name=params["role"]),
    )

    loop0_pool_name = prepared_executor.addressing_policy_resolver.resolve_loopback0_pool(view)
    loop0_pool = require(
        get_prefix_pool_by_name(session, loop0_pool_name),
        f"PrefixPool '{loop0_pool_name}' missing",
    )

    loop1_pool_name = prepared_executor.addressing_policy_resolver.resolve_loopback1_pool(view)
    loop1_pool = require(
        get_prefix_pool_by_name(session, loop1_pool_name),
        f"PrefixPool '{loop1_pool_name}' missing",
    )

    prepared_executor.device_builder.build_device(
        hostname=host_a,
        model_name=params["model_name"],
        role_name=params["role"],
        site_name=params["site"],
        loopback0_pool=loop0_pool,
        loopback1_pool=loop1_pool,
        tenant=params["tenant"],
        ring=params.get("ring"),
    )
    prepared_executor.device_builder.build_device(
        hostname=host_b,
        model_name=params["model_name"],
        role_name=params["role"],
        site_name=params["site"],
        loopback0_pool=loop0_pool,
        loopback1_pool=loop1_pool,
        tenant=params["tenant"],
        ring=params.get("ring"),
    )
    session.flush()

    dev_a = require(get_device_by_hostname(session, host_a), "Device A missing")
    dev_b = require(get_device_by_hostname(session, host_b), "Device B missing")

    assert get_cables_between_devices(session, dev_a, dev_b) == []

    with pytest.raises(RuntimeError, match="both exist but no cable"):
        handler.handle(add_pe_pair_step)
