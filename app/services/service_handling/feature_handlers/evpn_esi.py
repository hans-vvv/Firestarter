"""EVPN ESI feature handler — computes per-device EVPN ESI (multi-homing) context."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Interface
from app.repositories import get_all_devices
from app.utils import Tree

from .base import BaseFeatureHandler


class EVPN_ESIFeatureHandler(BaseFeatureHandler):
    """
    Computes EVPN ESI information and store per Interface.
    """

    def compute(self, svc_ctx: dict[str, Any]) -> dict[str, Any]:
        """
        Compute EVPN ESI values and store them per Interface.

        ESIs are only attached to interfaces whose evpn_esi field
        is set to "needs esi". This marker is set by the CE attachment
        builder.

        ESIs are allocated from a global pool and shared across
        redundant interfaces belonging to the same pair of PEs.
        """
        service_data: dict[str, Any] = svc_ctx["service_data"]
        pool_name: str = service_data["parameters"]["allocations"]["evpn_esi"]["pool"]
        variant: str = service_data["variant"]
        tenant: str = service_data["tenant"]

        # Construct base name for resource allocation
        service_name: str = service_data["service"]
        allocation_base = service_name + "_" + variant

        # Get affected nodes for computation
        all_devices = get_all_devices(self.session)
        sel_evpn_esi_devices: dict[str, Any] = service_data["selectors"]["devices"]["evpn_esi"]
        evpn_esi_devices = self.sb.selector_engine.select(all_devices, sel_evpn_esi_devices)
        evpn_esi_device_ids = [d.id for d in evpn_esi_devices]

        if not evpn_esi_devices:
            raise ValueError(
                f"EVPN ESI selector matched 0 devices for tenant={tenant} variant={variant}"
            )

        context = Tree()

        pending_stmt = (
            select(Interface)
            .where(
                Interface.evpn_esi == "needs esi",
                Interface.device_id.in_(evpn_esi_device_ids),
            )
            .options(selectinload(Interface.device))
        )

        pending_interfaces = self.session.scalars(pending_stmt).all()

        for iface in pending_interfaces:
            device = iface.device
            pair_label = device.labels.get("pair_label") or device.hostname
            allocations = {"evpn_esi": pool_name}
            pool_allocations = self.sb.rpa.allocate_per_service_instance(
                allocation_name=allocation_base + "_" + pair_label + "_" + iface.name,
                allocations=allocations,
            )
            iface.evpn_esi = str(pool_allocations["evpn_esi"])

        # Flush so the second SELECT below can see the assignments just made.
        self.session.flush()

        all_esi_stmt = (
            select(Interface)
            .where(
                Interface.evpn_esi.is_not(None),
                Interface.evpn_esi != "needs esi",
                Interface.device_id.in_(evpn_esi_device_ids),
            )
            .options(selectinload(Interface.device))
        )

        all_esi_interfaces = self.session.scalars(all_esi_stmt).all()

        for iface in all_esi_interfaces:
            hostname = iface.device.hostname
            base: Tree = context[hostname]["evpn_esi"]["variant"][variant]
            base.setdefault("interfaces", []).append(
                {
                    "if_name": iface.name,
                    "esi": iface.evpn_esi,
                }
            )

        return dict(context)
