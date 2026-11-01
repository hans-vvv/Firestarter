"""Creates Cable records between two interfaces."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Cable, CableStatus, Interface


class CableBuilder:
    """
    Builder class to create physical cables (links) between interfaces.
    """

    def __init__(self, *, session: Session):
        self.session = session

    def connect(self, iface_a: Interface, iface_b: Interface) -> Cable:
        """Create and persist a physical cable connecting two interfaces."""

        cable = Cable(
            interface_a=iface_a,
            interface_b=iface_b,
            description=(
                f"{iface_a.device.hostname}:{iface_a.name} <-> "
                f"{iface_b.device.hostname}:{iface_b.name}"
            ),
            status=CableStatus.connected,
        )

        self.session.add(cable)
        self.session.flush()
        return cable
