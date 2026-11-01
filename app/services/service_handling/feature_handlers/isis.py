"""IS-IS feature handler — computes per-device IS-IS adjacency and metric context."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Interface
from app.repositories import get_all_devices
from app.utils import Tree

from .base import BaseFeatureHandler


class ISISCoreFeatureHandler(BaseFeatureHandler):
    """
    Build per-device ISIS core feature context.

    This handler selects the devices targeted for ISIS, discovers eligible NNI
    interfaces, and composes the render context used to configure the core ISIS
    instance on each matching device.

    Notes
    -----
    - Only interfaces with `Interface.in_use` and `Interface.intf_role == "NNI"`
      are considered.
    - Interfaces are filtered further by `_isis_should_enable_on_interface()`.
    - Devices matched by the ISIS selector but with no eligible interfaces are
      omitted from the returned context.
    """

    def compute(self, svc_ctx: dict[str, Any]) -> dict[str, Any]:
        """
        Compose ISIS core configuration context for selected devices.

        The method:
        - loads service-level ISIS defaults
        - selects devices targeted for ISIS
        - loads in-use NNI interfaces with parent/child relationships
        - groups interfaces by device
        - builds an ISIS instance context for devices with at least one eligible
          interface
        """

        context = Tree()

        service_data: dict[str, Any] = svc_ctx["service_data"]
        device_defaults: dict[str, Any] = service_data["features"]["isis"]
        interface_defaults: dict[str, Any] = service_data["interface_features"]["isis"]
        overload_bit_roles: list[str] = device_defaults["set_overload_bit_for_roles"]
        instance_id: str = str(device_defaults["instance_id"])

        all_devices = get_all_devices(self.session)
        device_selector: dict[str, Any] = service_data["selectors"]["devices"]["isis"]
        isis_devices = self.sb.selector_engine.select(all_devices, device_selector)

        if not isis_devices:
            raise ValueError(f"ISIS selector matched 0 devices for tenant={service_data['tenant']}")

        interfaces_by_device = self._load_candidate_interfaces_by_device()

        for device in isis_devices:
            eligible_ifaces = [
                iface
                for iface in interfaces_by_device.get(device.id, [])
                if self._isis_should_enable_on_interface(iface)
            ]
            if not eligible_ifaces:
                continue

            instance_defaults = {
                key: value
                for key, value in device_defaults.items()
                if key != "set_overload_bit_for_roles"
            }

            instance_ctx: Tree = context[device.hostname]["isis"]["instances"][instance_id]
            instance_ctx.update(instance_defaults)
            instance_ctx["set_overload_bit"] = device.role.name in overload_bit_roles
            instance_ctx["interfaces"] = [
                {"iface_name": iface.name, **interface_defaults} for iface in eligible_ifaces
            ]

        return dict(context)

    def _load_candidate_interfaces_by_device(self) -> dict[int, list[Interface]]:
        """
        Load candidate NNI interfaces and group them by device ID.

        Returns
        -------
        dict[int, list[Interface]]
            Mapping of device ID to its in-use NNI interfaces. Parent and child
            interface relationships are preloaded for downstream filtering.
        """
        stmt = (
            select(Interface)
            .where(
                Interface.in_use,
                Interface.intf_role == "NNI",
            )
            .options(
                selectinload(Interface.parent),
                selectinload(Interface.children),
            )
        )

        interfaces = list(self.session.scalars(stmt))
        interfaces_by_device: dict[int, list[Interface]] = defaultdict(list)

        for iface in interfaces:
            interfaces_by_device[iface.device_id].append(iface)

        return interfaces_by_device

    def _isis_should_enable_on_interface(self, iface: Interface) -> bool:
        """
        Determine whether ISIS should be enabled on an interface.

        ISIS is excluded from loopback interfaces and child interfaces
        (interfaces with a parent). Eligible top-level NNI interfaces are
        accepted regardless of whether they have children.

        Parameters
        ----------
        iface : Interface
            Interface candidate to evaluate.

        Returns
        -------
        bool
            True if ISIS should be enabled on the interface, otherwise False.
        """
        if iface.name.lower().startswith("loopback"):
            return False
        return iface.parent is None
