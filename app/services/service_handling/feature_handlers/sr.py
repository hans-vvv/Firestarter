"""Segment Routing feature handler — computes per-device SR SID and prefix-SID context."""

from __future__ import annotations

from typing import Any

from app.repositories.device import get_all_devices
from app.utils import Tree

from .base import BaseFeatureHandler


class SRFeatureHandler(BaseFeatureHandler):
    """
    Computes Segment Routing (SR) feature data for selected devices.

    Uses selectors to identify devices with SR enabled and generates
    node Segment IDs (SIDs) based on Loopback0 IP addresses.
    """

    def compute(self, svc_ctx: dict[str, Any]) -> dict[str, Any]:
        """
        Compute SR feature data including node SIDs for each selected device.
        Uses idempotent resource pools based on pool allocation name

        Returns:
            dict: Nested structure keyed by device hostname containing SR features.
        """

        context = Tree()

        # Read implementation data and YAML def and instance data.
        service_data = svc_ctx["service_data"]
        variant: str = service_data["variant"]
        service_name: str = service_data["service"]

        allocation_base_name = f"{service_name}_{variant}"

        resource_pool_name: str = service_data["parameters"]["resource_pool_name"]

        all_devices = get_all_devices(self.session)
        sel_sr_devices: dict[str, Any] = service_data["selectors"]["devices"]["sr"]

        sr_devices = sorted(
            self.sb.selector_engine.select(all_devices, sel_sr_devices),
            key=lambda d: d.id,
        )

        # Start of production building no PE devices present
        # if not sr_devices:
        #     raise ValueError(f"SR selector matched 0 devices for tenant={tenant}")

        for device in sr_devices:
            allocations = {device.hostname: resource_pool_name}
            # Idempotency guarenteed by method.
            allocation = self.sb.rpa.allocate_per_service_instance(
                allocation_name=allocation_base_name + "_" + device.hostname,
                allocations=allocations,
            )

            context[device.hostname]["sr"]["node_sid"] = allocation[device.hostname]
            context[device.hostname]["sr"]["enabled"] = True

        return dict(context)
