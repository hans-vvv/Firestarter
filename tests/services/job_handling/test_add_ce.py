from __future__ import annotations

import pytest

from app.repositories import get_device_by_hostname
from app.services.job_handling.add_ce import AddCeJobHandler
from app.services.job_handling.job_executor import JobExecutor


@pytest.fixture
def add_ce_step():
    return {
        "action": "attach_ce",
        "params": {
            "ce_name": "test-switch3.tst-001",
            "ce_role_name": "test-switch",
            "site_name": "tst-001",
            "ce_model_name": "generic-switch",
            "connected_pe": None,
        },
    }


@pytest.fixture
def prepared_executor(session):
    return JobExecutor(session)


def test_add_ce_attaches_ce(
    session,
    seeded_inventory,
    prepared_executor,
    add_ce_step,
):
    handler = AddCeJobHandler(prepared_executor)

    result = handler.handle(add_ce_step)

    assert result["status"] == "attached"
    assert result["topology_changed"] is True
    assert "ce_id" in result

    ce = get_device_by_hostname(session, add_ce_step["params"]["ce_name"])
    assert ce is not None
    assert ce.id == result["ce_id"]


def test_add_ce_is_idempotent(
    session,
    seeded_inventory,
    prepared_executor,
    add_ce_step,
):
    handler = AddCeJobHandler(prepared_executor)

    first = handler.handle(add_ce_step)
    assert first["status"] == "attached"

    second = handler.handle(add_ce_step)

    assert second["status"] == "exists"
    assert second["ce_id"] == first["ce_id"]

    ce = get_device_by_hostname(session, add_ce_step["params"]["ce_name"])
    assert ce is not None
    assert ce.id == first["ce_id"]


@pytest.mark.parametrize(
    ("missing_param", "expected_msg"),
    [
        ("site_name", "site is required"),
        ("ce_name", "ce_name is required"),
        ("ce_role_name", "ce_role_name is required"),
        ("ce_model_name", "ce_model_name is required"),
    ],
)
def test_add_ce_missing_required_param_fails_fast(
    seeded_inventory,
    prepared_executor,
    add_ce_step,
    missing_param,
    expected_msg,
):
    handler = AddCeJobHandler(prepared_executor)

    step = {
        "action": "attach_ce",
        "params": dict(add_ce_step["params"]),
    }
    step["params"].pop(missing_param)

    with pytest.raises(ValueError, match=expected_msg):
        handler.handle(step)
