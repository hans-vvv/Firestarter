from __future__ import annotations

import pytest

from app.repositories import get_device_by_hostname
from app.services.job_handling.add_device import AddDeviceJobHandler
from app.services.job_handling.job_executor import JobExecutor
from app.utils import require


@pytest.fixture
def add_device_step():
    return {
        "action": "add_device",
        "params": {
            "hostname": "core1",
            "model_name": "7250-IXR-X3",
            "role": "core",
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


def test_add_device_creates_device(
    session,
    seeded_inventory,
    prepared_executor,
    add_device_step,
):
    handler = AddDeviceJobHandler(prepared_executor)

    result = handler.handle(add_device_step)

    assert result["status"] == "created"
    assert "device_id" in result

    device = get_device_by_hostname(session, add_device_step["params"]["hostname"])
    assert device is not None
    assert device.id == result["device_id"]
    assert device.role.name == add_device_step["params"]["role"]
    assert device.site.name == add_device_step["params"]["site"]


def test_add_device_is_idempotent(
    session,
    seeded_inventory,
    prepared_executor,
    add_device_step,
):
    handler = AddDeviceJobHandler(prepared_executor)

    first = handler.handle(add_device_step)
    assert first["status"] == "created"

    second = handler.handle(add_device_step)

    assert second["status"] == "exists"
    assert second["device_id"] == first["device_id"]

    device = require(
        get_device_by_hostname(session, add_device_step["params"]["hostname"]),
        "Device missing after idempotent run",
    )
    assert device.id == first["device_id"]


@pytest.mark.parametrize(
    "missing_param",
    ["hostname", "model_name", "role", "site", "tenant"],
)
def test_add_device_missing_required_param_fails_fast(
    seeded_inventory,
    prepared_executor,
    add_device_step,
    missing_param,
):
    handler = AddDeviceJobHandler(prepared_executor)

    step = {
        "action": "add_device",
        "params": dict(add_device_step["params"]),
    }
    step["params"].pop(missing_param)

    with pytest.raises(KeyError):
        handler.handle(step)


def test_add_device_fails_if_loopback_pool_missing(
    session,
    seeded_inventory,
    prepared_executor,
    add_device_step,
):
    handler = AddDeviceJobHandler(prepared_executor)

    prepared_executor.addressing_policy_resolver.resolve_loopback0_pool = lambda _: (
        "non_existent_pool"
    )

    with pytest.raises(ValueError, match="PrefixPool"):
        handler.handle(add_device_step)

    assert (
        get_device_by_hostname(
            session,
            add_device_step["params"]["hostname"],
        )
        is None
    )
