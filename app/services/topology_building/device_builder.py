"""Builds and mutates Device and Interface rows: loopbacks, LAGs, UNI/NNI port selection."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Device, DeviceStatus, Interface, IPAddress, IPStatus, PrefixPool
from app.repositories import get_loopback_interface
from app.services.service_handling.resource_pool_allocator import ResourcePoolAllocator
from app.services.topology_building.device_factory import DeviceFactory


class DeviceBuilder:
    """
    Build and mutate device-local topology objects.

    This helper encapsulates common device provisioning operations such as:

    - creating a device from inventory metadata
    - ensuring a loopback interface exists
    - allocating and assigning a loopback IP
    - selecting free access or network-facing interfaces
    - creating and populating LAG interfaces

    The class operates directly on SQLAlchemy model instances and flushes
    changes incrementally so newly created objects are immediately usable by
    later steps in the same transaction.
    """

    def __init__(
        self,
        *,
        session: Session,
        device_factory: DeviceFactory,
        prefix_allocator: ResourcePoolAllocator,
    ) -> None:
        """
        Initialize the builder with persistence and allocation dependencies.

        Parameters
        ----------
        session : sqlalchemy.orm.Session
            Active database session used to create and update model instances.
        device_factory : DeviceFactory
            Factory responsible for constructing the base `Device` object.
        prefix_allocator : ResourcePoolAllocator
            Allocator used to reserve loopback addresses from a prefix pool.
        """
        self.session = session
        self.device_factory = device_factory
        self.prefix_allocator = prefix_allocator

    def build_device(
        self,
        *,
        hostname: str,
        model_name: str,
        role_name: str,
        site_name: str,
        loopback0_pool: PrefixPool,
        loopback1_pool: PrefixPool,
        tenant: str,
        ring: str | None = None,
    ) -> Device:
        """
        Build a planned device and provision its system and mgmt loopback.

        The method performs the standard initial device bring-up flow:

        1. create the base device record
        2. ensure the requested loopback interface exists
        3. allocate a loopback IP from the supplied pool
        4. assign the IP to the loopback interface
        5. apply initial operational labels and status

        Parameters
        ----------
        hostname : str
            Device hostname.
        model_name : str
            Device model identifier.
        role_name : str
            Logical device role, such as core, rr, or pe.
        site_name : str
            Site to which the device belongs.
        loopback0_pool : PrefixPool
            Prefix pool used for loopback address allocation.
        loopback1_pool : PrefixPool
            Prefix pool used for management loopback address allocation.
        tenant : str
            Tenant label stored on the device.
        ring : str | None, optional
            Optional ring identifier stored in device labels.

        Returns
        -------
        Device
            The newly built and initialized device.

        Notes
        -----
        The device, loopback interface, and allocated IP are all marked
        as in use / planned before returning.
        """
        # Create the base device object with its static inventory attributes.
        device = self.device_factory.build_basic_device(
            hostname=hostname,
            model_name=model_name,
            role_name=role_name,
            site_name=site_name,
        )

        # Build loopback0 and loopback1 interfaces
        lo0 = self.build_loopback_interface(device=device, loopback_index=0)
        ip0 = self.prefix_allocator.allocate_loopback(pool=loopback0_pool, role="system")
        self.assign_ip_address_to_interface(lo0, ip0)

        lo1 = self.build_loopback_interface(device=device, loopback_index=1)
        ip1 = self.prefix_allocator.allocate_loopback(pool=loopback1_pool, role="management")
        self.assign_ip_address_to_interface(lo1, ip1)

        # Apply initial labels used by later topology / service workflows.
        if ring:
            device.labels["ring"] = ring

        device.status = DeviceStatus.planned
        device.labels["tenant"] = tenant

        # Mark the loopback resources as active for future selection.
        lo0.in_use = True
        ip0.status = IPStatus.allocated
        lo1.in_use = True
        ip1.status = IPStatus.allocated

        self.session.flush()
        return device

    def build_loopback_interface(self, *, device: Device, loopback_index: int) -> Interface:
        """
        Create or retrieve a loopback interface on a device.

        Parameters
        ----------
        device : Device
            Device that should own the loopback interface.
        loopback_index : int
            Loopback interface index, e.g. `0` for `Loopback0`.

        Returns
        -------
        Interface
            Existing or newly created loopback interface.
        """
        # Reuse an existing loopback if it is already present on the device.
        lo = get_loopback_interface(self.session, device, loopback_index)
        if lo:
            return lo

        lo = Interface(
            name=f"Loopback{loopback_index}",
            device=device,
        )

        self.session.add(lo)
        self.session.flush()
        return lo

    def assign_ip_address_to_interface(self, iface: Interface, ip: IPAddress) -> Interface:
        """
        Bind an IP address record to an interface.

        Parameters
        ----------
        iface : Interface
            Target interface.
        ip : IPAddress
            IP address object to associate with the interface.

        Returns
        -------
        Interface
            The same interface, for convenience in fluent workflows.
        """
        ip.interface = iface
        self.session.flush()
        return iface

    @staticmethod
    def select_free_nni(device: Device) -> Interface:
        """
        Return the first unused NNI interface on the device.

        Interfaces are evaluated in ascending database ID order to provide
        deterministic selection.

        Raises
        ------
        RuntimeError
            If no unused NNI interface is available.
        """
        for iface in sorted(device.interfaces, key=lambda i: i.id):
            if iface.intf_role == "NNI" and not iface.in_use:
                return iface
        raise RuntimeError(f"No free NNI on {device.hostname}")

    @staticmethod
    def select_free_uni(device: Device) -> Interface:
        """
        Return the first unused UNI interface on the device.

        Interfaces are evaluated in ascending database ID order to provide
        deterministic selection.

        Raises
        ------
        RuntimeError
            If no unused UNI interface is available.
        """
        for iface in sorted(device.interfaces, key=lambda i: i.id):
            if iface.intf_role == "UNI" and not iface.in_use:
                return iface
        raise RuntimeError(f"No free UNI on {device.hostname}")

    def create_nni_lag(self, device: Device) -> Interface:
        """
        Create a new NNI LAG using the next available NNI bundle index.

        NNI bundle numbering starts at 1 and increments from the highest
        existing NNI LAG index on the device.
        """
        idx = self._next_nni_lag_index(device)
        lag = Interface(device=device, name=f"{device.lag_name}-{idx}", intf_role="NNI")
        self.session.add(lag)
        self.session.flush()
        return lag

    def create_uni_lag(self, device: Device) -> Interface:
        """
        Create a new UNI LAG using the next available UNI bundle index.

        UNI bundle numbering starts at 33 and increments from the highest
        existing UNI LAG index on the device.
        """
        idx = self._next_uni_lag_index(device)
        lag = Interface(device=device, name=f"{device.lag_name}-{idx}", intf_role="UNI")
        self.session.add(lag)
        self.session.flush()
        return lag

    def create_lag_with_id(
        self,
        *,
        device: Device,
        lag_id: int,
        role: str,  # UNI or NNI
    ) -> Interface:
        """
        Create a LAG with an explicitly supplied numeric ID.

        This method does not check whether the ID is already in use; callers are
        responsible for selecting a valid free index beforehand.

        Parameters
        ----------
        device : Device
            Device that will own the LAG.
        lag_id : int
            Numeric LAG identifier to use in the interface name.
        role : str
            LAG role. Must be either ``"UNI"`` or ``"NNI"``.

        Returns
        -------
        Interface
            Newly created LAG interface.

        Raises
        ------
        ValueError
            If `role` is not one of the supported values.
        """
        if role not in ("UNI", "NNI"):
            raise ValueError("Invalid LAG role")

        name = f"{device.lag_name}-{lag_id}"
        lag = Interface(device=device, name=name, intf_role=role)
        self.session.add(lag)
        self.session.flush()
        return lag

    def attach_to_lag(self, physical: Interface, lag: Interface) -> None:
        """
        Attach a physical interface as a member of a LAG.

        Parameters
        ----------
        physical : Interface
            Physical member interface.
        lag : Interface
            Parent LAG interface.
        """
        # Parent/child relationship models LAG membership in the topology graph.
        physical.parent = lag
        self.session.flush()

    @staticmethod
    def _next_nni_lag_index(device: Device) -> int:
        """
        Compute the next available NNI LAG index for a device.

        Returns the **lowest unused** positive index starting from 1 —
        i.e. hole-filling, not monotonically increasing past deleted
        LAGs. This makes the topology builder's detach-then-rebuild
        round trip name-preserving: a LAG that's just been detached
        (deleted) gets the same index back on the next build, so the
        rendered config diff stays minimal across migrations.

        Returns
        -------
        int
            Next NNI LAG index. Defaults to 1 if no NNI LAG exists.
        """
        return _lowest_unused_lag_index(device, role="NNI", start=1)

    @staticmethod
    def _next_uni_lag_index(device: Device) -> int:
        """
        Compute the next available UNI LAG index for a device.

        Returns the **lowest unused** index starting from 33. UNI LAG
        numbering starts at 33 to keep UNI bundle IDs distinct from the
        lower NNI range. Hole-fills for the same reason as NNI: a UNI
        LAG that's just been detached gets the same index back on the
        next build.

        Returns
        -------
        int
            Next UNI LAG index. Defaults to 33 if no UNI LAG exists.
        """
        return _lowest_unused_lag_index(device, role="UNI", start=33)


def _lowest_unused_lag_index(device: Device, *, role: str, start: int) -> int:
    """Return the lowest positive integer >= ``start`` that's not already
    used as a LAG index on ``device`` for interfaces with the given
    ``intf_role``. Module-level helper shared by NNI and UNI lookups."""
    used: set[int] = set()
    for iface in device.interfaces:
        name = iface.name or ""
        if not name.startswith(device.lag_name) or iface.intf_role != role:
            continue
        suffix = name.replace(device.lag_name, "", 1).rsplit("-", 1)[-1]
        if suffix.isdigit():
            used.add(int(suffix))

    idx = start
    while idx in used:
        idx += 1
    return idx
