from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import IntegerAllocation, IntegerResourcePool
from app.repositories.device import get_all_devices
from app.services.service_handling.feature_handlers.sr import SRFeatureHandler
from app.utils import require


@pytest.fixture
def sr_service_ctx():
    return {
        "service_data": {
            "tenant": "lab",
            "variant": "core",
            "service": "sr",
            "parameters": {
                "resource_pool_name": "sr_core_pool_lab",
            },
            "selectors": {
                "devices": {
                    "sr": {
                        "match": {
                            "role": {"include": ["core", "rr"]},
                        }
                    }
                }
            },
        }
    }


@pytest.fixture
def sr_resource_pool(session):
    pool = session.scalars(
        select(IntegerResourcePool).where(IntegerResourcePool.name == "sr_core_pool_lab")
    ).one_or_none()
    if pool is None:
        pool = IntegerResourcePool(
            name="sr_core_pool_lab",
            range_start=1000,
            range_end=1999,
        )
        session.add(pool)
        session.flush()
    return pool


def test_sr_returns_device_scoped_output(
    session,
    seeded_inventory,
    sr_resource_pool,
    sr_service_ctx,
    dummy_service_builder,
):
    handler = SRFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(sr_service_ctx)

    all_devices = get_all_devices(session)
    sel = sr_service_ctx["service_data"]["selectors"]["devices"]["sr"]
    expected_hostnames = {
        d.hostname for d in dummy_service_builder.selector_engine.select(all_devices, sel)
    }

    assert set(result.keys()) == expected_hostnames

    for hostname in expected_hostnames:
        assert "sr" in result[hostname]
        assert result[hostname]["sr"]["enabled"] is True
        assert "node_sid" in result[hostname]["sr"]
        assert isinstance(result[hostname]["sr"]["node_sid"], int)


def test_sr_node_sid_is_deterministic(
    session,
    seeded_inventory,
    sr_resource_pool,
    sr_service_ctx,
    dummy_service_builder,
):
    handler = SRFeatureHandler(session=session, service_builder=dummy_service_builder)

    first = handler.compute(sr_service_ctx)
    second = handler.compute(sr_service_ctx)

    assert first == second


def test_sr_allocates_per_device_and_persists_allocations(
    session,
    seeded_inventory,
    sr_resource_pool,
    sr_service_ctx,
    dummy_service_builder,
):
    handler = SRFeatureHandler(session=session, service_builder=dummy_service_builder)

    result = handler.compute(sr_service_ctx)

    all_devices = get_all_devices(session)
    sel = sr_service_ctx["service_data"]["selectors"]["devices"]["sr"]
    affected_devices = dummy_service_builder.selector_engine.select(all_devices, sel)

    service_name = sr_service_ctx["service_data"]["service"]
    variant = sr_service_ctx["service_data"]["variant"]

    for dev in affected_devices:
        hostname = dev.hostname
        allocation_name = f"{service_name}_{variant}_{hostname}"

        row = require(
            session.scalars(
                select(IntegerAllocation)
                .where(IntegerAllocation.allocation_name == allocation_name)
                .where(IntegerAllocation.pool_key == hostname)
            ).one_or_none(),
            f"IntegerAllocation missing for {hostname}",
        )

        assert result[hostname]["sr"]["node_sid"] == row.value
        assert result[hostname]["sr"]["enabled"] is True
