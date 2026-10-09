"""Coordinates device building, cabling, and P2P address allocation for topology jobs."""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy.orm import Session

from app.models import (
    Cable,
    Device,
    DeviceStatus,
    IPStatus,
    PrefixPool,
)
from app.repositories import (
    get_device_by_hostname,
    get_intf_by_name_by_device,
)
from app.services.service_handling.resource_pool_allocator import ResourcePoolAllocator
from app.services.topology_building.cable_builder import CableBuilder
from app.services.topology_building.device_builder import DeviceBuilder
from app.utils import require


class TopologyBuilder:
    """
    Orchestrate topology-level link construction between existing devices.
    """

    PAIR_OVERRIDE_MODELS: ClassVar[set[str]] = {
        "7250-IXR-e2-400",
        "7250-IXR-e2-100",
    }

    def __init__(
        self,
        *,
        session: Session,
        device_builder: DeviceBuilder,
        prefix_allocator: ResourcePoolAllocator,
        cable_builder: CableBuilder,
    ) -> None:
        self.session = session
        self.device_builder = device_builder
        self.prefix_allocator = prefix_allocator
        self.cable_builder = cable_builder

    def build_p2p_link(
        self,
        dev_a_name: str,
        dev_b_name: str,
        pool: PrefixPool,
        *,
        on_lag: bool,
        iface_a_name: str | None = None,
        iface_b_name: str | None = None,
    ) -> Cable:
        """
        Build a routed point-to-point link between two devices.

        Interface selection precedence is:

        1. fixed model/pair override for supported paired platforms
        2. caller-supplied interface names
        3. automatic free-NNI selection
        """
        dev_a = require(
            get_device_by_hostname(self.session, dev_a_name),
            f"No dB record found for device '{dev_a_name}'",
        )
        dev_b = require(
            get_device_by_hostname(self.session, dev_b_name),
            f"No dB record found for device '{dev_b_name}'",
        )

        if self._should_use_pair_override(dev_a, dev_b):
            # For supported paired IXR-e2 platforms, the interconnect always
            # uses the same fixed breakout ports. This override takes precedence
            # over caller-supplied interface names.
            iface_a_name = "1/1/c2/1"
            iface_b_name = "1/1/c1/1"

            iface_a = require(
                get_intf_by_name_by_device(self.session, dev_a, iface_a_name),
                f"No dB record found for interface '{iface_a_name}' on {dev_a.hostname}",
            )
            iface_b = require(
                get_intf_by_name_by_device(self.session, dev_b, iface_b_name),
                f"No dB record found for interface '{iface_b_name}' on {dev_b.hostname}",
            )
        else:
            if iface_a_name is None:
                iface_a = self.device_builder.select_free_nni(dev_a)
            else:
                iface_a = require(
                    get_intf_by_name_by_device(self.session, dev_a, iface_a_name),
                    f"No dB record found for interface '{iface_a_name}' on {dev_a.hostname}",
                )

            if iface_b_name is None:
                iface_b = self.device_builder.select_free_nni(dev_b)
            else:
                iface_b = require(
                    get_intf_by_name_by_device(self.session, dev_b, iface_b_name),
                    f"No dB record found for interface '{iface_b_name}' on {dev_b.hostname}",
                )

        if iface_a.in_use:
            raise RuntimeError(f"Interface {iface_a.name} on {dev_a.hostname} is already in use")

        if iface_b.in_use:
            raise RuntimeError(f"Interface {iface_b.name} on {dev_b.hostname} is already in use")

        iface_a.in_use = True
        iface_b.in_use = True

        prefix = self.prefix_allocator.allocate_p2p_prefix(pool)
        ip_a, ip_b = self.prefix_allocator.allocate_ips_for_p2p(prefix)
        ip_a.status = IPStatus.allocated
        ip_b.status = IPStatus.allocated

        cable = self.cable_builder.connect(iface_a, iface_b)

        iface_a.description = f"Remote: {dev_b.hostname}:{iface_b.name}"
        iface_b.description = f"Remote: {dev_a.hostname}:{iface_a.name}"

        if on_lag:
            lag_a = self.device_builder.create_nni_lag(dev_a)
            self.device_builder.attach_to_lag(iface_a, lag_a)
            lag_a.in_use = True

            lag_b = self.device_builder.create_nni_lag(dev_b)
            self.device_builder.attach_to_lag(iface_b, lag_b)
            lag_b.in_use = True

            lag_a.description = f"Remote: {dev_b.hostname}:{lag_b.name}"
            lag_b.description = f"Remote: {dev_a.hostname}:{lag_a.name}"

            self.device_builder.assign_ip_address_to_interface(lag_a, ip_a)
            self.device_builder.assign_ip_address_to_interface(lag_b, ip_b)
        else:
            self.device_builder.assign_ip_address_to_interface(iface_a, ip_a)
            self.device_builder.assign_ip_address_to_interface(iface_b, ip_b)

        _restore_planned_if_unassigned(dev_a)
        _restore_planned_if_unassigned(dev_b)

        self.session.flush()
        return cable

    def _should_use_pair_override(self, dev_a: Device, dev_b: Device) -> bool:
        """
        Return whether the fixed paired-device interface override applies.

        The override is used only when both devices:
        - are the same supported model
        - carry a non-empty pair_label
        - share the same pair_label
        """
        pair_a = dev_a.labels.get("pair_label")
        pair_b = dev_b.labels.get("pair_label")

        return (
            dev_a.model_name in self.PAIR_OVERRIDE_MODELS
            and dev_b.model_name in self.PAIR_OVERRIDE_MODELS
            and dev_a.model_name == dev_b.model_name
            and pair_a is not None
            and pair_b is not None
            and pair_a == pair_b
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _restore_planned_if_unassigned(dev: Device) -> None:
    """Flip an ``unassigned`` device back to ``planned`` after rewiring.

    Called at the end of build_p2p_link for
    each endpoint. The state machine says: ``unassigned`` means
    "topology-disconnected, waiting for re-insertion". Once a new cable
    is wired onto the device, that condition no longer holds — the
    device returns to ``planned`` (it still needs operator activation
    before it becomes ``active``).

    No-op for ``planned`` (fresh device just getting its first cable),
    ``active`` (existing ring member gaining an extra cable, e.g. the
    neighbour endpoint during an insert), and ``retired`` (defensive —
    a retired device should never reach this code path, but if it
    somehow does we don't silently revive it).
    """
    if dev.status == DeviceStatus.unassigned:
        dev.status = DeviceStatus.planned
