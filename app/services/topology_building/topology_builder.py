"""Coordinates device building, cabling, and P2P address allocation for topology jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy.orm import Session

from app.models import (
    Cable,
    CableStatus,
    Device,
    DeviceStatus,
    IPAddress,
    IPStatus,
    Prefix,
    PrefixPool,
)
from app.repositories import (
    get_device_by_hostname,
    get_intf_by_name_by_device,
)
from app.repositories.cable import get_cables_between_devices
from app.services.service_handling.resource_pool_allocator import ResourcePoolAllocator
from app.services.topology_building.cable_builder import CableBuilder
from app.services.topology_building.device_builder import DeviceBuilder
from app.utils import require


@dataclass(frozen=True)
class P2PLinkDetachment:
    """The captured context returned by ``detach_p2p_link``.

    Carries every value a caller needs to re-create the same link via
    ``build_p2p_link_with_params`` with the same prefix, IPs, and physical
    interfaces — making the detach→build round trip name-preserving (modulo
    LAG-id reuse from the lowest-unused allocator).

    Field naming convention: ``dev_a`` is the device whose hostname was
    passed first to ``detach_p2p_link``; ``dev_b`` is second.
    """

    dev_a_name: str
    dev_b_name: str
    cable_id: int
    prefix_id: int
    prefix_str: str
    pool_id: int
    iface_a_name: str  # physical interface on dev_a
    iface_b_name: str  # physical interface on dev_b
    ip_a_id: int
    ip_a_str: str  # e.g. "10.0.0.0/31"
    ip_b_id: int
    ip_b_str: str
    on_lag: bool


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

    def build_p2p_link_with_params(
        self,
        dev_a_name: str,
        dev_b_name: str,
        *,
        iface_a_name: str,
        iface_b_name: str,
        prefix: Prefix,
        ip_a: IPAddress,
        ip_b: IPAddress,
        on_lag: bool,
        cable_status: CableStatus = CableStatus.planned,
        ip_status: IPStatus = IPStatus.allocated,
    ) -> Cable:
        """
        Build a P2P link using **caller-supplied** Prefix / IPAddress
        objects and explicit physical interface names.

        Use case: a caller that has already reserved Prefix + IPs (rows with
        status=reserved) passes those same row objects to this method, which
        promotes them (status=ip_status) and wires them into a new Cable +
        LAGs on the named physicals.

        Lifecycle of the supplied objects:
        - ``prefix``, ``ip_a``, ``ip_b`` are already in the session and
          already FK-linked (ip.prefix_id == prefix.id).
        - This method overwrites their ``status`` to the value supplied via
          parameters, and binds the IPs to the appropriate L3 interface
          (the LAG parent when on_lag=True, the physical otherwise).

        Differences from :meth:`build_p2p_link`:
        - No allocator calls. The caller supplies the addressing objects.
        - Interface selection is by name. Caller picks the physicals.
        - No ``in_use`` precondition check (explicit-params primitive,
          caller knows what they're doing — a topology change legitimately
          re-uses a physical that still carries an old retired cable).

        Returns the created Cable.
        """
        dev_a = require(
            get_device_by_hostname(self.session, dev_a_name),
            f"No dB record found for device '{dev_a_name}'",
        )
        dev_b = require(
            get_device_by_hostname(self.session, dev_b_name),
            f"No dB record found for device '{dev_b_name}'",
        )
        iface_a = require(
            get_intf_by_name_by_device(self.session, dev_a, iface_a_name),
            f"No dB record found for interface '{iface_a_name}' on {dev_a.hostname}",
        )
        iface_b = require(
            get_intf_by_name_by_device(self.session, dev_b, iface_b_name),
            f"No dB record found for interface '{iface_b_name}' on {dev_b.hostname}",
        )

        iface_a.in_use = True
        iface_b.in_use = True

        # Promote the supplied Prefix/IPs to the requested status.
        prefix.status = ip_status
        ip_a.status = ip_status
        ip_b.status = ip_status
        self.session.flush()

        cable = self.cable_builder.connect(iface_a, iface_b)
        cable.status = cable_status

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

    def detach_p2p_link(
        self,
        dev_a_name: str,
        dev_b_name: str,
    ) -> P2PLinkDetachment:
        """
        Inverse of :meth:`build_p2p_link`. Retire the single active cable
        between two devices and soft-delete its resources.

        Soft-retires (status=retired, row kept for audit):

        1. The two L3 IP addresses (interface_id cleared).
        2. The /31 prefix.
        3. The cable itself.

        Hard-deletes (infrastructure, not audit data):

        4. If the link was on a LAG: the two LAG-parent interfaces. The
           physical (LAG-child) interfaces are kept — their ``parent_id``
           is cleared, ``in_use`` flipped to False, description nulled.

        5. If non-LAG: the physical interfaces have ``in_use`` flipped
           to False and description nulled. No row deletions on them.

        Soft-deleting Cable/Prefix/IPs instead of hard-deleting them
        ensures IP address history is never lost — the allocator skips
        status != available rows, so a retired /31 is naturally excluded
        from future allocation without the "never reuse" invariant
        requiring explicit tracking. The pattern also works correctly
        inside a SQLAlchemy SAVEPOINT: the ROLLBACK restores all status
        changes, leaving the DB in its pre-detach state.

        Raises
        ------
        RuntimeError
            If there is not exactly one non-retired cable between the two
            devices.
        """
        dev_a = require(
            get_device_by_hostname(self.session, dev_a_name),
            f"No dB record found for device '{dev_a_name}'",
        )
        dev_b = require(
            get_device_by_hostname(self.session, dev_b_name),
            f"No dB record found for device '{dev_b_name}'",
        )

        active = get_cables_between_devices(self.session, dev_a, dev_b)
        if len(active) != 1:
            raise RuntimeError(
                f"Expected exactly one non-retired cable between "
                f"{dev_a_name!r} and {dev_b_name!r}, found {len(active)}."
            )
        cable = active[0]

        # Normalise endpoint orientation so iface_a is on dev_a and iface_b
        # is on dev_b — the Cable model doesn't guarantee that orientation.
        iface_a = cable.interface_a
        iface_b = cable.interface_b
        if iface_a.device_id == dev_b.id:
            iface_a, iface_b = iface_b, iface_a

        on_lag = iface_a.parent is not None or iface_b.parent is not None

        # L3 lives on the LAG parent (if present) or the physical itself.
        l3_iface_a = iface_a.parent if iface_a.parent is not None else iface_a
        l3_iface_b = iface_b.parent if iface_b.parent is not None else iface_b

        ip_a = self._sole_allocated_ip(l3_iface_a)
        ip_b = self._sole_allocated_ip(l3_iface_b)

        prefix = require(
            ip_a.prefix,
            f"IP {ip_a.address!r} has no associated prefix",
        )
        if ip_b.prefix_id != prefix.id:
            raise RuntimeError(
                f"IPs {ip_a.address!r} and {ip_b.address!r} on cable id={cable.id} "
                f"reference different prefixes — refusing to detach an "
                f"inconsistent link."
            )

        captured = P2PLinkDetachment(
            dev_a_name=dev_a.hostname,
            dev_b_name=dev_b.hostname,
            cable_id=cable.id,
            prefix_id=prefix.id,
            prefix_str=prefix.prefix,
            pool_id=prefix.pool_id,
            iface_a_name=iface_a.name,
            iface_b_name=iface_b.name,
            ip_a_id=ip_a.id,
            ip_a_str=ip_a.address,
            ip_b_id=ip_b.id,
            ip_b_str=ip_b.address,
            on_lag=on_lag,
        )

        # 1. Cable — soft-retire (keep row for audit).
        cable.status = CableStatus.retired
        self.session.flush()

        # 2. IPs — soft-retire. On a LAG the IPs sit on the LAG parent;
        #    clear interface_id before deleting the LAG parent so no FK
        #    constraint fires (Interface.ip_addresses cascade="all" without
        #    delete-orphan, so clearing the FK does not auto-delete the IP).
        if on_lag:
            for ip in list(l3_iface_a.ip_addresses):
                ip.status = IPStatus.retired
                ip.interface_id = None
            for ip in list(l3_iface_b.ip_addresses):
                ip.status = IPStatus.retired
                ip.interface_id = None
        else:
            ip_a.status = IPStatus.retired
            ip_a.interface_id = None
            ip_b.status = IPStatus.retired
            ip_b.interface_id = None
        self.session.flush()

        # 3. Prefix — soft-retire.
        prefix.status = IPStatus.retired
        self.session.flush()

        # 4. LAG parents (if used) — hard-delete (infrastructure, not audit
        #    data). Disassociate physicals first so Interface.children has no
        #    delete-orphan cascade and doesn't try to delete the physicals.
        #    Expire the collection so SQLAlchemy doesn't see stale IPs.
        if on_lag:
            iface_a.parent = None
            iface_b.parent = None
            self.session.flush()
            self.session.expire(l3_iface_a, ["ip_addresses"])
            self.session.expire(l3_iface_b, ["ip_addresses"])
            self.session.delete(l3_iface_a)
            self.session.delete(l3_iface_b)

        # 5. Reset physical interfaces so the next build_p2p_link sees them
        #    as free NNIs.
        iface_a.in_use = False
        iface_a.description = None
        iface_b.in_use = False
        iface_b.description = None

        self.session.flush()
        return captured

    def _sole_allocated_ip(self, iface):
        """Return the one allocated IP on a (LAG-parent or physical) interface.

        Raises if zero or multiple — a healthy P2P link has exactly one.
        Reserved or retired IPs are ignored (they are not on the network).
        """
        allocated = [ip for ip in iface.ip_addresses if ip.status == IPStatus.allocated]
        if len(allocated) != 1:
            raise RuntimeError(
                f"Interface {iface.name!r} on device {iface.device.hostname!r} "
                f"has {len(allocated)} allocated IPs; expected exactly one."
            )
        return allocated[0]

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

    Called at the end of build_p2p_link / build_p2p_link_with_params for
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
