"""Interface repository — query helpers for the Interface ORM model."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Cable, CableStatus, Device, Interface


def get_used_interfaces_by_device(session: Session, device: Device) -> list[Interface]:
    """Return all in-use interfaces for a device, ordered by id."""
    stmt = (
        select(Interface)
        .where(
            Interface.device_id == device.id,
            Interface.in_use,
        )
        .order_by(Interface.id)
    )
    return list(session.scalars(stmt))


def get_loopback_interface(session: Session, device: Device, loop_index: int) -> Interface | None:
    """Return the Loopback{loop_index} interface for a device, or None."""
    name = f"Loopback{loop_index}"
    return session.scalar(
        select(Interface).where(
            Interface.device_id == device.id,
            Interface.name == name,
        )
    )


def get_intf_by_name_by_device(
    session: Session,
    device: Device,
    intf_name: str,
) -> Interface | None:
    """Return the interface with the given name on a device, or None."""
    return session.scalar(
        select(Interface).where(
            Interface.device_id == device.id,
            Interface.name == intf_name,
        )
    )


def get_interface_names_by_device_without_cable_connected(
    session: Session, device: Device
) -> list[str]:
    """Return interface names on a device that have no cable on either side (loopbacks excluded)."""
    interfaces = list(session.scalars(select(Interface).where(Interface.device_id == device.id)))
    result = []
    for iface in interfaces:
        # Skip loopbacks
        if iface.name.lower().startswith("lo"):
            continue
        if "loopback" in iface.name.lower():
            continue

        # Must have no cable on either side
        if not iface.cables_as_a and not iface.cables_as_b:
            result.append(iface.name)
    return result


def get_remote_device_role_for_interface(
    session: Session,
    *,
    iface: Interface,
) -> str | None:
    """
    Return the role name of the device on the far end of an interface's link,
    traced through its cable.

    The far end's role is the one fact a template cannot reach from its own
    device context — it is what lets an underlay decide behaviour by role pair
    (e.g. the coherent ``rx-los-thresh`` line that only applies on
    core↔pe and pe↔pe links).

    Handles LAGs:
      - a LAG parent has no cable of its own, so trace its member interfaces;
      - a physical/member interface is traced directly.

    Only ``connected``/``active`` cables count — a ``planned`` or ``retired``
    row is not on the live network. Returns ``None`` when no such adjacency
    exists or the remote side is incomplete.
    """
    candidate_ifaces: list[Interface] = list(iface.children) if iface.children else [iface]

    for cand in candidate_ifaces:
        cable = session.scalar(
            select(Cable).where(
                Cable.status.in_([CableStatus.connected, CableStatus.active]),
                or_(Cable.interface_a_id == cand.id, Cable.interface_b_id == cand.id),
            )
        )
        if cable is None:
            continue

        remote_iface_id = (
            cable.interface_b_id if cable.interface_a_id == cand.id else cable.interface_a_id
        )

        remote_iface = session.scalar(select(Interface).where(Interface.id == remote_iface_id))
        if remote_iface is None or remote_iface.device is None or remote_iface.device.role is None:
            return None

        return remote_iface.device.role.name

    return None
