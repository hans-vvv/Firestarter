"""Device repository — query helpers for the Device ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device, DeviceStatus, Interface, IPAddress, Role


def get_device_by_hostname(session: Session, hostname: str) -> Device | None:
    """Return the Device with the given hostname, or None if not found."""
    return session.scalar(select(Device).where(Device.hostname == hostname))


def get_all_devices(session: Session) -> list[Device]:
    """Return only pipeline-visible Device rows ordered by id.

    Excluded: ``retired`` (permanently decommissioned). ``unassigned``
    devices are NOT excluded — they have valid loopback/SID resources
    and service computation is idempotent, so re-running compute_services
    on them is harmless and keeps the device fully prepared for the
    moment it gets re-inserted into a ring.
    Use a direct query with no status filter only for audit/dashboard purposes.
    """
    return list(
        session.scalars(
            select(Device).where(Device.status != DeviceStatus.retired).order_by(Device.id)
        )
    )


def get_all_device_names(session: Session) -> list[str]:
    """Return all device hostnames sorted alphabetically."""
    return list(session.scalars(select(Device.hostname).order_by(Device.hostname)))


def get_device_model_by_hostname(session: Session, hostname: str) -> str | None:
    """Return the model_name of the device with the given hostname, or None."""
    return session.scalar(select(Device.model_name).where(Device.hostname == hostname))


def get_devices_by_role(session: Session, role: Role) -> list[Device]:
    """Return all devices assigned to the given Role, ordered by hostname."""
    return list(
        session.scalars(select(Device).where(Device.role_id == role.id).order_by(Device.hostname))
    )


def get_devices_by_role_name(session: Session, role_name: str) -> list[Device]:
    """Return all devices whose role name matches, ordered by hostname."""
    return list(
        session.scalars(
            select(Device).join(Role).where(Role.name == role_name).order_by(Device.hostname)
        )
    )


def get_device_role_by_hostname(session: Session, hostname: str) -> str | None:
    """Return the role name for the given hostname, or None if not found."""
    return session.scalar(
        select(Role.name).join(Device, Device.role_id == Role.id).where(Device.hostname == hostname)
    )


def get_mgmt_address(session: Session, *, hostname: str) -> str | None:
    """Return the management IP of *hostname*, or None if it has none.

    Unlike :func:`get_devices_with_mgmt_address` this applies no status filter:
    a snippet push targets whichever device carries the rendered service, and
    that device may be ``active`` (lab), ``reachable``, or otherwise — its status
    is not what decides whether it has a management address to reach.

    The ``/32`` suffix is stripped, so the value is ready to connect to.
    """
    address = session.scalar(
        select(IPAddress.address)
        .join(Interface, IPAddress.interface_id == Interface.id)
        .join(Device, Interface.device_id == Device.id)
        .where(Device.hostname == hostname, IPAddress.role == "management")
    )
    return address.split("/")[0] if address else None


def get_devices_with_mgmt_address(session: Session) -> list[tuple[str, str]]:
    """Return ``(hostname, address)`` for every device that has a management IP.

    Unlike ``inventory.resolve_active_hosts``, a device without a management
    address is skipped rather than raising. The two functions answer different
    questions: the inventory must contain every active device or a run silently
    misses a router, whereas this one lists devices an operator *may* act on —
    and a device with no management address simply is not one of them. CEs,
    managed through their PE, are the standing example.

    No status filter: status records where a device is in its lifecycle, which
    is not the same question as whether an operator may act on it: a device whose
    status was never advanced, or was advanced ahead of the work, still has an
    address, and a picker that hides it is simply wrong.

    The ``/32`` suffix is stripped, so the value is ready to connect to.
    """
    stmt = (
        select(Device.hostname, IPAddress.address)
        .join(Interface, Interface.device_id == Device.id)
        .join(IPAddress, IPAddress.interface_id == Interface.id)
        .where(IPAddress.role == "management")
        .order_by(Device.hostname)
    )
    return [(hostname, address.split("/")[0]) for hostname, address in session.execute(stmt).all()]
