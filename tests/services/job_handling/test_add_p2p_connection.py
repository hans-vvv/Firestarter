from __future__ import annotations

import pytest

from app.repositories import (
    get_cables_between_devices,
    get_device_by_hostname,
)
from app.services.job_handling.add_p2p_connection import AddP2PConnectionJobHandler
from app.services.job_handling.job_executor import JobExecutor
from app.utils import require


@pytest.fixture
def add_p2p_step():
    return {
        "action": "add_cable",
        "params": {
            "device_a_name": "core1.tst-001",
            "device_b_name": "rr2.tst-001",
        },
    }


@pytest.fixture
def prepared_executor(session):
    executor = JobExecutor(session)
    executor.addressing_policy_resolver.install()
    return executor


def test_add_p2p_connection_creates_cable(
    session,
    seeded_inventory,
    prepared_executor,
    add_p2p_step,
):
    handler = AddP2PConnectionJobHandler(prepared_executor)

    result = handler.handle(add_p2p_step)

    assert result["status"] == "created"
    assert result["topology_changed"] is True
    assert "cable_id" in result

    dev_a = require(
        get_device_by_hostname(session, add_p2p_step["params"]["device_a_name"]),
        "Device A missing",
    )
    dev_b = require(
        get_device_by_hostname(session, add_p2p_step["params"]["device_b_name"]),
        "Device B missing",
    )

    cables = get_cables_between_devices(session, dev_a, dev_b)
    assert len(cables) == 1
    assert cables[0].id == result["cable_id"]


def test_add_p2p_connection_is_idempotent(
    session,
    seeded_inventory,
    prepared_executor,
    add_p2p_step,
):
    handler = AddP2PConnectionJobHandler(prepared_executor)

    first = handler.handle(add_p2p_step)
    assert first["status"] == "created"

    second = handler.handle(add_p2p_step)

    assert second["status"] == "exists"
    assert second["cable"] == first["cable_id"]

    dev_a = require(
        get_device_by_hostname(session, add_p2p_step["params"]["device_a_name"]),
        "Device A missing",
    )
    dev_b = require(
        get_device_by_hostname(session, add_p2p_step["params"]["device_b_name"]),
        "Device B missing",
    )

    cables = get_cables_between_devices(session, dev_a, dev_b)
    assert len(cables) == 1


def test_add_p2p_connection_idempotent_when_only_retired_cable_exists(
    session,
    seeded_inventory,
    prepared_executor,
    add_p2p_step,
):
    """A retired cable for the requested pair means a ring migration
    took that link out of service intentionally. The Excel-driven
    pipeline must not try to rebuild it (the interface has likely been
    reused by a different active cable). Treat the retired row as
    'exists' so the action is a no-op."""
    from app.models import Cable, CableStatus

    handler = AddP2PConnectionJobHandler(prepared_executor)

    # Build the cable, then mark it retired (simulating a ring-migration
    # commit that detached this link).
    first = handler.handle(add_p2p_step)
    assert first["status"] == "created"
    cable = session.get(Cable, first["cable_id"])
    cable.status = CableStatus.retired
    session.flush()

    # Replay the same action — must be idempotent on the retired cable.
    second = handler.handle(add_p2p_step)

    assert second["status"] == "exists"
    assert second["cable"] == first["cable_id"]
    # No new cable created.
    dev_a = require(
        get_device_by_hostname(session, add_p2p_step["params"]["device_a_name"]),
        "Device A missing",
    )
    dev_b = require(
        get_device_by_hostname(session, add_p2p_step["params"]["device_b_name"]),
        "Device B missing",
    )
    all_cables = get_cables_between_devices(
        session,
        dev_a,
        dev_b,
        exclude_retired=False,
    )
    assert len(all_cables) == 1


def test_add_p2p_fails_if_neither_device_exists(
    session,
    prepared_executor,
):
    handler = AddP2PConnectionJobHandler(prepared_executor)

    step = {
        "action": "add_cable",
        "params": {
            "device_a_name": "NONEXISTENT_A",
            "device_b_name": "NONEXISTENT_B",
        },
    }

    with pytest.raises(RuntimeError, match="Inconsistent topology"):
        handler.handle(step)


def test_add_p2p_fails_if_only_one_device_exists(
    session,
    seeded_inventory,
    prepared_executor,
    add_p2p_step,
):
    handler = AddP2PConnectionJobHandler(prepared_executor)

    dev_b = require(
        get_device_by_hostname(session, add_p2p_step["params"]["device_b_name"]),
        "Device B must exist in seeded topology",
    )
    session.delete(dev_b)
    session.flush()

    with pytest.raises(RuntimeError, match="Inconsistent topology"):
        handler.handle(add_p2p_step)


def test_add_p2p_fails_if_prefix_pool_missing(
    session,
    seeded_inventory,
    prepared_executor,
    add_p2p_step,
):
    handler = AddP2PConnectionJobHandler(prepared_executor)

    prepared_executor.addressing_policy_resolver.resolve_p2p_pool = lambda *_: "missing_p2p_pool"

    with pytest.raises(ValueError, match="PrefixPool"):
        handler.handle(add_p2p_step)

    dev_a = get_device_by_hostname(session, add_p2p_step["params"]["device_a_name"])
    dev_b = get_device_by_hostname(session, add_p2p_step["params"]["device_b_name"])

    assert dev_a is not None
    assert dev_b is not None
    assert get_cables_between_devices(session, dev_a, dev_b) == []
