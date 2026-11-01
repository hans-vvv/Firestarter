"""Cable repository — query helpers for the Cable ORM model."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.models import Cable, CableStatus, Device, Interface


def get_cables_between_devices(
    session: Session,
    device_a: Device,
    device_b: Device,
    *,
    exclude_retired: bool = True,
) -> list[Cable]:
    """Return cables that connect device_a and device_b, in any direction.

    By default retired cables are excluded. Pass ``exclude_retired=False``
    to include them (e.g. for audit queries). Callers that relied on the
    old behaviour (no filter) effectively got all statuses — those callers
    should review whether retired cables are intentional in their context.
    """
    iface_a = aliased(Interface)
    iface_b = aliased(Interface)

    q = (
        select(Cable)
        .join(iface_a, Cable.interface_a_id == iface_a.id)
        .join(iface_b, Cable.interface_b_id == iface_b.id)
        .where(
            ((iface_a.device_id == device_a.id) & (iface_b.device_id == device_b.id))
            | ((iface_a.device_id == device_b.id) & (iface_b.device_id == device_a.id))
        )
    )

    if exclude_retired:
        q = q.where(Cable.status != CableStatus.retired)

    return list(session.scalars(q))
