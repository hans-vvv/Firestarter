"""Composes per-device topology context (interfaces, IPs, LAG hierarchy) for template rendering."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    Device,
    Interface,
    IPStatus,
)
from app.repositories import get_device_by_hostname, get_used_interfaces_by_device
from app.repositories.interface import get_remote_device_role_for_interface
from app.utils import require


class DeviceContextComposer:
    """
    Composes render-ready context for a single device.

    Scope:
    - load topology (Device + Interfaces + LAG relations + IP addresses)
    - classify interfaces for rendering
    """

    def __init__(self, session: Session):
        self.session = session

    def compose(self, *, hostname: str) -> dict[str, Any]:
        """
        Compose device plus all used interfaces. including
        descriptions and where applicable, IP addresses
        """
        device = require(
            get_device_by_hostname(self.session, hostname), f"No dB record found for {hostname}"
        )

        result = {
            "hostname": device.hostname,
            "device_role_name": device.role.name,
            "device_model_name": device.model_name,
            "interfaces": self._compose_interfaces(device),
            "tenant": device.labels.get("tenant"),
        }

        return result

    def _compose_interfaces(self, device: Device) -> list[dict]:
        """
        Composes interfaces
        """

        result: list[dict] = []

        interfaces = get_used_interfaces_by_device(session=self.session, device=device)
        for iface in interfaces:
            ctx = self._compose_one_interface(iface)
            if ctx is not None:
                result.append(ctx)

        return result

    def _compose_one_interface(self, iface: Interface) -> dict | None:
        """
        Compose per interface and include interface name, IP addresses
        attached to interface and interface description
        Per interface type (LAG, physical, etc) a different render type
        is given, to ease printing.
        """

        if not iface.in_use:
            return

        name = iface.name or ""

        # Filter by status=allocated: a Prefix/IPAddress in 'reserved' state
        # is not yet on the network; one in 'retired' state was active but no
        # longer is. Only 'allocated' addresses belong on the rendered config.
        iface_ip_addresses = [
            address.address
            for address in iface.ip_addresses
            if address.status == IPStatus.allocated
        ]
        base = {
            "name": name,
            "iface_ip_addresses": iface_ip_addresses,
            "description": iface.description,
            "if_role": iface.intf_role,
        }

        # Loopbacks: typically not printed
        if name.lower().startswith("loopback"):
            return {**base, "render_type": "skip"}

        # LAG member
        if iface.parent is not None:
            return {
                **base,
                "render_type": "lag_member",
                "parent": iface.parent.name,
                "connector": iface.connector,
                # Role of the device on the far end of this member's cable. The
                # coherent-optic block gates its rx-los-thresh line on the local
                # ↔ remote role pair, and the remote role is the one fact the
                # template cannot see from its own device context.
                "remote_role": get_remote_device_role_for_interface(self.session, iface=iface),
            }

        # LAG parent
        if iface.children:
            return {
                **base,
                "render_type": "lag_parent",
                "members": sorted(c.name for c in iface.children if c.name and c.in_use),
            }

        # Standalone physical
        return {
            **base,
            "render_type": "physical",
        }
